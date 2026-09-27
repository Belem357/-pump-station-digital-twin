# -*- coding: utf-8 -*-
"""③ 后端 · 报警引擎 —— 小型二供泵房数字孪生

报警由后端判定、前端只显示（接口规范 3.3 节）。本模块负责：

    1. 阈值判定：液位过低/过高、超压、水泵故障、通信中断
    2. 三重防抖（交互流程图流程二的硬性要求，全在后端）：
       - 双阈值回差：液位触发 25cm、恢复 30cm，区间内波动不翻转
       - 时间防抖：液位跌破 25cm 需连续 30 秒才真正触发（瞬时尖峰不算）
       - 启泵间隔：联锁停泵后 30 秒内拒绝启泵指令（由 main.py 查 canStart）
    3. 报警记录生命周期：触发时写一条 active 记录，恢复时补 time_end
    4. 联锁动作：低液位报警触发时，返回 "interlock_stop" 事件，
       main.py 负责向模拟器写停泵线圈

每 500ms 被 main.py 调用一次，内部状态（防抖计时）保存在实例里。
"""

import time

# 报警类型 → (设备编号, 级别)。level 取值见接口规范 6.5：
#   warning（警告，黄色）/ critical（严重，红色，触发联锁停泵）
ALARM_LEVELS = {
    "level_low":     ("tank",  "critical"),
    "level_high":    ("tank",  "warning"),
    "pressure_high": ("pipe",  "warning"),
    "pump_01_fault": ("pump_01", "critical"),
    "pump_02_fault": ("pump_02", "critical"),
    "comm_lost":     ("sys",   "warning"),
}

# 通信中断：后端连续 5 秒读不到模拟器数据才生成报警记录（接口规范 6.5）
COMM_LOST_DEBOUNCE_SEC = 5.0

# 报警消息模板（对应 pointTable.json 里的中文名，便于验收时人眼核对）
MESSAGES = {
    "level_low":     "水箱液位过低(%.1fcm)",
    "level_high":    "水箱液位过高(%.1fcm)",
    "pressure_high": "出水总管压力超限(%.3fMPa)",
    "pump_01_fault": "1#泵故障(状态=2)",
    "pump_02_fault": "2#泵故障(状态=2)",
    "comm_lost":     "模拟器通信中断(连续5秒读不到数据)",
}


