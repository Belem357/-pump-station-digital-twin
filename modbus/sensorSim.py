# -*- coding: utf-8 -*-
"""① 传感器与设备模拟 —— 小型二供泵房数字孪生

用 pymodbus 起一个 Modbus TCP 从站，假装自己是一台真实的泵房控制柜：
按《05.Modbus建点表与IO点表》建点，持续产生数据，并响应后端下发的启停 / 调速指令。

运行：
    python sensorSim.py

跑起来后不要关这个窗口。另开一个窗口用 QModMaster 连 127.0.0.1:502，
读保持寄存器 40001（代码地址 0，数量 21），应能看到液位等数值在不断变化。

依赖：pymodbus==3.6.9（见 requirements.txt）
"""

import asyncio
import json
import os
import random
import struct
import sys
import time

from pymodbus.datastore import (
    ModbusSequentialDataBlock,
    ModbusServerContext,
    ModbusSlaveContext,
)
from pymodbus.server import StartAsyncTcpServer

# ---------------------------------------------------------------- 常量

FC_COIL = 1
FC_HOLDING = 3

# 保持寄存器总数（点数到 40021 即偏移 20，留一些余量）
HOLDING_COUNT = 64
COIL_COUNT = 16

# 水力参数（用于造出「看起来像真的」的数据，不必精确）
INFLOW_CM_PER_SEC = 0.45        # 进水总管给水箱补水的速度 cm/s
PUMP_MAX_OUT_CM_PER_SEC = 1.60  # 单台泵满频（50Hz）抽水的速度 cm/s
FREQ_RAMP_UP_HZ_PER_SEC = 8.0   # 变频升频速度
FREQ_RAMP_DOWN_HZ_PER_SEC = 15.0  # 变频降频速度
DEFAULT_SETPOINT_HZ = 40.0      # 启动后若未给频率，默认按 40Hz 跑
KWH_INITIAL = 12.6              # 今日耗电量的起始基数（kWh）
# 为什么要给基数：耗电量在真实泵房里是电度表量出来的，不是算出来的，
# 所以由模拟器提供（对应《接口规范》40012~40013）。
# 但模拟器从 0 开始积分的话，泵跑几秒才 0.004 度，演示时看着不真实；
# 给个「今天早些时候已经用掉一些」的基数，数值才像样。
# 12.6 这个值取自《接口规范》5.2 节的示例，对得上。

# 阈值（与 pointTable.json 的 alarm 段一致）
LEVEL_LOW = 25.0
LEVEL_HIGH = 140.0
LEVEL_AUTO_START = 90.0         # 自动模式：液位高于此值启泵
LEVEL_AUTO_STOP = 40.0          # 自动模式：液位低于此值停泵
INTERLOCK_DELAY_SEC = 30.0      # 低液位联锁防抖时间（PRD F10）


# ---------------------------------------------------------------- 配置


def loadConfig(configPath):
    """读取点表配置文件。

    Args:
        configPath: pointTable.json 的路径。

    Returns:
        dict: 解析后的配置字典，含 connection / timing / alarm / holdings / coils。
    """
    with open(configPath, "r", encoding="utf-8") as fileHandle:
        return json.load(fileHandle)


def buildServerContext():
    """按点表规模创建 Modbus 数据区。

    Returns:
        ModbusServerContext: pymodbus 的服务端上下文，所有从站共用一份数据。
    """
    slaveContext = ModbusSlaveContext(
        di=ModbusSequentialDataBlock(0, [0] * HOLDING_COUNT),
        co=ModbusSequentialDataBlock(0, [False] * COIL_COUNT),
        hr=ModbusSequentialDataBlock(0, [0] * HOLDING_COUNT),
        ir=ModbusSequentialDataBlock(0, [0] * HOLDING_COUNT),
    )
    return ModbusServerContext(slaves=slaveContext, single=True)


# ---------------------------------------------------------------- 数值换算


def float32ToWords(value):
    """把 32 位浮点数拆成两个 16 位寄存器，高字在前。

    这是 today_kwh 专用的特例：全表只有它不走「整数 × 缩放」那套，
    而是 IEEE754 双字，协议解析侧必须按同样顺序拼回去。

    Args:
        value: 要拆分的浮点数。

    Returns:
        list[int]: [高字, 低字]，两个 0~65535 的整数。
    """
    packed = struct.pack(">f", float(value))
    highWord, lowWord = struct.unpack(">HH", packed)
    return [highWord, lowWord]


