# 活动签到：本地证据清单

| 证据 | 类型 | 状态 | 用途 |
| --- | --- | --- | --- |
| `decrypted-js/main.js` | 原生客户端静态代码 | 已核对 | 消息号、Protobuf 字段、活动状态更新和自动签到条件。 |
| `decrypted-js/signinPanel1New.js`、`signinPanel1.js`、`itemSigninBox.js` | 原生客户端静态代码 | 已核对 | 手动领取均发送 `{ id: signinId }` 到 `21000`。 |
| `logs/websocket_raw/game_session/2026-07-30.jsonl` | 当前 UI 真实会话抓包 | 已核对 | 登录收到 `10490 Game_data`；发送 `21003` 后没有对应的 `21003` 入站包。 |
| `app/services/auto_task_service.py` | 当前实现 | 已修正 | 自动任务改为从共享会话的 `game_data` 读取签到状态。 |
| `tests/test_auto_task_service.py` | 回归测试 | 已覆盖 | 验证状态解析、资格筛选与不发送 `21003`。 |

当前抓包中最新登录快照（会话 `1785381544834672000`、序列 `68`）包含活动 `301`、`302`、`501`、`1001`；其中 `301` 的 `ticket.status=1` 且未当日签到，`302`、`501` 的状态为 `2`，`1001` 已当日签到。活动 ID 和状态来自本地捕获的登录数据，仅用于验证字段映射。
