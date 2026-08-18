# 状态机

## 1. 记号与完成判定

- `ret=0`：本地协议实现按成功处理。
- `ret!=0`：保留原始错误码；当前资料没有完整的地下城错误码字典。
- “动作调用成功”不等于“日常任务完成”。日常编排只接受 `Dailyquest_info` 前后 `finished` 从 `false` 变为 `true`。
- “抽取响应成功”不等于“奖励明细已落袋”。`Dun_start_draw` 是提交结果，`Storage_item_change_notify(source=71)` 是库存副作用同步。

## 2. 独立地下城任务（操作台）

| 当前状态 | 入站消息/本地事件 | 判定条件 | 状态变更 | 客户端后续发送 | 等待条件 | 下一状态 | 失败/超时 | 证据 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `INIT` | 用户选择地下城 | `dungeon_id` 为正整数 | 无 | 无 | 需要登录 | `LOGIN` | 配置缺失 | `app/routes.py:976-1000` |
| `LOGIN` | `Game_data(10490)` | 外层含 field 18 `dungeon` | 缓存 `DungeonStatus` | 心跳回应可选 | 同时等待 `Login_reunique` 与 `Game_data` | `READY` | 登录失败、kickout、超时 | `dungeon_sweep.py:433-481` |
| `READY` | 本地校验 | `unlocked ∩ showUnlocks` 含目标，且配置 `challengeTimes < limit` | 生成展示快照、历史最高分 | 无 | 扫荡前取消可直接结束 | `SWEEP_PENDING` | 不可展示/次数耗尽 | `app/services/dungeon_service.py:214-224,461-465` |
| `SWEEP_PENDING` | 本地动作 | `dunid` 正整数 | 无 | `19642 Dun_sweep {dunid}` | 等待同 ID `19642` 响应；期间处理心跳 | `SWEEP_OK` 或 `SWEEP_FAILED` | `ret!=0`、连接错误、超时 | `dungeon_sweep.py:489-506` |
| `SWEEP_OK` | `19642` `ret=0` | 原生处理器只发成功事件并提示 | 原生面板刷新；可能收到 `19618` 推送更新点数/总宝库次数 | 操作台直接发送 `19604`；原生 UI 通常先发送 `19626` 获取宝库信息 | 需等待扫荡结果及可能的异步状态同步 | `DRAW_PENDING` 或 `TREASURE_INFO_PENDING` | 无后续消息时 UI 可能只刷新不抽取 | `main.js:384`；`app/services/dungeon_service.py:226-250` |
| `TREASURE_INFO_PENDING` | `19626 Dun_treasure_info {id}` | `drawtimes < totaltimes` | 打开 Normal/Nightmare 宝库面板 | 宝库面板再发送 `19604`（发送点未在当前提取集找到） | 等 `19626` 回包 | `DRAW_PENDING` | `ret!=0` 显示 `gettreasureinfoerror` | `main.js:384,607` |
| `DRAW_PENDING` | 本地动作 | 目标 ID 必须与响应一致 | 无 | `19604 Dun_start_draw {dunid, all=true}` | 先等 `19604` 响应，再留出 1 秒通知窗口 | `DRAW_RESPONSE_OK` 或 `DRAW_FAILED` | `ret!=0`、ID 不匹配、超时 | `dungeon_sweep.py:508-560` |
| `DRAW_RESPONSE_OK` | `19604` `ret=0` | `dunid` 匹配且 `drawtimes <= totaltimes` | 更新本地投影中的 `draw_times/total_draw_times` | 无 | 等待 71 来源库存通知，允许晚到/断线 | `COMMITTED` | 通知缺失时仍可用 `dropids` 展示 | `dungeon_sweep.py:551-560` |
| `COMMITTED` | `12602 Storage_item_change_notify` | `source == 71` | 收集 items/props，去重已由 prop 覆盖的 item 数量 | 无 | 1 秒宽限期或连接关闭 | `COMPLETED` | 非 71 通知忽略；通知内容空则回退抽取 ID | `dungeon_sweep.py:561-572`；`app/services/dungeon_service.py:347-410` |

### 2.1 原生 UI 与操作台的差异

原生面板的静态路径是：

```text
OnClickWeep
  -> 19642 Dun_sweep
  -> OnDunSweep(ret=0)
  -> dungeon_sweep_suc
  -> DungeonEnterPanel.RefreshUI
  -> DungeonModule.TryDrawTreasure（条件：dunid>0 且 drawtimes<totaltimes）
  -> 19626 Dun_treasure_info
  -> DungeonTreasurePanel2/3
  -> 19604 Dun_start_draw
```

操作台路径是：

```text
get_status -> 19642 -> 19604(all=true) -> 12602(source=71)*N
```

运行时日志恰好验证了第二条路径；没有 `19626` 帧，不能把操作台的直连顺序宣称为原生 UI 的完整顺序。

## 3. 日常任务 107 与日活奖励

| 当前状态 | 入站消息/本地事件 | 判定条件 | 状态变更 | 客户端后续发送 | 等待条件 | 下一状态 | 失败/超时 | 证据 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `DAILY_INIT` | `Game_data(10490)` | field 19 存在 `dailyquest` | 缓存任务状态和 field 12 的条件进度 | 登录完成后可发 `19700` | 等 `Login_reunique` 与状态 | `DAILY_STATUS_PENDING` | 缺少状态即失败 | `daily_quest.py:396-409,584-635` |
| `DAILY_STATUS_PENDING` | `19700 Dailyquest_info` | 解析每日/每周重置秒数、任务 `datas[]` | 得到 `finished`、`score_claimed`、已领奖励 ID | 若任务 107 未完成，执行 `run_dungeon_sweep_action` | 等状态回包 | `ACTION_107` 或 `TASK_CLAIM_SCAN` | 查询失败/断线 | `daily_quest.py:640-655` |
| `ACTION_107` | 本地日常动作 | task 107 存在且未完成；`remaining=target-progress` | 固定最多 3 次扫荡请求 | 重复 `19642`，每次等待响应 | 再发 `19700` 确认服务端状态 | `TASK_FINISHED` 或 `TASK_INCOMPLETE` | 任一动作异常、服务端未标完成 | `daily_actions.py:160-241,559-611` |
| `TASK_CLAIM_SCAN` | `19700` 状态 | `finished=true && score_claimed=false` | 无 | `19702 {id,group}`，逐任务领取 | 每个响应 `ret=0` | `ACTIVITY_SCORE` | `ret!=0` 中止领取 | `daily_quest.py:694-715` |
| `ACTIVITY_SCORE` | 再次 `19700` | 计算已 `score_claimed` 任务的配置分数 | 得到日/周活跃分 | 对每个 `score <= activity_score` 且不在已领列表的奖励发 `19704` | 每个响应 `ret=0` | `DAILY_REWARD_CLAIMED` | 阈值未达、重复或 `ret!=0` | `daily_quest.py:716-747` |
| `DAILY_REWARD_CLAIMED` | 最终 `19700` | 已领 ID 出现在最终状态 | 日常任务/活跃奖励闭环完成 | 无 | 最终状态稳定 | `DAILY_DONE` | 最终查询失败或状态未同步 | `daily_quest.py:749-776` |

### 3.1 日常批次的边界

`DailyActionRunner.run` 在所有选定动作完成后才调用一次 `claim_available`；取消时只刷新状态，不领取奖励。地下城日常动作本身不调用 `draw_all`，所以“完成任务 107”与“领取地下城宝库掉落”是两个可以独立成功/失败的阶段。
