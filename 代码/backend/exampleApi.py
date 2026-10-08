# -*- coding: utf-8 -*-
"""后端集成示例 —— 小型二供泵房数字孪生

这是给 ③ 后端工程师 的**可运行参考实现**：把协议解析拿到的数据，
按《接口规范与数据字典》的要求加工后，通过 REST + WebSocket 提供给前端。

它演示了后端必须负责的三件事：
  1. 把协议解析的标签数据加上「报警标志位」（前端不判阈值）
  2. 每 500ms 通过 WebSocket 向前端推送全量数据
  3. 接收前端的控制指令，转成 Modbus 线圈 / 寄存器写下去
     并在**自动模式下拒绝手动指令**（PRD 非功能需求，返回错误码 1006）

跑法（三个窗口）：

    窗口 1  python sensorSim.py        # ① 传感器模拟器
    窗口 2  python exampleApi.py       # 本文件，后端服务
    窗口 3  浏览器打开 http://127.0.0.1:8000/docs  # 自带接口文档，可直接点着测

依赖：fastapi、uvicorn、websockets、pymodbus==3.6.9
"""

import asyncio
import os
import time

import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field

from protocolParser import (
    buildPayload,
    connect,
    loadConfig,
    readAllTags,
    writeCoil,
    writeSetpoint,
)

CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "pointTable.json")
PUSH_INTERVAL_MS = 500        # 向前端推送的周期，接口规范 5.2 节规定
ERROR_MODE_CONFLICT = 1006    # 自动模式下拒绝手动指令的错误码，接口规范七节

app = FastAPI(title="泵房数字孪生 · 后端", version="1.0")

config = loadConfig(CONFIG_PATH)
alarmConfig = config["alarm"]

# 与模拟器的长连接。pymodbus 客户端是同步的，所以所有调用都丢进线程池，
# 避免阻塞 FastAPI 的事件循环（否则 WebSocket 推送会把 HTTP 接口卡住）。
modbusClient = connect(config)
modbusLock = asyncio.Lock()


# ---------------------------------------------------------------- 报警判定


def evaluateAlarms(tags):
    """根据阈值计算报警标志位。

    这件事**必须由后端做**：如果前后端各判一套，报警状态会不一致
    （《接口规范》3.3 节已明确改为「后端算、前端只显示」）。

    Args:
        tags: readAllTags() 返回的标签字典。

    Returns:
        dict: 含 alarm_level_low / alarm_level_high / alarm_pressure_high / alarm_active。
    """
    level = tags.get("tank_level", 0)
    pressure = tags.get("pipe_pressure", 0)

    levelLow = 1 if level < alarmConfig["levelLow"] else 0
    levelHigh = 1 if level > alarmConfig["levelHigh"] else 0
    pressureHigh = 1 if pressure > alarmConfig["pressureHigh"] else 0

    return {
        "alarm_level_low": levelLow,
        "alarm_level_high": levelHigh,
        "alarm_pressure_high": pressureHigh,
        "alarm_active": levelLow + levelHigh + pressureHigh,
    }


def buildSnapshot(tags):
    """把标签数据 + 报警标志位 + 模式，拼成一次完整的推送内容。

    Args:
        tags: readAllTags() 返回的标签字典。

    Returns:
        dict: 形如 {"timestamp": ..., "data": {...}}，可直接推给前端。
    """
    payload = buildPayload(tags)
    payload["data"].update(evaluateAlarms(tags))
    return payload


async def readSnapshot():
    """异步读一次全网数据并加工成推送内容。

    Returns:
        dict | None: 推送内容；连不上模拟器时返回 None。
    """
    async with modbusLock:
        tags = await asyncio.to_thread(readAllTags, modbusClient, config)
    if tags is None:
        return None
    return buildSnapshot(tags)


# ---------------------------------------------------------------- REST 接口


@app.get("/api/v1/health")
async def health():
    """健康检查：后端能否读到模拟器。

    Returns:
        dict: {"status": "ok"|"degraded", "modbus": bool, "serverTime": int}
    """
    async with modbusLock:
        online = await asyncio.to_thread(modbusClient.is_socket_open)
        if not online:
            await asyncio.to_thread(modbusClient.connect)
            online = await asyncio.to_thread(modbusClient.is_socket_open)
    return {
        "status": "ok" if online else "degraded",
        "modbus": bool(online),
        "serverTime": int(time.time()),
    }


@app.get("/api/v1/status")
async def getStatus():
    """获取当前状态快照（前端首屏加载时调一次）。

    Returns:
        dict: 同 WebSocket 推送的内容；读不到数据时返回 error 字段。
    """
    snapshot = await readSnapshot()
    if snapshot is None:
        return {"error": "读不到模拟器数据", "hint": "请确认 sensorSim.py 已启动"}
    return snapshot


