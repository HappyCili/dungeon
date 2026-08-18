# 普通竞技场（骑士比武）原生流程分析

分析日期：2026-08-04
范围：本地原生解密脚本、当前 Python 实现、单元测试和已有 WebSocket 抓包。本文的“普通竞技场”仅指 `Arena_*` 消息族（骑士比武），不包含 `Scararena_*` 龙痕竞技场。

## 结论

- 入口状态来自 `Game_data.arenainfo` 或 `19800 Arena_info`。核心字段为赛季是否开放、每日已挑战数 `challengenum`、每日奖励状态 `getdailyreward`、候选列表 `mdatas` 和赛季挑战总数 `seasonchallnum`。[S1][S2]
- 当前项目以 `remaining = max(5 - challengenum, 0)` 计算剩余每日免费挑战次数。已确认普通竞技场每日免费挑战上限为 `5`；`refreshnum` 是刷新次数，不参与该预算。日常任务 109 的完成目标 `3` 次挑战与此上限独立。原生模块读取宏 `ma_arena_dailychallenge`，但可见模块代码未直接展示宏值。[S1][P1]
- 普通匹配对手使用 `opponenttype=0`，请求中的 `opponentid` 是 `mdatas` 的列表下标，不是候选的账号/实体 ID。当前项目优先选 `win==0` 的候选。[S1][P2]
- `19810 Arena_challenge` 成功只表示挑战被接收并更新次数/当前对手；随后的 `18002 Battle_info`（`battle_type=5`）仍是备战状态。只有发送 `18010 Battle_C2S_start` 并收到 `18012 Battle_S2C_start(ret=0)` 后，才能视为战斗已开始。[S3][P3]
- 战斗胜负与积分以 `19818 Arena_challenge_result` 为准；`18090 Battle_S2C_end` 不是等待该结算包的强制前置条件。[S1][P3]

## 主流程

| 阶段 | 前置条件与判定 | 原生/协议动作 | 成功后的本地状态 | 证据 |
| --- | --- | --- | --- | --- |
| 1. 进入与同步 | 登录数据可用，或缓存的 `dailylt`/`seasonlt` 到期 | 读取 `Game_data.arenainfo`；必要时发 `19800 Arena_info` | 缓存 `ArenaInfo`，初始化历史记录 | [S1][R1] |
| 2. 每日奖励 | `seasonopen=true` 且 `getdailyreward=false` | 原生点亮每日奖励红点；领取端点为 `19814 Arena_get_dailyreward` | 原生处理器把 `getdailyreward` 设为 true，并暂存 `rankid` | [S1][S2] |
| 3. 免费次数 | `seasonopen=true` | `used=challengenum`；当前项目采用每日免费上限 `limit=5`、`remaining=max(limit-used,0)`；不读取 `refreshnum` | `remaining==0` 时跳过免费挑战 | [P1] |
| 4. 选择对手 | `remaining>0` 且 `mdatas` 非空 | 选择 `opponenttype=0` 的候选下标；优先 `win==0`，候选耗尽时可发 `19808` 刷新 | 得到本轮 `opponent_index` | [S1][P2][R1] |
| 5. 准备进攻编队 | `Game_data.hero.teams[4]` 有有效阵容 | 当前项目先同步编队槽位 4：`15016 Hero_setteam`，等待 `15014 Hero_teamsync`；原生队伍界面确认后发送挑战 | 编队可用于构造 `18010` | [S4][P4] |
| 6. 提交挑战 | 已选择下标、编队已就绪 | 发 `19810 Arena_challenge`，请求字段为 `mode`、`opponenttype`、`opponentid`、`getteam` | `ret=0` 时更新 `challengenum`、`seasonchallnum`、当前对手类型/下标 | [S2][S1] |
| 7. 备战 | 收到 `18002 Battle_info`，且 `battle_type=5`、`ret=0` | 校验当前编队和敌方站位，发 `18010 Battle_C2S_start` | 等待 `18012`；此时尚未进入战斗中 | [S3][P3][R2] |
| 8. 开始战斗 | 收到 `18012 Battle_S2C_start` | 仅 `ret=0` 时进入战斗中；当前项目随后设置倍速与自动技能 | 接收战斗帧、哈希校验与结束通知 | [P3][R2] |
| 9. 战斗结算 | 收到 `19818 Arena_challenge_result` | 读取 `win`、对手类型/下标、赛季积分、积分变化和历史最高分 | 更新积分/最高分；普通匹配胜利时标记该候选 `win=1` | [S1][S2] |

## 每日奖励流程

