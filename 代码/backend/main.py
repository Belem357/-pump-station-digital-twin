# -*- coding: utf-8 -*-
"""③ 后端服务 —— 小型二供泵房数字孪生

把协议解析拿到的标签数据加工后，按《接口规范与数据字典 v1.1.1》通过
REST + WebSocket 提供给前端。相对 exampleApi.py（参考实现）的增强：

    1. 报警引擎：阈值判定 + 三重防抖 + 报警记录生命周期（alarmEngine.py）
    2. 历史数据：每 5 秒采样入库，/api/v1/history 按时段聚合返回曲线
    3. 完整错误码表：接口规范第七节 11 个错误码全部可触发
    4. 操作日志：每次控制指令记录操作人与时间戳（PRD 1.3）
    5. 设备讲解数据：/api/v1/devices，支撑「供水机房参观、设备讲解」场景
       （接口规范外的新增接口，需 PM 在群里公告确认，见 README）

跑法（三个窗口）：

    窗口 1  python sensorSim.py        # ① 传感器模拟器
    窗口 2  python main.py             # 本文件，后端服务
    窗口 3  浏览器打开 http://127.0.0.1:8000/docs  # 自带接口文档

依赖：fastapi、uvicorn、websockets、pymodbus==3.6.9（见 requirements.txt）
"""

import asyncio
import json
import os
import socket
import sys
import time

import uvicorn
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from alarmEngine import AlarmEngine
from protocolParser import (
    buildPayload,
    connect,
    loadConfig,
    readAllTags,
    writeCoil,
    writeSetpoint,
)
from storage import Storage

# 控制台编码兜底：Windows 控制台默认 GBK，print emoji（✅/❌）会抛 UnicodeEncodeError。
# 下面的启动横幅就在 FastAPI 的 startup 事件里，那里抛异常会让**整个服务起不来**
# （实测踩过：`python main.py` 直接崩，走带 chcp 65001 的 .bat 才没事）。
# 这里不强制改编码——改了中文反而在某些终端变乱码——只把无法编码的字符降级成 ?。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(errors="replace")
    except Exception:
        pass

# ---------------------------------------------------------------- 常量

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "pointTable.json")
DB_PATH = os.path.join(BASE_DIR, "data.db")
DEVICES_PATH = os.path.join(BASE_DIR, "devices.json")

PUSH_INTERVAL_MS = 500        # WebSocket 推送周期（接口规范 5.2）
PING_INTERVAL_SEC = 10        # 心跳周期（接口规范 5.3）
HISTORY_SAMPLE_SEC = 5        # 历史数据采样周期（验收 2-9 要求 1 小时曲线）
PRUNE_INTERVAL_SEC = 3600     # 历史数据清理周期
RATE_LIMIT_SEC = 2.0          # 同一设备 2 秒内重复下发 → 1008（交互流程图五）

# 错误码 → HTTP 状态码（接口规范第七节）
ERROR_STATUS = {
    1001: 400, 1002: 400, 1003: 400, 1004: 400, 1005: 400,
    1006: 409, 1007: 409, 1008: 429,
    2001: 500, 2002: 503,
}

ALLOWED_COMMANDS = ("start", "stop", "set_freq", "set_mode")
HISTORY_INTERVALS = {"10s": 10, "1m": 60, "5m": 300}

app = FastAPI(title="泵房数字孪生 · 后端", version="1.1.1")

# 前端目前用本地文件打开 sence.html，跨域直接放行（课程项目，无安全域要求）
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], allow_methods=["*"], allow_headers=["*"],
)

config = loadConfig(CONFIG_PATH)
alarmConfig = config["alarm"]
storage = Storage(DB_PATH)
alarmEngine = AlarmEngine(alarmConfig, storage)

# 与模拟器的长连接。pymodbus 客户端是同步的，所有调用都丢进线程池，
# 避免阻塞 FastAPI 事件循环（否则 WebSocket 推送会把 HTTP 接口卡住）。
modbusClient = connect(config)
modbusLock = asyncio.Lock()

