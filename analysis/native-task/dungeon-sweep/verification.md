# 验证记录

## 1. 静态验证

以下验证均为本地离线操作，不连接游戏服：

```text
../.venv/bin/python -m unittest tests.test_dungeon_sweep
../.venv/bin/python -m unittest tests.test_daily_actions
../.venv/bin/python -m unittest tests.test_ui_app
```

执行结果（2026-08-04）：

```text
../.venv/bin/python -m unittest tests.test_dungeon_sweep tests.test_auto_task_service tests.test_ui_app
Ran 58 tests in 1.929s
OK

../.venv/bin/python -m unittest discover -s tests -p 'test_*.py'
Ran 275 tests in 4.596s
OK
```

验证目标：

- `Game_data.dungeon` 的重复/非法 ID、次数和最高分校验。
- `Dun_sweep` 请求为 field 1，`Dun_start_draw` 请求为 field 1 + field 2。
- 正常序列必须是扫荡成功后才允许抽取；扫荡 `ret!=0` 不得发送后续业务包。
- 原生已定义的扫荡拒绝码保留中文原因；`ret=3` 必须显示为“无扫荡次数”，自动任务以跳过结束，未知拒绝码仍保留为失败。
- 抽取响应 ID、次数、`all` 和奖励通知字段正确解码。
- `DailyActionRunner` 仅在服务端状态 `unfinished -> finished` 后标记日常完成。
- 任务积分领取后重新查询状态，再按阈值领取日活/周活奖励；重复运行不重复发送已领奖励。

## 2. 运行时记录复核

对原始 JSONL 进行了只读字段级复核：

```text
范围：logs/websocket_raw/dungeon_sweep/*.jsonl
记录数：832
19642：outbound 104 / inbound 104；ret=0 52、ret=4 52
19604：outbound 52 / inbound 52；请求 all=true 52、响应 all=true 52
12602：104 条；source=71 104；每条 1 item + 1 prop
19626：0 条
```

每个成功抽取会话的业务序列为：

```text
19642 request -> 19642 ret=0 -> 19604(dunid=2302, all=true)
-> 19604(ret=0, 3/3, all=true)
-> 12602(source=71) -> 12602(source=71)
```

失败样本的序列在 `19642 ret=4` 后结束，没有观察到后续 `19604`。

日常日志复核结果：

```text
范围：logs/websocket_raw/daily_quest/*.jsonl
19700：outbound 668 / inbound 667
19702：outbound 230 / inbound 230
19704：outbound 285 / inbound 285
```

日常日志能够验证通用领取协议和状态字段，但不能证明任务 107 在同一窗口发出了 `19702 id=107`；该项保留为开放问题。

## 3. 未执行的验证

- 未对线上/真实账号发起新的请求。
- 未从原生 AssetBundle 得到宝库面板 TextAsset：目标 Bundle 的 UnityPy 对象中 `TextAsset_COUNT=0`，因此没有伪造面板发送点。
- 未把 `ret=4` 映射为具体文案，也未据此实现自动重试。
- 对新增 Markdown 执行了 `git diff --check -- analysis/native-task/dungeon-sweep 地下城扫荡与每日奖励逻辑分析.md`，未发现补丁格式问题。
