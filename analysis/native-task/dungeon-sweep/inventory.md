# 地下城扫荡与每日奖励取证清单

## 1. 分析范围

本目录覆盖两个容易被称为“地下城每日奖励”的不同闭环：

1. **地下城扫荡后的宝库抽取**：`Dun_sweep` 成功后，使用 `Dun_start_draw(all=true)` 抽取当次累计的宝库次数，并等待背包变更通知。
2. **日常任务 107 的日活奖励**：地下城操作推动任务 `50601`，任务完成后通过 `Dailyquest_get_questreward` 领取任务积分，再通过 `Dailyquest_get_scorereward` 领取达到阈值的日活宝箱。

“宝库抽取”不是“日常活跃奖励”；两者的消息号、状态字段和领取条件均不同。

## 2. 原始物清单

| 原始物 | 作用 | 证据等级 |
| --- | --- | --- |
| `dungeon_sweep.py` | 本地还原的游戏服会话、地下城状态解码、扫荡和批量抽取 | `verified-static` |
| `app/services/dungeon_service.py` | 操作台的“读取状态 -> 扫荡 -> 全部抽取”编排 | `verified-static` |
| `daily_actions.py` | 日常任务 107 的动作包装；固定发起 3 次扫荡 | `verified-static` |
| `daily_quest.py` | 日常/周常状态读取、任务积分领取和活跃奖励领取 | `verified-static` |
| `decrypted-js/main.js` | 原生客户端解密 JS；包含 DungeonModule、入口面板、protobuf 编解码和消息枚举 | `verified-static` |
| `../native_app/decrypted-data/tables/dungeon.json` | 地下城名称、挑战上限、消耗品、宝库展示和解锁条件 | `verified-static` |
| `../native_app/decrypted-data/tables/dungeon_reward.json` | 诅咒/宝库成就奖励配置；不是日活宝箱配置 | `verified-static` |
| `../native_app/decrypted-data/tables/daily_quest.json` | 日常任务配置；任务 107 对应 `questid=50601`、`scoreday=15` | `verified-static` |
| `../native_app/decrypted-task-data/activityreward.json` | 日常/周常活跃奖励阈值与奖励 ID | `verified-static` |
| `logs/websocket_raw/dungeon_sweep/*.jsonl` | 2026-07-23 至 2026-08-03 的原始 WebSocket 记录 | `verified-runtime` |
| `logs/websocket_raw/daily_quest/*.jsonl` | 日常状态与领取操作记录 | `verified-runtime`（仅部分任务） |
| `tests/test_dungeon_sweep.py` | 合成帧验证字段编码、顺序、奖励通知和 `ret` 分支 | `verified-static` |
| `tests/test_daily_actions.py` | 日常动作前后状态闭环和统一奖励领取验证 | `verified-static` |

## 2.1 复现指纹

以下 SHA-256 在 2026-08-03 对当前工作区只读计算。日志聚合值的输入是两个日志目录下按路径排序的全部 14 个 JSONL 文件的逐文件 SHA-256 输出。

| 输入 | SHA-256 |
| --- | --- |
| `dungeon_sweep.py` | `822ec61e057ebb043e71bfaa9c2c7190b03183363ff0f1f5d0b87959705d2349` |
| `app/services/dungeon_service.py` | `2d2b2cd626d1b76b50c0623315ae370bae7cb720c08c7dc04c4049bf2787cf69` |
| `daily_actions.py` | `4052aaa3ef95d952a5bac88dd9eeb36017cc40a84937a37a03974b5a0e55ae6a` |
| `daily_quest.py` | `98f99ce41c31620a88fc948221a6f3c798e724ed369b90b143408cde15d7a5b6` |
| `decrypted-js/main.js` | `f3fae395546cf3617b0e9f571f3afc9597d0bae774f7300c7c1084286f0bb58f` |
| `../native_app/decrypted-data/tables/dungeon.json` | `cf42baba6ffb89abfe93d4ac74fdc6bc69eb17859c077812717b2d4175da4cdc` |
| `../native_app/decrypted-data/tables/daily_quest.json` | `f55cb9a82f97e79e0859516a77e8b88f4ff7cc0610bbc3f105f2ff62330b8571` |
| `../native_app/decrypted-task-data/activityreward.json` | `f7495b704a1d5203961cfc710fc2451f4ef736dcb2f4a20225bf7555bd8f60c2` |
| `logs/websocket_raw/{dungeon_sweep,daily_quest}/*.jsonl` 聚合 | `5bca503ea6e2728182cfbe032151e9392891af7876ffa3e6ad1a48877bf1c567` |

