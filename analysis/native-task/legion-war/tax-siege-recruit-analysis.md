# 军团税收、城堡突围与军官招募流程分析

## 范围与结论等级

本文只分析本地原生客户端中与军官页面相关的三条闭环：

1. 已占领城堡的每日税收领取；
2. 城堡受围时的“突围”流程（用户口语中的“城堡围剿”：出击 -> 军团战 -> 恢复税收资格）；
3. 按军团币余额进行军官招募。

`verified-static` 表示本地解密客户端、配置表或现有协议实现可直接支持；`inferred` 表示由这些静态证据推导；`unknown` 表示没有本次真实运行时帧，不能当作额外发包依据。

| 结论 | 等级 | 依据 |
| --- | --- | --- |
| 军团币为道具 `80`，军官招募横幅为 `1`，单抽/五连分别消耗 `1000/5000` 军团币。 | `verified-static` | `../../../../native_app/decrypted-data/tables/item.json:944-947`；`gacha_banner_v2.json:3-21`。 |
| 城堡受围时不可收税，需先突围；税收成功后通过道具变更通知展示和更新奖励。 | `verified-static` | `rule_description.json:1191`；`decrypted-js/main.dragon-arena.js:179,5725,7803`。 |
| 城堡突围的胜利终态为 `Legion_battle_end_summary(20061).result == 1`。 | `verified-static` | `decrypted-js/main.dragon-arena.js:4745`；`legion_war.py:557-592`。 |
| 本次本地登录会话已完整跑通围攻、税收、招募和募兵升级；精简收发摘要保存在 `packet-trace.jsonl`。 | `verified-runtime` | `packet-trace.jsonl`；原始日志 `logs/websocket_raw/game_session/2026-08-04.jsonl`。 |

## 统一状态与消息

| 状态/消息 | 方向 | 作用 | 关键字段或副作用 |
| --- | --- | --- | --- |
| `Game_data(10490)` | S->C | 初始化背包、军团、城堡快照。 | 军团在字段 `28`，背包在字段 `8`。 |
| `Storage_notify_itemchange(12602)` | S->C 推送 | 道具余额的权威增量来源。 | 税收对应动作类型 `156`；首次占领奖励对应 `157`。 |
| `Legion_info_sync(20050)` | S->C 推送 | 更新军官列表和募兵等级。 | 招募后应等待它或 `Legion_new_officer(20063)`，而非只看抽卡响应。 |
| `Siege_sync(20074)` | C->S / S->C | 同步城堡列表及当天税收标志。 | `townList`、`todayTaxCollected`。 |
| `Siege_rescue_town(20075)` | C->S / S->C | 对被围困的已占领城堡发起突围。 | 请求城堡 ID；响应 `townId`、`ret`。 |
| `Legion_battle_* (20054-20062)` | 双向及推送 | 军团出击、回合策略、中间战斗推送、残留战斗清理和结算。 | `20061.result=1` 才是胜利；`20062` 仅用于 `20075.ret=5` 恢复。 |
| `Siege_collect_tax(20080)` | C->S / S->C | 请求当日全部可收税城堡的收益。 | 响应 `ret`、可能带 `trappedTowns` 刷新。 |
| `Pull_gacha_banner_v2(19532)` | C->S / S->C | 军官横幅普通抽取。 | 现有编码为 `banner=1`、`pullTimes=1/5`、`category=2`、`costItem=80`。 |

消息名称和编号由原生消息枚举直接给出：`decrypted-js/main.dragon-arena.js:7793,7803`。字段级请求编码见本地已实现客户端 `legion_war.py`，并已由 `packet-trace.jsonl` 的真实会话帧验证关键请求字段。

## 1. 每日税收领取

### 原生规则

- 每个已占领城堡每天可征收一次，收益为军团币。
- 任意城堡处于围城状态时，税收入口被阻止，页面提示“前往突围”。
- 若当天已经领取税收，之后新占领的城堡直接给予税收货币，不再要求再次发送税收请求。

