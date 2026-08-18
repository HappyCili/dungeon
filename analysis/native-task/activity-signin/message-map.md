# 活动签到：消息映射

| 消息 | 方向/时机 | 载荷 | 当前处理 |
| --- | --- | --- | --- |
| `10490 Game_data` | 登录后服务端推送 | `signinData` 在字段 `29` | `GameSession.game_data` 保存原始快照，自动任务从此解析。 |
| `21000 Activity_do_signin` | C2S 领取、S2C 结果 | C2S：`id` 字段 `1`；S2C：`id` #1、`act` #2、`ret` #4、`msg` #5 | 对每个可领取活动发送一次，并等待同消息号结果。 |
| `21001 Activity_signin_remedy` | 补签 | `id` 字段 `1` | 不属于自动签到，不发送。 |
| `21002 Activity_signin_sync` | 每日重置定时同步 | C2S `id` #1；S2C `id` 与 `act` | 原生仅在 `dailyResetRemainSecs` 到期后发送；本任务不把它作为领取前置。 |
| `21003 Activity_signin_sync_all` | 原生入站全量状态处理器 | 全量 `signinData` | 当前抓包中 C2S 发送后无匹配回包，自动任务不再发送或等待它。 |

`Game_data.signinData` 的结构：外层字段 `29`，其中 `acts` 为字段 `1` 的重复消息。每个活动的 `id` 是字段 `1`，`signinData` 是字段 `3`，`ticket` 是字段 `5`；嵌套 `todaySigned` 是字段 `3`，`ticket.status` 是字段 `2`。
