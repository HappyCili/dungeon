# 活动签到：验证

已执行：

```text
../.venv/bin/python -m unittest tests.test_auto_task_service tests.test_ui_app -v
```

覆盖目标：

- 从 `Game_data` 字段 `29` 解析活动。
- 仅保留 `ticket.status == 1` 且 `todaySigned == false` 的活动。
- 自动领取只发送 `21000`，不发送或等待 `21003`。
- 缺少登录快照时以可见跳过结果结束。

结果：`45` 项通过，`0` 项失败（包含 `tests.test_auto_task_service` 的签到状态解析与消息序列断言，以及 `tests.test_ui_app` 的自动任务页面/API 回归）。
