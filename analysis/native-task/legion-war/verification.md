# 验证记录

## 已完成的离线验证

| 验证项 | 方法 | 结果 |
| --- | --- | --- |
| 协议顺序 | `tests/test_legion_war.py` 的受控假会话预排 `20074 -> 20075 -> 20055 -> 20054 -> 20057 -> 20061 -> 20074 -> 20080 -> 19532 -> 20064`。 | 覆盖一座围攻城堡胜利后的税收、五连招募和募兵升级。 |
| 军官排序与槽位 | 单元测试断言按稀有度降序、等级降序、ID 升序，并按原生 `ma_legion_officer_slot_num=5` 截断后打包。 | 已覆盖。 |
| 战术选择 | 单元测试向服务端候选提供两种品质，断言选择最高品质。 | 已覆盖。 |
| 发包字段 | 单元测试断言招募次数/类别与打包军官 ID。 | 已覆盖。 |
| 战斗中间推送 | 单元测试覆盖 `20058/20059/20060` 和 `turn>0,result=1,options=[]` 过渡态，不发送空策略。 | 已覆盖。 |
| 残留战斗恢复 | 单元测试覆盖 `20075.ret=5 -> 20062 -> 20075` 的有限重试，以及已有 `20055/20061` 时直接恢复。 | 已覆盖。 |
| 失败时停止 | 抓包校验器拒绝“围攻失败后仍收税/招募/募兵升级”的时序。 | 校验器静态已覆盖；本次成功会话没有触发失败后续分支。 |

## 本次命令与结果

文档落盘后已执行：

```bash
../.venv/bin/python -m unittest tests.test_legion_war -v
../.venv/bin/python -m unittest discover -s tests -v
../.venv/bin/python -m py_compile legion_war.py tests/test_legion_war.py
git diff --check
```

结果：军团战专项测试、全量测试、Python 编译检查和 `git diff --check` 均通过。测试不连接游戏服务器，也不发送实际业务请求。

## 真实会话复核

2026-08-04 在本地登录会话中执行 `legion_war` 自动任务，完整链路成功：

| 项目 | 结果 |
| --- | --- |
| 任务状态 | `succeeded` |
| 围攻胜场 | `1` |
| 税收领取 | `true` |
| 军官招募 | `5` 次（五连） |
| 募兵升级 | `true` |
| 服务端失败数 | `0` |
| 关键修复 | `20054` 仅发送 5 名军官：`[205,101,102,103,104]`；此前 12 名会触发 `ret=20/officer num exceed` |

精简收发时序保存在 [packet-trace.jsonl](packet-trace.jsonl)，原始会话日志为 `logs/websocket_raw/game_session/2026-08-04.jsonl`。真实帧还确认 `20055` 可能早于 `20075` 响应到达，且战术选择后会出现 `20058/20059/20060` 与空候选过渡态。

## 运行时复核步骤

在已登录的模拟器中，仅执行一次正常的军团日常，并保留完整 JSONL：

```bash
ANDROID_SERIAL=emulator-5554 ./tools/start_frida_server.sh
./tools/run_frida_legion_war_capture.sh
../.venv/bin/python tools/validate_legion_war_capture.py /tmp/dungeon4_legion_war_frida.jsonl
```

验收条件：

1. 收到 `legion_probe_ready`，且收发两个 hook 都成功安装。
2. 围攻同步先于突围；突围响应城堡 ID 与请求一致。
3. `20054` 的军官顺序与最新 `20050` 一致，`20057` 只选择当前 `20055` 的最高品质候选。
4. `20061.field1 == 1` 后才再次同步围攻并尝试税收。
5. `20080`、`19532`、`20064` 均拥有对应成功响应；任何失败围攻之后没有三者的 C->S 请求。

## 军官升级/升阶的单独采集

不要把该操作与日常任务混采。先打开军官页并等待空闲，再分别只点一次“升级”或“升阶”，保留点击前后 10 秒内的全部收发帧、按钮选择的 `officer_id`、页面显示的材料和结果。只有发现原生客户端实际发送点和成功后的军团/背包同步后，才能把消息、载荷和终态写入 [message-map.md](message-map.md)。
