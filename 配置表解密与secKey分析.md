# 配置表解密与 secKey 来源分析

## 范围与结论

本文说明本仓库内 **Unity 配置表 TextAsset**（如 `mapareas`、`dungeon`、`item`）的加密方式，以及与 **游戏服 WebSocket Pack1** 密钥的区别。依据包括：

- 仓库内接口分析类 md（尤其是 [活动签到接口分析.md](活动签到接口分析.md)）
- `ui_app/decrypted-js/main.js` 静态逻辑
- `ui_app/harvest_fief.py` 中已验证的 Pack1 / DES 实现
- `native_app/assets/PlayerAssets/playerassets.json`
- `native_app/decrypted-data/data.unityfs` 中抽出的 TextAsset 密文
- 对已知通信密钥与元数据候选密钥的解密探测

**主要结论：**

1. 配置表与游戏服业务包共用 **同一族 Pack1 封装**（长度前缀 + 可选 zlib + DES-ECB + Base64），但 **使用不同密钥**。
2. 通信引导密钥 `getMK()` 可从客户端公式还原为 `45985633`（即 `SOCKET_PACK_KEY`），仅用于解开 `Pack_password`。
3. 配置表解密密钥在客户端代码中记为 **`BuildTimeData.secKey`**，经 `DecryptData` 传给 `CS.Sec.Pack1Decode`。
4. 静态安装包内可确认 **`encryptPaths` 包含 `Assets/Data`**，但 **APK 明文配置里没有 `secKey` 字段**；通信密钥与 8 位数字爆破均无法解表。
5. **2026-07-21 运行时 hook 已拿到 `secKey`**（见下文「运行时结论」）；已用其解密 `mapareas` 写入 `native_app/decrypted-data/mapareas.json`，聚宝地图名称可正常解析。

本文为本地静态分析与离线探测记录，不包含对线上服务的请求，也不展开可用于重放或伪造通信的操作步骤。

---

## 相关项目文档

| 文档 | 与本文关系 |
| --- | --- |
| [活动签到接口分析.md](活动签到接口分析.md) | 定义 WebSocket Pack1 流程、会话密码建立、`getMK()` 用途；**不写具体密钥材料**，也不涉及配置表 `DecryptData`。 |
| [庄园收获接口分析.md](庄园收获接口分析.md) / [庄园收获进度.md](庄园收获进度.md) | 复用同一 Pack1 会话；`harvest_fief.py` 实现与自测。 |
| [游戏入口解析.md](游戏入口解析.md) | 登录 → `Pack_password` → `Login_reunique`；细节指向签到文档。 |
| [dragon_arena_websocket_analysis.md](dragon_arena_websocket_analysis.md) | 实战抓包：前几条未加密，之后业务全程 Pack1。 |
| [每日任务功能清单与实施计划.md](每日任务功能清单与实施计划.md) | 说明 `decrypted-task-data` 等已解密表及 manifest；**未记载**解密所用 `secKey`。 |
| [README.md](README.md) | `ui_app`（含解密 JS）与 `native_app`（含解密数据）的目录分工。 |

**文档缺口：** 仓库 md 对「通信 Pack1」描述充分，对「配置表 TextAsset / secKey」几乎没有专门章节。本文补这一块。

---

## 三类密钥（必须分开）

```text
1) getMK() / SOCKET_PACK_KEY（固定引导密钥）
   └─ 用途：Pack1Decode(Pack_password 载荷) → 得到会话密码

2) 会话密码（每次登录由服务端下发，setMsgPwd 保存）
   └─ 用途：之后游戏服 WebSocket 业务消息的 Pack1 加解密

3) BuildTimeData.secKey（构建 / 更新配置）
   └─ 用途：DecryptData → 解密 Assets/Data（及 encryptPaths 下）配置表 TextAsset
```

| 密钥 | 是否已在仓库落地 | 能否解 mapareas / dungeon 密文 |
| --- | --- | --- |
| `SOCKET_PACK_KEY` = `45985633` | 是（`harvest_fief.py`） | **否**（已实测） |
| `ACCOUNT_PACK_KEY` = `46154569` | 是（账号 HTTP 侧） | **否**（已实测） |
| 会话密码 | 运行时，不入库 | 与配置表无关 |
| `secKey` | **未找到明文** | **目标密钥** |

---

