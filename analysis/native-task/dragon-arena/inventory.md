# 龙痕竞技场任务清单

## 入口与可见结果

- UI 入口：`app/services/arena_service.py` 的 `ArenaService.run`。
- 任务入口：`DragonArenaClient.run_round` 发送 `Scararena_challenge(21104)`。
- 2026-07-29 可见结果：首轮 `21104 ret=2`，旧逻辑将候选拒绝误报为停止；修复后先完成本地候选轮次，服务端仍有 `challenge=false` 时重新尝试而非匹配刷新。

## 证据资产

| 资产 | 路径 | 结论等级 |
| --- | --- | --- |
| 受控运行摘要 | `logs/dragon_arena/2026-07-29.jsonl` | verified-runtime |
| 原始共享会话帧 | `logs/websocket_raw/game_session/2026-07-29.jsonl` | verified-runtime |
| 原生 JS 模块 | `decrypted-js/main.dragon-arena.js` | verified-static |
| 原生 Android 解包数据 | `../native_app/` | verified-static |

原始帧含会话材料；本目录不复制其 Base64 载荷。
