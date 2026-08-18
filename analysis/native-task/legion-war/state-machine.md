# 状态机

## 日常闭环：围攻 -> 税收 -> 招募 -> 募兵

| 当前状态 | 入站消息/本地事件 | 判定条件 | 状态变更 | 客户端后续发送 | 等待条件 | 下一状态 | 失败/超时 | 证据 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `SESSION_READY` | 会话恢复；`10490` | `Game_data` 含军团字段 `28`。 | 刷新军团与背包快照。 | `20074` 空载荷。 | `20074` 响应。 | `SIEGE_SYNCED` | 无军团信息或会话异常。 | `legion_war.py:442-463, 513-515` |
| `SIEGE_SYNCED` | `20074` | 找到 `progress > 100 && trapped_left_seconds == 0` 的城堡。 | 固定当前城堡。 | `20075(town_id)`。 | 同 ID 响应。 | `RESCUE_ACCEPTED` | 无待突围城堡则进入 `TAX_GATE`。 | `legion_war.py:86-91, 649-655` |
| `RESCUE_ACCEPTED` | `20075` | `ret=0` 且回传城堡 ID 为 `0` 或请求 ID。 | 接受突围；若先收到 `20055`，先缓存战斗状态。 | 无。 | `20055` 军团战状态。 | `BATTLE_PREPARE` 或 `BATTLE_CHOOSE` | 非零 `ret`、城堡 ID 不匹配、等待超时。`ret=5` 进入恢复分支。 | `legion_war.py:_rescue_and_fight` |
| `RESCUE_RECOVERY` | `20075` `ret=5` | 服务端提示已有军团战，且尚未得到可继续的 `20055/20061`。 | 先消费延迟队列；仍无状态时发送空载荷 `20062` 清理旧战斗，再有限重试 `20075`。 | `20062`。 | `20062` 响应后重新等待 `20055/20061`。 | `RESCUE_ACCEPTED` | 第二次仍为 `ret=5` 或其他非零 ret，停止当前日常并保留服务端 message。 | `legion_war.py:_rescue_and_fight, _retreat_stale_battle` |
| `BATTLE_PREPARE` | `20055` | `turn <= 0`。 | 从当前军团快照按 rarity desc、level desc、ID asc 排序，只取原生宏 `ma_legion_officer_slot_num` 指定的槽位数（当前值 `5`）。 | `20054(battle_id,event_id,packed officer_ids)`。 | `20054` 响应。 | `BATTLE_WAIT` | 无可用军官、超过服务端上限、`ret != 0`。 | `legion_war.py:select_battle_officers, _start_battle`；`macros.json:ma_legion_officer_slot_num=5` |
| `BATTLE_CHOOSE` | `20055` | `turn > 0` 且候选非空且均有配置品质。 | 选择候选中最高品质；并列随机。 | `20057(strategy_id)`。 | `20057` 响应。 | `BATTLE_WAIT` | 候选为空时等待后续 `20055/20061`，不发送空 `20057`；未知战术、`ret != 0` 则停止。 | `legion_war.py:_fight_battle, _wait_for_battle_state` |
| `BATTLE_WAIT` | `20055`、`20058`、`20059`、`20060` 或 `20061` | 收到新状态、战斗特效/熟练度推送或结算。 | 消费 `20058/20059/20060`；新 `20055` 按 `turn` 回到准备/选择；`20061.field1=1` 计胜。 | 仅在新状态需要时发送。 | 结算或下一状态。 | `RESYNC_AFTER_WIN` | `20061.field1 != 1`，终止日常。 | `legion_war.py:_wait_for_battle_state, _fight_battle` |
| `RESYNC_AFTER_WIN` | 本地胜利 | 已确认胜利。 | `siege_wins += 1`。 | `20074` 空载荷。 | `20074` 响应。 | `SIEGE_SYNCED` | 同步超时。 | `legion_war.py:644-660` |
| `TAX_GATE` | 最后一份 `20074` | 无待突围城堡。 | 检查 `today_tax_collected`。 | 当未领取时发送 `20080` 空载荷。 | `20080` 响应。 | `RECRUIT_GATE` | 税收 `ret != 0`；已领取则直接下一步。 | `legion_war.py:594-602, 661-662` |
| `RECRUIT_GATE` | 本地背包快照 | `item[80] >= 5000` 选 5；`>=1000` 选 1；否则跳过。 | 记录本次抽取数。 | `19532(1,times,2,80)`。 | `19532` 响应。 | `TROOP_UPGRADE_GATE` | 招募 `ret != 0`；余额不足不是错误。 | `legion_war.py:604-625` |
| `TROOP_UPGRADE_GATE` | 军团/背包快照 | 当前 `troops_level` 有成本且对应道具充足。 | 准备升级。 | `20064` 空载荷。 | `20064` 响应。 | `DONE` | `ret != 0`；成本缺失/不足为跳过。 | `legion_war.py:628-641, 664-668` |
| `DONE` | 所有前置操作稳定 | 汇总围攻胜场、税收、抽取次数与募兵状态。 | 交还 `DailyActionRunner`。 | 无。 | 无。 | 结束。 | 不在本地提前标记任务完成。 | `daily_actions.py:685-710` |

## 军官等级升级与升阶：当前边界

| 当前状态 | 观察到的页面事件 | 可确认条件 | 已证实的状态变更/后续发送 | 下一状态 | 证据 |
| --- | --- | --- | --- | --- | --- |
| `OFFICER_SELECTED` | 点击“升级”或“升阶”。 | 原生文案证明两种操作独立存在。 | `unknown`。 | `UPGRADE_PENDING_CAPTURE` | `zh-Hans.json:3718-3731` |
| `UPGRADE_PENDING_CAPTURE` | 服务端响应。 | 本地化定义 success、无效 ID、未解锁、上限、费用不足等语义。 | 消息 ID、字段和同步包均 `unknown`。 | 仅在采集到真实时序后细化。 | `zh-Hans.json:3718-3731` |

`20064` 的名称和实现均为 `Legion_upgrade_troops_level`，对应“募兵”而非单个军官；因此它只能驱动第一张状态机的 `TROOP_UPGRADE_GATE`。
