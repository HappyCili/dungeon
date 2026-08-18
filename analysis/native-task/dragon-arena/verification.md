# 验证记录

## 原始帧复核

- 文件：`logs/websocket_raw/game_session/2026-07-29.jsonl`
- 会话：`1785256999646589000`
- 末段序列：650 `21100` 出站、651 `21100` 入站、652 `21104` 出站、653 `21104` 入站。
- 解析结果：`21104` 为 `ret=2,index=6,evaluation=1,quick=false`。
- 同一时段不存在 `18002`、`18012`、`18502`、`18090` 或 `21106`。

## 自动化测试

- `../.venv/bin/python -m unittest tests.test_arena_service -v`：13/13 通过。
- `../.venv/bin/python dragon_arena.py --self-test`：通过。
- `../.venv/bin/python -m unittest discover -s tests -v`：279/279 通过。
- `../.venv/bin/python -m py_compile app/services/arena_service.py app/services/auto_task_service.py dragon_arena.py tests/test_arena_service.py tests/test_auto_task_service.py`：通过。
- `git diff --check`：通过。

覆盖的关键断言：

1. `21104 ret!=0` 不增加完成轮数，不报告为缺失战斗结算。
2. 拒绝分支按原生逻辑重新读取 `21100`，并继续尝试下一名未挑战候选。
3. 本地 `attempted` 只在当前候选轮次去重；服务端仍有 `challenge=false` 时清空该集合并重新尝试。
4. 仅在服务端候选耗尽后发送 `21102`；其 `ret!=0` 显示实际返回码并延迟重试。
5. `battle=None` 且 `challenge.ret==0` 仍报告真正的“未收到服务端战斗结算”。
6. `21100` 刷新后候选重排时，按 `robot_id` 继续选择新候选的当前序号。
7. 当前候选全部被拒绝时，进入可取消的 750 ms 冷却，避免无等待地循环发送 `21104`。
8. 匹配成功或候选被拒后刚读取的 `21100` 会被下一次候选选择复用，不再发送重复状态请求。
9. UI 仅在成功 `21104` 与 `Battle_info` 到达时显示进入战斗；`ret!=0` 仅标记当前请求未进入战斗。

补充验证已执行：自动任务在 `GetDailyreward=true` 时依次发出 `21100`、`21110`、`12910(kind=6)` 与每个候选的 `12912(kind=6, uid)`；`test_dragon_reward_task_reads_ranking_and_likes_eligible_players` 断言本人和 `likenum` 中已有 UID 不会进入点赞请求，`test_dragon_reward_task_uses_only_the_remaining_like_slots` 断言仅补足剩余点赞次数，`test_dragon_reward_task_skips_likes_for_another_leaderboard_kind` 断言 `rankkind!=6` 时不发送点赞。

`../.venv/bin/python -m unittest tests.test_auto_task_service tests.test_arena_service tests.test_ui_app -v`：70/70 通过。
