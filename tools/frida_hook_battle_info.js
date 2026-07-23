/**
 * Frida：捕获 dungeon4 游戏服 WebSocket 上的 Battle_info / 地图协议。
 *
 * ## 架构结论（本机 emulator-5554 + frida-il2cpp-bridge 实测）
 *
 * - 传输：`TJ.TJWebSocket`
 *   - `Send(Puerts.ArrayBuffer)`  C→S
 *   - `OnWebsocketMessage(System.Byte[])`  S→C（解密后的 MsgHdr 明文）
 * - 业务：几乎全在 Puerts/JS（main.js 的 onBattleInfo / tryEnterBattle）
 * - Assembly-CSharp 几乎无 Battle 业务类（仅 BattleHealthBarFollower 等）
 *
 * ## 用法
 *
 * ```bash
 * # 1) frida-server 已启动（tools/start_frida_server.sh）
 * # 2) 游戏已打开并登录
 * cd ui_app
 * ./tools/run_frida_battle_capture.sh
 * # 3) 在游戏里：进聚宝 / 点怪开战 / 重登进战斗
 * # 4) 日志见 /tmp/dungeon4_battle_frida.jsonl 与终端 [battle]
 * ```
 *
 * 消息体为 protobuf MsgHdr：field1=message_id (varint)，field4=payload。
 */

'use strict';

function log(msg) {
  console.log('[battle] ' + msg);
}

var MSG = {
  10010: 'Login',
  10022: 'Login_reunique',
  10030: 'Kickout',
  10040: 'Heartbeat',
  10090: 'Pack_password',
  10490: 'Game_data',
  10531: 'Client_talog',
  15502: 'Map_enter_area',
  15504: 'Map_exit_area',
  15516: 'Map_processloc',
  15550: 'Evt_script_trigger',
  15554: 'Map_move',
  15555: 'Map_reset_area',
  15562: 'Map_enter_treasure',
  15570: 'Map_treasure_info',
  15574: 'Map_movetrigger_active',
  18002: 'Battle_info',
  18004: 'Battle_unitinfo',
  18006: 'Battle_offline',
  18010: 'Battle_C2S_start',
  18012: 'Battle_S2C_start',
  18050: 'Battle_S2C_frame',
  18052: 'Battle_S2C_end',
};

function msgName(id) {
  return MSG[id] || ('id_' + id);
}

function interesting(id) {
  if (id === 18002 || id === 18010 || id === 18012 || id === 18052) return true;
  if (id === 15516 || id === 15550 || id === 15562 || id === 15502) return true;
  if (id === 10531 || id === 10490) return true;
  if (id >= 18000 && id < 18200) return true;
  if (id >= 15500 && id < 15600) return true;
  return false;
}

function decodeVarint(bytes, off) {
  var result = 0;
  var shift = 0;
  var i = off;
  while (i < bytes.length) {
    var b = bytes[i] & 0xff;
    result |= (b & 0x7f) << shift;
    i++;
    if ((b & 0x80) === 0) break;
    shift += 7;
    if (shift > 35) break;
  }
  return { value: result >>> 0, next: i };
}

/** MsgHdr：field1 varint = message_id（与 harvest_fief.encode_message_header 一致） */
function parseMsgId(bytes) {
  if (!bytes || bytes.length < 2) return null;
  if ((bytes[0] & 0x07) !== 0) {
    // 非 field1 varint 时宽松扫
  }
  if (bytes[0] === 0x08) {
    var v = decodeVarint(bytes, 1);
    return v.value;
  }
  // 兼容：有时外层还有长度前缀
  if (bytes.length > 4 && bytes[4] === 0x08) {
    var v2 = decodeVarint(bytes, 5);
    return v2.value;
  }
  return null;
}

function hex(bytes, n) {
  n = Math.min(n || 20, bytes.length);
  var s = '';
  for (var i = 0; i < n; i++) {
    var b = bytes[i] & 0xff;
    s += (b < 16 ? '0' : '') + b.toString(16);
  }
  return s + (bytes.length > n ? '…' : '');
}

function readSystemByteArray(obj) {
  try {
    if (!obj || !obj.class) return null;
    var len = obj.length;
    if (typeof len !== 'number' || len < 0 || len > 4000000) return null;
    var max = Math.min(len, 96);
    var head = [];
    for (var i = 0; i < max; i++) head.push(obj.get(i) & 0xff);
    return { len: len, head: head };
  } catch (e) {
    return null;
  }
}

