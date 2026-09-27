# -*- coding: utf-8 -*-
"""② 协议解析 —— 小型二供泵房数字孪生

把模拟器用 Modbus「说」出来的数据读出来，翻译成统一标签名并还原工程值，
再交给后端。它做三件事，对应分工方案里协议解析工程师的职责：

    1. 按点表轮询 Modbus 保持寄存器（默认 200ms 一次）
    2. 地址 → 标签名（40001 → tank_level）
    3. 缩放换算（存 855 → 实际 85.5）

两种用法：

  【当命令行工具用】—— 联调时验证数据通不通
      python protocolParser.py            # 持续采集，控制台打印可读状态
      python protocolParser.py --json     # 持续采集，打印后端要用的 JSON
      python protocolParser.py --once     # 只读一次就退出

  【当模块用】—— 后端工程师在 FastAPI 里直接调
      from protocolParser import readAllTags, buildPayload
      tags = readAllTags(client, config)
      payload = buildPayload(tags)          # 形状与《接口规范》5.2 节一致

依赖：pymodbus==3.6.9（见 requirements.txt）
"""

import argparse
import json
import os
import struct
import sys
import time

from pymodbus.client import ModbusTcpClient

FC_HOLDING = 3
RECONNECT_INTERVAL_SEC = 3.0   # 断线后重连间隔（验收清单要求前端 3 秒重连，这里对齐）


# ---------------------------------------------------------------- 配置


def loadConfig(configPath):
    """读取点表配置文件。

    Args:
        configPath: pointTable.json 的路径。

    Returns:
        dict: 配置字典，含 connection / timing / holdings / coils。
    """
    with open(configPath, "r", encoding="utf-8") as fileHandle:
        return json.load(fileHandle)


def getDataHoldings(config):
    """取出点表里所有「模拟器产生」的保持寄存器（排除预留空号和后端写入项）。

    Args:
        config: 点表配置。

    Returns:
        list[dict]: 需要读取并翻译的 holding 配置列表。
    """
    result = []
    for spec in config["holdings"]:
        if not spec.get("tag"):
            continue
        if spec["tag"].endswith("_setpoint"):
            continue
        result.append(spec)
    return result


# ---------------------------------------------------------------- 数值换算


def wordsToFloat32(highWord, lowWord):
    """把高字在前的两个寄存器拼回 32 位浮点数。

    这是 today_kwh 专用的特例：全表只有它不走「整数 ÷ 缩放」那套。
    顺序必须和模拟器写入时一致（高字在前），写反了会得到一个离谱的大数。

    Args:
        highWord: 高 16 位。
        lowWord: 低 16 位。

    Returns:
        float: 还原后的浮点数。
    """
    return struct.unpack(">f", struct.pack(">HH", highWord, lowWord))[0]


def decodeRegisters(spec, registers):
    """按点表规则把寄存器整数还原成工程值。

    Args:
        spec: 一条 holding 配置。
        registers: 该测点占用的寄存器列表。

    Returns:
        float: 工程值；若该测点未在模拟器中启用则返回 None。
    """
    if spec.get("type") == "float32":
        return round(wordsToFloat32(registers[0], registers[1]), 3)
    scale = spec.get("scale", 1)
    if scale == 1:
        # 缩放系数为 1 的测点（状态、模式）在接口规范里定义为 Int，
        # 不能返回 0.0 这种浮点写法，否则前后端类型对不上。
        return int(registers[0])
    return round(registers[0] / scale, 3)


# ---------------------------------------------------------------- 采集


def connect(config):
    """建立 Modbus TCP 连接。

    Args:
        config: 点表配置。

    Returns:
        ModbusTcpClient: 已尝试连接的客户端（是否成功用 is_socket_open() 判断）。
    """
    client = ModbusTcpClient(
        config["connection"]["host"],
        port=config["connection"]["port"],
        timeout=3,
    )
    client.connect()
    return client