# 最近一次成功读到的完整推送内容（data + 报警标志位），由 readLoop 每 500ms 更新
latestSnapshot = None
lastDataTime = 0.0
startTime = time.time()

# 控制指令防抖：记录每台设备上一次成功接收指令的时间
lastCommandTime = {}

# 已知标签集合（历史查询校验用）
KNOWN_TAGS = {spec["tag"] for spec in config["holdings"] if spec.get("tag")}


# ---------------------------------------------------------------- 通用工具


def error(code, message, statusOverride=None):
    """按接口规范第七节构造统一错误响应。

    Args:
        code: 错误码（1001~2002）。
        message: 给开发/前端看的说明文字。
        statusOverride: 可选，覆盖错误码表默认的 HTTP 状态码。

    Returns:
        JSONResponse: {"code": ..., "msg": ..., "timestamp": ...}
    """
    return JSONResponse(
        status_code=statusOverride or ERROR_STATUS.get(code, 400),
        content={"code": code, "msg": message, "timestamp": int(time.time())},
    )


def ok(**extra):
    """构造成功响应，统一带 code=0 与 msg=success。"""
    body = {"code": 0, "msg": "success", "timestamp": int(time.time())}
    body.update(extra)
    return JSONResponse(content=body)


@app.exception_handler(RequestValidationError)
async def validationErrorHandler(request: Request, exc: RequestValidationError):
    """请求体/参数不合法 → 1005 缺少必填字段。

    不这么做的话 FastAPI 默认返回 422，错误码表（1005）就触发不了，
    验收 4-22「不传 device_id 返回错误码 1005」会挂。
    """
    return error(1005, "请求参数不完整: %s" % exc.errors())


@app.exception_handler(Exception)
async def internalErrorHandler(request: Request, exc: Exception):
    """兜底：任何未捕获异常 → 2001 后端内部错误，绝不 500 裸奔。"""
    print("  [错误] 未捕获异常: %r" % exc)
    return error(2001, "后端内部错误")


def ensureConnected():
    """确认与模拟器的 TCP 连接可用（断开则尝试重连一次）。

    Returns:
        bool: True 表示连接可用。
    """
    if modbusClient.is_socket_open():
        return True
    try:
        modbusClient.connect()
    except Exception:
        # 模拟器没开时 connect 可能直接抛异常，统一按「未连接」处理（错误码 2002）
        return False
    return bool(modbusClient.is_socket_open())


async def readSnapshot():
    """异步读一次全网数据，加上报警标志位后返回。

    Returns:
        dict | None: {"timestamp":..., "data":{...}}；读不到返回 None。
    """
    async with modbusLock:
        tags = await asyncio.to_thread(readAllTags, modbusClient, config)
    if tags is None:
        return None
    payload = buildPayload(tags)
    flags, _ = alarmEngine.update(tags, time.time())
    payload["data"].update(flags)
    return payload


# ---------------------------------------------------------------- 主循环


class WSManager:
    """WebSocket 客户端集合，负责广播与断线清理。"""

    def __init__(self):
        self.clients = set()

    def add(self, socket):
        self.clients.add(socket)

    def remove(self, socket):
        self.clients.discard(socket)

    def count(self):
        return len(self.clients)

    async def broadcast(self, message):
        """向所有客户端推送同一条消息，发送失败（前端已断开）的顺手清掉。

        Args:
            message: dict，会被序列化为 JSON。
        """
        dead = []
        for socket in list(self.clients):
            try:
                await socket.send_json(message)
            except Exception:
                dead.append(socket)
        for socket in dead:
            self.remove(socket)


wsManager = WSManager()


