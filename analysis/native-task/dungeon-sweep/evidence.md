# 证据与结论等级

## 1. `verified-static`

| 主张 | 证据位置 |
| --- | --- |
| `Dun_sweep` 消息号为 19642，`Dun_start_draw` 为 19604，`Dun_treasure_info` 为 19626 | `decrypted-js/main.js:7800-7801`；生成枚举约字节偏移 3869800-3870900 |
| 扫荡请求只有 `dunid`，响应只有 `ret` | `dungeon_sweep.py:249-264`；生成 codec `oh/sh` 约字节偏移 1500877/1501421 |
| 扫荡 `ret=3` 的原生文案为“无扫荡次数” | `../native_app/decrypted-task-data/zh-Hans.json:2311-2318` |
| 批量抽取请求字段为 `dunid`、`all`；响应返回次数、掉落 ID、概率和 `all` | `dungeon_sweep.py:267-315`；生成 codec `bh/kh` 约字节偏移 1512064/1512766 |
| `DungeonData` 的关键字段及其 field number | `$u` codec，`decrypted-js/main.js:2941`，约字节偏移 1481938；字段映射见 `message-map.md` |
| 原生扫荡入口要求已通关、未达挑战上限、消耗品充足和装备容量可用 | `decrypted-js/main.js:321330-323397`（第 622-624 行） |
| 原生 UI 成功扫荡后只发成功事件和提示；宝库查询由 `TryDrawTreasure` 以次数条件触发 | `decrypted-js/main.js:384`，约字节偏移 196399；`main.js:384` 约字节偏移 197687 |
| `DUN_DRAW` item-change source 为 71，Storage 模块将其发出 `dungeon_draw_itemchange` | `decrypted-js/main.js:178`、`main.js:1484` |
| 任务 107 对应 `questid=50601`、日活分 15 | `../native_app/decrypted-data/tables/daily_quest.json`，对象 `id=107` |
| 日常任务领取和日活奖励领取按两阶段、三次状态查询闭环 | `daily_quest.py:691-776` |
| 日常动作只发送三次扫荡，不发送宝库抽取 | `daily_actions.py:559-611` |
| 操作台独立地下城任务在扫荡成功后继续全部抽取 | `app/services/dungeon_service.py:226-300` |

## 2. `verified-runtime`

运行时来源：`logs/websocket_raw/dungeon_sweep/*.jsonl`，覆盖 2026-07-23、24、26、27、28、29 和 2026-08-03；统计脚本按 `message_payload_base64` 解码后只保留字段级摘要。

| 主张 | 统计结果 | 证据范围 |
| --- | --- | --- |
| 扫荡成功/失败分支均真实出现 | 104 出站 + 104 入站；入站 `ret=0` 52 次、`ret=4` 52 次 | 所有 `dungeon_sweep` 日志中 message_id 19642 |
| 成功扫荡后直连批量抽取 | 52 个成功会话出现 `19642 -> 19604` | 每个含 19604 的 session；序列通常为 5/6/7/8 |
| `all=true` 是实际出站字段 | 52 个 19604 出站请求均为 `dunid=2302, all=true` | 19604 出站 payload 字段解码 |
| 批量抽取结果成功且耗尽 | 52 个入站响应均 `ret=0, all=true, drawtimes=3, totaltimes=3` | 19604 入站 payload 字段解码 |
| 奖励通过异步库存通知落袋 | 104 条 12602 通知均 source 71；每条 1 item + 1 prop | 同一 52 个成功 session，序列 9/10 |
| 样本奖励种类 | item 9001 与 prop kind=1 各 52；item 9002 与 prop kind=2 各 52 | 12602 payload 字段解码 |
| 日常状态中曾出现任务 107 | 可识别状态中 37 次 `finished=true, score_claimed=false`，2 次 `true,true`，1 次 `false,false` | `logs/websocket_raw/daily_quest/*.jsonl` 的 19700 入站 |
| 日常领取协议可运行 | 19702 与 19704 均有成对请求/响应，样本响应均为 `ret=0`（除 1 条聚合响应/1 条 ret=5 异常） | 同目录日常日志 |

## 3. `inferred`

1. `Dun_start_draw(all=true)` 的业务语义是“把当前可用次数一次性抽完”。字段名、实现命名和 52 次响应的 `3/3` 支持该解释，但原生宝库面板源码发送点未在当前提取集落地。
2. `challengeTimes`、`drawtimes/totaltimes` 在日常操作中按周期重置。代码和 UI 文案按“今日”使用它们，但现有材料没有给出服务器时区或重置事件的明确时间点。
3. `Dun_point_update_notify(19618)` 很可能是扫荡后异步同步总宝库次数的推送；静态处理器存在且无客户端发送点，但当前扫荡样本未出现该消息。

## 4. `unknown`

- `Dun_sweep ret=4` 的确切业务原因。
- `Dun_treasure_info` 与 `Dun_start_draw` 之间所有 UI 弹窗选择/默认选项的完整代码。
- 奖励概率 `prob[]` 的单位、权重归一化方式，以及 `dropids[]` 是否可能包含重复项。
- 日常任务 107 的直接 `19702` 领取帧（当前日常日志未出现明确 `id=107` 的出站请求）。
- 日常/地下城的服务器重置时区、跨日边界和重连恢复策略。