def wordsToFloat32(highWord, lowWord):
    """把高字在前的两个寄存器拼回 32 位浮点数（与 float32ToWords 互为逆运算）。

    Args:
        highWord: 高 16 位。
        lowWord: 低 16 位。

    Returns:
        float: 还原后的浮点数。
    """
    return struct.unpack(">f", struct.pack(">HH", highWord, lowWord))[0]


def encodeValue(spec, realValue):
    """把工程值按点表规则编码成寄存器整数列表。

    Args:
        spec: pointTable.json 里的一条 holding 配置。
        realValue: 实际工程值，例如液位 85.5 cm。

    Returns:
        list[int]: 待写入的寄存器值；float32 类型返回两个寄存器。
    """
    if spec.get("type") == "float32":
        return float32ToWords(realValue)
    scale = spec.get("scale", 1)
    scaled = int(round(float(realValue) * scale))
    return [max(0, min(65535, scaled))]


def decodeValue(spec, registers):
    """把寄存器整数解码成工程值（与 encodeValue 互为逆运算）。

    Args:
        spec: pointTable.json 里的一条 holding 配置。
        registers: 从寄存器读回的整数列表。

    Returns:
        float: 还原后的工程值。
    """
    if spec.get("type") == "float32":
        return round(wordsToFloat32(registers[0], registers[1]), 3)
    scale = spec.get("scale", 1)
    return round(registers[0] / scale, 3)


# ---------------------------------------------------------------- 读写下层


def writeTag(context, spec, realValue):
    """把一个工程值写进对应的保持寄存器。

    Args:
        context: ModbusServerContext。
        spec: 一条 holding 配置。
        realValue: 工程值。

    Returns:
        None
    """
    context[0].setValues(FC_HOLDING, spec["offset"], encodeValue(spec, realValue))


def readRawHolding(context, offset, count=1):
    """读取原始寄存器值（未做缩放），用于读后端写入的给定值。

    Args:
        context: ModbusServerContext。
        offset: 0 基地址。
        count: 读几个寄存器。

    Returns:
        list[int]: 原始寄存器值列表。
    """
    return context[0].getValues(FC_HOLDING, offset, count)


def readCoil(context, offset):
    """读一个线圈当前是否为 1。

    Args:
        context: ModbusServerContext。
        offset: 线圈的 0 基地址。

    Returns:
        bool: True 表示后端写入了指令。
    """
    return bool(context[0].getValues(FC_COIL, offset, 1)[0])


def resetCoil(context, offset):
    """把线圈复位为 0。

    线圈是「一次性脉冲」：模拟器执行完动作后必须自己复位，
    否则后端下次再写 1 时分不清是新指令还是上次的残留。

    Args:
        context: ModbusServerContext。
        offset: 线圈的 0 基地址。

    Returns:
        None
    """
    context[0].setValues(FC_COIL, offset, [False])


# ---------------------------------------------------------------- 模拟逻辑


def createInitialState(config):
    """创建泵房运行状态的初始值。

    Args:
        config: 点表配置。

    Returns:
        dict: 模拟器内部状态。
    """
    return {
        "level": 80.0,            # 液位 cm
        "pumpStatus": [0, 0],     # 0=停止 1=运行 2=故障
        "pumpFreq": [0.0, 0.0],   # 当前频率 Hz
        "pumpSetpoint": [DEFAULT_SETPOINT_HZ, DEFAULT_SETPOINT_HZ],  # 目标频率
        "pumpTemp": [26.0, 26.0],  # 电机温度 ℃
        "kwh": KWH_INITIAL,       # 今日耗电量（带基数，见 KWH_INITIAL 说明）
        "mode": 0,                # 0=手动 1=自动
        "lowLevelTimer": 0.0,     # 低液位持续时间，用于 30 秒防抖
        "valveOpening": 100.0,    # 进水阀开度 %
        "pressure": 0.0,          # 出水压力 MPa
        "flowSpeed": 0.0,         # 出水主管流速 m/s
        "lastLogTime": 0.0,
    }