## Pack1 算法摘要

与 [活动签到接口分析.md](活动签到接口分析.md) 及 `NodeCrypto` / `harvest_fief.pack1_*` 一致：

```text
明文字节
  → 若长度 > limit（默认 100）：zlib deflate（BEST_COMPRESSION）
       并写 4 字节小端「压缩前长度」
  → 否则：4 字节小端 0 + 原文
  → 若密码长度 ≥ 8：DES-ECB，密钥为密码 UTF-8 前 8 字节，PKCS#7 填充
  → Base64 文本
```

- **通信路径**：`CryptUtil` / `NodeRuntime` → `NodeCrypto.Pack1Encode|Decode`，密码为会话密码或 `getMK()`。
- **配置表路径**：`CS.Sec.Pack1Decode(密文, BuildTimeData.secKey)`（原生侧），外壳与上相同，**密码为 secKey**。

已知明文 `dungeon.json` 与包内 dungeon TextAsset：再编码后 **Base64 长度一致**（约 4832），说明表数据确为「压缩 + Pack1」，而非另一套完全不同的算法。

---

## 通信引导密钥 getMK（已还原）

### 客户端公式

`decrypted-js/main.js`：

```text
const _e = 4598
getMK() { return (1e4 * _e + 5633).toString() }
```

```text
4598 × 10000 + 5633 = 45985633
```

与实现一致：

```text
// harvest_fief.py
SOCKET_PACK_KEY = "45985633"
```

### 用途（与签到文档一致）

1. WebSocket 登录阶段收到 `Pack_password`；
2. `Pack1Decode(t.p, getMK())` 得到会话密码字符串；
3. `SocketManager.setMsgPwd(...)`；
4. 后续业务包使用**会话密码**做 Pack1。

**来源性质：** 客户端 **JS 编译期常量**，不是服务器下发，也不在 `playerassets.json` 中。  
签到类文档刻意不展开密钥材料；工程代码因自动化需要已硬编码同值。

### 账号侧密钥（对照）

```text
ACCOUNT_PACK_KEY = "46154569"
```

用于账号服请求中的 Pack1 字段等。JS 明文中未直接出现该数字串；与 `getMK`、与表 `secKey` 均不是同一用途。

---

## 配置表解密链路 DecryptData

### 调用链

```text
loadTableByName("mapareas" | "dungeon" | "item" | …)
  → 异步加载 TextAsset：Assets/Data/<表名>.json
  → 文本内容多为 Base64 密文（不是明文 JSON）
  → DecryptData(资源路径, text)
       data = CS.TJ.Updater.GetBuildTimeData()
       for pathPrefix in data.encryptPaths:
           if 资源路径.startsWith(pathPrefix):
               return CS.Sec.Pack1Decode(text, data.secKey)
       return text   # 未命中加密前缀则原样
  → JSON.parse(结果)
```

### encryptPaths（已在包内确认）

`native_app/assets/PlayerAssets/playerassets.json`：

```json
"encryptPaths": ["Assets/JavaScripts", "Assets/JavaScripts", "Assets/Data"],
"channel": "zs2605",
"version": "1.4.1"
```

因此 **`Assets/Data/*` 下配置表会走 `Pack1Decode(..., secKey)`**。  
同文件中 **没有 `secKey` 字段**。

### BuildTimeData 与 secKey

| 线索 | 内容 |
| --- | --- |
| JS 用法 | `GetBuildTimeData().secKey`、`.encryptPaths`、`.version`、`.resVer`、`.splitMode` 等 |
| 元数据字段名（节选） | `updateInfoURL`、`downloadURL`、`resVer`、`splitMode`、`encryptPaths`；**未在相邻字段列表中看到表用 secKey 的明文常量** |
| `LoadBuildTimeData` | Updater 负责加载构建信息；具体落盘格式 / 文件名在静态搜索中未完全钉死 |
| 易混淆 | 元数据中另有 WebSocket 握手用的 `secKey`（`CreateSecKeyAndSecWebSocketAccept`），与配置表无关 |

**当前判断：** 设计上 `secKey` 属于运行时 `BuildTimeData`；**本仓库静态文件未能提供其值**。可能存在于原生实现、未收录的构建产物、热更配置或仅在运行时注入——均待运行时或进一步原生逆向确认。

---

## 聚宝之地与 mapareas

