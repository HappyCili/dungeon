/**
 * Frida: capture battle start/end to diagnose BATTLE_END_RESULT_ABORT (result=4).
 *
 * Hooks TJ.TJWebSocket Send / OnWebsocketMessage (same as frida_hook_battle_info.js)
 * and fully dumps 18002/18010/18012/18090 payloads.
 *
 * Usage:
 *   cd ui_app
 *   ../venv/bin/frida -U -n com.zygames.dungeon4 \
 *     -l node_modules/frida-il2cpp-bridge/dist/index.js \
 *     -l tools/frida_hook_battle_abort.js
 */

'use strict';

var OUT = '/tmp/dungeon4_battle_abort.jsonl';

var MSG = {
  10030: 'Kickout',
  13300: 'Event_start',
  13305: 'Event_option',
  13315: 'Event_end',
  13320: 'Event_func_action',
  13325: 'Event_func_next',
  15502: 'Map_enter_area',
  15504: 'Map_exit_area',
  15516: 'Map_processloc',
  15562: 'Map_enter_treasure',
  18002: 'Battle_info',
  18010: 'Battle_C2S_start',
  18012: 'Battle_S2C_start',
  18050: 'Battle_S2C_frame_broadcast',
  18052: 'Battle_S2C_frame_hash',
  18090: 'Battle_S2C_end',
  18100: 'Battle_C2S_setTimescale',
  18110: 'Battle_C2S_auto_unique_skill',
  18114: 'Battle_C2S_auto_artifact_skill',
};

var RESULT = {
  0: 'NONE',
  1: 'LOSE',
  2: 'WIN',
  3: 'RETREAT',
  4: 'ABORT',
  5: 'TIMEOUT',
};

function log(msg) {
  console.log('[abort] ' + msg);
}

function appendJsonl(obj) {
  try {
    var f = new File(OUT, 'a');
    f.write(JSON.stringify(obj) + '\n');
    f.close();
  } catch (e) {
    // File API may be unavailable; still send()
  }
  try {
    send({ type: 'battle_abort', data: obj });
  } catch (e2) {}
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
    if (shift > 63) break;
  }
  return { value: result, next: i };
}

function decodeZigZag32(n) {
  // protobuf int32 is signed varint (not zigzag for int32 in protobuf)
  // signed interpretation of 64-bit varint lower 32 bits
  n = n >>> 0;
  if (n & 0x80000000) return n - 0x100000000;
  return n;
}

function parseFields(bytes) {
  var fields = [];
  var i = 0;
  while (i < bytes.length) {
    var key = decodeVarint(bytes, i);
    i = key.next;
    var fn = key.value >>> 3;
    var wt = key.value & 7;
    if (fn === 0) break;
    if (wt === 0) {
      var v = decodeVarint(bytes, i);
      i = v.next;
      fields.push({ f: fn, wt: 0, v: decodeZigZag32(v.value), raw: v.value });
    } else if (wt === 2) {
      var len = decodeVarint(bytes, i);
      i = len.next;
      var end = i + len.value;
      if (end > bytes.length) break;
      var sub = bytes.slice(i, end);
      i = end;
      fields.push({ f: fn, wt: 2, len: sub.length, hex: hex(sub, 40), nested: parseFields(sub) });
    } else if (wt === 1) {
      i += 8;
      fields.push({ f: fn, wt: 1 });
    } else if (wt === 5) {
      i += 4;
      fields.push({ f: fn, wt: 5 });
    } else {
      break;
    }
  }
  return fields;
}

function hex(bytes, n) {
  n = Math.min(n || 32, bytes.length);
  var s = '';
  for (var i = 0; i < n; i++) {
    var b = bytes[i] & 0xff;
    s += (b < 16 ? '0' : '') + b.toString(16);
  }
  if (bytes.length > n) s += '…';
  return s;
}

