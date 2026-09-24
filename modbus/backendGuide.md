# 给后端同学：接入说明

**一句话：你不需要懂 Modbus，只要调 4 个函数。**

模拟器和协议解析都已经写好、实测通过了。你的活是把数据加工后，通过 REST + WebSocket 提供给前端。

---

## 一、先把环境跑起来（3 步）

```bash
# 1. 装依赖（只装一个包，版本已锁死）
pip install -r requirements.txt

# 2. 窗口 1：启动模拟器（造数据的，别关这个窗口）
python sensorSim.py

# 3. 窗口 2：跑后端示例，看它长什么样
python exampleApi.py
```

然后浏览器打开 **http://127.0.0.1:8000/docs** —— FastAPI 自带的接口文档，可以直接在页面上点着测。

> `exampleApi.py` 就是给你抄的参考实现。运行它能看到完整效果；你自己写后端时照着改就行。

---

## 二、你能用的 4 个函数

从 `protocolParser` 导入即可（**不需要 pip 装 Modbus 相关的任何东西**，那些都在模块内部处理掉了）：

| 函数 | 作用 | 返回 |
|---|---|---|
| `connect(config)` | 连上模拟器 | 客户端对象 |
| `loadConfig(path)` | 读点表 | 配置字典 |
| `readAllTags(client, config)` | **读全部测点并翻译成标签名** | `{"tank_level": 85.5, "pipe_pressure": 0.42, ...}` |
| `buildPayload(tags)` | 包装成推送格式 | `{"timestamp": 1790225571, "data": {...}}` |
| `writeCoil(client, config, tag, True)` | 写线圈（启停） | 成功返回 `True` |
| `writeSetpoint(client, config, 0, 25.0)` | 写频率给定值（调速） | 成功返回 `True` |

**核心就一个**：`readAllTags()` 已经把「地址 → 标签名 → 缩放换算」全做完了，你拿到的就是真实工程值（液位 85.5 而不是寄存器里的 855）。

### 最简单的用法

```python
from protocolParser import connect, loadConfig, readAllTags

config = loadConfig("pointTable.json")
client = connect(config)

tags = readAllTags(client, config)
print(tags["tank_level"])      # 85.5   （cm）
print(tags["pipe_pressure"])   # 0.42   （MPa）
```

### ⚠️ 一个重要细节

`readAllTags` 是**同步阻塞**的。在 FastAPI 里直接调会把事件循环卡住，导致 WebSocket 推送时 HTTP 接口没响应。正确写法是用线程池：

```python
tags = await asyncio.to_thread(readAllTags, client, config)
```

`exampleApi.py` 里全是这么写的，照抄即可。

---

## 三、后端**必须**自己做的两件事

这两件协议解析不管，是你的职责。

### 1. 计算报警标志位（前端不判阈值）

《接口规范》3.3 节明确：**报警由后端判定后推送标志位，前端只负责显示**。原因是如果前后端各判一套，状态会不一致。

阈值在 `pointTable.json` 的 `alarm` 段里：

| 标志位 | 触发条件 |
|---|---|
| `alarm_level_low` | `tank_level < 25` |
| `alarm_level_high` | `tank_level > 140` |
| `alarm_pressure_high` | `pipe_pressure > 0.8` |
| `alarm_active` | 上面三个相加（0 表示无报警） |

参考实现见 `exampleApi.py` 的 `evaluateAlarms()`。

### 2. 自动模式下拒绝手动指令

PRD 非功能需求要求：自动模式下不仅要**前端置灰按钮**，**后端也必须拦一道**——否则谁用工具直接调接口就能绕过。

```python
if tags.get("sys_mode") == 1 and command != "set_mode":
    return {"ok": False, "code": 1006, "message": "当前为自动模式，不接受手动指令"}
```

**这条是 P0 验收项**，验收时会绕过前端直接调接口测。参考实现见 `exampleApi.py`。

---

## 四、控制指令的字段名（别写错）

以《排班表》第七节的统一字段命名表为准：

```json
POST /api/v1/control
{
  "device_id": "pump_01",
  "command": "start",
  "value": 0,
  "operator": "张三"
}
```

| 字段 | 取值 |
|---|---|
| `device_id` | `pump_01` / `pump_02` / `sys` |
| `command` | `start` / `stop` / `set_freq` / `set_mode` |
| `value` | `set_freq` 时为频率 0~50；`set_mode` 时为 0 或 1 |

> ⚠️ 是 `device_id` 不是 `device`，是 `command` 不是 `action`。这两处文档里原本有冲突，已统一成上表。

### 错误码

| 码 | 含义 |
|---|---|
| 1001 | 读不到模拟器数据，指令未下发 |
| 1002 | device_id 不合法 |
| 1003 | command 不合法 |
| 1004 | 写入模拟器失败 |
| 1006 | 自动模式下拒绝手动指令 |

---

## 五、WebSocket 推送

- 地址：`ws://127.0.0.1:8000/ws/realtime`
- 周期：**500ms**，每次推全量（不是增量），前端不用维护合并逻辑
- 心跳：每 10 秒一条 `{"type":"ping","timestamp":<秒>}`，前端回 `{"type":"pong"}`

消息形状（就是 `buildPayload()` 的返回值 + 报警标志位）：

```json
{
  "type": "data",
  "timestamp": 1790225571,
  "data": {
    "tank_level": 85.5, "pipe_pressure": 0.42, "today_kwh": 12.729,
    "pump_01_status": 1, "pump_01_freq": 25.0, "pump_01_current": 7.5, "pump_01_temp": 44.8,
    "pump_02_status": 0, "pump_02_freq": 0.0, "pump_02_current": 0.0, "pump_02_temp": 26.0,
    "sys_mode": 0, "valve_inlet_opening": 100.0, "pipe_flow_speed": 0.5,
    "alarm_level_low": 0, "alarm_level_high": 0, "alarm_pressure_high": 0, "alarm_active": 0
  }
}
```

**注意类型**：`pump_01_status`、`sys_mode` 和 `alarm_*` 系列是**整数**，其余是浮点。前端如果写 `status === 1` 这种判断，类型必须对上。

---

## 六、常见问题

| 现象 | 原因 |
|---|---|
| `ImportError: cannot import name 'ModbusSlaveContext'` | pymodbus 装成新版了。`pip uninstall pymodbus -y` 再 `pip install -r requirements.txt` |
| `readAllTags` 返回 `None` | 模拟器没启动，或端口不一致（两边都用 `pointTable.json`，改端口改一处就行） |
| 下发指令后状态没立刻变 | 正常。线圈写完要等模拟器下一拍（200ms）才更新寄存器 |
| 下发启停没反应 | 检查是不是在**自动模式**下——自动模式会拒绝手动指令（错误码 1006） |
