/**
 * 只读捕获军团战日常流程的明文 MsgHdr。
 *
 * 传输层为 TJ.TJWebSocket：Send 是 C->S，OnWebsocketMessage 是解密后的 S->C。
 * 脚本只通过 send() 上报军团战消息，不改写参数、返回值或游戏状态。
 */

'use strict';

var MAX_CAPTURE_BYTES = 65536;
var PREVIEW_BYTES = 512;
var sequence = 0;
var transportCalls = { inbound: 0, outbound: 0 };

var MESSAGE_NAMES = {
  19532: 'Pull_gacha_banner_v2',
  20050: 'Legion_info_sync',
  20054: 'Legion_battle_start',
  20055: 'Legion_battle_sync',
  20057: 'Legion_battle_choose_strategy',
  20058: 'Legion_battle_strategy_effects',
  20059: 'Legion_battle_proficiency',
  20060: 'Legion_battle_turn_fight',
  20061: 'Legion_battle_end_summary',
  20062: 'Legion_battle_retreat',
  20064: 'Legion_upgrade_troops_level',
  20074: 'Siege_sync',
  20075: 'Siege_rescue_town',
  20080: 'Siege_collect_tax'
};

function log(message) {
  console.log('[legion-war] ' + message);
}

function noteTransportCall(direction, method, object) {
  var key = direction === 'S->C' ? 'inbound' : 'outbound';
  transportCalls[key] += 1;
  // The first few calls distinguish an unparsed frame from an unused transport
  // class without flooding the host during normal gameplay.
  if (transportCalls[key] <= 3) {
    var className = '?';
    try {
      className = object && object.class ? object.class.name : '?';
    } catch (_) {}
    send({
      type: 'legion_probe_transport',
      direction: direction,
      method: method,
      call_count: transportCalls[key],
      argument_class: className
    });
  }
}

function isTracked(messageId) {
  return Object.prototype.hasOwnProperty.call(MESSAGE_NAMES, messageId);
}

function decodeVarint(bytes, offset) {
  var value = 0;
  var shift = 0;
  var index = offset;
  while (index < bytes.length && shift <= 35) {
    var current = bytes[index] & 0xff;
    value += (current & 0x7f) * Math.pow(2, shift);
    index += 1;
    if ((current & 0x80) === 0) {
      return { value: value, next: index };
    }
    shift += 7;
  }
  return null;
}

function parseMsgHeaderAt(bytes, start) {
  var index = start;
  var messageId = null;
  var payload = [];
  while (index < bytes.length) {
    var tag = decodeVarint(bytes, index);
    if (!tag || tag.value === 0) break;
    index = tag.next;
    var fieldNumber = Math.floor(tag.value / 8);
    var wireType = tag.value & 7;
    if (wireType === 0) {
      var scalar = decodeVarint(bytes, index);
      if (!scalar) break;
      if (fieldNumber === 1) messageId = scalar.value;
      index = scalar.next;
      continue;
    }
    if (wireType === 2) {
      var length = decodeVarint(bytes, index);
      if (!length || length.value > bytes.length - length.next) break;
      var start = length.next;
      var end = start + length.value;
      if (fieldNumber === 4) payload = bytes.slice(start, end);
      index = end;
      continue;
    }
    if (wireType === 1) {
      index += 8;
      continue;
    }
    if (wireType === 5) {
      index += 4;
      continue;
    }
    break;
  }
  return { messageId: messageId, payload: payload };
}

function parseMsgHeader(bytes) {
  var direct = parseMsgHeaderAt(bytes, 0);
  // Some Android builds keep a 4-byte transport length before MsgHdr. Prefer a
  // tracked framed header when the direct decode does not identify one.
  if (bytes.length > 4) {
    var framed = parseMsgHeaderAt(bytes, 4);
    if (framed.messageId !== null &&
        (direct.messageId === null || isTracked(framed.messageId))) {
      return framed;
    }
  }
  return direct;
}

function toHex(bytes) {
  var result = '';
  for (var index = 0; index < bytes.length; index += 1) {
    var value = bytes[index] & 0xff;
    result += (value < 16 ? '0' : '') + value.toString(16);
  }
  return result;
}