规则文字见 `../../../../native_app/decrypted-data/tables/rule_description.json:1191`；错误分支见 `../../../../native_app/decrypted-task-data/zh-Hans.json:3663-3667`。

### 状态机

| 当前状态 | 事件/判定 | 客户端动作 | 成功后的状态 | 失败或停止条件 |
| --- | --- | --- | --- | --- |
| `SIEGE_SYNC` | 收到 `20074`，读取 `todayTaxCollected` 与城堡列表。 | 无。 | 有受围城堡则进入突围；否则进入税收门槛。 | 同步失败或超时。 |
| `TAX_GATE` | `todayTaxCollected=true`。 | 跳过。 | 结束税收步骤。 | 无。 |
| `TAX_GATE` | `todayTaxCollected=false` 且没有受围城堡。 | 发送空载荷 `20080`。 | 等待税收响应。 | 尚有受围城堡时不发送。 |
| `TAX_RESPONSE` | `20080.ret=0`。 | 更新本地 `todayTaxCollected=true`；等待道具通知。 | `TAX_REWARD_APPLIED`。 | `ret=1/5/10` 分别对应已收取、仍有城堡未解围、没有可收税城堡。 |
| `TAX_REWARD_APPLIED` | 收到 `12602`，动作类型为 `156`。 | 刷新道具 `80` 总量，军官页打开时显示“每日税收”奖励框，否则缓存后展示。 | 可按最新余额评估招募。 | 未等到余额同步时，不应用旧缓存决定招募。 |

原生 `SiegeModule.onSiegeCollectTax` 在 `ret=0` 时更新 `todayTaxCollected`、红点和城堡状态；原生存储模块把动作 `SIEGE_TOWN_TAX_INCOME(156)` 交给奖励框处理。两段处理分别位于 `decrypted-js/main.dragon-arena.js:5725` 和 `:179`。

### 各城堡每日税收

`siege_town.tax_rewards` 的格式为 `kind,itemId,count`，当前 11 条配置均为 `1,80,<军团币数量>`：

| 城堡 ID | 城堡 | 每日军团币 |
| ---: | --- | ---: |
| 1 | 泰特尔要塞 | 1000 |
| 2 | 西姆伊克堡 | 1000 |
| 3 | 瓦尔勒要塞 | 1200 |
| 4 | 黄金东堡垒 | 1400 |
| 5 | 獠牙堡 | 1600 |
| 6 | 黄金西堡垒 | 1800 |
| 7 | 白鹿堡 | 2000 |
| 8 | 恶魔堡垒 | 2400 |
| 9 | 西侧堡垒·1年后 | 2500 |
| 10 | 泰特尔要塞·1年后 | 2500 |
| 11 | 西姆伊克堡·1年后 | 2600 |

来源：`../../../../native_app/decrypted-data/tables/siege_town.json:1-210`。实际到账数量仍可能受服务端状态或科技效果修正，静态表不能替代到账通知。

## 2. 城堡突围：出击 -> 对战 -> 奖励边界

### 术语与前置条件

原生协议名为 `Siege_rescue_town`，页面文案为“突围”；它处理的是**已经占领但被围困**的城堡，不是新城堡的 `Siege_do_action(20070)` 占领流程。原生 `isTownTrapped` 的静态条件是：

```text
isTownOcc(town) && town.trappedLeftSecs == 0
isTownOcc(town) := town.progress > 100
```

证据：`decrypted-js/main.dragon-arena.js:5725`；协议解析的字段映射见 `legion_war.py:198-227`。

### 完整链路

```text
20074 Siege_sync
  -> 找到受围的已占领城堡
  -> 20075 Siege_rescue_town(townId)
  -> 20075 响应：ret == 0，townId 与请求相符
  -> 20055 Legion_battle_sync
     -> turn <= 0：准备界面，按 ma_legion_officer_slot_num=5 截断军官，发送 20054 Legion_battle_start(battleId,eventId,officerIds)
     -> turn > 0：策略界面，循环发送 20057 Legion_battle_choose_strategy(strategyId)
  -> 20058/20059/20060：战斗特效、熟练度、回合战斗推送
  -> 20061 Legion_battle_end_summary(result)
     -> result == 1：突围胜利，重新 20074 同步城堡状态
     -> result != 1：停止，不能继续收税或招募
```

