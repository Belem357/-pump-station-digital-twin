# -*- coding: utf-8 -*-
"""③ 后端 · 数据存储 —— 小型二供泵房数字孪生

用 SQLite 存三类数据，全部由后端主程序调用：

    1. 历史数据（history）—— 每 5 秒采样一次，供 /api/v1/history 画曲线
    2. 报警记录（alarms） —— 报警引擎触发/恢复时写入，供 /api/v1/alarms 查询
    3. 操作日志（op_logs）—— 每次控制指令都记，含操作人与时间戳（PRD 1.3 要求）

存储方案说明：接口规范第十节 #4 建议首选 InfluxDB、安装困难则降级 SQLite。
课程项目直接采用 SQLite（标准库自带、零安装、单文件），功能与验收项完全一致。
将来要换 InfluxDB，只改这一个文件即可。

依赖：无（仅 Python 标准库 sqlite3）
"""

import os
import sqlite3
import threading
import time

# 历史数据保留时长（秒）。48 小时足够覆盖"过去 1 小时曲线"的验收要求，
# 留出富余也避免数据库文件无限增长。
HISTORY_RETENTION_SEC = 48 * 3600


class Storage:
    """SQLite 存储封装。

    所有写操作都经过一把线程锁（sqlite3 连接不是线程安全的，
    而报警引擎在异步循环里跑、控制接口在线程池里跑）。
    """

    def __init__(self, dbPath):
        """打开数据库并建表。

        Args:
            dbPath: 数据库文件路径，父目录不存在会自动创建。
        """
        os.makedirs(os.path.dirname(dbPath), exist_ok=True)
        self.conn = sqlite3.connect(dbPath, check_same_thread=False)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.lock = threading.Lock()
        self._createTables()

    def _createTables(self):
        """建三张表（已存在则跳过）。"""
        with self.lock, self.conn:
            self.conn.execute(
                """CREATE TABLE IF NOT EXISTS history (
                       tag TEXT NOT NULL,
                       t   INTEGER NOT NULL,
                       v   REAL NOT NULL,
                       PRIMARY KEY (tag, t)
                   )""")
            self.conn.execute(
                """CREATE INDEX IF NOT EXISTS idx_history_t ON history(t)""")
            self.conn.execute(
                """CREATE TABLE IF NOT EXISTS alarms (
                       alarm_id         TEXT PRIMARY KEY,
                       type             TEXT NOT NULL,
                       device_id        TEXT NOT NULL,
                       level            TEXT NOT NULL,
                       status           TEXT NOT NULL,
                       time_start       INTEGER NOT NULL,
                       time_end         INTEGER,
                       value_at_trigger REAL,
                       threshold        REAL,
                       message          TEXT NOT NULL
                   )""")
            self.conn.execute(
                """CREATE INDEX IF NOT EXISTS idx_alarms_start ON alarms(time_start)""")
            self.conn.execute(
                """CREATE TABLE IF NOT EXISTS op_logs (
                       id        INTEGER PRIMARY KEY AUTOINCREMENT,
                       ts        INTEGER NOT NULL,
                       device_id TEXT NOT NULL,
                       command   TEXT NOT NULL,
                       value     REAL,
                       operator  TEXT NOT NULL,
                       code      INTEGER NOT NULL,
                       msg       TEXT
                   )""")

    # ---------------------------------------------------------------- 历史数据

    def insertHistoryAt(self, timestamp, tagValues):
        """把同一时刻的一批标签值写入历史表。

        Args:
            timestamp: Unix 秒级时间戳。
            tagValues: dict {标签名: 数值}。

        Returns:
            None
        """
        rows = [(tag, timestamp, float(value))
                for tag, value in tagValues.items() if value is not None]
        if not rows:
            return
        with self.lock, self.conn:
            self.conn.executemany(
                "INSERT OR REPLACE INTO history(tag, t, v) VALUES (?, ?, ?)", rows)

    def queryHistory(self, tag, start, end):
        """查询某个标签在时间区间内的原始样本（升序）。

        Args:
            tag: 标签名。
            start: 起始时间戳（含）。
            end: 结束时间戳（含）。

        Returns:
            list[tuple[int, float]]: [(t, v), ...]，无数据返回空列表。
        """
        with self.lock:
            cursor = self.conn.execute(
                "SELECT t, v FROM history WHERE tag = ? AND t >= ? AND t <= ? ORDER BY t",
                (tag, start, end))
            return cursor.fetchall()

    def pruneHistory(self, now):
        """删除过期历史样本。

        Args:
            now: 当前时间戳。

        Returns:
            int: 删除的行数。
        """
        cutoff = now - HISTORY_RETENTION_SEC
        with self.lock, self.conn:
            cursor = self.conn.execute("DELETE FROM history WHERE t < ?", (cutoff,))
            return cursor.rowcount

    # ---------------------------------------------------------------- 报警记录

    def nextAlarmId(self, now):
        """生成报警编号，格式 A + 日期 + 当日三位序号，如 A20260923001。

        Args:
            now: 当前时间戳。

        Returns:
            str: 报警编号。
        """
        datePart = time.strftime("%Y%m%d", time.localtime(now))
        dayStart = int(time.mktime(time.strptime(datePart, "%Y%m%d")))
        with self.lock:
            cursor = self.conn.execute(
                "SELECT COUNT(*) FROM alarms WHERE time_start >= ?", (dayStart,))
            count = cursor.fetchone()[0]
        return "A%s%03d" % (datePart, count + 1)

    def addAlarm(self, alarm):
        """插入一条新报警记录（status=active）。

        Args:
            alarm: dict，含 type / device_id / level / time_start /
                   value_at_trigger / threshold / message。

        Returns:
            str: alarm_id，供恢复时 closeAlarm 使用。
        """
        alarmId = self.nextAlarmId(alarm["time_start"])
        with self.lock, self.conn:
            self.conn.execute(
                """INSERT INTO alarms(alarm_id, type, device_id, level, status,
                       time_start, time_end, value_at_trigger, threshold, message)
                   VALUES (?, ?, ?, ?, 'active', ?, NULL, ?, ?, ?)""",
                (alarmId, alarm["type"], alarm["device_id"], alarm["level"],
                 alarm["time_start"], alarm["value_at_trigger"],
                 alarm["threshold"], alarm["message"]))
        return alarmId

    def closeAlarm(self, alarmId, timeEnd):
        """把报警记录标记为已恢复。

        Args:
            alarmId: addAlarm 返回的编号。
            timeEnd: 恢复时间戳。

        Returns:
            None
        """
        with self.lock, self.conn:
            self.conn.execute(
                "UPDATE alarms SET status = 'recovered', time_end = ? WHERE alarm_id = ?",
                (timeEnd, alarmId))

    def closeActiveAlarms(self, now, note=""):
        """把库里所有还挂着 active 的报警闭合（服务启动时清理遗留状态）。

        后端重启后报警引擎状态归零，数据库里遗留的 active 记录会与
        实时状态不一致，启动时统一标记为 recovered 并注明原因。

        Args:
            now: 闭合时间戳。
            note: 追加到 message 末尾的说明，如「服务重启」。

        Returns:
            int: 闭合的条数。
        """
        with self.lock, self.conn:
            rows = self.conn.execute(
                "SELECT alarm_id, message FROM alarms WHERE status = 'active'").fetchall()
            for alarmId, message in rows:
                newMessage = "%s（%s）" % (message, note) if note else message
                self.conn.execute(
                    "UPDATE alarms SET status = 'recovered', time_end = ?, message = ?"
                    " WHERE alarm_id = ?", (now, newMessage, alarmId))
        return len(rows)

    def queryAlarms(self, start=None, end=None, deviceId=None, status=None,
                    page=1, pageSize=20):
        """按条件分页查询报警记录（按触发时间倒序）。

        Args:
            start: 起始时间戳（可选）。
            end: 结束时间戳（可选）。
            deviceId: 按设备筛选（可选）。
            status: active / recovered（可选）。
            page: 页码，从 1 开始。
            pageSize: 每页条数，调用方保证 ≤ 100。

        Returns:
            tuple[int, list[dict]]: (符合条件的总条数, 本页记录列表)。
        """
        conditions = []
        params = []
        if start is not None:
            conditions.append("time_start >= ?")
            params.append(start)
        if end is not None:
            conditions.append("time_start <= ?")
            params.append(end)
        if deviceId:
            conditions.append("device_id = ?")
            params.append(deviceId)
        if status in ("active", "recovered"):
            conditions.append("status = ?")
            params.append(status)
        where = ("WHERE " + " AND ".join(conditions)) if conditions else ""

        with self.lock:
            cursor = self.conn.execute(
                "SELECT COUNT(*) FROM alarms %s" % where, params)
            total = cursor.fetchone()[0]
            cursor = self.conn.execute(
                """SELECT alarm_id, type, device_id, level, status,
                          time_start, time_end, value_at_trigger, threshold, message
                   FROM alarms %s ORDER BY time_start DESC LIMIT ? OFFSET ?"""
                % where, params + [pageSize, (page - 1) * pageSize])
            rows = cursor.fetchall()

        items = [{
            "alarm_id": row[0], "type": row[1], "device_id": row[2],
            "level": row[3], "status": row[4], "time_start": row[5],
            "time_end": row[6], "value_at_trigger": row[7],
            "threshold": row[8], "message": row[9],
        } for row in rows]
        return total, items

    # ---------------------------------------------------------------- 操作日志

    def addOpLog(self, ts, deviceId, command, value, operator, code, msg):
        """记录一条控制指令的操作日志（PRD 1.3：记录操作人与时间戳）。

        无论指令成功还是被拒绝都记录，便于验收时按错误码追溯。

        Args:
            ts: 时间戳。
            deviceId: 设备编号。
            command: 指令名。
            value: 数值（可空）。
            operator: 操作人。
            code: 结果码，0 表示成功。
            msg: 附加说明。

        Returns:
            None
        """
        with self.lock, self.conn:
            self.conn.execute(
                """INSERT INTO op_logs(ts, device_id, command, value, operator, code, msg)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (ts, deviceId, command, value, operator, code, msg))

    def close(self):
        """关闭数据库连接（进程退出时调用）。"""
        with self.lock:
            self.conn.close()
