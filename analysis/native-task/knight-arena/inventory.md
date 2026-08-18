# 资产清单

## 分析目标

普通竞技场（骑士比武）完整流程：每日奖励、免费次数、候选选择、进攻编队、挑战、战斗握手和结算。

## 本地资产

| 类型 | 路径 | 用途 |
| --- | --- | --- |
| 原生模块与 codec | `decrypted-js/main.dragon-arena.js` | `ArenaModule`、`ArenaInfo`/挑战/结算 codec、消息 ID 枚举 |
| 消息 ID 映射 | `dragon_arena_business_map.py` | `Arena_*`、`Battle_*`、`Hero_*` 常量 |
| 普通竞技场实现 | `knight_arena.py` | 查询、选人、编队同步、挑战和战斗等待逻辑 |
| 共用战斗握手 | `dragon_arena.py` | `Battle_C2S_start` 编码、开战后控制 |
| 恢复状态判定 | `session_recovery.py` | `battle_type=5` 的备战/战斗中区分 |
| 日常编排 | `app/services/auto_task_service.py`、`daily_actions.py` | 当前免费上限策略和任务 109 计数口径 |
| 单元测试 | `tests/test_treasure_area.py`、`tests/test_daily_actions.py` | 编队槽位 4、挑战计数不要求胜利 |
| 本地抓包 | `logs/websocket_raw/knight_arena/2026-07-24.jsonl` | Arena 状态、挑战请求、`Battle_info` 和开战响应 |

## 关键静态入口

| 位置 | 结论 |
| --- | --- |
| `decrypted-js/main.dragon-arena.js:1225-1235` | `ArenaModule` 注册 `19800` 至 `19824` 的处理器；`OnArenaInfo`、每日奖励、挑战、对手队伍、结算和积分变更的状态处理均在此处。 |
| `decrypted-js/main.dragon-arena.js:3252-3290` | `Bf/Uf/Lf/xf/Gf/Ff/Vf/jf/Hf/Wf/qf/Kf/$f/Zf` 定义普通竞技场请求/响应字段。 |
| `decrypted-js/main.dragon-arena.js:6184` | 队伍界面 `OnArenaSetTeam` 将已保存的挑战数据发送为 `Arena_challenge`。 |
| `decrypted-js/main.dragon-arena.js:7801` | `Arena_*` 消息 ID 的原生枚举。 |

## 资源指纹

| 文件 | SHA-256 |
| --- | --- |
| `decrypted-js/main.dragon-arena.js` | `1b40b8d854394686aed49c809a64a32f6d2acda222d56526f4c1a7729cdcb8ec` |
| `knight_arena.py` | `58d5bd6e70779ceb1f80c5b5466284d8611e99965913154195506a37cd9e9601` |
| `dragon_arena.py` | `139aeebb8b5d4950ddf964edf545472e46e2cc3f687cc3452ce72de8d90bb14a` |
| `dragon_arena_business_map.py` | `fabdf515b4b3bbf0b0b115d2f78ca3ddec58da286866057ad67750e91f56b230` |
| `session_recovery.py` | `5e3ebe7457ef629e29728190fdeda4efb62728f905f775c2bd1ef20817f3c92d` |
| `app/services/auto_task_service.py` | `915b942b0021554f7e340bc2bc20682c95ab1131887960467a7c7812f8e63184` |
| `logs/websocket_raw/knight_arena/2026-07-24.jsonl` | `e731e63dae1511fe7c16328d3b95fabd640f01ee1216dcb0bcfabe38043224f5` |