| 阶段 | 协议与原生处理 | 成功判定 | 失败分支 |
| --- | --- | --- | --- |
| 出击 | `20075`，原生处理 `onSiegeRescueTown`。 | `ret=0`，记录返回 `townId`。 | `ret=5` 时先恢复延迟的战斗状态；无状态才发送 `20062` 并最多重试一次；其他 ret 停止。 |
| 战前 | `20055.turn<=0` 打开 `PreparationPanel`；按宏上限 5 名军官发送 `20054`。 | `20054.ret=0`。 | 兵力不足、军官未解锁、没有上阵军官、超过上限等；保留服务端 message。 |
| 回合 | `20055.turn>0` 打开 `LegionBattlePanel`；选择当前下发的策略并发送 `20057`。 | `20057.ret=0`，等待下一轮推送或结算。 | 策略不在候选、无效策略、策略道具不足。 |
| 结算 | `20061` 由 `onLegionBattleEndSummary` 缓存并驱动战斗界面。 | `result=1`。 | 非 `1` 为非胜利，日常链路应终止。 |
| 恢复 | 胜利后重新 `20074`。 | 该城堡不再满足受围条件。 | 同步仍显示受围或超时，不能进入税收。 |

原生客户端会把 `20058`、`20059`、`20060` 加入战斗消息队列，并在 `20061` 时把胜利标志写入本地状态；见 `decrypted-js/main.dragon-arena.js:4745`。现有协议客户端的受控测试覆盖了 `20074 -> 20075 -> 20055 -> 20054 -> 20057 -> 20061 -> 20074` 的顺序，见 `tests/test_legion_war.py:154-229`。

### “获取奖励”不能混为一类

| 奖励类别 | 触发点 | 原生存储动作 | 是否是突围胜利的独立领奖请求 |
| --- | --- | --- | --- |
| 军团战结算 | `20061.result`。 | `LEGION_BATTLE_END_PROC(154)` 仅被存储模块特殊处理，不弹通用奖励框。 | 没有观察到独立领奖请求。 |
| 首次占领奖励 | 新城堡被占领时。 | `SIEGE_TOWN_OCCUPY_REWARD(157)`；配置字段 `occupy_rewards`。 | 与“已占领城堡的突围”不同。 |
| 每日税收 | 无受围城堡时请求 `20080`。 | `SIEGE_TOWN_TAX_INCOME(156)`；配置字段 `tax_rewards`。 | 是突围后恢复资格所能领取的收益。 |

因此，突围成功的可证实业务结果是“恢复城堡控制和税收资格”；不能仅因 `20061.result=1` 就把 `occupy_rewards` 当作本次突围的战后奖励。只有 `12602` 的实际道具变更或原始运行时帧才能确认某次具体到账内容。

## 3. 按军团币余额招募军官

### 配置与请求

| 项目 | 值 | 证据 |
| --- | --- | --- |
| 货币 | `itemId=80`，名称“军团币”。 | `item.json:944-947`。 |
| 横幅 | `bannerId=1`，名称“军官招募”。 | `gacha_banner_v2.json:3-21`。 |
| 单抽 | `cost1=1,1000`。 | 同上。 |
| 五连 | `cost2=5,5000`。 | 同上。 |
| 普通抽取 | `category=2`。 | `legion_war.py:46,307-315`。 |
| 请求 | `19532 Pull_gacha_banner_v2`。 | `decrypted-js/main.dragon-arena.js:4745,7803`。 |

当前工作区的编码器将普通军官招募请求编码为：

```text
field 1: bannerId = 1
field 2: pullTimes = 1 或 5
field 3: category = 2
field 6: costItem = 80
```

