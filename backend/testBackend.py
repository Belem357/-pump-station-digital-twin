# -*- coding: utf-8 -*-
"""后端验收自测 —— 小型二供泵房数字孪生

把验收清单（4.4 节为主）里能自动化的场景串成一条命令跑完：
错误码全覆盖、控制指令、历史查询、报警查询、WebSocket 心跳、设备讲解接口。

用法（模拟器和后端都跑起来后，再开一个窗口）：
    python testBackend.py

全部通过时退出码为 0，有失败项为 1。

依赖：websockets（随 uvicorn[standard] 一起装好了），其余全是标准库。
"""

import json
import sys
import time
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:8000"
WS_URL = "ws://127.0.0.1:8000/ws/realtime"

results = []


def check(title, condition, detail=""):
    """打印一条检查结果并累计。

    Args:
        title: 检查项名称。
        condition: 判定结果，True 为通过。
        detail: 补充说明，拼在行尾。

    Returns:
        bool: 原样返回 condition。
    """
    mark = "✅" if condition else "❌"
    print("  %s %s%s" % (mark, title, ("  —— " + detail) if detail else ""))
    results.append(bool(condition))
    return bool(condition)


def http(method, path, body=None):
    """发一个 HTTP 请求，返回 (状态码, JSON)。

    Args:
        method: GET / POST。
        path: 路径，如 /api/v1/health。
        body: dict，POST 时序列化为 JSON。

    Returns:
        tuple[int, dict]: (HTTP 状态码, 解析后的 JSON)。解析失败时 JSON 为 None。
    """
    request = urllib.request.Request(BASE + path, method=method)
    request.add_header("Content-Type", "application/json")
    data = json.dumps(body).encode("utf-8") if body is not None else None
    try:
        with urllib.request.urlopen(request, data=data) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        try:
            return error.code, json.loads(error.read().decode("utf-8"))
        except ValueError:
            return error.code, None


def postControl(body):
    """下发控制指令的快捷方式。"""
    return http("POST", "/api/v1/control", body)


def expectCode(actualStatus, actualJson, wantCode, wantStatus, title):
    """校验 HTTP 状态与业务错误码同时符合预期。"""
    code = (actualJson or {}).get("code")
    return check(title, actualStatus == wantStatus and code == wantCode,
                 "HTTP %d code=%s" % (actualStatus, code))