class ControlCommand(BaseModel):
    """前端下发的控制指令。

    字段名以《排班表》第七节的统一字段命名表为准：
    设备编号用 device_id（不是 device），动作用 command（不是 action）。
    """

    device_id: str = Field(..., description="设备编号，如 pump_01 / pump_02 / sys")
    command: str = Field(..., description="动作：start / stop / set_freq / set_mode")
    value: float = Field(0, description="set_freq 时为频率 0~50；set_mode 时为 0 或 1")
    operator: str = Field("未填写", description="操作人姓名（PRD 要求记录操作人与时间戳）")


@app.post("/api/v1/control")
async def postControl(command: ControlCommand):
    """下发控制指令。

    Args:
        command: 前端传来的控制指令。

    Returns:
        dict: 成功时返回 {"ok": True, ...}；失败时返回错误码与原因。
    """
    # 先校验参数、再校验模式：不然在自动模式下发一个拼错的指令，
    # 会被报成「模式冲突 1006」，掩盖了真正的问题（指令名写错了）。
    if command.command not in ("start", "stop", "set_freq", "set_mode"):
        return {"ok": False, "code": 1003,
                "message": "command 只能是 start / stop / set_freq / set_mode"}

    pumpIndex = None
    if command.device_id == "pump_01":
        pumpIndex = 0
    elif command.device_id == "pump_02":
        pumpIndex = 1

    if command.command != "set_mode" and pumpIndex is None:
        return {"ok": False, "code": 1002,
                "message": "device_id 只能是 pump_01 或 pump_02"}

    async with modbusLock:
        tags = await asyncio.to_thread(readAllTags, modbusClient, config)
        if tags is None:
            return {"ok": False, "code": 1001, "message": "读不到模拟器数据，指令未下发"}

        # 安全要求：自动模式下后端必须自己拒绝手动指令，不能只靠前端置灰按钮
        if tags.get("sys_mode") == 1 and command.command != "set_mode":
            return {"ok": False, "code": ERROR_MODE_CONFLICT,
                    "message": "当前为自动模式，不接受手动指令（请先切换为手动）"}

        if command.command in ("start", "stop"):
            coilTag = "pump_0%d_cmd_%s" % (pumpIndex + 1, command.command)
            success = await asyncio.to_thread(writeCoil, modbusClient, config, coilTag, True)
        elif command.command == "set_freq":
            success = await asyncio.to_thread(
                writeSetpoint, modbusClient, config, pumpIndex, command.value)
        else:
            success = await asyncio.to_thread(
                writeCoil, modbusClient, config, "sys_cmd_set_mode", True)

    if not success:
        return {"ok": False, "code": 1004, "message": "写入模拟器失败"}

    print("  [控制] %s %s value=%s 操作人=%s"
          % (command.device_id, command.command, command.value, command.operator))
    return {"ok": True, "timestamp": int(time.time()), "operator": command.operator}


# ---------------------------------------------------------------- WebSocket


@app.websocket("/ws/realtime")
async def realtime(socket: WebSocket):
    """向前端持续推送全量数据（默认 500ms 一次）。

    断线时前端负责每 3 秒重连；重连后的第一次推送就是全量数据，
    所以前端不需要维护增量合并逻辑。

    Args:
        socket: 前端建立的 WebSocket 连接。

    Returns:
        None
    """
    await socket.accept()
    print("  [WS] 前端已连接")
    try:
        lastPingTime = 0.0
        while True:
            snapshot = await readSnapshot()
            if snapshot is None:
                await socket.send_json({"type": "error", "message": "读不到模拟器数据"})
            else:
                await socket.send_json({"type": "data", **snapshot})

            # 心跳：接口规范 5.3 节要求后端每 10 秒发一条 ping
            now = time.time()
            if now - lastPingTime >= 10.0:
                lastPingTime = now
                await socket.send_json({"type": "ping", "timestamp": int(now)})

            await asyncio.sleep(PUSH_INTERVAL_MS / 1000.0)
    except WebSocketDisconnect:
        print("  [WS] 前端已断开")


if __name__ == "__main__":
    print("-" * 74)
    print("泵房后端已启动")
    print("  接口文档 : http://127.0.0.1:8000/docs")
    print("  状态快照 : http://127.0.0.1:8000/api/v1/status")
    print("  实时推送 : ws://127.0.0.1:8000/ws/realtime")
    print("  前提条件 : 另开一个窗口运行 python sensorSim.py")
    print("-" * 74)
    uvicorn.run(app, host="127.0.0.1", port=8000, log_level="warning")
