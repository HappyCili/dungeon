# 验证记录

## 离线验证目标

本次验证不连接游戏服务器、不发送业务包，只检查现有解码器、单元测试、文档链接和已保存 JSONL 的解析结果。

| 检查项 | 命令/方法 | 状态 | 结果 |
| --- | --- | --- | --- |
| 普通竞技场协议自检 | `../.venv/bin/python knight_arena.py --self-test` | 已通过 | `knight_arena self-test passed` |
| 编队同步、自动任务与日常任务单元测试 | `../.venv/bin/python -m unittest tests.test_treasure_area tests.test_auto_task_service tests.test_daily_actions` | 已通过 | 67 项通过，0 失败 |
| 全量 Python 单元测试 | `../.venv/bin/python -m unittest discover -s tests` | 已通过 | 294 项通过，0 失败；包含 `grave_abyss self-test OK` |
| JSONL 摘要解码 | 使用 `ProtoReader`、`decode_arena_info`、`decode_arena_challenge_response`、`decode_battle_info` 读取已有抓包 | 已完成 | 见 `evidence.md` 与 `packet-trace.jsonl` |
| 文档结构检查 | 检查本目录所需 Markdown 文件与相对链接 | 已通过 | 9 个产物齐全，相对链接存在 |
| 工作树空白检查 | `git diff --check`；另用 `rg` 检查未跟踪文档 | 已通过 | 无尾随空白 |

## 已核验的抓包结论

- `19800` 快照中的缺失 protobuf 标量按 codec 默认值解码为 `challenge_num=0`、`getdailyreward=false`。
- 运行时曾收到 `18002 Battle_info(battle_type=5, ret=0)`，随后发出 `18010`，但 `18012` 返回 `ret=7`。
- 该样本不含 `19818 Arena_challenge_result`，因此任何验证脚本均不应将它计作成功完成场次。