1. 从 `Arena_info` 读取字段 `seasonopen`（#14）和 `getdailyreward`（#7）。原生在 `seasonopen && !getdailyreward` 时显示每日奖励红点。
2. 领取入口是 `19814 Arena_get_dailyreward`，响应 codec 为 `ret`（#1）和 `rankid`（#2）。
3. 当前静态模块的 `OnArenaGetDailyreward` 会直接把 `getdailyreward` 设为 `true`、保存 `rankid`、刷新 UI 并关闭红点；该处理器未按 `ret` 分支。
4. 本地抓包没有 `19814` 的请求或成功响应。因此在任何自动化实现中，应先补采一次真实点击的请求体与 `ret` 语义，再以服务端状态更新或明确成功回包作为领取完成条件。

`19822 Arena_get_challreward` 是按 `seasonchallnum` 领取的挑战次数阶段奖励，与每日奖励是独立流程；不应以 `getdailyreward` 代替其已领取标记 `rewardids`。[S1][S2]

## 免费次数判定

```text
if not Arena_info.seasonopen:
    stop("赛季未开放")

used = Arena_info.challengenum
limit = macro("ma_arena_dailychallenge")  # 原生来源；当前业务规则为 5

# 当前项目自动任务策略：limit = 5；refreshnum 不参与计算
limit = 5
remaining = max(limit - used, 0)
```

- `challengenum` 是日内已挑战数：在 `19810` 成功响应中由服务端返回并被原生模块写回缓存。
- 字段在 protobuf 中为默认值 `0` 时可省略。抓包第 14、27 帧未携带字段 #1，按 codec 默认值解码为 `0`。
- 静态代码中的 `challengenum == 0` 仅用于“今日可挑战”红点，不能据此推导“剩余次数始终为 1”。
- 若未能读取宏表，当前项目按已确认规则将每日免费上限配置为 `5`，并在挑战成功响应后重新采用服务端返回的 `challengenum` 计算下一轮。刷新次数 `refreshnum` 不改变该预算。
- 自动任务现在只读取一次 `Arena_info`，并以已被 `19810` 接受的挑战数作为不可扩张的免费预算。若某次已接受挑战未取得 `19818` 结算，则立即停止，不再挑选下一名对手；结果会返回已用、请求、已接受、已结算和被拒绝次数。

## 对手选择与编队准备

### 选择对手

- `Arena_info.mdatas`（字段 #10）是普通匹配候选；单项含 `id`、`type`、`win`、`score`、头像、昵称、防守队伍等字段。
- 对普通匹配，`Arena_challenge.opponenttype=0`，`opponentid` 指向 `mdatas` 当前下标。列表刷新后下标可能改变，不能复用旧列表的下标。
- 当前辅助实现从未尝试候选中随机选取，并优先 `win==0`；候选全部尝试后，按配置决定是否调用 `19808 Arena_refresh_opponents`。[P2]
- 若缓存的敌方战力与准备页展示值不同，原生会调用 `19816 Arena_get_opponentteam` 取回对应对手阵容后再继续。[S1][S2]

### 准备编队

- 普通竞技场 PVP 使用英雄编队槽位 `4`。当前辅助实现从 `Game_data.hero.teams[4]` 取出完整阵容；空编队会在构造 `18010` 前终止。[P4]
- 原生协议同时存在 `19806 Arena_set_attackteam`，其响应有 `ret` 和 `team`。现有抓包未看到该消息。
- 当前项目对齐队伍编辑器流程，先发送通用 `15016 Hero_setteam`，再等待 `15014 Hero_teamsync`。这是一项项目实现行为，不能与未观测到的 `19806` 发送时机混为一谈。[P4][T1]

## 战斗与结算

```text
19810 Arena_challenge(ret=0)
  -> 18002 Battle_info(ret=0, battle_type=5)       # 备战，不等于已开始
  -> 18010 Battle_C2S_start                         # 回送我方/敌方站位与战斗参数
  -> 18012 Battle_S2C_start(ret=0)                  # 战斗中
  -> 18500/18502 战斗帧与哈希校验（可多次）
  -> 18090 Battle_S2C_end（可能出现）
  -> 19818 Arena_challenge_result                   # 权威胜负/积分结算
```

已有抓包证明了 `18002(battle_type=5)` 和之后的 `18010`，但第 30 帧的 `18012` 返回 `ret=7`，随后没有 `19818`；该样本是失败握手，不得计为完成挑战或成功结算。[R2]

详见：[资产清单](inventory.md)、[消息映射](message-map.md)、[状态机](state-machine.md)、[失败矩阵](failure-matrix.md)、[证据记录](evidence.md)、[待确认项](open-questions.md)、[验证记录](verification.md)。

## 证据标记

- `[S*]`：原生静态脚本/codec 已验证。
- `[R*]`：本地运行时 JSONL 抓包已验证。
- `[P*]`：当前项目实现或单元测试已验证。
- `[T*]`：测试用例已验证。