async def handleInterlockStop():
    """低液位联锁：向模拟器写停泵线圈，切断所有运行中的水泵。

    由报警引擎触发（液位 < 25cm 持续 30 秒），对应 PRD F10（P0）。
    注意：超压只报警不停泵，只有低液位才联锁。
    """
    print("  [联锁] 低液位联锁触发：下发停泵指令")
    async with modbusLock:
        for tag in ("pump_01_cmd_stop", "pump_02_cmd_stop"):
            await asyncio.to_thread(writeCoil, modbusClient, config, tag, True)
    storage.addOpLog(int(time.time()), "sys", "interlock_stop", None,
                     "系统(联锁)", 0, "低液位联锁停泵")


async def readLoop():
    """后台主循环：每 500ms 读一次模拟器，更新快照、推进报警引擎、推送前端。

    同时承担三件周期性杂事：
      - 每 10 秒发一次心跳（接口规范 5.3）
      - 每 5 秒把标签值写进历史库（供 /api/v1/history）
      - 每小时清理过期历史数据
    """
    global latestSnapshot, lastDataTime
    tickCount = 0
    lastPingTime = time.time()
    lastSampleTime = 0.0
    lastPruneTime = time.time()

    while True:
        try:
            now = time.time()

            # ---- 读数据（阻塞调用丢线程池）----
            async with modbusLock:
                try:
                    tags = await asyncio.to_thread(readAllTags, modbusClient, config)
                except Exception:
                    # 连接断开时 read_holding_registers 会直接抛异常（协议解析器
                    # 只对 isError 返回 None），统一按「读不到」处理：
                    # comm_lost 计时和快照清空都靠 tags=None 这条路径
                    tags = None

            # ---- 推进报警引擎（数据读不到时也推进，通信中断报警靠这个计时）----
            flags, events = alarmEngine.update(tags, now)
            for event in events:
                if event == "interlock_stop":
                    await handleInterlockStop()

            # ---- 更新全局快照 ----
            if tags is None:
                latestSnapshot = None
                message = {"type": "error", "message": "读不到模拟器数据"}
            else:
                payload = buildPayload(tags)
                payload["data"].update(flags)
                latestSnapshot = payload
                lastDataTime = now
                message = {"type": "data", **payload}

            # ---- 推送 + 心跳 ----
            if wsManager.count() > 0:
                await wsManager.broadcast(message)
            if now - lastPingTime >= PING_INTERVAL_SEC:
                lastPingTime = now
                if wsManager.count() > 0:
                    await wsManager.broadcast(
                        {"type": "ping", "timestamp": int(now)})

            # ---- 历史采样（每 5 秒）----
            if tags is not None and now - lastSampleTime >= HISTORY_SAMPLE_SEC:
                lastSampleTime = now
                storage.insertHistoryAt(int(now), tags)

            # ---- 历史数据清理（每小时）----
            if now - lastPruneTime >= PRUNE_INTERVAL_SEC:
                lastPruneTime = now
                removed = storage.pruneHistory(now)
                if removed:
                    print("  [存储] 清理过期历史数据 %d 行" % removed)

            tickCount += 1
        except Exception as error:
            # 单周期异常不能杀死主循环：否则后端会带着旧快照“装活”。
            # 记一笔日志、睡一拍继续。
            print("  [主循环] 本周期异常，跳过：%r" % error)
        await asyncio.sleep(PUSH_INTERVAL_MS / 1000.0)


# ---------------------------------------------------------------- REST 接口


@app.get("/api/v1/health")
async def health():
    """健康检查：确认后端活着、且能连上模拟器（接口规范 6.6）。

    联调时数据传不过来，先访问这个接口，就能判断问题出在后端之前还是之后。
    """
    async with modbusLock:
        online = await asyncio.to_thread(ensureConnected)
    return ok(
        status="ok" if online else "degraded",
        plc_connected=bool(online),
        ws_clients=wsManager.count(),
        uptime_sec=int(time.time() - startTime),
    )


@app.get("/api/v1/status")
async def getStatus():
    """当前状态快照（接口规范 6.3）。

    前端首屏加载时调一次，立即拿到数据，不必等 WebSocket 第一次推送。
    读不到数据时按错误码表返回 2002（数据服务未就绪）。
    """
    snapshot = await readSnapshot()
    if snapshot is None:
        return error(2002, "模拟器未连接，请确认 sensorSim.py 已启动")
    return ok(data=snapshot["data"], timestamp=snapshot["timestamp"])


