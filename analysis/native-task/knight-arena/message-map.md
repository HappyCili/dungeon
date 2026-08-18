# 消息映射

同一消息 ID 用于请求与响应时，以方向区分。字段号来自 `decrypted-js/main.dragon-arena.js:3252-3290`；“未观测”表示本地抓包没有该方向的样本。

| 方向 | ID/名称 | 请求或响应字段 | 作用 | 证据 |
| --- | --- | --- | --- | --- |
| C -> S / S -> C | `19800 Arena_info` | 响应 `ArenaInfo`：#1 `challengenum`、#2 `refreshnum`、#3 `seasonid`、#4 `seasonscore`、#5 `dailylt`、#6 `seasonlt`、#7 `getdailyreward`、#8/#9 编队、#10 `mdatas`、#13 `bestscore`、#14 `seasonopen`、#15/#16 当前对手、#17 `seasonchallnum`、#18 `rewardids`、#19 `revengeids` | 进入/刷新普通竞技场状态 | 静态、运行时 |
| C -> S / S -> C | `19802 Arena_history` | 响应 #1 `recs[]` | 初始化防守/复仇历史 | 静态；请求未观测 |
| C -> S / S -> C | `19804 Arena_set_defendteam` | 请求 `team`；响应 #1 `ret`、#2 `team`、#3 `dteam` | 设置防守编队 | 静态；未观测 |
| C -> S / S -> C | `19806 Arena_set_attackteam` | 请求 `team`；响应 #1 `ret`、#2 `team` | 设置进攻编队的专用端点 | 静态；未观测 |
| C -> S / S -> C | `19808 Arena_refresh_opponents` | 请求 #1 `mode`；响应 #1 `ret`、#2 `mode`、#3 `refreshnum`、#4 `mdatas[]` | 刷新普通匹配候选 | 静态、运行时 |
| C -> S / S -> C | `19810 Arena_challenge` | 请求 #1 `mode`、#2 `opponenttype`、#3 `opponentid`、#4 `getteam`；响应 #1 `ret`、#2 `mode`、#3 `opponenttype`、#4 `opponentid`、#5 `challengenum`、#6 `getteam`、#7 `dteam`、#8 `seasonchallnum` | 提交挑战并得到次数/当前对手状态 | 静态、运行时 |
| C -> S / S -> C | `19814 Arena_get_dailyreward` | 已知响应 #1 `ret`、#2 `rankid`；请求 codec/载荷未在本地证据中定位 | 领取每日排名奖励 | 静态；未观测 |
| C -> S / S -> C | `19816 Arena_get_opponentteam` | 请求 #1 `opponenttype`、#2 `opponentid`；响应 #1 `ret`、#2 `opponenttype`、#3 `opponentid`、#4 `dteam` | 当缓存的敌方队伍需要更新时查询 | 静态；未观测 |
| S -> C | `19818 Arena_challenge_result` | #1 `win`、#2 `opponenttype`、#3 `opponentid`、#4 `seasonscore`、#5 `getscore`、#6 `bestscore` | 普通竞技场权威结算 | 静态；未观测 |
| S -> C | `19820 Arena_add_log` | `ArenaLog` | 追加进攻战报 | 静态；未观测 |
| C -> S / S -> C | `19822 Arena_get_challreward` | 请求 #1 `rewardid`；响应 #1 `ret`、#2 `rewardid`、#3 `rewardids[]` | 领取挑战次数阶段奖励 | 静态；未观测 |
| S -> C | `19824 Arena_changed_score` | #1 `seasonscore` | 推送积分变更 | 静态；未观测 |
| C -> S / S -> C | `15016 Hero_setteam` / `15014 Hero_teamsync` | 当前项目对槽位 4 的保存确认 | 当前辅助实现的进攻编队同步 | 项目实现、测试 |
| S -> C | `18002 Battle_info` | 包含 `ret`、`battle_id`、`battle_type`、敌方站位等 | 普通战斗备战数据 | 运行时 `battle_type=5` |
| C -> S | `18010 Battle_C2S_start` | 战斗 ID、我方站位、敌方站位、`BattleExtra` | 明确请求开始战斗 | 项目实现、运行时 |
| S -> C | `18012 Battle_S2C_start` | 含 `ret` | `ret=0` 后进入战斗中 | 项目实现、运行时失败样本 |
| S -> C | `18090 Battle_S2C_end` | 结束通知 | 战斗视觉/帧流程结束 | 项目实现 |
| S -> C | `18500` / `18502` | 战斗帧 / 哈希校验 | 战斗中持续消息 | 项目实现、恢复逻辑 |

## 对手字段与索引规则

`mdatas` 的单项 codec `Ef` 包含：#1 候选实体 ID、#2 类型、#3 是否已胜、#4 积分、#5 头像、#6 昵称、#7 防守队、#8 赛季 ID、#9 头像框。

普通匹配中，`Arena_challenge.opponentid` 的语义是 `mdatas` 下标：原生 `GetCurrentChallengeMatchData` 在 `opponenttype==0` 时执行 `mdatas[opponentid]`；当前实现也明确以候选 `index` 编码请求。刷新候选后必须重建下标映射。