def readAllTags(client, config):
    """一次读完所有测点并翻译成统一标签名。

    有意做成「一次请求读整块寄存器」而不是逐个读：
    这样所有数值来自同一瞬间，不会出现液位是上一秒、压力是这一秒的错位。

    Args:
        client: 已连接的 ModbusTcpClient。
        config: 点表配置。

    Returns:
        dict: {标签名: 工程值}；读取失败时返回 None。
    """
    specs = getDataHoldings(config)
    if not specs:
        return {}

    firstOffset = min(spec["offset"] for spec in specs)
    lastOffset = max(spec["offset"] + spec.get("regCount", 1) for spec in specs)
    readCount = lastOffset - firstOffset

    response = client.read_holding_registers(
        address=firstOffset,
        count=readCount,
        slave=config["connection"]["deviceId"],
    )
    if response.isError():
        return None

    rawRegisters = response.registers
    tags = {}
    for spec in specs:
        start = spec["offset"] - firstOffset
        registers = rawRegisters[start:start + spec.get("regCount", 1)]
        if len(registers) < spec.get("regCount", 1):
            continue
        tags[spec["tag"]] = decodeRegisters(spec, registers)
    return tags


def writeCoil(client, config, tag, value):
    """按标签名写一个线圈，用于把后端下发的控制指令传给模拟器。

    对应《接口规范》第八节控制时序的第 6 步：「后端通过协议解析写 Modbus 线圈」。

    Args:
        client: 已连接的 ModbusTcpClient。
        config: 点表配置。
        tag: 线圈标签名，如 pump_01_cmd_start。
        value: True 写 1，False 写 0。

    Returns:
        bool: True 表示写入成功。
    """
    coil = next((item for item in config["coils"] if item.get("tag") == tag), None)
    if coil is None:
        return False
    response = client.write_coil(
        address=coil["offset"],
        value=value,
        slave=config["connection"]["deviceId"],
    )
    return not response.isError()


def writeSetpoint(client, config, pumpIndex, frequencyHz):
    """写某台泵的频率给定值（40020 / 40021），实现变频调速。

    Args:
        client: 已连接的 ModbusTcpClient。
        config: 点表配置。
        pumpIndex: 0 表示 1# 泵，1 表示 2# 泵。
        frequencyHz: 目标频率，0~50。

    Returns:
        bool: True 表示写入成功。
    """
    tag = "pump_0%d_freq_setpoint" % (pumpIndex + 1)
    spec = next((item for item in config["holdings"] if item.get("tag") == tag), None)
    if spec is None:
        return False
    frequencyHz = max(0.0, min(50.0, float(frequencyHz)))
    registerValue = int(round(frequencyHz * spec.get("scale", 1)))
    response = client.write_register(
        address=spec["offset"],
        value=registerValue,
        slave=config["connection"]["deviceId"],
    )
    return not response.isError()


def readSetpoint(client, config, pumpIndex):
    """读某台泵当前的频率给定值，用于前端回显。

    Args:
        client: 已连接的 ModbusTcpClient。
        config: 点表配置。
        pumpIndex: 0 表示 1# 泵，1 表示 2# 泵。

    Returns:
        float | None: 给定频率 Hz；读不到返回 None。
    """
    tag = "pump_0%d_freq_setpoint" % (pumpIndex + 1)
    spec = next((item for item in config["holdings"] if item.get("tag") == tag), None)
    if spec is None:
        return None
    response = client.read_holding_registers(
        address=spec["offset"], count=1, slave=config["connection"]["deviceId"])
    if response.isError():
        return None
    return round(response.registers[0] / spec.get("scale", 1), 1)


def buildPayload(tags):
    """把标签字典包装成后端 / WebSocket 使用的消息体。

    形状与《接口规范与数据字典》5.2 节一致，便于后端直接往上加报警标志位后转发前端。
    注意：alarm_* 系列由后端判定后追加，协议解析不负责。

    Args:
        tags: readAllTags() 的返回值。

    Returns:
        dict: {"timestamp": <Unix 秒>, "data": {标签名: 值}}
    """
    return {
        "timestamp": int(time.time()),
        "data": tags,
    }


