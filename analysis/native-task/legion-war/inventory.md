# 资产与页面盘点

## 任务入口

| 页面/动作 | 原生可见证据 | 前置状态 | 用户可见结果 | 结论等级 |
| --- | --- | --- | --- | --- |
| 军团页 | `zh-Hans.json` 的 `legion` 文案包含“募兵”“升级”“升阶”“招募{{num}}次”。 | 军团功能已进入。 | 显示兵力、士气、军官与招募入口。 | `verified-static` |
| 围攻城堡 | `siege.tax` 和 `siege.town` 文案包含“前往突围”“每日税收”“当前城堡正遭到围攻”。 | 城堡已占领且围攻状态由服务端下发。 | 税收不可领，跳转突围。 | `verified-static` |
| 军官招募 | 横幅表 `id=1` 名称为“军官招募”。 | 横幅开放，持有道具 `80`。 | 单抽或五连结果及背包/军团刷新。 | `verified-static` + `verified-static`（协议实现） |
| 募兵升级 | `legion_recruit_troop` 按 `lv` 配置成本；文案 `upTroopsLv` 有上限/材料不足结果。 | 当前募兵等级存在成本且材料充足。 | 募兵成功或错误提示。 | `verified-static` + `verified-static`（协议实现） |
| 军官等级升级 | `officerLvUp` 有成功、无效 ID、未解锁、已达等级上限、费用不足。 | 选中已解锁军官。 | 等级刷新或错误提示。 | `verified-static`；协议 `unknown` |
| 军官升阶 | `officerRarityUp` 有成功、无效 ID、未解锁、已达稀有度上限、费用不足。 | 选中已解锁军官并持有升阶材料。 | 稀有度刷新或错误提示。 | `verified-static`；协议 `unknown` |

## 本地原始物

| 类型 | 路径 | 用途 | 结论等级 |
| --- | --- | --- | --- |
| 原生 APK 解包 | `../native_app/` | `global-metadata.dat`、`assets/PlayerAssets/`、`data.unityfs` 与资源表根。 | `verified-static` |
| 本地化页面与错误文案 | `../native_app/decrypted-task-data/zh-Hans.json` | 军团页、围攻税收、军官升级/升阶的页面能力与错误分支。 | `verified-static` |
| 军官配置 | `../native_app/decrypted-data/tables/legion_officer.json` | 233 条军官稀有度/专精/升阶材料行，17 个 `officer_id`。 | `verified-static` |
| 军官等级成本 | `../native_app/decrypted-data/tables/legion_officer_upgrade_cost.json` | 仅有 `lv=1, cost=""` 一行，不能推出等级升级成本或上限。 | `verified-static` |
| 招募横幅 | `../native_app/decrypted-data/tables/gacha_banner_v2.json` | 横幅 `1` 的单抽、五连、主消耗与最大次数。 | `verified-static` |
| 募兵配置 | `../native_app/decrypted-data/tables/legion_recruit_troop.json` | 100 个 `lv` 的兵力和前进成本。 | `verified-static` |
| 战术配置 | `../native_app/decrypted-data/tables/legion_strategy_group.json` | 392 个战术 ID 到品质 `rarity` 的映射。 | `verified-static` |
| 城堡配置 | `../native_app/decrypted-data/tables/siege_town.json` | 11 座城堡和每座 `tax_rewards`。 | `verified-static` |
| 协议客户端 | `legion_war.py` | 已实现的解析、载荷和日常顺序。 | `verified-static`（仓库实现） |
| 受控会话测试 | `tests/test_legion_war.py` | 分阶段响应、发送顺序、战术和成功字段断言。 | `verified-static`（测试） |
| 运行时抓包器 | `tools/frida_hook_legion_war.js`、`tools/capture_legion_war_frida.py` | 只读截获军团战相关的明文 `MsgHdr`。 | `verified-static`（采集能力） |

## 已定位的字段与配置

### 会话初始数据

`Game_data` 的军团数据位于字段 `28`：军团内部字段 `1` 是募兵等级、重复字段 `3` 是军官；每个军官的字段 `1/2/3` 分别为 `officer_id/level/rarity`。背包总量从 `Game_data.storage.items.list` 读取，即外层字段 `8`、内层 `1`、重复项字段 `3`，每项字段 `1/2` 为 `item_id/total`。

后台同步的 `20050` 直接更新军团信息；背包变更 `12602` 直接更新道具总量。详见 [message-map.md](message-map.md)。

### 原生规则

- 军官通过军官招募获取；重复/勋章相关材料用于升阶并提升专精。
- 所有军官的专精总和影响各兵种兵力；消耗功绩募兵提升总兵力。
- 每座已占领城堡每日可征税；围城中的城堡不可征收，须先突围。
- `gacha_banner_v2.id=1`：`cost1="1,1000"`、`cost2="5,5000"`、`maincost=80`、`maximum=10000`。

上述规则来自本地表和本地化文案，并不单独证明服务器的实时资格、数值修正或消息顺序。

## 未获得的原生页面源码

`../native_app/assets/PlayerAssets/` 中有 765 个 AssetBundle，原始 Puerts/JS 军官页面脚本未在当前已解密的文本资产中出现。因此不能从 UI 按钮回调静态确认军官等级升级或升阶的消息 ID、载荷字段、成功字段和最终同步包。该缺口已登记为 `unknown`，见 [open-questions.md](open-questions.md)。

