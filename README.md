# 每日任务操作台（阶段 B）

## 目录

- `ui_app/`：本操作台的 Python 源码、UI 静态资源、配置、测试及解密 JS。
- `native_app/`：原生 Android 包的清单、DEX、资源、库和非 JS 解密数据。

以下命令均从项目根目录先进入 `ui_app/` 后执行：

```bash
cd ui_app
```

本机操作台不提供离线日常状态或离线动作分支。启动本身不会发起网络请求；登录、选择区服并刷新日常状态后，才会通过现有 `GameLoginClient`、账号服 `Logincheck` 和当前区服的真实 WebSocket 会话加载日常进度。

`daily_actions.py` 提供五项已接入动作（101、104、105、112、119）的统一包装器和显式的 `build_live_daily_action_runner()`；真实调用必须由上层先解析游戏服入口后构造该编排器。动作是否完成始终以服务端日常状态从未完成变为完成作为依据。

## 日常状态与奖励

```bash
../.venv/bin/python daily_quest.py status
../.venv/bin/python daily_quest.py claim
```

`status` 仅读取当前区服的日常/周常状态；`claim` 先读取状态，只请求服务端明确显示为已完成但未领积分的日常和周常任务，再分别领取已达到且未领取的每日、每周活跃奖励。Flask 每日任务批次也会在所有动作结束后自动执行同一检查。两条命令均默认追加写入 `daily_quest.jsonl`；可用 `--result-log PATH` 指定其他路径。

每条 JSONL 记录包含查询时间、区服、20 条日常的 `finished`、`getscore`、进度、每日/每周重置时间、两组已领取奖励和本次领取摘要。记录不包含账号令牌、游戏令牌、会话密码、WebSocket 地址或原始协议载荷。

## 启动

```bash
../.venv/bin/python ui_app.py --host 127.0.0.1 --port 8765
```

打开 `http://127.0.0.1:8765`。首次运行后，非敏感设置保存在 `config/ui-settings.json`。

开发时使用 `--reload` 监视源码并自动重启：

```bash
../.venv/bin/python ui_app.py --host 127.0.0.1 --port 8765 --reload
```

自动重启会中断正在执行的任务。

## 验证

```bash
../.venv/bin/python -m unittest discover -s tests -v
```