function parseMsg(bytes) {
  // MsgHdr: field1 varint message_id, field4 bytes payload
  if (!bytes || bytes.length < 2) return null;
  var fields = parseFields(bytes);
  var messageId = null;
  var payload = null;
  for (var i = 0; i < fields.length; i++) {
    if (fields[i].f === 1 && fields[i].wt === 0) messageId = fields[i].v;
    if (fields[i].f === 4 && fields[i].wt === 2) {
      // re-extract payload bytes
      // walk again for payload slice
    }
  }
  // re-parse for payload bytes properly
  var off = 0;
  var mid = null;
  var pay = null;
  while (off < bytes.length) {
    var k = decodeVarint(bytes, off);
    off = k.next;
    var field = k.value >>> 3;
    var wire = k.value & 7;
    if (wire === 0) {
      var vv = decodeVarint(bytes, off);
      off = vv.next;
      if (field === 1) mid = decodeZigZag32(vv.value);
    } else if (wire === 2) {
      var ln = decodeVarint(bytes, off);
      off = ln.next;
      var slice = bytes.slice(off, off + ln.value);
      off += ln.value;
      if (field === 4) pay = slice;
    } else if (wire === 1) off += 8;
    else if (wire === 5) off += 4;
    else break;
  }
  return {
    message_id: mid,
    message_name: mid != null ? MSG[mid] || ('id_' + mid) : null,
    payload: pay,
    payload_hex: pay ? hex(pay, 64) : null,
    payload_fields: pay ? parseFields(pay) : null,
  };
}

function summarizeEnd(fields) {
  if (!fields) return {};
  var out = {};
  for (var i = 0; i < fields.length; i++) {
    var it = fields[i];
    if (it.wt !== 0) continue;
    if (it.f === 1) out.round = it.v;
    if (it.f === 2) out.win = it.v;
    if (it.f === 3) out.bid = it.v;
    if (it.f === 4) out.durtime = it.v;
    if (it.f === 10) {
      out.result = it.v;
      out.result_name = RESULT[it.v] || String(it.v);
    }
  }
  return out;
}

function summarizeStart(fields) {
  if (!fields) return {};
  var out = { team: [], eteam: [] };
  for (var i = 0; i < fields.length; i++) {
    var it = fields[i];
    if (it.f === 1 && it.wt === 0) out.id = it.v;
    if (it.f === 2 && it.wt === 2) {
      // map entry key/value
      var key = null;
      var xy = {};
      for (var j = 0; j < (it.nested || []).length; j++) {
        var n = it.nested[j];
        if (n.f === 1 && n.wt === 0) key = n.v;
        if (n.f === 2 && n.wt === 2) {
          for (var k = 0; k < (n.nested || []).length; k++) {
            var p = n.nested[k];
            if (p.f === 1 && p.wt === 0) xy.x = p.v;
            if (p.f === 2 && p.wt === 0) xy.y = p.v;
          }
        }
      }
      out.team.push({ id: key, x: xy.x, y: xy.y });
    }
    if (it.f === 3 && it.wt === 2) {
      var key2 = null;
      var xy2 = {};
      for (var j2 = 0; j2 < (it.nested || []).length; j2++) {
        var n2 = it.nested[j2];
        if (n2.f === 1 && n2.wt === 0) key2 = n2.v;
        if (n2.f === 2 && n2.wt === 2) {
          for (var k2 = 0; k2 < (n2.nested || []).length; k2++) {
            var p2 = n2.nested[k2];
            if (p2.f === 1 && p2.wt === 0) xy2.x = p2.v;
            if (p2.f === 2 && p2.wt === 0) xy2.y = p2.v;
          }
        }
      }
      out.eteam.push({ id: key2, x: xy2.x, y: xy2.y });
    }
  }
  return out;
}

function interesting(id) {
  if (id == null) return false;
  if (MSG[id]) return true;
  if (id >= 18000 && id < 18200) return true;
  if (id >= 13300 && id <= 13330) return true;
  if (id >= 15500 && id < 15600) return true;
  return false;
}

function readSystemByteArray(obj) {
  try {
    if (!obj || !obj.class) return null;
    var len = obj.length;
    if (typeof len !== 'number' || len < 0 || len > 4000000) return null;
    var arr = [];
    for (var i = 0; i < len; i++) arr.push(obj.get(i) & 0xff);
    return arr;
  } catch (e) {
    return null;
  }
}

