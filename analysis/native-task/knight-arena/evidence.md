# 证据记录

## 证据等级

| 标记 | 定义 |
| --- | --- |
| 静态已验证 | 从本地原生解密脚本直接读取到的消息注册、发送点、codec 或状态更新。 |
| 运行时已验证 | 从本地 JSONL 帧解码得到的实际收发与字段值。 |
| 项目实现已验证 | 当前 Python 实现和对应测试明确采用的行为；不自动提升为原生服务端语义。 |
| 未知 | 本地资产没有足够证据，需补采或补资源。 |

## 静态已验证

| 编号 | 结论 | 来源 |
| --- | --- | --- |
| S1 | `ArenaModule` 注册 `Arena_info`、每日奖励、挑战、对手队伍、挑战结算、挑战次数奖励和积分变更；`OnArenaInfo` 使用 `seasonopen`/`getdailyreward` 设每日奖励红点，以 `challengenum==0` 设挑战红点。 | `decrypted-js/main.dragon-arena.js:1225-1235` |
| S2 | `ArenaInfo`、刷新、挑战、每日奖励、对手队伍、挑战结算和挑战次数奖励的字段号。 | `decrypted-js/main.dragon-arena.js:3252-3290` |
| S3 | `Arena_*` ID 是 `19800` 至 `19824`；共用战斗 ID 包含 `18002/18010/18012/18090`。 | `decrypted-js/main.dragon-arena.js:7801`；`dragon_arena_business_map.py:22-59` |
| S4 | 队伍界面 `OnArenaSetTeam` 在设置完成后发送 `Arena_challenge`。 | `decrypted-js/main.dragon-arena.js:6184` |
| S5 | `OnArenaChallengeResult` 更新赛季积分、最高分；普通匹配胜利时写入 `mdatas[opponentid].win=1`。 | `decrypted-js/main.dragon-arena.js:1234` |
| S6 | `OnArenaGetDailyreward` 直接设置 `getdailyreward=true`、保存 `rankid` 并关红点；可见代码没有 `ret` 分支。 | `decrypted-js/main.dragon-arena.js:1233-1234` |
| S7 | `TryQueryArenaInfo` 在缓存为空或 `dailylt/seasonlt` 到期时发送 `Arena_info`。 | `decrypted-js/main.dragon-arena.js:1234` |

## 运行时已验证

来源：`logs/websocket_raw/knight_arena/2026-07-24.jsonl`。精简索引见 [packet-trace.jsonl](packet-trace.jsonl)。

| 编号 | 帧 | 解码结果 | 结论 |
| --- | --- | --- | --- |
| R1 | 14、27，入站 `19800` | `season_id=1255`、`season_score=223`、`season_open=true`、`challenge_num=0`、`refresh_num=0`、`season_challenge_num=5`、3 个候选且均未胜 | 状态读取、默认零值语义和候选列表已被实际观察。 |
| R1 | 14、27，入站 `19800` | 字段 #7 缺失，codec 默认 `getdailyreward=false`；#5 `dailylt` 约 13610 秒、#6 `seasonlt` 约 251210 秒 | 该快照显示每日奖励尚未标记为已领取，但没有领取操作样本。 |
| R2 | 22，出站 `19810` | 请求只含字段 #3 值 `2` | 普通匹配可用候选下标作为 `opponentid`。 |
| R2 | 28，入站 `18002` | `battle_id=101002`、`ret=0`、`battle_type=5`、敌方单位数 6 | 已看到普通竞技场备战包。 |
| R2 | 29，出站 `18010` | 130 字节战斗启动载荷 | 收到备战包后确实有开始战斗请求。 |
| R2 | 30，入站 `18012` | `battle_id=101002`、`ret=7` | 本次握手被拒绝；未进入可证明的成功战斗。 |
| R3 | 32、34、38、40、42、46、48、50、54、56、58，入站 `19810` | 均为 `ret=9`，部分带回候选下标 | 挑战拒绝路径存在；具体错误码含义未知。 |
| R4 | 36、44、52，入站 `19808` | `ret=0`，均有 3 个未胜候选；60 帧为 `ret=1`、空列表 | 刷新既有成功也有失败/空结果分支。 |

本抓包中没有 `19814`、`19816`、`19818`、`19822` 或 `18090`，因此不把每日奖励、对手队伍、挑战结算或挑战次数奖励描述为运行时已验证。

## 项目实现已验证

| 编号 | 结论 | 来源 |
| --- | --- | --- |
| P1 | 自动任务以已确认的每日免费上限 `KNIGHT_ARENA_DAILY_FREE_LIMIT=5` 执行一次 `run_daily_free_challenges`；初始 `challengenum` 决定预算，`refreshnum` 不参与计算，`19810.ret=0` 占用预算，未结算即停止。 | `app/services/auto_task_service.py:47-52`、`1133-1195`；`knight_arena.py:647-736` |
| P2 | 候选优先 `win==0`；`opponent_id` 对普通匹配是列表下标；候选耗尽可刷新。 | `knight_arena.py:205-227`、`340-351`、`609-693` |
| P3 | 收到 `Battle_info` 后仅发送一次 `18010`；`18012.ret==0` 才进入战斗中；收到 `19818` 即返回结算，`18090` 不作为前置。 | `knight_arena.py:484-570`；`dragon_arena.py:1766-1833` |
| P4 | 普通竞技场使用编队槽位 4；当前辅助实现发 `15016` 并等待 `15014` 同步后再挑战。 | `knight_arena.py:363-428`；`tests/test_treasure_area.py:397-514` |
| P5 | 日常任务 109 按挑战场次计数，不要求胜利。 | `daily_actions.py:614-670`；`tests/test_daily_actions.py:713-749` |
| P6 | 恢复逻辑将单独的 `Battle_info(type=5)` 视为备战而非运行中。 | `session_recovery.py:33-69`、`465-560` |
| P7 | 预算执行器测试覆盖“已接受但未结算即停止”和赛季关闭；服务层测试覆盖单次预算调用与赛季关闭结果。 | `tests/test_treasure_area.py:523-588`；`tests/test_auto_task_service.py:637-746` |