def applyCommands(context, state, config):
    """读取线圈指令并执行，执行后把线圈复位。

    Args:
        context: ModbusServerContext。
        state: 模拟器内部状态（会被就地修改）。
        config: 点表配置。

    Returns:
        list[str]: 本次执行的指令描述，用于日志。
    """
    actions = []
    coilMap = {coil["tag"]: coil["offset"] for coil in config["coils"] if coil.get("tag")}

    for pumpIndex in (0, 1):
        startTag = "pump_0%d_cmd_start" % (pumpIndex + 1)
        stopTag = "pump_0%d_cmd_stop" % (pumpIndex + 1)

        if startTag in coilMap and readCoil(context, coilMap[startTag]):
            if state["mode"] == 1:
                actions.append("忽略 %s（当前为自动模式，PRD 要求自动模式屏蔽手动指令）" % startTag)
            else:
                state["pumpStatus"][pumpIndex] = 1
                if state["pumpFreq"][pumpIndex] <= 0.0:
                    state["pumpFreq"][pumpIndex] = 0.1  # 给个起步值，随后升频
                actions.append("%d#泵启动" % (pumpIndex + 1))
            resetCoil(context, coilMap[startTag])

        if stopTag in coilMap and readCoil(context, coilMap[stopTag]):
            state["pumpStatus"][pumpIndex] = 0
            actions.append("%d#泵停止" % (pumpIndex + 1))
            resetCoil(context, coilMap[stopTag])

    if "sys_cmd_set_mode" in coilMap and readCoil(context, coilMap["sys_cmd_set_mode"]):
        state["mode"] = 0 if state["mode"] == 1 else 1
        actions.append("切换为%s模式" % ("自动" if state["mode"] == 1 else "手动"))
        resetCoil(context, coilMap["sys_cmd_set_mode"])

    return actions


def autoControl(state):
    """自动模式下的控泵逻辑：液位高于上限启泵、低于下限停泵。

    Args:
        state: 模拟器内部状态（会被就地修改）。

    Returns:
        list[str]: 本次自动执行的动作描述。
    """
    if state["mode"] != 1:
        return []

    actions = []
    runningCount = sum(1 for status in state["pumpStatus"] if status == 1)

    if state["level"] >= LEVEL_AUTO_START and runningCount == 0:
        state["pumpStatus"][0] = 1
        state["pumpFreq"][0] = 0.1
        actions.append("自动启 1#泵（液位 %.1f ≥ %.0f）" % (state["level"], LEVEL_AUTO_START))
    elif state["level"] >= LEVEL_HIGH - 10 and runningCount == 1:
        state["pumpStatus"][1] = 1
        state["pumpFreq"][1] = 0.1
        actions.append("自动启 2#泵（液位 %.1f 偏高）" % state["level"])
    elif state["level"] <= LEVEL_AUTO_STOP and runningCount > 0:
        for pumpIndex in (0, 1):
            state["pumpStatus"][pumpIndex] = 0
        actions.append("自动停泵（液位 %.1f ≤ %.0f）" % (state["level"], LEVEL_AUTO_STOP))

    return actions


def checkInterlock(state, deltaTime):
    """低液位联锁保护：液位低于 25cm 持续 30 秒则停掉所有泵，防止空转烧毁。

    对应 PRD F10（P0）。带防抖，避免液位在阈值附近波动时频繁启停。

    Args:
        state: 模拟器内部状态（会被就地修改）。
        deltaTime: 本次推进的秒数。

    Returns:
        list[str]: 触发联锁时的动作描述，未触发返回空列表。
    """
    if state["level"] < LEVEL_LOW:
        state["lowLevelTimer"] += deltaTime
        if state["lowLevelTimer"] >= INTERLOCK_DELAY_SEC:
            stillRunning = [i for i, status in enumerate(state["pumpStatus"]) if status == 1]
            if stillRunning:
                for pumpIndex in stillRunning:
                    state["pumpStatus"][pumpIndex] = 0
                state["lowLevelTimer"] = 0.0
                return ["⚠ 低液位联锁触发：已切断所有运行中的水泵（液位 %.1fcm 持续 %.0f 秒）"
                        % (state["level"], INTERLOCK_DELAY_SEC)]
    else:
        state["lowLevelTimer"] = 0.0
    return []