def formatLine(tags, config):
    """把标签字典格式化成一行人类可读的状态摘要，便于联调时肉眼检查。

    Args:
        tags: readAllTags() 的返回值。
        config: 点表配置（用于取中文名和单位）。

    Returns:
        str: 形如「液位 82.3cm | 出水总管压力 0.48MPa | ...」的单行文本。
    """
    cnMap = {spec["tag"]: (spec.get("cn", spec["tag"]), spec.get("unit", ""))
             for spec in config["holdings"] if spec.get("tag")}

    important = ["tank_level", "pipe_pressure", "pump_01_status", "pump_01_freq",
                 "pump_02_status", "pump_02_freq", "today_kwh"]
    parts = []
    for tag in important:
        if tag not in tags:
            continue
        cn, unit = cnMap.get(tag, (tag, ""))
        value = tags[tag]
        if tag.endswith("_status"):
            value = {0: "停止", 1: "运行", 2: "故障"}.get(int(value), value)
            parts.append("%s %s" % (cn, value))
        else:
            parts.append("%s %s%s" % (cn, value, unit))
    return " | ".join(parts)


# ---------------------------------------------------------------- 运行模式


def runWatch(client, config, asJson, once=False):
    """持续采集并输出。

    Args:
        client: ModbusTcpClient。
        config: 点表配置。
        asJson: True 时打印 JSON（给后端 / 前端用），False 时打印可读文本。
        once: True 时只读一次就返回。

    Returns:
        int: 退出码，0 表示正常，1 表示连不上或读不到数据。
    """
    intervalMs = config["timing"]["sampleIntervalMs"]
    logIntervalSec = config["timing"].get("logIntervalSec", 5)

    print("-" * 78)
    print("协议解析已启动")
    print("  目标地址 : %s:%d（从站 %d）"
          % (config["connection"]["host"], config["connection"]["port"],
             config["connection"]["deviceId"]))
    print("  采集周期 : %d ms" % intervalMs)
    print("  输出格式 : %s" % ("JSON" if asJson else "可读文本"))
    print("-" * 78)

    lastLogTime = 0.0
    while True:
        if not client.is_socket_open() or not client.connect():
            print("⚠ 连接断开，%.0f 秒后重连…" % RECONNECT_INTERVAL_SEC)
            time.sleep(RECONNECT_INTERVAL_SEC)
            client.connect()
            continue

        tags = readAllTags(client, config)
        if tags is None:
            print("⚠ 读取失败（模拟器可能没启动），%.0f 秒后重试…" % RECONNECT_INTERVAL_SEC)
            time.sleep(RECONNECT_INTERVAL_SEC)
            if once:
                return 1
            continue

        if asJson:
            print(json.dumps(buildPayload(tags), ensure_ascii=False), flush=True)
        else:
            now = time.time()
            if once or now - lastLogTime >= logIntervalSec:
                lastLogTime = now
                print("  " + formatLine(tags, config), flush=True)

        if once:
            return 0
        time.sleep(intervalMs / 1000.0)


def main():
    """命令行入口。

    Returns:
        int: 进程退出码。
    """
    parser = argparse.ArgumentParser(
        description="泵房 Modbus 协议解析：读寄存器 → 翻译标签名 → 还原工程值")
    parser.add_argument("--json", action="store_true",
                        help="输出后端使用的 JSON（默认输出可读文本）")
    parser.add_argument("--once", action="store_true",
                        help="只读一次就退出，用于快速验证链路")
    parser.add_argument("--config", default=None,
                        help="点表路径，默认与本文件同目录的 pointTable.json")
    args = parser.parse_args()

    configPath = args.config or os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "pointTable.json")
    config = loadConfig(configPath)

    client = connect(config)
    if not client.is_socket_open():
        print("❌ 连不上 %s:%d"
              % (config["connection"]["host"], config["connection"]["port"]))
        print("   请先确认模拟器已启动：另开一个窗口运行 python sensorSim.py")
        return 1

    try:
        return runWatch(client, config, args.json, args.once)
    except KeyboardInterrupt:
        print("\n已停止采集。")
        return 0
    finally:
        client.close()


if __name__ == "__main__":
    sys.exit(main())