function readSystemByteArray(object, limit) {
  try {
    if (!object || !object.class) return null;
    var length = object.length;
    if (typeof length !== 'number' || length < 0 || length > 4000000) return null;
    var count = Math.min(length, limit);
    var bytes = [];
    for (var index = 0; index < count; index += 1) {
      bytes.push(object.get(index) & 0xff);
    }
    return { object: object, length: length, bytes: bytes, truncated: count < length };
  } catch (_) {
    return null;
  }
}

function outgoingByteArray(object) {
  if (!object || !object.class) return null;
  var direct = readSystemByteArray(object, PREVIEW_BYTES);
  if (direct) return direct.object;

  var fields = ['Bytes', 'bytes', 'buffer', 'data', 'm_Bytes', 'raw'];
  for (var index = 0; index < fields.length; index += 1) {
    try {
      var value = object.field(fields[index]).value;
      if (readSystemByteArray(value, PREVIEW_BYTES)) return value;
    } catch (_) {}
  }
  var methods = ['get_Bytes', 'GetBytes', 'ToArray', 'toArray'];
  for (var methodIndex = 0; methodIndex < methods.length; methodIndex += 1) {
    try {
      var result = object.method(methods[methodIndex]).invoke();
      if (readSystemByteArray(result, PREVIEW_BYTES)) return result;
    } catch (_) {}
  }
  return null;
}

function capture(direction, method, byteArray) {
  var preview = readSystemByteArray(byteArray, PREVIEW_BYTES);
  if (!preview) return;
  var parsedPreview = parseMsgHeader(preview.bytes);
  if (!isTracked(parsedPreview.messageId)) return;

  var full = readSystemByteArray(byteArray, MAX_CAPTURE_BYTES);
  if (!full) return;
  var parsed = parseMsgHeader(full.bytes);
  if (parsed.messageId !== parsedPreview.messageId) {
    log('message header changed while reading; dropped');
    return;
  }
  var event = {
    type: 'legion_ws',
    sequence: sequence++,
    ts: Date.now(),
    direction: direction,
    method: method,
    message_id: parsed.messageId,
    message_name: MESSAGE_NAMES[parsed.messageId],
    message_length: full.length,
    payload_hex: toHex(parsed.payload),
    truncated: full.truncated
  };
  send(event);
  log(direction + ' ' + parsed.messageId + ' ' + event.message_name + ' payload=' + parsed.payload.length);
}

function install() {
  if (typeof Il2Cpp === 'undefined') {
    send({ type: 'legion_probe_error', error: 'frida-il2cpp-bridge 未加载' });
    return;
  }
  Il2Cpp.perform(function () {
    var websocketClass;
    try {
      websocketClass = Il2Cpp.domain.assembly('Assembly-CSharp').image.class('TJ.TJWebSocket');
    } catch (_) {
      try {
        websocketClass = Il2Cpp.domain.assembly('Assembly-CSharp').image.class('TJWebSocket');
      } catch (error) {
        send({ type: 'legion_probe_error', error: 'TJWebSocket 未找到: ' + error });
        return;
      }
    }

    var hookedReceive = false;
    var hookedSend = false;
    websocketClass.methods.forEach(function (method) {
      if (method.name === 'OnWebsocketMessage') {
        Interceptor.attach(method.virtualAddress, {
          onEnter: function (args) {
            try {
              var object = new Il2Cpp.Object(args[1]);
              noteTransportCall('S->C', 'OnWebsocketMessage', object);
              capture('S->C', 'OnWebsocketMessage', object);
            } catch (error) {
              log('receive capture error: ' + error);
            }
          }
        });
        hookedReceive = true;
      }
      if (method.name === 'Send') {
        Interceptor.attach(method.virtualAddress, {
          onEnter: function (args) {
            try {
              var object = new Il2Cpp.Object(args[1]);
              noteTransportCall('C->S', 'Send', object);
              var bytes = outgoingByteArray(object);
              if (bytes) capture('C->S', 'Send', bytes);
            } catch (error) {
              log('send capture error: ' + error);
            }
          }
        });
        hookedSend = true;
      }
    });
    send({
      type: 'legion_probe_ready',
      receive_hooked: hookedReceive,
      send_hooked: hookedSend
    });
  });
}

setImmediate(install);
