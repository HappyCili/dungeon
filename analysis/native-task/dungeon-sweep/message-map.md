# 消息映射

> 约定：`C -> S` 表示客户端发送，`S -> C` 表示服务端响应/推送。字段号为 protobuf wire field number；消息体外层仍经过项目通用的 WebSocket Pack1 封装。

## 1. 地下城扫荡与宝库

| 方向 | ID/名称 | 编码类型 | 发起/处理函数 | 字段 | 触发条件 | 证据 |
| --- | --- | --- | --- | --- | --- | --- |
| `S -> C` | `10490 / Game_data` | `GameData` | 登录分发；`dungeon_sweep.py:433-481` | 外层 field 18 = `dungeon` | 登录完成后下发初始状态 | `decrypted-js/main.js:376`；`dungeon_sweep.py:240-246` |
| `C -> S` | `19642 / Dun_sweep` | `DunSweepReq` | `DungeonEnterPanel.OnClickWeep`；`DungeonSweepClient.sweep` | field 1 `dunid:int32` | 已解锁、已通关、次数未达上限、消耗品和容量检查通过 | `decrypted-js/main.js:623`；`dungeon_sweep.py:249-256` |
| `S -> C` | `19642 / Dun_sweep` | `DunSweepRet` | `DungeonModule.OnDunSweep`；`decode_dungeon_sweep_response` | field 1 `ret:int32` | 对应扫荡请求的同步响应 | `decrypted-js/main.js:384`；`dungeon_sweep.py:259-264` |
| `S -> C` | `19618 / Dun_point_update_notify` | `DunPointUpdateNotify` | `DungeonModule.OnPointUpdateNotify` | `dunid`、`bestdunid`、`bestpoint`、`point`、`todaypoint`、`totaltimes`（字段编码来自生成协议） | 可能在扫荡/结算后异步同步状态 | 静态处理器 `decrypted-js/main.js:384`；无本地客户端发送点，归类为推送候选 |
| `C -> S` | `19626 / Dun_treasure_info` | `DunTreasureReq` | `DungeonModule.TryDrawTreasure` / `DungenInfoItem.OnClickTreasury` | field 1 `id:int32` | `dunid>0` 且 `drawtimes < totaltimes`；原生 UI 先请求宝库信息 | `decrypted-js/main.js:384,607`；`Oh` codec，约字节偏移 1529001 |
| `S -> C` | `19626 / Dun_treasure_info` | `DunTreasureInfo` | `DungeonModule.OnTreasureInfo` | field 1 `id`、field 2 `ret`、field 3 `prob[]`、field 4 `timesleft{}`、field 5 `replace{}`、field 6 `treasureid` | 宝库信息查询响应 | `Bh` codec，约字节偏移 1529521；处理器在 `decrypted-js/main.js:384` |
| `C -> S` | `19604 / Dun_start_draw` | `DunStartDrawReq` | 宝库面板（原生面板源码未在当前提取集落地）；操作台 `DungeonSweepClient.draw_all` | field 1 `dunid:int32`；field 2 `all:bool` | 有可用抽取次数；操作台固定 `all=true` | `bh` codec，约字节偏移 1512064；`dungeon_sweep.py:267-275`；运行时 52 帧 |
| `S -> C` | `19604 / Dun_start_draw` | `DunStartDrawRet` | `DungeonModule.OnStartDraw`；`decode_dungeon_draw_response` | field 1 `ret`；2 `dunid`；3 `drawtimes`；4 `totaltimes`；5 `dropids[]`；6 `prob[]`；7 `all` | 抽取请求同步响应；`ret=0` 才更新次数 | `kh` codec，约字节偏移 1512766；`decrypted-js/main.js:380`；`dungeon_sweep.py:277-315` |
| `S -> C` | `12602 / Storage_item_change_notify` | `ItemChangeNotify` | `StorageModule` 分发；`DungeonSweepClient.draw_all` 收集 | `source=71 (DUN_DRAW)`；items；props | 宝库结算落袋的异步通知，可能一条或多条 | `decrypted-js/main.js:178`、枚举 `main.js:1484`；`dungeon_sweep.py:561-572`；运行时 104 帧 |

### 1.1 `Game_data.dungeon` 关键字段

原生生成的 `DungeonData`（`$u`，`main.js` 约字节偏移 1481938）包含：