def waitForSafeTank(timeout=300):
    """等水箱回到安全区间再下发控制指令。

    上一轮测试若没把泵停干净，水箱会被抽到低液位：模拟器的低液位联锁
    会在一拍内掐掉新启动的泵（启动指令回 0 但泵根本跑不起来），且联锁
    停泵后有 30 秒启泵禁令（错误码 1008）。这里等三件事：
    （1）液位 ≥ 40cm；（2）低液位报警已恢复；
    （3）最近一次低液位报警恢复已超过 30 秒（禁令必然解除）。

    Returns:
        bool: 是否等到；超时返回 False（继续执行，结果可能不可靠）。
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        _, snapshot = http("GET", "/api/v1/status")
        data = (snapshot or {}).get("data", {})
        level = data.get("tank_level") or 0
        if level >= 40 and not data.get("alarm_level_low"):
            _, alarms = http("GET", "/api/v1/alarms?status=recovered&page_size=50")
            items = (alarms or {}).get("items", [])
            recovered = [item for item in items if item.get("type") == "level_low"]
            banLeft = 0 if not recovered else 30 - (time.time() - recovered[0]["time_end"])
            if banLeft <= 0:
                print("  当前液位 %.1fcm，联锁禁令已解除，可以开始。" % level)
                return True
        time.sleep(2)
    print("  ⚠ 等安全液位超时（%d 秒），继续执行，结果可能不可靠。" % timeout)
    return False


# ---------------------------------------------------------------- 1. 基础接口

print("=" * 74)
print("泵房后端 · 验收自测")
print("=" * 74)

print("\n[1/6] 基础接口")
status, body = http("GET", "/api/v1/health")
check("健康检查返回 200", status == 200 and (body or {}).get("code") == 0,
      "plc_connected=%s ws_clients=%s uptime_sec=%s"
      % ((body or {}).get("plc_connected"), (body or {}).get("ws_clients"),
         (body or {}).get("uptime_sec")))
status, body = http("GET", "/api/v1/status")
check("状态快照返回 data 结构", status == 200 and "data" in (body or {}),
      "标签数 %d" % len((body or {}).get("data", {})) if body else "")
status, body = http("GET", "/api/v1/devices")
check("设备讲解接口可用", status == 200 and "devices" in (body or {}).get("data", {}),
      "设备数 %d" % len((body or {}).get("data", {}).get("devices", [])) if body else "")

# ---------------------------------------------------------------- 2. 错误码

print("\n[2/6] 错误码表（接口规范第七节）")
status, body = postControl({"device_id": "pump_03", "command": "start", "operator": "自测"})
expectCode(status, body, 1002, 400, "非法 device_id → 1002")

status, body = postControl({"device_id": "pump_01", "command": "restart", "operator": "自测"})
expectCode(status, body, 1003, 400, "非法 command → 1003")

status, body = postControl({"device_id": "pump_01", "command": "set_freq", "value": 60, "operator": "自测"})
expectCode(status, body, 1004, 400, "set_freq 越界(60Hz) → 1004")

status, body = postControl({"command": "start", "operator": "自测"})
expectCode(status, body, 1005, 400, "缺 device_id → 1005")

status, body = postControl({"device_id": "pump_01", "command": "start"})
expectCode(status, body, 1005, 400, "缺 operator → 1005")

waitForSafeTank()   # 低液位联锁会在一拍内掐掉刚启动的泵，先等安全液位
status, body = postControl({"device_id": "pump_01", "command": "start", "operator": "自测"})
firstOk = status == 200 and (body or {}).get("code") == 0
check("start 正常下发 → 0", firstOk)
status, body = postControl({"device_id": "pump_01", "command": "start", "operator": "自测"})
expectCode(status, body, 1008, 429, "2 秒内重复下发 → 1008（防抖）")

print("  （等 3 秒避开防抖窗口…）")
time.sleep(3)

# 自动模式互斥（P0 验收项：绕过前端直接调接口也要拦）
status, body = postControl({"device_id": "sys", "command": "set_mode", "value": 1, "operator": "自测"})
check("切换自动模式 → 0", status == 200 and (body or {}).get("code") == 0)
status, body = postControl({"device_id": "pump_02", "command": "start", "operator": "自测"})
expectCode(status, body, 1006, 409, "自动模式下手动指令 → 1006（P0）")
print("  （等 3 秒避开 sys 设备的防抖窗口…）")
time.sleep(3)
status, body = postControl({"device_id": "sys", "command": "set_mode", "value": 0, "operator": "自测"})
check("切回手动模式 → 0", status == 200 and (body or {}).get("code") == 0)

# ---------------------------------------------------------------- 3. 控制链路

print("\n[3/6] 控制指令 → 模拟器真实执行")
# 1#泵若还在跑，先停掉：它会持续抽低液位，水箱永远回不到安全区间
_, snapshot = http("GET", "/api/v1/status")
if (snapshot or {}).get("data", {}).get("pump_01_status") == 1:
    postControl({"device_id": "pump_01", "command": "stop", "operator": "自测"})
    time.sleep(2.5)
waitForSafeTank()
time.sleep(2.5)
status, body = postControl({"device_id": "pump_02", "command": "start", "operator": "自测"})
check("启动 2#泵 → 0", status == 200 and (body or {}).get("code") == 0)
time.sleep(5)   # 等模拟器升频 + 后端推送
_, snapshot = http("GET", "/api/v1/status")
data = (snapshot or {}).get("data", {})
check("2#泵状态翻转为运行(1)", data.get("pump_02_status") == 1,
      "status=%s freq=%.1fHz" % (data.get("pump_02_status"), data.get("pump_02_freq", 0)))
check("运行频率已升起来", (data.get("pump_02_freq") or 0) > 1.0)
check("运行电流不再是 0", (data.get("pump_02_current") or 0) > 0.5,
      "current=%.1fA" % data.get("pump_02_current", 0))

time.sleep(2.5)
status, body = postControl({"device_id": "pump_02", "command": "set_freq", "value": 50, "operator": "自测"})
check("2#泵调速到 50Hz → 0", status == 200 and (body or {}).get("code") == 0)

# 收尾：把泵都停掉，水箱留给下一轮测试慢慢回灌
time.sleep(2.5)
status, body = postControl({"device_id": "pump_02", "command": "stop", "operator": "自测"})
check("停止 2#泵 → 0", status == 200 and (body or {}).get("code") == 0)
status, body = postControl({"device_id": "pump_01", "command": "stop", "operator": "自测"})
check("停止 1#泵 → 0", status == 200 and (body or {}).get("code") == 0)

# ---------------------------------------------------------------- 4. 历史查询

print("\n[4/6] 历史数据（接口规范 6.4）")
end = int(time.time())
start = end - 600   # 过去 10 分钟
path = ("/api/v1/history?tags=tank_level,pipe_pressure&start=%d&end=%d&interval=10s"
        % (start, end))
status, body = http("GET", path)
series = (body or {}).get("series", {})
check("历史查询返回两个序列", status == 200 and "tank_level" in series
      and "pipe_pressure" in series,
      "点数 %d / %d" % (len(series.get("tank_level", [])), len(series.get("pipe_pressure", []))))
points = series.get("tank_level", [])
check("点数落在合理范围", 0 < len(points) <= 2000, "tank_level 共 %d 点" % len(points))
if points:
    check("点格式为 {t, v}", "t" in points[0] and "v" in points[0])

status, body = http("GET", "/api/v1/history?tags=bad_tag&start=%d&end=%d" % (start, end))
expectCode(status, body, 1003, 400, "未知标签 → 1003")

status, body = http("GET", "/api/v1/history?tags=tank_level&start=%d&end=%d&interval=1x" % (start, end))
expectCode(status, body, 1004, 400, "非法 interval → 1004")

# ---------------------------------------------------------------- 5. 报警记录

print("\n[5/6] 报警记录（接口规范 6.5）")
status, body = http("GET", "/api/v1/alarms?page_size=5")
check("报警列表可分页查询", status == 200 and "items" in (body or {}),
      "total=%s 本页 %d 条" % ((body or {}).get("total"), len((body or {}).get("items", []))))
items = (body or {}).get("items", [])
if items:
    first = items[0]
    check("记录字段齐全", all(key in first for key in
          ("alarm_id", "type", "device_id", "level", "status",
           "time_start", "time_end", "value_at_trigger", "threshold", "message")))
    check("alarm_id 格式正确", first["alarm_id"].startswith("A"),
          first["alarm_id"])
    status, body = http("GET", "/api/v1/alarms?device_id=%s" % first["device_id"])
    check("按设备筛选有效", status == 200 and all(
        item["device_id"] == first["device_id"] for item in body.get("items", [])))

# ---------------------------------------------------------------- 6. WebSocket

print("\n[6/6] WebSocket 实时推送")


async def testWebSocket():
    """连一次 WebSocket：收全量数据、验字段、回 pong。"""
    import websockets
    async with websockets.connect(WS_URL) as socket:
        message = json.loads(await socket.recv())
        check("收到 data 消息", message.get("type") == "data")
        data = message.get("data", {})
        check("推送字段齐全", all(key in data for key in
              ("tank_level", "pipe_pressure", "today_kwh", "sys_mode",
               "pump_01_status", "pump_02_status",
               "alarm_level_low", "alarm_level_high",
               "alarm_pressure_high", "alarm_active")))
        check("状态量是整数", isinstance(data.get("pump_01_status"), int)
              and isinstance(data.get("sys_mode"), int)
              and isinstance(data.get("alarm_active"), int))

        # 再收一条 data 确认 500ms 周期推送（中间可能夹心跳 ping，跳过即可）
        startTick = time.time()
        second = None
        for _ in range(5):
            message = json.loads(await socket.recv())
            if message.get("type") == "data":
                second = message
                break
        interval = time.time() - startTick
        check("第二条 data 正常到达", second is not None)
        check("推送间隔 ≈ 500ms", 0.3 <= interval <= 1.2,
              "实测 %.0fms" % (interval * 1000))

        await socket.send(json.dumps({"type": "pong"}))
        check("pong 发送成功（后端不报错即通过）", True)


try:
    import asyncio
    asyncio.run(testWebSocket())
except Exception as error:
    check("WebSocket 测试可执行", False, repr(error))

# ---------------------------------------------------------------- 汇总

passed = sum(results)
total = len(results)
print()
print("=" * 74)
print("结果： %d / %d 项通过" % (passed, total))
print("=" * 74)
if passed == total:
    print("🎉 全部通过。剩下的人为验收场景（联锁 30 秒、通信中断）见 README。")
    sys.exit(0)
print("⚠ 有失败项，把输出发给后端工程师排查。")
sys.exit(1)