字段编号来自 `legion_war.py`，本次真实帧已验证 `19532` 请求和 `ret=0` 响应。原生 `LegionModule.onPullGachaBannerV2` 则确认响应至少按 `category`、`result`、`data`、`props`、`pullTimes` 驱动结果展示，并在成功后刷新军团页：`decrypted-js/main.dragon-arena.js:4745-4751`。

### 当前日常策略的余额决策

`LegionWarClient._recruit_officers` 每轮最多发送一次招募请求，决策如下：

| 当前已同步的军团币余额 | 动作 | 请求次数 | 本轮余额剩余（不计后续到账） |
| ---: | --- | ---: | ---: |
| `< 1000` | 跳过 | 0 | 原值 |
| `1000-4999` | 单抽 | 1 | `余额 - 1000` |
| `>= 5000` | 五连 | 5 | `余额 - 5000` |

证据：`legion_war.py:604-625`。这不是“耗尽全部军团币”的循环策略：即使余额高于 `5000`，当前日常实现也只执行一次五连。配置中的 `maximum=10000` 不能单独证明可以在同一日常循环中连续自动多次抽取。

### 必须等待余额副作用

税收响应 `20080.ret=0` 只确认服务器受理并更新税收状态；原生客户端把实际税收奖励交由随后的 `12602` 道具变更处理。因此正确的余额判定顺序为：

```text
完成突围（若有）
  -> 20080 成功
  -> 等待并消费税收相关 12602
  -> 读取 item[80] 的最新 total
  -> 按 0 / 1 / 5 的阈值决策
  -> 19532 成功
  -> 等待 20063、20050 或对应背包变更使军官/道具状态稳定
```

本次真实会话中，税收相关 `12602` 在 `20080` 响应之前到达（序列 89 -> 90），招募后又收到一次 `12602`（序列 92）；实现仍在等待报文时消费两种顺序，避免把旧余额误用于招募决策。不同服务端版本若改变推送顺序，仍应保留该等待逻辑。

## 4. 失败、停止与验收条件

| 现象 | 处理 | 依据 |
| --- | --- | --- |
| `20080` 返回 `ret=5` | 仍有城堡未解围；回到城堡同步，不能直接重试收税。 | `zh-Hans.json:3663-3667`。 |
| `20075` 或 `20054` 被拒绝 | 记录原始 `ret`，停止该条城堡流程。 | `zh-Hans.json:3657-3661,3675-3683`。 |
| `20061.result != 1` | 结束日常链路，不继续税收或招募。 | `legion_war.py:649-663`；`tools/validate_legion_war_capture.py:184-206`。 |
| 招募返回非零 | 不把余额不足、日限或参数错误当作成功；保留 `ret`。 | `zh-Hans.json:3685-3693`。 |
| 收到税收成功但未看到 `12602` | 视为“余额未稳定”，不以旧余额启动招募。 | 原生奖励处理与上述竞态分析。 |

离线受控测试和真实会话均覆盖一座受围城堡胜利后的收税、五连招募和募兵升级顺序。真实摘要见 `packet-trace.jsonl`，其中包含 `20074`、`20075`、`20054`、`20055`、`20057`、`20058/20059/20060`、`20061`、胜利后的 `20074`、`20080`、`19532` 和 `20064`；本次服务端失败数为 0。

## 5. 关联资产

- 原生城堡/军团模块：`decrypted-js/main.dragon-arena.js`。
- 城堡配置：`../../../../native_app/decrypted-data/tables/siege_town.json`。
- 军官招募横幅：`../../../../native_app/decrypted-data/tables/gacha_banner_v2.json`。
- 军团币定义：`../../../../native_app/decrypted-data/tables/item.json`。
- 既有字段解析与受控会话实现：`legion_war.py`。
- 运行时只读采集器：`tools/frida_hook_legion_war.js`、`tools/capture_legion_war_frida.py`。
- 同目录的消息映射与状态机：`message-map.md`、`state-machine.md`、`verification.md`。