### 协议与表

| 项目 | 内容 |
| --- | --- |
| 状态 / 扫荡消息 | `Map_treasure_info` (15570)、`Map_treasure_sweep` (15571)、`Map_treasure_clear_result` (15572) |
| 可扫荡地图 id 列表 | 服务端 `TreasureData.areas` |
| 中文名权威表 | 设计表 **`mapareas`**（`id` + `name` 等） |
| 客户端聚宝区域特征 | `mapareas.worldid == 2`（大地图 / 聚宝 UI） |

业务代码侧（已实现，不依赖 secKey）：

- 协议客户端：`ui_app/treasure_area.py`
- 作业与托管日志：`ui_app/app/services/treasure_service.py`（event `treasure_area`）
- 名称解析：`ui_app/id_descriptions.treasure_area_name`  
  优先读 `native_app/decrypted-data/mapareas.json`（及 map/maps 回退），否则 `未知聚宝地图（ID …）`

### 资源现状

| 路径 | 状态 |
| --- | --- |
| `data.unityfs` 内 TextAsset `mapareas` | 存在，内容为 Base64 密文 |
| `native_app/decrypted-data/mapareas.json` | 若存在且内容以密文 Base64 开头，则**尚不是可用明文表**（误落盘密文时不可当名称表） |
| `dungeon.json` / `item.json` 等 | 已有明文；manifest 标注来自同一 `data.unityfs`，**未记录 secKey** |

物品文案中虽有「聚宝之地·尖啸山谷」等名称，但 **无 area_id 权威映射**，不能据此编造 id→名称表（见项目 skill `resolve-id-descriptions`）。

---

## 离线探测摘要

在本地对 `dungeon` TextAsset 密文进行的探测（结果均为失败，用于收窄范围）：

| 探测 | 结果 |
| --- | --- |
| `SOCKET_PACK_KEY` / `ACCOUNT_PACK_KEY` | 无法解密 |
| 仿 `getMK` 的 `a×10000+b` 邻域约 1400 组 | 无命中 |
| `global-metadata` 中约 6900+ 个 8 字符候选 | 无命中 |
| 不做 DES、直接 zlib | 失败（密文不是裸压缩流） |
| 明文包长度 + PKCS 填充 | 与密文长度一致（3624），结构像 Pack1 |

首块对照（在「明文 = 当前仓库 dungeon.json + zlib level 9」假设下）：

```text
假定明文首块：77 50 00 00 78 da ed 9c
密文首块：    e8 33 60 2b c5 3c 8f 60
```

说明：算法族匹配；**密钥不在上述候选集合中**。若历史解密时的 dungeon 明文与当前包密文版本不完全一致，首块假设会偏差，但不改变「通信密钥解不开当前密文」的事实。

---

## 已解密表与 manifest

各目录 `manifest.json` 典型字段：

```text
source_bundle: .../data.unityfs
assets: [{ name, path, bytes, sha256 }, ...]
```

已有明文表分布示例：

- `native_app/decrypted-data/`：`item`、`dungeon`、`rewardbox` 等  
- `native_app/decrypted-task-data/`：`daily_quest`、`quests`、`systemfunc` 等  
- `native_app/decrypted-tavern-data/`：`heroes`、`heroname` 等  

**含义：** 工程曾成功得到明文，但 **解密密钥与步骤未写入 manifest 或 md**。不能从「已有明文」反推「仓库内已有 secKey」。

---

## 与工程代码的衔接（解密成功之后）

1. 使用与 `harvest_fief.pack1_decode` 等价的流程，密钥为 **`secKey`**，解密 TextAsset 文本。  
2. 将明文写入：  
   `native_app/decrypted-data/mapareas.json`  
   （JSON 数组/对象需含 `id` 与 `name`，与 `_name_table` 约定一致。）  
3. 可选：仅保留或标注 `worldid == 2` 的聚宝地图行，便于校验。  
4. 无需改协议：`treasure_area_name` / 聚宝 UI / `treasure_area` 日志会自动解析中文名。  
5. 同步更新本目录 manifest 的 sha256（若项目有维护习惯）。

托管日志 event 名：`treasure_area`（见 `logging_store.EVENT_SPECS` 与 skill `project-logging`）。

---

## 建议的后续取证（按优先级）