class ControlCommand(BaseModel):
    """前端下发的控制指令（接口规范 6.2）。

    字段名以接口规范为准：device_id（不是 device）、command（不是 action）。
    字段故意做成可选，缺失时在端点里手动返回 1005，
    而不是让 FastAPI 默认的 422 冒出来。
    """

    device_id: str | None = Field(None, description="pump_01 / pump_02 / sys")
    command: str | None = Field(None, description="start / stop / set_freq / set_mode")
    value: float | None = Field(None, description="set_freq 时 0~50；set_mode 时 0 或 1")
    operator: str | None = Field(None, description="操作人姓名（PRD 要求记录操作人与时间戳）")


@app.post("/api/v1/control")
async def postControl(cmd: ControlCommand):
    """下发控制指令（接口规范 6.2）。

    校验顺序有意为之（参考 exampleApi.py 的教训）：
    先校验参数本身、再校验运行环境——否则自动模式下发一个拼错的指令，
    会被报成「模式冲突 1006」，掩盖真正的问题（指令名写错了）。
    """
    ts = int(time.time())
    deviceId = cmd.device_id
    command = cmd.command
    value = cmd.value
    operator = cmd.operator

    # ---- 1. 参数校验 ----
    if not deviceId:
        return error(1005, "缺少必填字段 device_id")
    if not command:
        return error(1005, "缺少必填字段 command")
    if not operator:
        return error(1005, "缺少必填字段 operator")
    if command not in ALLOWED_COMMANDS:
        return error(1003, "command 只能是 start / stop / set_freq / set_mode")

    pumpIndex = 0 if deviceId == "pump_01" else 1 if deviceId == "pump_02" else None
    if command == "set_mode":
        if deviceId != "sys":
            return error(1002, "set_mode 的 device_id 只能是 sys")
        if value is None:
            return error(1005, "set_mode 必须携带 value")
        if value not in (0, 1):
            return error(1004, "set_mode 的 value 只能是 0(手动) 或 1(自动)")
    else:
        if pumpIndex is None:
            return error(1002, "device_id 只能是 pump_01 或 pump_02")
        if command == "set_freq":
            if value is None:
                return error(1005, "set_freq 必须携带 value")
            if not 0 <= value <= 50:
                return error(1004, "频率须在 0~50Hz 之间")

    # ---- 2. 连接与数据 ----
    async with modbusLock:
        if not await asyncio.to_thread(ensureConnected):
            storage.addOpLog(ts, deviceId, command, value, operator, 2002, "模拟器未连接")
            return error(2002, "模拟器未连接，请检查 sensorSim.py 是否启动")
        tags = await asyncio.to_thread(readAllTags, modbusClient, config)
    if tags is None:
        storage.addOpLog(ts, deviceId, command, value, operator, 1001, "PLC 通信超时")
        return error(1001, "PLC 通信超时")

    # ---- 3. 防抖：同一设备 2 秒内重复下发 → 1008（交互流程图五）----
    lastTs = lastCommandTime.get(deviceId, 0.0)
    if time.time() - lastTs < RATE_LIMIT_SEC:
        return error(1008, "指令过于频繁，请稍后再试")
    lastCommandTime[deviceId] = time.time()

    # ---- 4. 模式互斥：自动模式下拒绝手动指令（PRD 非功能需求，P0 验收项）----
    if tags.get("sys_mode") == 1 and command != "set_mode":
        storage.addOpLog(ts, deviceId, command, value, operator, 1006, "自动模式拒绝手动指令")
        return error(1006, "当前为自动模式，不接受手动指令（请先切换为手动）")

    # ---- 5. 联锁禁令：低液位联锁停泵后 30 秒内拒绝启泵 → 1008 ----
    if command == "start" and not alarmEngine.canStart():
        storage.addOpLog(ts, deviceId, command, value, operator, 1008, "联锁停泵 30 秒内禁止启泵")
        return error(1008, "刚触发低液位联锁，30 秒内禁止启泵（防止频繁启停损坏电机）")

    # ---- 6. 故障互斥：故障泵拒绝启动 → 1007（验收 4-23）----
    if command == "start" and tags.get("pump_0%d_status" % (pumpIndex + 1)) == 2:
        storage.addOpLog(ts, deviceId, command, value, operator, 1007, "故障泵拒绝启动")
        return error(1007, "该水泵处于故障状态，无法启动")

    # ---- 7. 执行写入 ----
    async with modbusLock:
        if command in ("start", "stop"):
            coilTag = "pump_0%d_cmd_%s" % (pumpIndex + 1, command)
            success = await asyncio.to_thread(writeCoil, modbusClient, config, coilTag, True)
        elif command == "set_freq":
            success = await asyncio.to_thread(
                writeSetpoint, modbusClient, config, pumpIndex, value)
        else:  # set_mode
            success = await asyncio.to_thread(
                writeCoil, modbusClient, config, "sys_cmd_set_mode", True)

    if not success:
        storage.addOpLog(ts, deviceId, command, value, operator, 1001, "写入模拟器失败")
        return error(1001, "写入模拟器失败")

    # 模式切换多等一拍（模拟器 200ms 一个周期才处理线圈）：
    # 否则 set_mode 刚返回、sys_mode 寄存器还是旧值，紧跟其后的手动指令
    # 会读着旧模式绕过 1006 互斥检查（P0 验收项，实测踩过这个坑）
    if command == "set_mode":
        await asyncio.sleep(0.3)

    storage.addOpLog(ts, deviceId, command, value, operator, 0, "下发成功")
    print("  [控制] %s %s value=%s 操作人=%s"
          % (deviceId, command, value, operator))
    return ok()


