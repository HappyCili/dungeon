# 用模拟器 + Frida 抓取 secKey

## 结论（能否用 hook）

**可以。** 配置表密钥在运行时由 `GetBuildTimeData().secKey` 交给 `Sec.Pack1Decode`；静态 APK 里没有明文，**模拟器里 hook 是最直接的办法**。

当前本机状态（2026-07-21 已装主机侧）：

| 项 | 状态 |
| --- | --- |
| `adb` | 已安装 |
| 已连接设备/模拟器 | **无**（`adb devices` 为空） |
| `frida` / `frida-tools` | **已安装** `.venv` / `venv` → **17.16.3** |
| `frida-il2cpp-bridge` | **已安装** `ui_app/node_modules/` |
| `frida-server` 二进制 | **已下载** `ui_app/tools/frida-server/`（arm/arm64/x86/x86_64） |
| 游戏包名 | **`com.zygames.dungeon4`** |

主机侧就绪；**真正 hook 仍需**：启动可 root 模拟器/真机 → 推送并运行 frida-server → 安装游戏。

---

## 环境准备

### 1. 主机（已完成可跳过）

```bash
# 建议用项目 venv
cd /Users/max/Downloads/dungeon4_M521957
.venv/bin/pip install frida-tools

# IL2CPP 反查（推荐，在 ui_app 下）
cd ui_app
npm init -y
npm i frida-il2cpp-bridge
```

### 2. 模拟器 / 真机

- 任意可 root 的 Android 模拟器（或已 root 真机），或可调试的 repack 包  
- 安装游戏 **`com.zygames.dungeon4`**  
- 推送与主机版本一致的 frida-server（本仓库已备好 17.16.3）并运行：

```bash
# 一键：按设备 ABI 推送并启动
ui_app/tools/start_frida_server.sh

# 或手动（以 arm64 为例）：
adb push ui_app/tools/frida-server/frida-server-17.16.3-android-arm64 /data/local/tmp/frida-server
adb shell "chmod 755 /data/local/tmp/frida-server && su -c /data/local/tmp/frida-server &"
adb devices   # 应能看到 device
.venv/bin/frida-ps -U   # 应能列出进程
```

### 3. 启动 hook

**推荐（IL2CPP，可打出 secKey）：**

```bash
cd /Users/max/Downloads/dungeon4_M521957/ui_app

../.venv/bin/frida -U -f com.zygames.dungeon4 \
  -l node_modules/frida-il2cpp-bridge/dist/index.js \
  -l tools/frida_hook_seckey_il2cpp.js \
  --no-pause
```

**轻量探测（无 bridge，信息较少）：**

```bash
../.venv/bin/frida -U -f com.zygames.dungeon4 -l tools/frida_hook_seckey.js --no-pause
```

进游戏等到资源/表加载（主城或打开聚宝之地），日志中查找：

```text
[secKey] BuildTimeData.secKey = "..."
[secKey] 疑似 secKey/password = "..."
```

把打印出的字符串用于：

```text
pack1_decode(mapareas密文, secKey) → native_app/decrypted-data/mapareas.json
```

---

## Hook 点说明

```text
客户端加载表
  loadTableByName("mapareas")
    → DecryptData(path, text)
         → data = TJ.Updater.GetBuildTimeData()
         → CS.Sec.Pack1Decode(text, data.secKey)   ← 第二参数即表密钥
```

| Hook | 目的 |
| --- | --- |
| `TJ.Updater.GetBuildTimeData` | dump 整个 BuildTimeData（含 secKey / encryptPaths） |
| `Sec.Pack1Decode` | 每次解表时的 password 参数（即 secKey） |
| `Sec.get_MsgKey` | 对照通信相关 MsgKey（通常不是表密钥） |

---

## 备选（不 hook 也能用）