## 3. 入口与前置状态

### 3.1 原生客户端入口

- `DungeonEnterPanel.OnClickWeep` 位于 `decrypted-js/main.js` 第 623 行（约字节偏移 323324）。
- 面板先执行 `CheckDungeonValid()`：列表非空且当前条目 `unlocked`。
- 然后执行 `CheckCanEnterDungeon()`：
  - `getChallangeTimes(dungeonId) < dungeon.limit`；
  - `storage.judgeLackItem(row.consume, ...)` 通过；
  - 装备背包有空位。
- 扫荡按钮只有在 `CheckCompleteDungeon(dungeonId)` 为真时显示。该判断读取 `Game_data.dungeon.compeleted[dungeonId]`（第 392 行，约字节偏移 204478）。
- 确认框显示历史最高分和按分数计算的宝库次数，然后发送 `{dunid}`。

### 3.2 本地操作台入口

- `DungeonService.run` 先读取 `Game_data.dungeon`，再做已解锁/可展示和次数上限校验。
- `run_dungeon_sweep_action`（`daily_actions.py:559-611`）用于日常 107，按历史最高分降序、ID 升序选择一个候选地下城，固定发送 3 次扫荡；它本身不发送宝库抽取请求。
- 操作台的独立地下城任务（`app/services/dungeon_service.py:200-308`）在一次扫荡成功后继续调用 `draw_all`，因此与日常动作不是同一编排。

## 4. 运行时样本概况

对 `logs/websocket_raw/dungeon_sweep/*.jsonl` 做字段级解码（不读取或输出令牌/密文）得到：

- 总记录：832 条。
- `Dun_sweep`：104 个出站请求、104 个入站响应；响应 `ret=0` 52 次、`ret=4` 52 次。`ret=4` 的业务含义尚未由本地资料命名，不能直接解释为某一具体原因。
- 成功样本的 52 个会话均出现：`Dun_sweep` 请求/响应 -> `Dun_start_draw` 请求/响应 -> 两条 `Storage_item_change_notify`。
- 52 个 `Dun_start_draw` 请求字段均为 `dunid=2302, all=true`；响应均为 `ret=0, all=true, drawtimes=3, totaltimes=3`。
- 104 条背包通知的 `source` 均为 71（原生枚举 `DUN_DRAW`），每条各含 1 个 item 和 1 个 prop；样本 ID 为 9001/9002，各出现 52 次。
- 可用样本中没有 `Dun_treasure_info` 帧；因此“原生面板先查询宝库信息”的发送点来自静态 JS，而不是本批运行时记录。

日常日志中共有 667 个 `Dailyquest_info` 入站响应。任务 107 在可识别的样本中出现过“未完成”“已完成未领积分”和“已完成已领积分”状态，但没有观察到一个明确的 `Dailyquest_get_questreward` 出站请求以 `id=107`，所以任务 107 的具体领取帧仍标为 `inferred/static-only`。

## 5. 结论等级定义

- `verified-static`：能在原生解密 JS、配置表、当前实现或测试中直接定位。
- `verified-runtime`：能在本地保存的 JSONL 原始帧中按字段解码复现。
- `inferred`：由多个静态证据推导，但没有同一操作窗口的直接帧或没有明确发送点。
- `unknown`：现有素材无法判定，文档不把它写成实现常量。
