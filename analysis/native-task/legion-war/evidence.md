# 证据台账

## 结论等级定义

- `verified-static`：当前工作区中的原生解密表/文案，或仓库内可复现的协议实现与测试，能够直接支持该条结论。
- `verified-runtime`：原始客户端的只读采集帧已验证，且已脱敏保存为本目录的 `packet-trace.jsonl`。
- `inferred`：由多项证据推导，不能作为额外发包依据。
- `unknown`：尚无足以确定消息、字段或终态的证据。

## 主要结论

| 主张 | 等级 | 证据 |
| --- | --- | --- |
| 原生军团页展示募兵、招募、升级与升阶能力。 | `verified-static` | `../native_app/decrypted-task-data/zh-Hans.json:345-387`。 |
| 原生城堡页在被围攻时阻止征收并引导突围。 | `verified-static` | `../native_app/decrypted-task-data/zh-Hans.json:389-412`；`rule_description.json:1191-1192`。 |
| 围攻、税收、招募、募兵升级分别使用 `20074/20075/20080/19532/20064`，并按本文状态机顺序处理。 | `verified-runtime` | `legion_war.py`；`tests/test_legion_war.py`；`packet-trace.jsonl`。 |
| 突围战需以 `20061.field1 == 1` 作为胜利终态，失败后禁止继续税收、招募和募兵升级。 | `verified-static`（实现与校验器） | `legion_war.py:559-591, 649-657`；`tools/validate_legion_war_capture.py:184-206, 237-255`。 |
| 招募横幅为 `1`，单抽/五连耗费为 `1000/5000`，主消耗道具为 `80`。 | `verified-static` | `gacha_banner_v2.json:3-21`；`legion_war.py:49-52, 307-315`。 |
| 原生军官槽位宏 `ma_legion_officer_slot_num` 为 `5`；调试会话中发送 12 名军官时服务端返回 `ret=20` 和 `officer num exceed`，修复后请求截断为 5 名。 | `verified-runtime` | `../native_app/decrypted-data/tables/macros.json:1125-1128`；`verification.md` 的真实会话记录；`legion_war.py:select_battle_officers`。 |
| `20075` 响应前可能先收到 `20055`，战术选择后会收到 `20058/20059/20060`；空候选过渡态后再收到 `20061.result=1`。 | `verified-runtime` | `packet-trace.jsonl`；`legion_war.py:_wait_for_battle_state`。 |
| 募兵等级配置为 1-100 级；等级成本从 `legion_recruit_troop.cost` 读取。 | `verified-static` | `legion_recruit_troop.json:1-44` 及完整 100 行；`legion_war.py:331-344, 628-641`。 |
| 军官等级升级和升阶为独立页面操作，且均具有成功、无效 ID、未解锁、上限、费用不足分支。 | `verified-static` | `zh-Hans.json:3718-3731`。 |
| 军官等级升级/升阶的消息 ID、载荷、成功字段和最终同步包。 | `unknown` | 当前已解密文本资产未发现页面发送点；`legion_officer_upgrade_cost.json` 只含空成本占位行。 |

## 原生表校验指纹

| 文件 | 行数/条数 | SHA-256 |
| --- | ---: | --- |
| `legion_officer.json` | 233 | `9f1dbeee7a6dd41dc96058d24321ef9d78be16e1ced8bffdd56f17b515857175` |
| `legion_officer_upgrade_cost.json` | 1 | `31803e829de9fbe83852221c1d18551e19b49386d988f43af061561877b69725` |
| `legion_recruit_troop.json` | 100 | `1170f4a34834d2519a5e37e7b81fe950fa17507a70bc9b89d07a970c04efc538` |
| `legion_strategy_group.json` | 392 | `69cf917c954786a84b94ae68ce0e96e5f9acfb22fca67e4d93be4ffc0eef124b` |
| `gacha_banner_v2.json` | 208 | `c9c733e25f3ea02d767d00bbf2fa102f7e26e6b408e39531b687dcb45d581457` |
| `siege_town.json` | 11 | `23503c59ce1fd884ca8c8068588e3a91feecab729de87f1ab942a1e837e54d86` |

## 运行时采集能力

`tools/frida_hook_legion_war.js:15-26` 注册了 `19532`、`20050`、`20054`、`20055`、`20057`、`20058`、`20059`、`20060`、`20061`、`20062`、`20064`、`20074`、`20075`、`20080`；`tools/frida_hook_legion_war.js:201-260` 在 `TJ.TJWebSocket.Send` 和 `OnWebsocketMessage` 只读截获明文 `MsgHdr`。采集器和校验器的使用说明在 `tools/frida_seckey_README.md:166-185`。

脱敏运行时摘要见 [packet-trace.jsonl](packet-trace.jsonl)；完整原始日志保留在 `logs/websocket_raw/game_session/2026-08-04.jsonl`。