1. **模拟器 + Frida hook（推荐）**  
   包名 `com.zygames.dungeon4`。脚本与步骤见：  
   - [tools/frida_seckey_README.md](tools/frida_seckey_README.md)  
   - [tools/frida_hook_seckey_il2cpp.js](tools/frida_hook_seckey_il2cpp.js)  
   在 `GetBuildTimeData` / `Sec.Pack1Decode` 上读取 `secKey` 或 password 参数。  

2. **完整构建与热更产物**  
   对比是否存在带 `secKey` 的 BuildTime 配置；当前 `playerassets.json` 仅有 `encryptPaths`。  

3. **原生 `LoadBuildTimeData` 逆向**  
   注意：元数据中 `BuildTimeData` 字段列表**未出现** `secKey` 成员名，运行时仍可能有动态字段或其它类型承载密钥。  

4. **避免无效方向**  
   通信密钥（`45985633`）无法解表；对 metadata 随机字符串枚举性价比低。  
   8 位数字 DES 爆破仅为形态探测，不替代运行时 hook。  

---

## 静态分析边界

- 未确认当前生产环境实际 `secKey` 值、热更是否轮换密钥。  
- 未确认 `CS.Sec.Pack1Decode` 与 `NodeCrypto.Pack1Decode` 在边界条件（空密钥、非标准填充）上是否 100% 一致；通信路径已用后者验证，表路径名义上走前者。  
- 已解密 JSON 的历史生成方式未在仓库文档中记录，本文不臆测具体工具链。  
- 本文不提供对线上服务或未授权环境的密钥获取指引。

---

## 运行时结论（Frida hook，2026-07-21）

在模拟器 `emulator-5554` 上对 **`com.zygames.dungeon4`** attach，经 `frida-il2cpp-bridge` 调用：

```text
TJ.Updater.GetBuildTimeData()
```

得到：

| 字段 | 值 |
| --- | --- |
| `channel` | `zs2605` |
| `version` | `1.4.1` |
| `resVer` | `7.2.7` |
| `encryptPaths` | `Assets/JavaScripts`, `Assets/Data` |
| **`secKey`** | **`RO#4k%m1`**（DES 取前 8 字符，即整串 8 字节） |

校验：

- `pack1_decode(dungeon 密文, secKey)` 与仓库已有 `dungeon.json` **字节一致**
- 解密 `mapareas` 共 204 行；其中 **`worldid == 2` 聚宝地图 9 个**：

| id | name |
| --- | ---: |
| 230101 | 尖啸山谷 |
| 530101 | 沉默之城 |
| 730101 | 石化森林 |
| 12211 | 蚀骨之野 |
| 13211 | 锈蚀荒野 |
| 13212 | 符印遗迹 |
| 14211 | 帝国军旧址 |
| 15211 | 扭曲丘壑 |
| 15212 | 晦沉墓园 |

落盘：`native_app/decrypted-data/mapareas.json`  
脚本：`ui_app/tools/frida_capture_seckey.js`、`tools/frida_seckey_README.md`

### 全量解密（同日后续）

脚本：`ui_app/tools/decrypt_data_tables.py`（`secKey` 批量解 `data.unityfs`）

| 输出目录 | 内容 | 数量（约） |
| --- | --- | ---: |
| `native_app/decrypted-data/tables/` | 命名设计表（含 manifest） | 349 |
| `native_app/decrypted-data/zone-layouts/` | 纯数字命名的地图/区域布局 JSON | 666 |
| 历史权威路径 | `item`/`dungeon`/`mapareas`/日常/酒馆/律文等 | 同步更新 |

`data.unityfs` 内 **1058+** 条可解 JSON TextAsset 均已导出；客户端 `DecryptData` 仅作用于 `Assets/Data/*`，与此一致。

**说明：** `secKey` 属构建/渠道配置，热更后可能变化；若解密失败应重新 hook。勿将密钥提交到公开仓库（本仓库为本地自动化工程则按需自控）。

---

## 修订记录

| 日期 | 说明 |
| --- | --- |
| 2026-07-21 | 初版：对照项目 md、客户端 DecryptData/getMK、playerassets.encryptPaths、Pack1 探测与聚宝 mapareas 衔接说明。 |
| 2026-07-21 | Frida hook 成功：`secKey=RO#4k%m1`；解密 mapareas（9 处聚宝图）并落盘。 |
