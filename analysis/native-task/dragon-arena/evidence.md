# 证据

- `logs/dragon_arena/2026-07-29.jsonl:1`：UI 摘要记为 0/100、积分 40、剩余 14/15。
- `logs/websocket_raw/game_session/2026-07-29.jsonl`，会话 `1785256999646589000`，序号 650-653：本次只含 `21100` 请求/响应和 `21104` 请求/响应；末帧为 00:49:08.597 的 `21104`。
- 同一末帧的已解析业务载荷：`ret=2,index=6,evaluation=1,quick=false`。
- `decrypted-js/main.dragon-arena.js:8157`：原生 `OnArenaChallenge` 在 `ret != 0` 时发送 `Scararena_info`。
- `decrypted-js/main.dragon-arena.js:7778-7780`：原生普通战斗包含帧哈希处理；本次没有进入该阶段。
- `logs/websocket_raw/game_session/2026-07-26.jsonl`，会话 `1785057769687109000`，序列 712-749：连续 `ret=2`（序号 7、12、9、10、8、13、15、14、3）后，序号 2 返回 `ret=0` 并进入 `18002`。
- `logs/websocket_raw/game_session/2026-07-27.jsonl`，会话 `1785145204481016000`：同样存在多个拒绝候选后成功进入战斗的序列。
- `logs/websocket_raw/game_session/2026-07-29.jsonl`，会话 `1785285782130806000`，序列 493-506：候选 6、12 被拒后，`21102` 返回 `ret=2` 且候选数为 0；这与挑战拒绝是不同阶段的服务端返回。
- `logs/websocket_raw/game_session/2026-07-29.jsonl`，会话 `1785291678035595000`，序列 188-189：10:22:46.417 发送 `21104` 载荷 `08 0a`（第 10 位），10:22:46.464 返回 `ret=2,index=10,evaluation=1`，没有对手字段或战斗帧。随后序列 194-195 的第 1 位返回 `ret=0` 并带 3056 字节对手数据。
- 同文件会话 `1785293786984783000`：10:56:34.283 第 10 位返回 `ret=2,index=10,evaluation=1`；随后第 11 位也返回 `ret=2`；10:56:34.572 请求第 1 位，10:56:34.684 的 `21104` 返回 `index=1` 和对手数据，10:56:34.706 才收到 `18002`，并在 10:56:34.802 发送 `18010`。进入战斗归属第 1 位成功请求，而非第 10 位拒绝请求。
- 该会话在匹配成功和每次 `ret=2` 后均有两次连续 `21100` 读取（29/31、35/37、41/43）；第二次不改变候选，属于客户端服务层的冗余状态读取。
- 同一会话序列 167-191 中，`21100` 一直将第 1-15 位标为 `challenge=false`；第 11、3、13、10 位仍连续返回 `ret=2`。该标记不是服务端对本次挑战资格的保证。
- `decrypted-js/main.dragon-arena.js:8157`：原生仅在 `Scararena_challenge_result.win` 为真时，将 `mdata[index - 1].challenge` 置为真；会话进度也按该字段计数。因此它表示胜利通过状态，而非即时可挑战状态。
- `decrypted-js/main.dragon-arena.js:8813`：本地原生运行器将候选轮次重试间隔设为 750 ms；服务层采用同一间隔，且允许作业停止按钮中断等待。

结论：`21104 ret=2` 是候选级服务端拒绝；客户端可验证请求序号和回包序号一致，但本地包未给出精确业务错误名称。一次后续成功挑战可能紧随拒绝请求进入战斗，必须按成功 `21104` 与后续 `18002` 归属，不能将战斗误关联到先前的拒绝序号。`challenge=false` 只能用于排除已胜利通过的对手，不能推导该位置本次必定可进入战斗。刷新后的候选位置也不能作为去重键，服务层必须以 `robot_id` 关联重试状态。

补充（2026-07-30）：自动任务原实现仅发送 `21100` 与 `21110`，并把 `liked_count` 固定为 `0`，因此不会执行排行榜点赞。原生 `RankModule` 使用 `12910 CLeaderboardGetList` 查询 `LEADERBOARD_KIND_SCARARENA=6`，并用 `12912 CLeaderboardLike` 点赞；`DragonArenaModule.CheckCanLikeit` 明确排除本人、`likenum` 已存在 UID 和已达上限的情况。原生 `macros.json` 的 `ma_arenascar_like_limit` 为 `10`。

`Scararena_info.GetDailyreward` 是字段 #17：原生 `OnArenaGetDailyReward` 在 `21110.ret == 0` 时将它写为 `false`，所以 `true` 表示当前可领取，而不是已领取。`CLeaderboardGetList` 的响应 `rankkind` 是字段 #9；只有值为 `6` 时才可将该响应用于龙痕点赞。证据：`decrypted-js/main.dragon-arena.js:3721-3726,4697-4698,8157`。
