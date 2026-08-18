# 消息映射

## 已证实的军团日常消息

字段编号以 protobuf wire field 表示；`C->S`/`S->C` 指消息方向。除本表明确标为推送的消息外，均按同 ID 请求/响应处理。

| 方向 | ID/名称 | 字段 | 发起或处理位置 | 触发条件与成功条件 | 证据 |
| --- | --- | --- | --- | --- | --- |
| S->C | `10490` `Game_data` | 外层 `8=storage`、`28=legion`；军团 `1=troops_level`、`3=officer`；军官 `1=id,2=level,3=rarity` | `decode_game_data_*`、`_refresh_game_data` | 会话就绪和后台刷新；用于本地资源检查，非动作完成终态。 | `legion_war.py:142-195, 451-463` |
| S->C | `12602` 背包变更 | 由 `decode_item_change_notify` 解码的 `item_id/total` | `_consume_background` | 任意等待期间刷新道具余额。 | `legion_war.py:465-470` |
| S->C | `20050` `Legion_info_sync` | 军团 `1=troops_level`、重复 `3=officer` | `_consume_background` | 任意等待期间更新军官/募兵快照。 | `legion_war.py:156-164, 471-473` |
| C->S, S->C | `20074` `Siege_sync` | 请求为空；响应重复 `1=town`、顶层 `2=today_tax_collected`；城堡 `1=id,2=progress,21=trapped_legion_battle,25=trapped_left_seconds` | `_sync_siege`、`decode_siege_state` | 每轮查找待突围城堡；`progress > 100 && trapped_left_seconds == 0` 才进入突围。 | `legion_war.py:198-227, 513-515` |
| C->S, S->C | `20075` `Siege_rescue_town` | 请求 `1=town_id`；响应 `1=town_id,10=ret,11=message` | `_rescue_and_fight` | `ret=0` 且返回 ID 为 `0` 或请求 ID 后，等待军团战状态；`ret=5` 表示已有战斗，先恢复/清理再有限重试。 | `legion_war.py:_rescue_and_fight, _decode_response_message` |
| S->C | `20055` `Legion_battle_sync` | `1=battle_id,4=result,12=turn,14=strategy options,20=event_id`；候选结构的重复/packed `1=strategy_id` | `_rescue_and_fight`、`_fight_battle` | 可在 `20075` 响应前到达；`turn <= 0` 表示开始阶段，`turn > 0` 表示等待选择。`turn>0,result=1,options=[]` 是结算过渡态，应继续等待 `20061`。 | `legion_war.py:_wait_for_battle_state, _fight_battle` |
| C->S, S->C | `20054` `Legion_battle_start` | 请求 `1=battle_id,2=event_id,3=packed officer_id[]`；响应 `10=ret,11=message` | `_start_battle` | 军官按稀有度降序、等级降序、ID 升序，并截断到 `ma_legion_officer_slot_num=5`；`ret=0` 才继续。 | `legion_war.py:_start_battle, select_battle_officers` |
| C->S, S->C | `20057` `Legion_battle_choose_strategy` | 请求 `1=strategy_id`；响应 `2=ret` | `_fight_battle` | 只从当前 `20055` 候选选择配置中最高 `rarity`；同品质随机；`ret=0` 才等待下一状态。 | `legion_war.py:301-305, 347-370, 568-585` |
| S->C | `20058` `Legion_battle_strategy_effects` | 战术效果推送 | `_consume_background` | 战术选择后的中间推送，消费后继续等待下一条战斗状态。 | `legion_war.py:_DEFERRED_WORKFLOW_MESSAGES, _wait_for_battle_state` |
| S->C | `20059` `Legion_battle_proficiency` | 熟练度推送 | `_consume_background` | 战术选择后的中间推送，不能当作战斗终态。 | `legion_war.py:_DEFERRED_WORKFLOW_MESSAGES, _wait_for_battle_state` |
| S->C | `20060` `Legion_battle_turn_fight` | 回合战斗推送 | `_consume_background` | 战术选择后的中间推送，不能触发重复出击。 | `legion_war.py:_DEFERRED_WORKFLOW_MESSAGES, _wait_for_battle_state` |
| C->S, S->C | `20062` `Legion_battle_retreat` | 请求为空；响应 `1=ret,2=message` | `_retreat_stale_battle` | 仅用于 `20075.ret=5` 且没有可恢复战斗状态时清理残留战斗；成功后回到突围请求。 | `legion_war.py:_retreat_stale_battle` |
| S->C | `20061` `Legion_battle_end_summary` | `1=result` | `_fight_battle` | 仅 `result=1` 计胜；其他值终止日常后续。 | `legion_war.py:559-591, 649-657` |
| C->S, S->C | `20080` `Siege_collect_tax` | 请求为空；响应 `10=ret` | `_collect_tax` | 当前同步没有围攻且顶层 `today_tax_collected=false`；`ret=0` 成功。 | `legion_war.py:594-602` |
| C->S, S->C | `19532` `Pull_gacha_banner_v2` | 请求 `1=banner=1,2=pull_times(1/5),3=category=2,6=costItem=80`；响应 `20=ret` | `_recruit_officers` | 余额至少 5000 发五连，至少 1000 发单抽；`ret=0` 成功。 | `legion_war.py:307-315, 604-625` |
| C->S, S->C | `20064` `Legion_upgrade_troops_level` | 请求为空；响应 `10=ret` | `_upgrade_troops` | 用当前募兵 `lv` 查表；仅成本存在且道具充足时发送；`ret=0` 成功。 | `legion_war.py:628-641` |

## 原生页面可见、协议尚未定位的动作

| 页面动作 | 原生错误分支 | 发送点/消息 ID | 载荷 | 成功后的状态更新 | 结论 |
| --- | --- | --- | --- | --- | --- |
| 军官等级升级 `officerLvUp` | `ret0/1/5/10/15` = 成功/无效 ID/未解锁/等级上限/费用不足。 | `unknown` | `unknown` | `unknown` | 不能用 `20064` 代替。 |
| 军官升阶 `officerRarityUp` | `ret0/1/5/10/15` = 成功/无效 ID/未解锁/稀有度上限/费用不足。 | `unknown` | `unknown` | `unknown` | 不能从 `legion_officer` 的静态成本反推。 |

本地化文件证明页面处理这些业务错误，但不包含消息注册表或按钮回调。下次只读采集必须先记录页面操作窗口内的全部未知消息，再将候选 ID 与发送点逐一交叉验证。

## 消息时序和缓存

等待预期响应期间，`10490`、`12602`、`20050` 是后台状态更新，必须立即消费；`20055/20058/20059/20060/20061/20062` 进入军团延迟队列供恢复逻辑消费；非本流程消息在关闭时通过 `push_headers` 归还共享会话。此规则避免把晚到的战斗状态、战斗中间推送、背包刷新或其他日常消息误认成当前动作的结果。