1. **内存 dump**：游戏加载表后，在内存里搜 `尖啸山谷` 或 JSON `"name"`，可能直接抠出已解密的 `mapareas` 片段（无需 key）。  
2. **明文表导出**：若能改客户端或用 GM/调试包，在 `JSON.parse` 前把 `DecryptData` 返回值写文件。  

---

## 与静态分析的关系

- 静态：只有 `encryptPaths`、密文 TextAsset、算法 Pack1，**没有 secKey 明文**。  
- Hook：在运行时读 C# 对象字段 / 函数参数，**一次成功即可永久解密本地表**。  

拿到密钥后建议：

1. 写入本地笔记或环境变量（勿提交公开仓库若有安全顾虑）  
2. 解密 `mapareas` 等到 `native_app/decrypted-data/`  
3. 现有 `treasure_area_name()` 会自动解析中文名  

---

## 代理可代操作的条件

当你：

1. 模拟器已开，`adb devices` 有设备  
2. frida-server 在跑，`frida-ps -U` 正常  
3. 游戏已安装  

即可让本机代理执行 `frida -U -f com.zygames.dungeon4 ...` 并回收日志中的 secKey。  
**当前不满足条件时，无法凭空连接不存在的模拟器。**

---

## Battle_info / 聚宝挂起战（Frida 实测）

包名：`com.zygames.dungeon4`。传输层 IL2CPP 类：

| 方法 | 签名 | 方向 |
| --- | --- | --- |
| `TJ.TJWebSocket.Send` | `Task Send(Puerts.ArrayBuffer)` | C→S |
| `TJ.TJWebSocket.OnWebsocketMessage` | `void OnWebsocketMessage(byte[])` | S→C（解密后 MsgHdr） |

业务（`onBattleInfo` / `tryEnterBattle`）在 **Puerts/JS**，不在 Assembly-CSharp。

抓包脚本：

```bash
# frida-server 已启动、游戏已登录
cd ui_app
./tools/run_frida_battle_capture.sh
# 游戏内进聚宝/开战；看终端 [battle] 与 *** Battle_info (18002) ***
```

MsgHdr：`field1` varint = message_id（与 `encode_message_header` 一致）。  
`18002` = Battle_info。自动化侧已对齐客户端进图序列：`Client_talog(enter_map/scene_change)` + `Evt_script_trigger` + 加长监听（见 `treasure_farm._signal_stage_ready`）。

**注意**：手机会话与自动化会话互斥（Kickout）。Frida 抓到的 18002 **不能**直接注入另一 WebSocket；只能用来对照协议与完善自动化就绪序列。挂起战仍建议客户端打完再刷，或依赖就绪序列让服务端向自动化会话补发 18002。

---

## 军团战流程校验

`tools/frida_hook_legion_war.js` 只读截获 `TJ.TJWebSocket` 的军团战 `MsgHdr`，并保存以下协议：`20050`、`20054`、`20055`、`20057`、`20061`、`20064`、`20074`、`20075`、`20080`、`19532`。不会改写收发参数或客户端状态。

```bash
cd /Users/max/Downloads/dungeon4_M521957/ui_app
ANDROID_SERIAL=emulator-5554 ./tools/start_frida_server.sh
./tools/run_frida_legion_war_capture.sh
# 在模拟器游戏内完成一次军团战日常，然后按 Ctrl+C
```

默认日志为 `/tmp/dungeon4_legion_war_frida.jsonl`，停止捕获后会自动执行：

```bash
../.venv/bin/python tools/validate_legion_war_capture.py /tmp/dungeon4_legion_war_frida.jsonl
```

校验器会确认围攻状态后再出击、城堡 ID 与响应匹配、出击军官按品质/等级/ID 排序、战术来自当前候选且为最高品质、失败围攻后不继续收税/招募/升级，以及各消息的服务端成功字段。

捕获器固定使用 `ANDROID_SERIAL`（默认 `emulator-5554`）并经 `adb forward` 连接，避免在同时连接真机时误附加到 USB 设备。实际客户端必须已登录；若启动页提示登录超时，则不会有可校验的军团战报文。