def stepPhysics(state, deltaTime, config):
    """推进一帧水力/电机物理量。

    规则尽量贴近真实，但不必精确：
      - 水箱：进水总管持续补水，运行中的水泵往外抽水
      - 压力、流速：与在运行的泵的总频率成正比
      - 电流：与频率成正比，满频约 15A
      - 温度：运行时升温趋向 26 + 0.75×频率，停机时自然降温
      - 电量：按电流估算功率后对时间积分

    Args:
        state: 模拟器内部状态（会被就地修改）。
        deltaTime: 本次推进的秒数。
        config: 点表配置。

    Returns:
        None
    """
    # 频率向给定值靠拢（变频器升降频不是瞬间完成的）
    for pumpIndex in (0, 1):
        target = state["pumpSetpoint"][pumpIndex] if state["pumpStatus"][pumpIndex] == 1 else 0.0
        current = state["pumpFreq"][pumpIndex]
        rate = FREQ_RAMP_UP_HZ_PER_SEC if target > current else FREQ_RAMP_DOWN_HZ_PER_SEC
        step = rate * deltaTime
        if abs(target - current) <= step:
            state["pumpFreq"][pumpIndex] = target
        else:
            state["pumpFreq"][pumpIndex] = current + (step if target > current else -step)
        state["pumpFreq"][pumpIndex] = max(0.0, min(50.0, state["pumpFreq"][pumpIndex]))

    # 液位
    inflow = INFLOW_CM_PER_SEC * (state["valveOpening"] / 100.0)
    outflow = sum(PUMP_MAX_OUT_CM_PER_SEC * (freq / 50.0) for freq in state["pumpFreq"])
    state["level"] += (inflow - outflow) * deltaTime
    state["level"] = max(0.0, min(150.0, state["level"]))

    # 压力与流速
    totalFreqRatio = sum(state["pumpFreq"]) / 50.0
    state["pressure"] = round(min(1.0, 0.12 + 0.36 * totalFreqRatio), 3)
    state["flowSpeed"] = round(min(3.0, 1.20 * totalFreqRatio), 2)

    # 电机温度与耗电量
    for pumpIndex in (0, 1):
        freq = state["pumpFreq"][pumpIndex]
        targetTemp = 26.0 + 0.75 * freq
        temp = state["pumpTemp"][pumpIndex]
        tempStep = 0.06 * deltaTime
        if abs(targetTemp - temp) <= tempStep:
            state["pumpTemp"][pumpIndex] = targetTemp
        else:
            state["pumpTemp"][pumpIndex] = temp + (tempStep if targetTemp > temp else -tempStep)
        state["pumpTemp"][pumpIndex] = max(26.0, min(120.0, state["pumpTemp"][pumpIndex]))

        current = 0.30 * freq + random.uniform(-0.12, 0.12)
        current = max(0.0, min(20.0, current))
        powerKw = 1.732 * 380 * current * 0.85 / 1000.0
        state["kwh"] += powerKw * deltaTime / 3600.0


def publishValues(context, state, config):
    """把内部状态按点表写进保持寄存器。

    Args:
        context: ModbusServerContext。
        state: 模拟器内部状态。
        config: 点表配置。

    Returns:
        None
    """
    values = {
        "tank_level": state["level"],
        "pipe_pressure": state["pressure"],
        "pump_01_status": state["pumpStatus"][0],
        "pump_01_freq": state["pumpFreq"][0],
        "pump_01_current": max(0.0, 0.30 * state["pumpFreq"][0]),
        "pump_01_temp": state["pumpTemp"][0],
        "pump_02_status": state["pumpStatus"][1],
        "pump_02_freq": state["pumpFreq"][1],
        "pump_02_current": max(0.0, 0.30 * state["pumpFreq"][1]),
        "pump_02_temp": state["pumpTemp"][1],
        "sys_mode": state["mode"],
        "today_kwh": round(state["kwh"], 3),
        "valve_inlet_opening": state["valveOpening"],
        "pipe_flow_speed": state["flowSpeed"],
    }

    for spec in config["holdings"]:
        tag = spec.get("tag")
        if tag in values:
            writeTag(context, spec, values[tag])


