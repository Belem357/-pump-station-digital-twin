# -*- coding: utf-8 -*-
"""链路自检 —— 小型二供泵房数字孪生

一条命令验证「模拟器 ↔ 协议解析」双向都通：
  上行：模拟器造数 → 协议解析读得到、翻译得对
  下行：写线圈 → 模拟器真的执行、寄存器真的翻转

第 1 周交给导师的最小 demo，跑这个脚本出结果就行。

用法（先另开一个窗口把 sensorSim.py 跑起来）：
    python linkCheck.py

全部通过时退出码为 0，有失败项为 1。

依赖：pymodbus==3.6.9（见 requirements.txt）
"""

import os
import sys
import time

from pymodbus.client import ModbusTcpClient

from protocolParser import buildPayload, getDataHoldings, loadConfig, readAllTags

FC_COIL = 1
WAIT_AFTER_COMMAND_SEC = 4.0   # 等模拟器把频率升上去


def check(title, condition, detail=""):
    """打印一条检查结果。

    Args:
        title: 检查项名称。
        condition: 判定结果，True 为通过。
        detail: 补充说明，会拼在行尾。

    Returns:
        bool: 原样返回 condition，便于调用处累计。
    """
    mark = "✅" if condition else "❌"
    print("  %s %s%s" % (mark, title, ("  —— " + detail) if detail else ""))
    return bool(condition)


def writeCoil(client, config, tag, value):
    """按标签名写一个线圈。

    Args:
        client: ModbusTcpClient。
        config: 点表配置。
        tag: 线圈标签名，如 pump_01_cmd_start。
        value: True 写 1，False 写 0。

    Returns:
        bool: 写入是否成功。
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


def readTag(client, config, tag):
    """读一个测点的当前工程值。

    Args:
        client: ModbusTcpClient。
        config: 点表配置。
        tag: 标签名。

    Returns:
        float | int | None: 工程值；读不到返回 None。
    """
    tags = readAllTags(client, config)
    return None if tags is None else tags.get(tag)


def main():
    """执行全部自检项。

    Returns:
        int: 0 表示全部通过，1 表示有失败项。
    """
    configPath = os.path.join(os.path.dirname(os.path.abspath(__file__)), "pointTable.json")
    config = loadConfig(configPath)
    host = config["connection"]["host"]
    port = config["connection"]["port"]

    print("=" * 74)
    print("泵房数字孪生 · 链路自检")
    print("=" * 74)

    results = []

    # ---------- 1. 连接 ----------
    print("\n[1/5] 连接模拟器 %s:%d" % (host, port))
    client = ModbusTcpClient(host, port=port, timeout=3)
    client.connect()
    if not client.is_socket_open():
        print("  ❌ 连不上。请先另开一个窗口运行：python sensorSim.py")
        return 1
    results.append(check("TCP 连接建立", True))

    # ---------- 2. 上行：读数据 ----------
    print("\n[2/5] 上行：读数据并翻译标签名")
    tags = readAllTags(client, config)
    results.append(check("一次读回全部测点", tags is not None and len(tags) > 0,
                         "共 %d 个" % (len(tags) if tags else 0)))
    if tags:
        expected = [spec["tag"] for spec in getDataHoldings(config)]
        missing = [tag for tag in expected if tag not in tags]
        results.append(check("标签名全部齐了", not missing,
                             "缺 %s" % missing if missing else "无缺失"))

        level = tags.get("tank_level")
        results.append(check("液位在合理量程内（0~150cm）",
                             level is not None and 0 <= level <= 150,
                             "当前 %.1f cm" % level if level is not None else "读不到"))

        pressure = tags.get("pipe_pressure")
        results.append(check("压力在合理量程内（0~1.0MPa）",
                             pressure is not None and 0 <= pressure <= 1.0,
                             "当前 %.3f MPa" % pressure if pressure is not None else "读不到"))

        results.append(check("状态量是整数而非浮点（接口规范定义为 Int）",
                             isinstance(tags.get("pump_01_status"), int),
                             "类型 %s" % type(tags.get("pump_01_status")).__name__))

    # ---------- 3. 数据确实在变 ----------
    print("\n[3/5] 上行：数据是否在持续变化（说明模拟器在推进）")
    before = readTag(client, config, "tank_level")
    time.sleep(3.0)
    after = readTag(client, config, "tank_level")
    changed = before is not None and after is not None and abs(after - before) > 0.05
    results.append(check("3 秒后液位有变化", changed,
                         "%.1f → %.1f" % (before, after) if changed
                         else "无变化，模拟器可能没在跑"))

    # ---------- 4. 下行：控制指令 ----------
    print("\n[4/5] 下行：写线圈指令，模拟器是否真的执行")
    if tags and tags.get("pump_01_status") == 1:
        writeCoil(client, config, "pump_01_cmd_stop", True)
        time.sleep(WAIT_AFTER_COMMAND_SEC)
        print("  （1#泵原本在运行，先停掉再测）")

    results.append(check("写入启动线圈 00001", writeCoil(client, config, "pump_01_cmd_start", True)))
    time.sleep(WAIT_AFTER_COMMAND_SEC)
    status = readTag(client, config, "pump_01_status")
    freq = readTag(client, config, "pump_01_freq")
    results.append(check("1#泵状态变为运行", status == 1, "状态 = %s" % status))
    results.append(check("1#泵频率已升起来", freq is not None and freq > 1.0,
                         "频率 = %s Hz" % freq))
    results.append(check("线圈已自动复位（可重复下发）",
                         readCoilValue(client, config, "pump_01_cmd_start") is False))

    current = readTag(client, config, "pump_01_current")
    results.append(check("运行后电流不再是 0", current is not None and current > 0.5,
                         "电流 = %s A" % current))

    # ---------- 5. 停止 + 输出样例 ----------
    print("\n[5/5] 下行：停止指令 + 输出后端要用的数据")
    results.append(check("写入停止线圈 00002", writeCoil(client, config, "pump_01_cmd_stop", True)))
    time.sleep(2.0)

    finalTags = readAllTags(client, config)
    if finalTags:
        import json
        print("\n  协议解析输出样例（后端拿到的就是这个形状）：")
        print("  " + json.dumps(buildPayload(finalTags), ensure_ascii=False))

    client.close()

    # ---------- 汇总 ----------
    passed = sum(1 for item in results if item)
    total = len(results)
    print()
    print("=" * 74)
    print("结果： %d / %d 项通过" % (passed, total))
    print("=" * 74)
    if passed == total:
        print("🎉 链路全通。第 1 周的最小 demo 达标了。")
        return 0
    print("⚠ 有失败项，对照上面的 ❌ 逐条排查，或把输出发给组长。")
    return 1


def readCoilValue(client, config, tag):
    """读一个线圈的当前值（用于验证模拟器是否已自动复位）。

    Args:
        client: ModbusTcpClient。
        config: 点表配置。
        tag: 线圈标签名。

    Returns:
        bool | None: 线圈状态；读不到返回 None。
    """
    coil = next((item for item in config["coils"] if item.get("tag") == tag), None)
    if coil is None:
        return None
    response = client.read_coils(address=coil["offset"], count=1,
                                 slave=config["connection"]["deviceId"])
    if response.isError():
        return None
    return bool(response.bits[0])


if __name__ == "__main__":
    sys.exit(main())
