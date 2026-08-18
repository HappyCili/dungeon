/**
 * 只读捕获宫廷棋 WebSocket 明文 MsgHdr。
 *
 * 传输层为 TJ.TJWebSocket：Send 是 C->S，OnWebsocketMessage 是解密后的 S->C。
 * 此脚本只经 send()/console 上报消息，不改写参数、返回值或游戏状态。
 */

'use strict';

var MAX_CAPTURE_BYTES = 65536;
var PREVIEW_BYTES = 512;
var sequence = 0;

var MESSAGE_NAMES = {
  10490: 'Game_data',
  12602: 'Storage_notify_itemchange',
  13300: 'Event_start',
  13305: 'Event_option',
  13315: 'Event_end',
  13320: 'Event_func_action',
  13325: 'Event_func_next',
  22300: 'Monopoly_info',
  22302: 'Monopoly_rolldice',
  22304: 'Monopoly_buydice',
  22306: 'Monopoly_reset_layout',
  22308: 'Monopoly_select_layout',
  22310: 'Monopoly_triggerevent',
  22314: 'Monopoly_move',
  22315: 'Monopoly_translimit',
  22316: 'Monopoly_transother',
  22318: 'Monopoly_exitother',
  22328: 'Monopoly_syn_visitorlist',
  22330: 'Monopoly_confirm_other',
  22342: 'Monopoly_captured',
  22344: 'Monopoly_exemption_punish'
};

function log(message) {
  console.log('[monopoly] ' + message);
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
    if ((current & 0x80) === 0) return { value: value, next: index };
    shift += 7;
  }
  return null;
}

function parseMsgHeaderAt(bytes, offset) {
  var index = offset;
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
    } else if (wireType === 2) {
      var length = decodeVarint(bytes, index);
      if (!length || length.value > bytes.length - length.next) break;
      var start = length.next;
      var end = start + length.value;
      if (fieldNumber === 4) payload = bytes.slice(start, end);
      index = end;
    } else if (wireType === 1) {
      index += 8;
    } else if (wireType === 5) {
      index += 4;
    } else {
      break;
    }
  }
  return { messageId: messageId, payload: payload };
}

function parseMsgHeader(bytes) {
  var direct = parseMsgHeaderAt(bytes, 0);
  if (bytes.length > 4) {
    var framed = parseMsgHeaderAt(bytes, 4);
    if (framed.messageId !== null &&
        (direct.messageId === null || isTracked(framed.messageId))) return framed;
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
    for (var index = 0; index < count; index += 1) bytes.push(object.get(index) & 0xff);
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
  if (parsed.messageId !== parsedPreview.messageId) return;
  var event = {
    type: 'monopoly_ws',
    sequence: sequence++,
    direction: direction,
    method: method,
    message_id: parsed.messageId,
    message_name: MESSAGE_NAMES[parsed.messageId],
    message_length: full.length,
    payload_hex: toHex(parsed.payload),
    truncated: full.truncated
  };
  send(event);
  log(direction + ' ' + event.message_id + ' ' + event.message_name +
      ' payload=' + parsed.payload.length + ' ' + event.payload_hex);
}

function install() {
  if (typeof Il2Cpp === 'undefined') {
    send({ type: 'monopoly_probe_error', error: 'frida-il2cpp-bridge 未加载' });
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
        send({ type: 'monopoly_probe_error', error: 'TJWebSocket 未找到: ' + error });
        return;
      }
    }
    var receiveHooked = false;
    var sendHooked = false;
    websocketClass.methods.forEach(function (method) {
      if (method.name === 'OnWebsocketMessage') {
        Interceptor.attach(method.virtualAddress, {
          onEnter: function (args) {
            try { capture('S->C', 'OnWebsocketMessage', new Il2Cpp.Object(args[1])); }
            catch (error) { log('receive capture error: ' + error); }
          }
        });
        receiveHooked = true;
      }
      if (method.name === 'Send') {
        Interceptor.attach(method.virtualAddress, {
          onEnter: function (args) {
            try {
              var bytes = outgoingByteArray(new Il2Cpp.Object(args[1]));
              if (bytes) capture('C->S', 'Send', bytes);
            } catch (error) { log('send capture error: ' + error); }
          }
        });
        sendHooked = true;
      }
    });
    send({ type: 'monopoly_probe_ready', receive_hooked: receiveHooked, send_hooked: sendHooked });
  });
}

setImmediate(install);