function readPuertsArrayBuffer(obj) {
  if (!obj || !obj.class) return null;
  var cn = obj.class.name;
  if (cn !== 'ArrayBuffer' && cn.indexOf('ArrayBuffer') < 0) return null;

  // 常见字段 / 属性
  var fieldNames = [
    'Bytes',
    'bytes',
    'buffer',
    'data',
    'm_Bytes',
    'raw',
    'ptr',
  ];
  for (var i = 0; i < fieldNames.length; i++) {
    try {
      var f = obj.field(fieldNames[i]);
      if (!f) continue;
      var v = f.value;
      var asArr = readSystemByteArray(v);
      if (asArr) return asArr;
    } catch (e) {}
  }
  // 方法 get_Bytes / ToBytes
  var methodNames = ['get_Bytes', 'GetBytes', 'ToArray', 'toArray'];
  for (var j = 0; j < methodNames.length; j++) {
    try {
      var m = obj.method(methodNames[j]);
      if (!m) continue;
      var ret = m.invoke();
      var asArr2 = readSystemByteArray(ret);
      if (asArr2) return asArr2;
    } catch (e2) {}
  }
  // 枚举字段兜底
  try {
    obj.class.fields.forEach(function (field) {
      // no-op collect
    });
  } catch (e3) {}
  return null;
}

function emitEvent(dir, method, info) {
  var id = info ? parseMsgId(info.head) : null;
  var name = id != null ? msgName(id) : '?';
  var keep = id == null || interesting(id) || dir === 'S→C';
  // 默认：S→C 全打 interesting；C→S 只打 interesting
  if (dir === 'C→S' && id != null && !interesting(id)) return;
  if (dir === 'S→C' && id != null && !interesting(id) && id !== 10040 && id !== 10041) {
    // 心跳降噪
    if (id === 10040 || id === 10041) return;
  }

  var line =
    dir +
    ' ' +
    method +
    ' len=' +
    (info ? info.len : -1) +
    ' id=' +
    (id != null ? id + '(' + name + ')' : '?') +
    ' head=' +
    (info ? hex(info.head, 24) : '');
  log(line);

  // 结构化输出：Frida 终端可重定向；也 send 到主机
  try {
    send({
      type: 'ws_msg',
      dir: dir,
      method: method,
      message_id: id,
      message_name: name,
      length: info ? info.len : 0,
      head_hex: info ? hex(info.head, 48) : '',
      ts: Date.now(),
    });
  } catch (e) {}
}

function mainIl2Cpp() {
  if (typeof Il2Cpp === 'undefined') {
    log('需要 -l frida-il2cpp-bridge/dist/index.js');
    return;
  }

  Il2Cpp.perform(function () {
    log('attach ok — TJWebSocket capture');
    var cls = null;
    try {
      cls = Il2Cpp.domain.assembly('Assembly-CSharp').image.class('TJ.TJWebSocket');
    } catch (e) {
      try {
        cls = Il2Cpp.domain.assembly('Assembly-CSharp').image.class('TJWebSocket');
      } catch (e2) {
        log('TJWebSocket 未找到: ' + e2);
        return;
      }
    }

    cls.methods.forEach(function (m) {
      if (m.name === 'OnWebsocketMessage') {
        Interceptor.attach(m.virtualAddress, {
          onEnter: function (args) {
            try {
              var o = new Il2Cpp.Object(args[1]);
              var info = readSystemByteArray(o);
              emitEvent('S→C', 'OnWebsocketMessage', info);
              if (info) {
                var id = parseMsgId(info.head);
                if (id === 18002) {
                  log('*** Battle_info (18002) 到达客户端会话 ***');
                }
              }
            } catch (e) {
              log('OnWebsocketMessage err ' + e);
            }
          },
        });
        log('hook OnWebsocketMessage(byte[])');
      }
      if (m.name === 'Send') {
        Interceptor.attach(m.virtualAddress, {
          onEnter: function (args) {
            try {
              var o = new Il2Cpp.Object(args[1]);
              var info = readPuertsArrayBuffer(o) || readSystemByteArray(o);
              if (!info) {
                log(
                  'Send unparsed class=' +
                    (o && o.class ? o.class.name : '?') +
                    ' fields try…'
                );
                try {
                  o.class.fields.forEach(function (f) {
                    log('  field ' + f.name + ' : ' + f.type.name);
                  });
                } catch (e2) {}
              }
              emitEvent('C→S', 'Send', info);
              if (info) {
                var id = parseMsgId(info.head);
                if (id === 15550) log('*** 客户端发出 Evt_script_trigger ***');
                if (id === 10531) log('*** 客户端发出 Client_talog ***');
                if (id === 18010) log('*** 客户端发出 Battle_C2S_start ***');
              }
            } catch (e) {
              log('Send err ' + e);
            }
          },
        });
        log('hook Send(Puerts.ArrayBuffer)');
      }
    });

    log('准备就绪：请在游戏内进聚宝/开战。关注 18002 Battle_info 与其前序 C→S。');
  });
}

setImmediate(mainIl2Cpp);