@app.get("/api/v1/history")
async def getHistory(tags: str, start: int, end: int, interval: str = "1m"):
    """查询历史数据（接口规范 6.4），画「过去 1 小时压力和液位折线图」用。

    实现说明：
      - 原始样本每 5 秒一条存 SQLite，查询时按 interval 分桶取均值
      - 单次返回点数不超过 2000；超了就自动把 interval 翻倍，
        并在响应里返回 actual_interval（验收 2-10）
    """
    tagList = [tag.strip() for tag in tags.split(",") if tag.strip()]
    if not tagList:
        return error(1005, "缺少必填参数 tags")
    unknown = [tag for tag in tagList if tag not in KNOWN_TAGS]
    if unknown:
        return error(1003, "未知标签: %s" % ",".join(unknown))
    if start >= end:
        return error(1004, "start 必须小于 end")
    if interval not in HISTORY_INTERVALS:
        return error(1004, "interval 只能是 10s / 1m / 5m")

    intervalSec = HISTORY_INTERVALS[interval]
    maxPoints = 2000

    # 分桶：每个标签一个桶，跨度过大导致桶数超限时自动翻倍间隔
    actualSec = intervalSec
    while (end - start) / actualSec > maxPoints:
        actualSec *= 2

    series = {}
    for tag in tagList:
        rows = storage.queryHistory(tag, start, end)
        buckets = {}
        for t, v in rows:
            bucketStart = start + (t - start) // actualSec * actualSec
            if bucketStart not in buckets:
                buckets[bucketStart] = []
            buckets[bucketStart].append(v)
        series[tag] = [
            {"t": int(bucketStart),
             "v": round(sum(values) / len(values), 3)}
            for bucketStart, values in sorted(buckets.items())
        ]

    actualInterval = (actualSec // 60 and "%dm" % (actualSec // 60)
                      if actualSec % 60 == 0 else "%ds" % actualSec)
    return ok(series=series, actual_interval=actualInterval)