def readSetpoints(context, state, config):
    """读取后端写入的频率给定值（40020 / 40021），更新内部目标频率。

    Args:
        context: ModbusServerContext。
        state: 模拟器内部状态（会被就地修改）。
        config: 点表配置。

    Returns:
        None
    """
    for pumpIndex in (0, 1):
        tag = "pump_0%d_freq_setpoint" % (pumpIndex + 1)
        spec = next((item for item in config["holdings"] if item.get("tag") == tag), None)
        if spec is None:
            continue
        raw = readRawHolding(context, spec["offset"], 1)[0]
        if raw == 0:
            continue
        setpoint = raw / spec.get("scale", 1)
        if 0 < setpoint <= 50:
            state["pumpSetpoint"][pumpIndex] = setpoint


def formatStatusLine(state, config):
    """生成一行人类可读的状态摘要，用于控制台输出。

    Args:
        state: 模拟器内部状态。
        config: 点表配置。

    Returns:
        str: 形如「液位 82.3cm | 压力 0.48MPa | 1#泵 运行 40.0Hz 12.0A 45.6℃」的单行文本。
    """
    pumpText = []
    for pumpIndex in (0, 1):
        statusText = {0: "停止", 1: "运行", 2: "故障"}[state["pumpStatus"][pumpIndex]]
        pumpText.append(
            "%d#泵 %s %4.1fHz %4.1fA %4.1f℃"
            % (pumpIndex + 1, statusText,
               state["pumpFreq"][pumpIndex],
               0.30 * state["pumpFreq"][pumpIndex],
               state["pumpTemp"][pumpIndex])
        )
    return ("液位 %5.1fcm | 压力 %.2fMPa | %s | 今日 %.3fkWh | %s"
            % (state["level"], state["pressure"], "  ".join(pumpText),
               state["kwh"], "自动" if state["mode"] == 1 else "手动"))


async def simulationLoop(context, config):
    """模拟主循环：按 simTickMs 推进物理量、执行指令、写入寄存器。

    Args:
        context: ModbusServerContext。
        config: 点表配置。

    Returns:
        None（协程永不正常返回，靠 Ctrl+C 结束）
    """
    tickSeconds = config["timing"]["simTickMs"] / 1000.0
    logInterval = config["timing"].get("logIntervalSec", 5)
    state = createInitialState(config)

    print("-" * 78)
    print("泵房模拟器已启动")
    print("  监听地址 : %s:%d" % (config["connection"]["host"], config["connection"]["port"]))
    print("  从站地址 : %d" % config["connection"]["deviceId"])
    print("  推进周期 : %d ms" % config["timing"]["simTickMs"])
    print("  验证方法 : 用 QModMaster 连 127.0.0.1:%d，读保持寄存器地址 0 共 21 个"
          % config["connection"]["port"])
    print("  按 Ctrl+C 退出")
    print("-" * 78)

    while True:
        await asyncio.sleep(tickSeconds)

        for action in applyCommands(context, state, config):
            print("  [指令] %s" % action)

        for action in autoControl(state):
            print("  [自动] %s" % action)

        for action in checkInterlock(state, tickSeconds):
            print("  [联锁] %s" % action)

        readSetpoints(context, state, config)
        stepPhysics(state, tickSeconds, config)
        publishValues(context, state, config)

        now = time.time()
        if now - state["lastLogTime"] >= logInterval:
            state["lastLogTime"] = now
            print("  " + formatStatusLine(state, config))


# ---------------------------------------------------------------- 入口


def main():
    """程序入口：读点表、建数据区、起服务。

    Returns:
        int: 进程退出码，0 表示正常结束。
    """
    configPath = os.path.join(os.path.dirname(os.path.abspath(__file__)), "pointTable.json")
    config = loadConfig(configPath)
    context = buildServerContext()

    address = (config["connection"]["host"], config["connection"]["port"])

    async def runServer():
        loop = asyncio.get_event_loop()
        loop.create_task(simulationLoop(context, config))
        await StartAsyncTcpServer(context, address=address)

    try:
        asyncio.run(runServer())
    except KeyboardInterrupt:
        print("\n模拟器已停止。")
    except OSError as error:
        print("\n❌ 端口绑定失败：%s" % error)
        print("   可能是 %d 端口被占用。把 pointTable.json 里的 port 改成 5020，")
        print("   protocolParser.py 里也一起改，两边保持一致即可。"
              % config["connection"]["port"])
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