function readPuertsArrayBuffer(obj) {
  if (!obj || !obj.class) return null;
  var names = ['Bytes', 'bytes', 'buffer', 'data', 'm_Bytes'];
  for (var i = 0; i < names.length; i++) {
    try {
      var f = obj.field(names[i]);
      if (!f) continue;
      var asArr = readSystemByteArray(f.value);
      if (asArr) return asArr;
    } catch (e) {}
  }
  var methods = ['get_Bytes', 'GetBytes', 'ToArray'];
  for (var j = 0; j < methods.length; j++) {
    try {
      var m = obj.method(methods[j]);
      if (!m) continue;
      var ret = m.invoke();
      var a2 = readSystemByteArray(ret);
      if (a2) return a2;
    } catch (e2) {}
  }
  return null;
}

function emit(dir, bytes) {
  var msg = parseMsg(bytes);
  if (!msg || !interesting(msg.message_id)) return;

  var rec = {
    ts: Date.now(),
    dir: dir,
    message_id: msg.message_id,
    message_name: msg.message_name,
    payload_hex: msg.payload_hex,
    payload_len: msg.payload ? msg.payload.length : 0,
  };

  if (msg.message_id === 18010) {
    rec.start = summarizeStart(msg.payload_fields);
    log(
      dir +
        ' Battle_C2S_start id=' +
        rec.start.id +
        ' team=' +
        rec.start.team.length +
        ' eteam=' +
        JSON.stringify(rec.start.eteam)
    );
  } else if (msg.message_id === 18090) {
    rec.end = summarizeEnd(msg.payload_fields);
    log(
      dir +
        ' Battle_S2C_end result=' +
        rec.end.result +
        '(' +
        (rec.end.result_name || '?') +
        ') bid=' +
        rec.end.bid +
        ' durtime=' +
        rec.end.durtime +
        ' win=' +
        rec.end.win
    );
    if (rec.end.result === 4) {
      log('*** ABORT detected — dump written ***');
    }
  } else if (msg.message_id === 18002) {
    log(dir + ' Battle_info len=' + rec.payload_len + ' head=' + rec.payload_hex);
  } else if (msg.message_id === 18012) {
    log(dir + ' Battle_S2C_start len=' + rec.payload_len);
  } else {
    log(dir + ' ' + msg.message_name + '(' + msg.message_id + ') len=' + rec.payload_len);
  }

  appendJsonl(rec);
}

function mainIl2Cpp() {
  if (typeof Il2Cpp === 'undefined') {
    log('need frida-il2cpp-bridge');
    return;
  }
  Il2Cpp.perform(function () {
    log('attach ok; log => ' + OUT);
    var cls = null;
    try {
      cls = Il2Cpp.domain.assembly('Assembly-CSharp').image.class('TJ.TJWebSocket');
    } catch (e) {
      cls = Il2Cpp.domain.assembly('Assembly-CSharp').image.class('TJWebSocket');
    }
    cls.methods.forEach(function (m) {
      if (m.name === 'OnWebsocketMessage') {
        Interceptor.attach(m.virtualAddress, {
          onEnter: function (args) {
            try {
              var bytes = readSystemByteArray(new Il2Cpp.Object(args[1]));
              if (bytes) emit('S→C', bytes);
            } catch (e) {
              log('rx err ' + e);
            }
          },
        });
        log('hook OnWebsocketMessage');
      }
      if (m.name === 'Send') {
        Interceptor.attach(m.virtualAddress, {
          onEnter: function (args) {
            try {
              var o = new Il2Cpp.Object(args[1]);
              var bytes = readPuertsArrayBuffer(o) || readSystemByteArray(o);
              if (bytes) emit('C→S', bytes);
            } catch (e) {
              log('tx err ' + e);
            }
          },
        });
        log('hook Send');
      }
    });
    log('Ready: enter 聚宝 → click monster → start battle. Watch for ABORT.');
  });
}

setImmediate(mainIl2Cpp);