@app.get("/api/v1/alarms")
async def getAlarms(start: int | None = None, end: int | None = None,
                    device_id: str | None = None, status: str | None = None,
                    page: int = 1, page_size: int = 20):
    """查询报警记录（接口规范 6.5）。

    支持按时间 / 设备 / 状态筛选，分页返回。
    """
    if page < 1:
        return error(1004, "page 必须 ≥ 1")
    pageSize = min(max(page_size, 1), 100)   # 每页最多 100 条（验收 4-17）
    total, items = storage.queryAlarms(
        start=start, end=end, deviceId=device_id, status=status,
        page=page, pageSize=pageSize)
    return ok(total=total, page=page, page_size=pageSize, items=items)


@app.get("/api/v1/devices")
async def getDevices():
    """设备讲解数据 —— 支撑「供水机房参观、供水系统及设备讲解」场景。

    ⚠️ 本接口不在接口规范 v1.1.1 里，是后端为课程选题场景新增的扩展，
    需 PM 在群里公告确认（接口规范总则第三条：新增字段必须 PM 公告）。
    型号于 2026-10-08 按厂商官方公开资料逐个核实（原《传感器推荐型号表》图片已不可查），
    选型理由见 共享文档/10.传感器选型说明_v1.2：
      液位 MPM4700 投入式液位变送器（麦克传感）、压力 MPM480 压力变送器、
      温度 PT100 铂电阻+一体化变送器、电流 BH-0.66 电流互感器+变送器、
      电度 DTSD1352 三相多功能电能表（安科瑞，有功 0.5S 级）。

    ⚠️ 型号数据只在 devices.json 里维护，不要在本文件硬编码——改型号要同时改那份文档。
    """
    try:
        with open(DEVICES_PATH, "r", encoding="utf-8") as fileHandle:
            devices = json.load(fileHandle)
    except OSError:
        return error(2001, "devices.json 缺失或读取失败")
    return ok(data=devices)


# ---------------------------------------------------------------- WebSocket


@app.websocket("/ws/realtime")
async def realtime(socket: WebSocket):
    """向前端持续推送全量数据（默认 500ms 一次，接口规范 5.2）。

    断线时前端负责每 3 秒重连；重连后的第一次推送就是全量数据，
    所以前端不需要维护增量合并逻辑。心跳每 10 秒一条（接口规范 5.3），
    前端回 pong，这里开一个后台任务安静地收掉，避免消息在缓冲区堆积。
    """
    await socket.accept()
    wsManager.add(socket)
    print("  [WS] 前端已连接，当前 %d 个" % wsManager.count())
    try:
        receiveTask = asyncio.create_task(_consumeClientMessages(socket))
        while True:
            await asyncio.sleep(1.0)   # 实际推送由 readLoop 统一广播
    except WebSocketDisconnect:
        pass
    finally:
        receiveTask.cancel()
        wsManager.remove(socket)
        print("  [WS] 前端已断开，当前 %d 个" % wsManager.count())


async def _consumeClientMessages(socket):
    """消费前端发来的消息（目前只有 pong），保持接收缓冲区干净。"""
    try:
        while True:
            await socket.receive_text()
    except Exception:
        pass


# ---------------------------------------------------------------- 生命周期


@app.on_event("startup")
async def startup():
    """启动时：确认模拟器连接、起后台主循环、清理过期历史数据。"""
    online = await asyncio.to_thread(ensureConnected)
    removed = storage.pruneHistory(time.time())
    closed = storage.closeActiveAlarms(time.time(), "服务重启")
    asyncio.create_task(readLoop())
    print("-" * 74)
    print("泵房后端已启动")
    print("  模拟器连接 : %s" % ("✅ 已连上 127.0.0.1:%d"
                                % config["connection"]["port"] if online else "❌ 未连上（先跑 sensorSim.py）"))
    print("  接口文档   : http://127.0.0.1:8000/docs")
    print("  状态快照   : http://127.0.0.1:8000/api/v1/status")
    print("  实时推送   : ws://127.0.0.1:8000/ws/realtime")
    if removed:
        print("  历史数据   : 启动时清理过期样本 %d 行" % removed)
    if closed:
        print("  报警记录   : 启动时闭合遗留活跃报警 %d 条" % closed)
    print("-" * 74)


