# 消息映射

| 方向 | ID/名称 | 触发/处理 | 2026-07-29 观察 |
| --- | --- | --- | --- |
| C -> S | `21100 Scararena_info` | 挑战前读取候选 | 已发送并成功返回 |
| C -> S | `21104 Scararena_challenge` | 挑战候选序号 | 00:49:08.568 发送 |
| S -> C | `21104 Scararena_challenge` | 挑战响应 | 00:49:08.597 返回 `ret=2,index=6,evaluation=1,quick=false` |
| S -> C | `18002 Battle_info` | 普通战斗握手 | not-observed |
| S -> C | `21106 Scararena_challenge_result` | 竞技场战斗结算 | not-observed in failed run |
| C -> S / S -> C | `21110 Scararena_get_dailyreward` | 领取龙痕每日奖励 | 仅当 `Scararena_info.GetDailyreward` 字段 #17 为 true 时发送空载荷；响应 `ret` 在字段 #1。 |
| C -> S / S -> C | `12910 CLeaderboardGetList` | 请求龙痕排行榜首屏 | 请求字段 `kind` #4，龙痕值为 `6`；响应列表在字段 #4，`rankkind` 在字段 #9 且必须为 `6`。 |
| C -> S / S -> C | `12912 CLeaderboardLike` | 点赞排行榜成员 | 请求字段 `kind` #1、`uid` #3；响应含 `kind` #1、`uid` #3、`like` #4、`ret` #5。 |

原生 `DragonArenaModule.OnArenaChallenge` 对非零 `ret` 发送 `21100` 刷新状态；它不会把该响应当作战斗已开始。历史成功会话显示刷新后会继续选择其他未挑战序号。

原生点赞条件：`Scararena_info.likenum`（字段 #5 map）中没有该 UID、目标不等于 `Game_data.uid`（字段 #2），且已赞数量小于 `ma_arenascar_like_limit`。当前原生表 `macros.json` 的该上限为 `10`。

原生 `OnArenaGetDailyReward` 在成功后把 `GetDailyreward` 置为 false，因此该标记表示“可领取”而非“已领取”。