class AlarmEngine:
    """报警判定与记录管理。

    线程安全性：update() 只在 main.py 的异步主循环里调用（单线程），
    不需要加锁；但 canStart() 会被控制接口从其他任务调用，
    所以 interlockUntil 的读写用普通属性即可（GIL 保证原子性）。
    """

    def __init__(self, alarmConfig, store):
        """初始化报警引擎。

        Args:
            alarmConfig: pointTable.json 的 alarm 段（阈值 + 防抖时长）。
            store: Storage 实例，用于写报警记录。
        """
        self.levelLow = alarmConfig["levelLow"]          # 液位过低阈值 25.0
        self.levelHigh = alarmConfig["levelHigh"]        # 液位过高阈值 140.0
        self.pressureHigh = alarmConfig["pressureHigh"]  # 超压阈值 0.8
        self.debounceSec = alarmConfig.get("interlockDelaySec", 30)  # 低液位时间防抖
        self.store = store

        # 每类报警的运行时状态
        self.state = {}
        for alarmType in ALARM_LEVELS:
            self.state[alarmType] = {
                "active": False,       # 当前是否处于报警状态
                "since": None,         # 越限起始时间戳（防抖计时用）
                "recordId": None,      # 已写入的报警记录编号
            }

        # 联锁停泵的 30 秒启泵禁令窗口（交互流程图流程二第三重防抖）
        self.interlockUntil = 0.0

    # ---------------------------------------------------------------- 对外接口

    def canStart(self):
        """联锁停泵后 30 秒内是否允许启泵指令。

        Returns:
            bool: True 表示可以下发 start；False 表示处于禁令窗口（应返回 1008）。
        """
        return time.time() >= self.interlockUntil

    def update(self, tags, now):
        """推进一步报警判定。

        Args:
            tags: readAllTags() 的标签字典；None 表示本周期读不到模拟器数据。
            now: 当前 Unix 秒级时间戳。

        Returns:
            tuple[dict, list[str]]:
              - flags: 要随 WebSocket 推送的报警标志位，
                       {"alarm_level_low": 0|1, ..., "alarm_active": 数量}
              - events: 触发的动作列表，目前只有 "interlock_stop" 一种，
                        main.py 收到后向模拟器写停泵线圈。
        """
        events = []
        # 通信中断单独处理：数据都读不到了，其他判定无从谈起
        if tags is None:
            self._updateCommLost(now)
        else:
            self._recoverCommLost(now)
            self._checkLevelLow(tags, now, events)
            self._checkLevelHigh(tags, now)
            self._checkPressure(tags, now)
            self._checkPumpFault(tags, now)

        flags = {
            "alarm_level_low": 1 if self.state["level_low"]["active"] else 0,
            "alarm_level_high": 1 if self.state["level_high"]["active"] else 0,
            "alarm_pressure_high": 1 if self.state["pressure_high"]["active"] else 0,
            "alarm_active": sum(1 for item in self.state.values() if item["active"]),
        }
        return flags, events

    # ---------------------------------------------------------------- 通用逻辑

    def _trigger(self, alarmType, now, value, threshold):
        """报警触发：进入报警态并写一条 active 记录。

        只在「未报警 → 报警」的上升沿生效，重复调用不会重复写记录。

        Args:
            alarmType: 报警类型。
            now: 时间戳。
            value: 触发时刻的测量值。
            threshold: 阈值。

        Returns:
            None
        """
        item = self.state[alarmType]
        if item["active"]:
            return
        item["active"] = True
        deviceId, level = ALARM_LEVELS[alarmType]
        template = MESSAGES[alarmType]
        # 静态文案（泵故障、通信中断）没有 % 占位符，只有带占位符的模板才格式化
        message = template % value if "%" in template and value is not None else template
        item["recordId"] = self.store.addAlarm({
            "type": alarmType, "device_id": deviceId, "level": level,
            "time_start": int(now), "value_at_trigger": value,
            "threshold": threshold, "message": message,
        })

    def _recover(self, alarmType, now):
        """报警恢复：退出报警态并把记录标记为 recovered。

        Args:
            alarmType: 报警类型。
            now: 时间戳。

        Returns:
            None
        """
        item = self.state[alarmType]
        if not item["active"]:
            return
        item["active"] = False
        item["since"] = None
        if item["recordId"] is not None:
            self.store.closeAlarm(item["recordId"], int(now))
            item["recordId"] = None

    # ---------------------------------------------------------------- 各类判定

    def _checkLevelLow(self, tags, now, events):
        """液位过低：< 25cm 连续 30 秒才触发；> 30cm 恢复（双阈值回差）。

        触发的同时返回 interlock_stop 事件（PRD F10 联锁停泵），
        并进入 30 秒启泵禁令窗口（交互流程图流程二）。
        """
        item = self.state["level_low"]
        level = tags.get("tank_level")
        if level is None:
            return
        if level < self.levelLow:
            if item["since"] is None:
                item["since"] = now
            if item["active"]:
                return
            if now - item["since"] >= self.debounceSec:
                self._trigger("level_low", now, level, self.levelLow)
                self.interlockUntil = now + self.debounceSec
                events.append("interlock_stop")
        elif level > self.levelLow + 5.0:   # 恢复阈值 30cm，高于触发阈值 25cm
            if item["active"]:
                self._recover("level_low", now)
            item["since"] = None
        # 在 25~30 之间：保持现状不动，这是防抖的关键区间

    def _checkLevelHigh(self, tags, now):
        """液位过高：> 140cm 立即报警；< 135cm 恢复（同样带回差）。"""
        item = self.state["level_high"]
        level = tags.get("tank_level")
        if level is None:
            return
        if level > self.levelHigh and not item["active"]:
            self._trigger("level_high", now, level, self.levelHigh)
        elif level < self.levelHigh - 5.0 and item["active"]:
            self._recover("level_high", now)

    def _checkPressure(self, tags, now):
        """超压：> 0.8MPa 报警；< 0.78MPa 恢复。

        超压只报警不联锁停泵（接口规范第十节 #3：停泵会导致高层住户断水）。
        """
        item = self.state["pressure_high"]
        pressure = tags.get("pipe_pressure")
        if pressure is None:
            return
        if pressure > self.pressureHigh and not item["active"]:
            self._trigger("pressure_high", now, pressure, self.pressureHigh)
        elif pressure < self.pressureHigh - 0.02 and item["active"]:
            self._recover("pressure_high", now)

    def _checkPumpFault(self, tags, now):
        """水泵故障：pump_0x_status = 2 立即报警，恢复为 0/1 时消除。"""
        for pumpIndex in (1, 2):
            alarmType = "pump_0%d_fault" % pumpIndex
            item = self.state[alarmType]
            status = tags.get("pump_0%d_status" % pumpIndex)
            if status is None:
                continue
            if status == 2 and not item["active"]:
                self._trigger(alarmType, now, 2, None)
            elif status != 2 and item["active"]:
                self._recover(alarmType, now)

    def _updateCommLost(self, now):
        """通信中断计时：连续 5 秒读不到数据才触发 comm_lost 报警。"""
        item = self.state["comm_lost"]
        if item["since"] is None:
            item["since"] = now
        if not item["active"] and now - item["since"] >= COMM_LOST_DEBOUNCE_SEC:
            self._trigger("comm_lost", now, None, None)

    def _recoverCommLost(self, now):
        """数据恢复即消除 comm_lost 报警。"""
        self._recover("comm_lost", now)