@app.on_event("shutdown")
async def shutdown():
    """退出时：关闭 Modbus 连接与数据库。"""
    modbusClient.close()
    storage.close()


# ---------------------------------------------------------------- 静态前端与 3D 模型

# 前端目录自动探测：仓库里的 代码/frontend，以及队友解压到桌面的 pump-monitor。
# 挂上之后 http://<地址>:8000/ 一个端口同时给前端页面、REST 接口和 WebSocket，
# 不用再单独起静态服务器，也顺带绕开了 file:// 加载不了 .glb 的问题。
_FRONTEND_PAGE = "scene.html"      # 注意是 scene，不是 sence
_FRONTEND_CANDIDATES = [
    os.path.abspath(os.path.join(BASE_DIR, "..", "frontend")),
    os.path.abspath(os.path.join(BASE_DIR, "..", "..", "pump-monitor", "pump-monitor")),
    os.path.abspath(os.path.join(BASE_DIR, "..", "pump-monitor", "pump-monitor")),
]
# 认目录不算数，得有那个页面文件才算找到了
FRONTEND_DIR = next((d for d in _FRONTEND_CANDIDATES
                     if os.path.isfile(os.path.join(d, _FRONTEND_PAGE))), None)

# 模型在仓库根，不在 代码/frontend 里，静态挂载覆盖不到，所以单独开一条路由
# 后端已挪到 代码/backend，离仓库根是两级，所以这里要退两次
MODEL_PATH = os.path.abspath(os.path.join(BASE_DIR, "..", "..", "泵房.glb"))


@app.get("/model.glb")
async def pumpRoomModel():
    """把仓库根的 泵房.glb 发给前端（GLTFLoader 直接按二进制取）。

    MIME 用 model/gltf-binary；用默认的 octet-stream 有些浏览器会拒绝加载。

    Returns:
        FileResponse: 模型文件；文件不存在时返回 1003 错误。
    """
    if not os.path.isfile(MODEL_PATH):
        return error(1003, "找不到模型文件 泵房.glb")
    return FileResponse(MODEL_PATH, media_type="model/gltf-binary")


if FRONTEND_DIR:
    @app.get("/")
    async def frontendIndex():
        """前端没有 index.html，"/" 显式指向 scene.html。"""
        return FileResponse(os.path.join(FRONTEND_DIR, _FRONTEND_PAGE))

    app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")


def getLanAddress():
    """取本机在局域网里的地址，用于打印给导师访问的网址。

    用一个 UDP socket 探出口地址（UDP connect 不会真的发包）：
    比 socket.gethostbyname 可靠——装了 VMware / 虚拟网卡时，
    后者常返回一个别人根本连不上的虚拟网段地址。

    Returns:
        str: 形如 192.168.1.5 的地址；探测失败时返回 None。
    """
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect(("8.8.8.8", 80))
            return probe.getsockname()[0]
    except Exception:
        return None


if __name__ == "__main__":
    print("=" * 62)
    print("  泵房数字孪生 · 后端服务")
    print("=" * 62)
    if FRONTEND_DIR:
        print("  前端页面   : http://127.0.0.1:8000/    （已挂载 %s）" % FRONTEND_DIR)
    else:
        print("  前端页面   : 没找到 %s，只提供接口" % _FRONTEND_PAGE)
    print("  3D 模型    : http://127.0.0.1:8000/model.glb")
    print("  接口文档   : http://127.0.0.1:8000/docs")
    lanAddress = getLanAddress()
    if lanAddress:
        print("-" * 62)
        print("  局域网访问（导师手机/别的电脑用这个地址）:")
        print("      http://%s:8000/" % lanAddress)
        print("  连不上先看 Windows 防火墙有没有拦入站 8000（首次启动会弹窗，选允许）")
    print("=" * 62)
    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="warning")