| field | 原生字段 | 本地投影 | 作用 |
| --- | --- | --- | --- |
| 1 | `unlocks[]` | `unlocked_ids` | 已解锁地下城 |
| 5 | `dunid` | `current_dungeon_id` | 当前地下城 |
| 7 | `drawtimes` | `draw_times` | 已抽取次数 |
| 21 | `totaltimes` | `total_draw_times` | 当前累计可抽次数 |
| 29 | `challengeTimes{dungeon_id:int32}` | `challenge_times` | 每个地下城挑战次数 |
| 30 | `bests{dungeon_id:int64}` | `best_scores` | 每个地下城历史最高分 |
| 31 | `treasuryTimes{dungeon_id -> [cur,total]}` | 未投影 | 原生宝库分地下城次数显示 |
| 33 | `boxids[]` | 未投影 | 已领取的地下城箱/成就标记 |
| 34 | `sweeps{dungeon_id -> {times, ts}}` | 未投影 | 原生扫荡记录 |
| 35 | `showUnlocks[]` | `visible_ids` | 当前面板展示列表 |
| 11 | `compeleted{dungeon_id:bool}` | 未投影 | 是否完成过该地下城；决定原生扫荡按钮是否显示 |

当前 Python 解码器只投影表中标注为必要的字段；`compeleted`、`treasuryTimes`、`sweeps` 未进入 `DungeonStatus`。现有运行时样本没有 field 34，且原生 `OnClickWeep` 未以该字段作前置检查，因此不把它臆测为 `ret=3` 的客户端预检依据。

## 2. 日常任务与奖励

| 方向 | ID/名称 | 编码类型 | 发起/处理函数 | 字段 | 触发条件 | 证据 |
| --- | --- | --- | --- | --- | --- | --- |
| `S -> C` | `10490 / Game_data` | `GameData` | `DailyQuestClient.login` | field 19 = `dailyquest`；field 12 = quest progress | 登录初始化 | `daily_quest.py:396-409` |
| `C -> S` | `19700 / Dailyquest_info` | 空请求 | `DailyQuestClient.get_status` | 无业务请求字段 | 登录后主动刷新日常/周常状态 | `daily_quest.py:640-655`；枚举 `main.js:7801` |
| `S -> C` | `19700 / Dailyquest_info` | `DailyQuestInfo` | `QuestModule.OnDailyquestInfo` | `dailylt`、`dailyrt`、`dailyrewards[]`、`weeklylt`、`weeklyrt`、`weeklyrewards[]`、`datas[]` | 主动查询响应或刷新推送 | `daily_quest.py:283-326`；`main.js:5608` |
| `C -> S` | `19702 / Dailyquest_get_questreward` | `DailyQuestRewardReq` | `claim_task_reward` | field 1 `id`（日常 ID）；field 2 `group`（1 日常/2 周常） | `finished=true` 且 `score_claimed=false` | `daily_quest.py:657-672`；`Hh`/`Wh` codec |
| `S -> C` | `19702 / Dailyquest_get_questreward` | `DailyQuestRewardRet` | `QuestModule.OnDailyquestGetQuestReward` | `ret`、`id`、`group`、`ids[]` | 任务积分领取响应 | `main.js:5608`；`Wh` codec，约字节偏移 1539362 |
| `C -> S` | `19704 / Dailyquest_get_scorereward` | `DailyScoreRewardReq` | `claim_score_reward` | field 1 `group`；field 2 `id`（活跃奖励 ID） | 奖励阈值 `score <= activity_score` 且未在已领列表 | `daily_quest.py:674-689`；`qh` codec |
| `S -> C` | `19704 / Dailyquest_get_scorereward` | `DailyScoreRewardRet` | `QuestModule.OnDailyquestGetScoreReward` | `ret`、`group`、`id`、`rewardids[]` | 活跃奖励领取响应 | `main.js:5608`；`Kh` codec，约字节偏移 1541362 |

## 3. 任务 107 的本地配置

`../native_app/decrypted-data/tables/daily_quest.json` 中：

```text
id=107, groupId=1, questid=50601, scoreday=15, scoreweek=0
```

因此地下城动作只负责推动 `50601` 的服务端进度；任务积分和日活宝箱仍由日常协议单独领取。
