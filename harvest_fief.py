#!/usr/bin/env python3
"""收取庄园材料的游戏服 WebSocket 客户端。

默认读取同目录 ``tokens.json`` 中的 ``userid`` 和 ``verify_token``，按当前
Android 客户端的登录链路获取游戏服地址，然后发送一次普通庄园征收。

用法：
    .venv/bin/python harvest_fief.py
    .venv/bin/python harvest_fief.py --zone-id 10001
    .venv/bin/python harvest_fief.py --self-test
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import socket
import ssl
import struct
import sys
import time
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen

from id_descriptions import item_change_text, reward_text, zone_name


PROJECT_ROOT = Path(__file__).resolve().parent
DEFAULT_TOKEN_FILE = PROJECT_ROOT / "tokens.json"
DEFAULT_ACCOUNT_URL = "http://dixcbdlogin.gamelunar.com:8101/api"

ACCOUNT_PACK_KEY = "46154569"
SOCKET_PACK_KEY = "45985633"
FIEF_HARVEST_MESSAGE_ID = 19154
FIEF_HARVEST_SOURCE = 37
FIEF_HARVEST_REJECTION_MESSAGES = {
    4: "暂无可收取资源",
}
LOGIN_MESSAGE_ID = 10010
LOGIN_FAIL_MESSAGE_ID = 10014
LOGIN_REUNIQUE_MESSAGE_ID = 10022
HEARTBEAT_MESSAGE_ID = 10040
HEARTBEAT_RET_MESSAGE_ID = 10041
PACK_PASSWORD_MESSAGE_ID = 10090
STORAGE_ITEM_CHANGE_MESSAGE_ID = 12602


class HarvestError(RuntimeError):
    """协议、网络或服务端响应不符合当前客户端约定。"""


class FiefHarvestRejected(HarvestError):
    """游戏服已响应庄园收取请求，但拒绝了本次操作。"""

    def __init__(self, ret: int) -> None:
        self.ret = ret
        super().__init__(f"庄园收获返回 ret={ret}")


def describe_fief_harvest_rejection(ret: int) -> str:
    return FIEF_HARVEST_REJECTION_MESSAGES.get(ret, f"服务端拒绝收取（ret={ret}）")


class ProtoError(HarvestError):
    """Protobuf 二进制数据无法按预期读取。"""


# DES permutation tables. The client uses Node's ``des-ecb`` cipher with
# PKCS#7 padding; keeping it here avoids a runtime dependency on a package.
IP = (
    58,
    50,
    42,
    34,
    26,
    18,
    10,
    2,
    60,
    52,
    44,
    36,
    28,
    20,
    12,
    4,
    62,
    54,
    46,
    38,
    30,
    22,
    14,
    6,
    64,
    56,
    48,
    40,
    32,
    24,
    16,
    8,
    57,
    49,
    41,
    33,
    25,
    17,
    9,
    1,
    59,
    51,
    43,
    35,
    27,
    19,
    11,
    3,
    61,
    53,
    45,
    37,
    29,
    21,
    13,
    5,
    63,
    55,
    47,
    39,
    31,
    23,
    15,
    7,
)
FP = (
    40,
    8,
    48,
    16,
    56,
    24,
    64,
    32,
    39,
    7,
    47,
    15,
    55,
    23,
    63,
    31,
    38,
    6,
    46,
    14,
    54,
    22,
    62,
    30,
    37,
    5,
    45,
    13,
    53,
    21,
    61,
    29,
    36,
    4,
    44,
    12,
    52,
    20,
    60,
    28,
    35,
    3,
    43,
    11,
    51,
    19,
    59,
    27,
    34,
    2,
    42,
    10,
    50,
    18,
    58,
    26,
    33,
    1,
    41,
    9,
    49,
    17,
    57,
    25,
)
E = (
    32,
    1,
    2,
    3,
    4,
    5,
    4,
    5,
    6,
    7,
    8,
    9,
    8,
    9,
    10,
    11,
    12,
    13,
    12,
    13,
    14,
    15,
    16,
    17,
    16,
    17,
    18,
    19,
    20,
    21,
    20,
    21,
    22,
    23,
    24,
    25,
    24,
    25,
    26,
    27,
    28,
    29,
    28,
    29,
    30,
    31,
    32,
    1,
)
P = (
    16,
    7,
    20,
    21,
    29,
    12,
    28,
    17,
    1,
    15,
    23,
    26,
    5,
    18,
    31,
    10,
    2,
    8,
    24,
    14,
    32,
    27,
    3,
    9,
    19,
    13,
    30,
    6,
    22,
    11,
    4,
    25,
)
PC1 = (
    57,
    49,
    41,
    33,
    25,
    17,
    9,
    1,
    58,
    50,
    42,
    34,
    26,
    18,
    10,
    2,
    59,
    51,
    43,
    35,
    27,
    19,
    11,
    3,
    60,
    52,
    44,
    36,
    63,
    55,
    47,
    39,
    31,
    23,
    15,
    7,
    62,
    54,
    46,
    38,
    30,
    22,
    14,
    6,
    61,
    53,
    45,
    37,
    29,
    21,
    13,
    5,
    28,
    20,
    12,
    4,
)
PC2 = (
    14,
    17,
    11,
    24,
    1,
    5,
    3,
    28,
    15,
    6,
    21,
    10,
    23,
    19,
    12,
    4,
    26,
    8,
    16,
    7,
    27,
    20,
    13,
    2,
    41,
    52,
    31,
    37,
    47,
    55,
    30,
    40,
    51,
    45,
    33,
    48,
    44,
    49,
    39,
    56,
    34,
    53,
    46,
    42,
    50,
    36,
    29,
    32,
)
SHIFTS = (1, 1, 2, 2, 2, 2, 2, 2, 1, 2, 2, 2, 2, 2, 2, 1)
SBOXES = (
    (
        (14, 4, 13, 1, 2, 15, 11, 8, 3, 10, 6, 12, 5, 9, 0, 7),
        (0, 15, 7, 4, 14, 2, 13, 1, 10, 6, 12, 11, 9, 5, 3, 8),
        (4, 1, 14, 8, 13, 6, 2, 11, 15, 12, 9, 7, 3, 10, 5, 0),
        (15, 12, 8, 2, 4, 9, 1, 7, 5, 11, 3, 14, 10, 0, 6, 13),
    ),
    (
        (15, 1, 8, 14, 6, 11, 3, 4, 9, 7, 2, 13, 12, 0, 5, 10),
        (3, 13, 4, 7, 15, 2, 8, 14, 12, 0, 1, 10, 6, 9, 11, 5),
        (0, 14, 7, 11, 10, 4, 13, 1, 5, 8, 12, 6, 9, 3, 2, 15),
        (13, 8, 10, 1, 3, 15, 4, 2, 11, 6, 7, 12, 0, 5, 14, 9),
    ),
    (
        (10, 0, 9, 14, 6, 3, 15, 5, 1, 13, 12, 7, 11, 4, 2, 8),
        (13, 7, 0, 9, 3, 4, 6, 10, 2, 8, 5, 14, 12, 11, 15, 1),
        (13, 6, 4, 9, 8, 15, 3, 0, 11, 1, 2, 12, 5, 10, 14, 7),
        (1, 10, 13, 0, 6, 9, 8, 7, 4, 15, 14, 3, 11, 5, 2, 12),
    ),
    (
        (7, 13, 14, 3, 0, 6, 9, 10, 1, 2, 8, 5, 11, 12, 4, 15),
        (13, 8, 11, 5, 6, 15, 0, 3, 4, 7, 2, 12, 1, 10, 14, 9),
        (10, 6, 9, 0, 12, 11, 7, 13, 15, 1, 3, 14, 5, 2, 8, 4),
        (3, 15, 0, 6, 10, 1, 13, 8, 9, 4, 5, 11, 12, 7, 2, 14),
    ),
    (
        (2, 12, 4, 1, 7, 10, 11, 6, 8, 5, 3, 15, 13, 0, 14, 9),
        (14, 11, 2, 12, 4, 7, 13, 1, 5, 0, 15, 10, 3, 9, 8, 6),
        (4, 2, 1, 11, 10, 13, 7, 8, 15, 9, 12, 5, 6, 3, 0, 14),
        (11, 8, 12, 7, 1, 14, 2, 13, 6, 15, 0, 9, 10, 4, 5, 3),
    ),
    (
        (12, 1, 10, 15, 9, 2, 6, 8, 0, 13, 3, 4, 14, 7, 5, 11),
        (10, 15, 4, 2, 7, 12, 9, 5, 6, 1, 13, 14, 0, 11, 3, 8),
        (9, 14, 15, 5, 2, 8, 12, 3, 7, 0, 4, 10, 1, 13, 11, 6),
        (4, 3, 2, 12, 9, 5, 15, 10, 11, 14, 1, 7, 6, 0, 8, 13),
    ),
    (
        (4, 11, 2, 14, 15, 0, 8, 13, 3, 12, 9, 7, 5, 10, 6, 1),
        (13, 0, 11, 7, 4, 9, 1, 10, 14, 3, 5, 12, 2, 15, 8, 6),
        (1, 4, 11, 13, 12, 3, 7, 14, 10, 15, 6, 8, 0, 5, 9, 2),
        (6, 11, 13, 8, 1, 4, 10, 7, 9, 5, 0, 15, 14, 2, 3, 12),
    ),
    (
        (13, 2, 8, 4, 6, 15, 11, 1, 10, 9, 3, 14, 5, 0, 12, 7),
        (1, 15, 13, 8, 10, 3, 7, 4, 12, 5, 6, 11, 0, 14, 9, 2),
        (7, 11, 4, 1, 9, 12, 14, 2, 0, 6, 10, 13, 15, 3, 5, 8),
        (2, 1, 14, 7, 4, 10, 8, 13, 15, 12, 9, 0, 3, 5, 6, 11),
    ),
)


def _permute(value: int, table: Iterable[int], width: int) -> int:
    result = 0
    for position in table:
        result = (result << 1) | ((value >> (width - position)) & 1)
    return result


def _des_round_keys(key: bytes) -> list[int]:
    if len(key) != 8:
        raise HarvestError("DES 密钥必须为 8 字节")
    selected = _permute(int.from_bytes(key, "big"), PC1, 64)
    left = selected >> 28
    right = selected & ((1 << 28) - 1)
    keys: list[int] = []
    for shift in SHIFTS:
        left = ((left << shift) | (left >> (28 - shift))) & ((1 << 28) - 1)
        right = ((right << shift) | (right >> (28 - shift))) & ((1 << 28) - 1)
        keys.append(_permute((left << 28) | right, PC2, 56))
    return keys


def _des_feistel(right: int, subkey: int) -> int:
    mixed = _permute(right, E, 32) ^ subkey
    substituted = 0
    for index, box in enumerate(SBOXES):
        chunk = (mixed >> (42 - index * 6)) & 0x3F
        row = ((chunk & 0x20) >> 4) | (chunk & 0x01)
        column = (chunk >> 1) & 0x0F
        substituted = (substituted << 4) | box[row][column]
    return _permute(substituted, P, 32)


def _des_block(block: bytes, key: bytes, *, decrypt: bool = False) -> bytes:
    if len(block) != 8:
        raise HarvestError("DES 分组必须为 8 字节")
    keys = _des_round_keys(key)
    if decrypt:
        keys.reverse()
    permuted = _permute(int.from_bytes(block, "big"), IP, 64)
    left = permuted >> 32
    right = permuted & 0xFFFFFFFF
    for subkey in keys:
        left, right = right, left ^ _des_feistel(right, subkey)
    result = _permute((right << 32) | left, FP, 64)
    return result.to_bytes(8, "big")


def _des_ecb_encrypt(data: bytes, key: bytes) -> bytes:
    padding_size = 8 - (len(data) % 8)
    padded = data + bytes([padding_size]) * padding_size
    return b"".join(_des_block(padded[index : index + 8], key) for index in range(0, len(padded), 8))


def _des_ecb_decrypt(data: bytes, key: bytes) -> bytes:
    if not data or len(data) % 8:
        raise HarvestError("DES 密文长度无效")
    padded = b"".join(
        _des_block(data[index : index + 8], key, decrypt=True)
        for index in range(0, len(data), 8)
    )
    padding_size = padded[-1]
    if padding_size < 1 or padding_size > 8 or padded[-padding_size:] != bytes([padding_size]) * padding_size:
        raise HarvestError("DES 填充无效")
    return padded[:-padding_size]


def _pack_key(password: str) -> bytes:
    key = password.encode("utf-8")[:8]
    if len(key) < 8:
        raise HarvestError("Pack1 密码长度小于 8 字节")
    return key


def pack1_encode(payload: bytes | str, password: str, limit: int = 100) -> str:
    """与客户端 ``NodeCrypto.Pack1Encode`` 保持相同的字节布局。"""
    raw = payload.encode("utf-8") if isinstance(payload, str) else payload
    if len(raw) > limit:
        packet = struct.pack("<I", len(raw)) + zlib.compress(raw, level=9)
    else:
        packet = struct.pack("<I", 0) + raw
    return base64.b64encode(_des_ecb_encrypt(packet, _pack_key(password))).decode("ascii")


def pack1_decode(payload: str | bytes, password: str) -> bytes:
    encoded = payload.encode("ascii") if isinstance(payload, str) else payload
    try:
        ciphertext = base64.b64decode(encoded, validate=True)
    except ValueError as exc:
        raise HarvestError("Pack1 Base64 无效") from exc
    packet = _des_ecb_decrypt(ciphertext, _pack_key(password))
    if len(packet) < 4:
        raise HarvestError("Pack1 数据长度无效")
    raw_length = struct.unpack("<I", packet[:4])[0]
    body = packet[4:]
    if raw_length == 0:
        return body
    try:
        decoded = zlib.decompress(body)
    except zlib.error as exc:
        raise HarvestError("Pack1 压缩数据无效") from exc
    if len(decoded) != raw_length:
        raise HarvestError("Pack1 解压长度不匹配")
    return decoded


def encode_varint(value: int) -> bytes:
    if value < 0:
        value &= (1 << 64) - 1
    encoded = bytearray()
    while value > 0x7F:
        encoded.append((value & 0x7F) | 0x80)
        value >>= 7
    encoded.append(value)
    return bytes(encoded)


class ProtoReader:
    def __init__(self, data: bytes) -> None:
        self.data = data
        self.position = 0

    def read_varint(self) -> int:
        value = 0
        for shift in range(0, 70, 7):
            if self.position >= len(self.data):
                raise ProtoError("Protobuf varint 截断")
            byte = self.data[self.position]
            self.position += 1
            value |= (byte & 0x7F) << shift
            if not byte & 0x80:
                return value
        raise ProtoError("Protobuf varint 过长")

    def read_bytes(self) -> bytes:
        length = self.read_varint()
        end = self.position + length
        if end > len(self.data):
            raise ProtoError("Protobuf 字段长度超出报文")
        value = self.data[self.position : end]
        self.position = end
        return value

    def fields(self) -> Iterable[tuple[int, int, int | bytes]]:
        while self.position < len(self.data):
            key = self.read_varint()
            field_number = key >> 3
            wire_type = key & 0x07
            if field_number == 0:
                raise ProtoError("Protobuf 字段号为 0")
            if wire_type == 0:
                yield field_number, wire_type, self.read_varint()
            elif wire_type == 1:
                if self.position + 8 > len(self.data):
                    raise ProtoError("Protobuf fixed64 截断")
                value = self.data[self.position : self.position + 8]
                self.position += 8
                yield field_number, wire_type, value
            elif wire_type == 2:
                yield field_number, wire_type, self.read_bytes()
            elif wire_type == 5:
                if self.position + 4 > len(self.data):
                    raise ProtoError("Protobuf fixed32 截断")
                value = self.data[self.position : self.position + 4]
                self.position += 4
                yield field_number, wire_type, value
            else:
                raise ProtoError(f"不支持的 Protobuf wire type：{wire_type}")


def encode_int_field(field_number: int, value: int) -> bytes:
    return encode_varint((field_number << 3) | 0) + encode_varint(value)


def decode_int32(value: int) -> int:
    """Match protobufjs Reader.int32() for an already-read varint."""

    value &= 0xFFFFFFFF
    return value - 0x100000000 if value & 0x80000000 else value


def encode_bytes_field(field_number: int, value: bytes) -> bytes:
    return encode_varint((field_number << 3) | 2) + encode_varint(len(value)) + value


def encode_message_header(message_id: int, data: bytes = b"") -> bytes:
    # ``MsgHdr.data`` is omitted by the JavaScript encoder for an empty payload.
    packet = encode_int_field(1, message_id)
    if data:
        packet += encode_bytes_field(4, data)
    return packet


@dataclass(frozen=True)
class MessageHeader:
    message_id: int
    sid: int
    data: bytes


def decode_message_header(data: bytes) -> MessageHeader:
    message_id = 0
    sid = 0
    payload = b""
    for field_number, wire_type, value in ProtoReader(data).fields():
        if field_number == 1 and wire_type == 0:
            message_id = int(value)
        elif field_number == 2 and wire_type == 0:
            sid = int(value)
        elif field_number == 4 and wire_type == 2:
            payload = bytes(value)
    if message_id == 0:
        raise ProtoError("MsgHdr 缺少消息号")
    return MessageHeader(message_id=message_id, sid=sid, data=payload)


def encode_login_payload(game_token: str, *, last_sid: int = 0, unique: int = 0) -> bytes:
    """Encode the client's initial Login payload.

    A cold client starts with ``lastSId=0`` and ``unique=0``.  Retaining both
    optional fields makes the encoder usable for a resumed game session too,
    while preserving the generated JavaScript's omission of zero values.
    """

    packet = b""
    if last_sid:
        packet += encode_int_field(1, last_sid)
    if unique:
        packet += encode_int_field(2, unique)
    return packet + encode_int_field(4, 1) + encode_bytes_field(5, game_token.encode("utf-8"))


def decode_pack_password(data: bytes) -> str:
    for field_number, wire_type, value in ProtoReader(data).fields():
        if field_number == 1 and wire_type == 2:
            try:
                return bytes(value).decode("utf-8")
            except UnicodeDecodeError as exc:
                raise ProtoError("Pack_password 不是 UTF-8 字符串") from exc
    raise ProtoError("Pack_password 缺少 p 字段")


@dataclass(frozen=True)
class FiefHarvestResponse:
    ret: int
    mode: int
    factory_type: int
    times: int


def decode_fief_harvest_response(data: bytes) -> FiefHarvestResponse:
    values = {"ret": 0, "mode": 0, "factory_type": 0, "times": 0}
    for field_number, wire_type, value in ProtoReader(data).fields():
        if wire_type != 0:
            continue
        if field_number == 1:
            values["ret"] = int(value)
        elif field_number == 2:
            values["mode"] = int(value)
        elif field_number == 4:
            values["factory_type"] = int(value)
        elif field_number == 5:
            values["times"] = int(value)
    return FiefHarvestResponse(**values)


@dataclass(frozen=True)
class ItemChange:
    item_id: int
    delta: int
    total: int


@dataclass(frozen=True)
class RewardProp:
    kind: int
    item_id: int
    amount: int


@dataclass(frozen=True)
class ItemChangeNotify:
    source: int
    items: tuple[ItemChange, ...]
    props: tuple[RewardProp, ...]


def _decode_item_change(data: bytes) -> ItemChange:
    values = {"item_id": 0, "delta": 0, "total": 0}
    for field_number, wire_type, value in ProtoReader(data).fields():
        if wire_type != 0:
            continue
        if field_number == 1:
            values["item_id"] = decode_int32(int(value))
        elif field_number == 2:
            values["delta"] = decode_int32(int(value))
        elif field_number == 3:
            values["total"] = decode_int32(int(value))
    return ItemChange(**values)


def _decode_reward_prop(data: bytes) -> RewardProp:
    values = {"kind": 0, "item_id": 0, "amount": 0}
    for field_number, wire_type, value in ProtoReader(data).fields():
        if wire_type != 0:
            continue
        if field_number == 1:
            values["kind"] = int(value)
        elif field_number == 2:
            values["item_id"] = int(value)
        elif field_number == 3:
            values["amount"] = int(value)
    return RewardProp(**values)


def decode_item_change_notify(data: bytes) -> ItemChangeNotify:
    source = 0
    items: list[ItemChange] = []
    props: list[RewardProp] = []
    for field_number, wire_type, value in ProtoReader(data).fields():
        if field_number == 1 and wire_type == 0:
            source = int(value)
        elif field_number == 2 and wire_type == 2:
            items.append(_decode_item_change(bytes(value)))
        elif field_number == 21 and wire_type == 2:
            props.append(_decode_reward_prop(bytes(value)))
    return ItemChangeNotify(source=source, items=tuple(items), props=tuple(props))


def _required_string(payload: Mapping[str, Any], key: str, context: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise HarvestError(f"{context} 缺少非空字段：{key}")
    return value


def load_tokens(path: Path) -> dict[str, str]:
    try:
        payload = json.loads(path.expanduser().read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise HarvestError(f"令牌文件不存在：{path}") from exc
    except json.JSONDecodeError as exc:
        raise HarvestError(f"令牌文件不是有效 JSON：{path}") from exc
    if not isinstance(payload, dict):
        raise HarvestError("令牌文件根节点不是对象")
    return {
        "userid": _required_string(payload, "userid", "令牌文件"),
        "verify_token": _required_string(payload, "verify_token", "令牌文件"),
    }


def _post_body(payload: Mapping[str, Any], post_format: str) -> tuple[bytes, str]:
    if post_format == "json":
        return (
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
            "application/json; charset=utf-8",
        )
    if post_format == "form":
        form: dict[str, str] = {}
        for key, value in payload.items():
            if isinstance(value, (dict, list)):
                form[key] = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
            else:
                form[key] = str(value)
        return urlencode(form).encode("utf-8"), "application/x-www-form-urlencoded"
    raise HarvestError(f"未知 HTTP 编码格式：{post_format}")


def post_json(url: str, payload: Mapping[str, Any], timeout: float, post_format: str) -> Mapping[str, Any]:
    body, content_type = _post_body(payload, post_format)
    request = Request(
        url,
        data=body,
        method="POST",
        headers={
            "Content-Type": content_type,
            "Accept": "application/json, text/plain, */*",
            "User-Agent": "dungeon4-fief-harvest/1.0",
        },
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            text = response.read().decode("utf-8")
    except HTTPError as exc:
        raise HarvestError(f"HTTP 请求失败，状态码：{exc.code}") from exc
    except URLError as exc:
        raise HarvestError(f"HTTP 网络错误：{exc.reason}") from exc
    except OSError as exc:
        raise HarvestError(f"HTTP 连接错误：{exc}") from exc
    try:
        decoded = json.loads(text)
    except json.JSONDecodeError as exc:
        raise HarvestError("HTTP 响应不是 JSON") from exc
    if not isinstance(decoded, dict):
        raise HarvestError("HTTP 响应根节点不是对象")
    return decoded


def _message_from_response(response: Mapping[str, Any]) -> str:
    for key in ("msg", "message", "error", "notify"):
        value = response.get(key)
        if isinstance(value, str) and value:
            return value
    return "服务端未提供错误说明"


def _zone_candidates(zone_info: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    raw_list = zone_info.get("list")
    if not isinstance(raw_list, dict):
        raise HarvestError("账号服响应缺少 zoneinfo.list")

    buckets = ("Sps", "Historys", "Recos")
    candidates: list[Mapping[str, Any]] = []
    for bucket in buckets:
        zones = raw_list.get(bucket, [])
        if isinstance(zones, list):
            candidates.extend(zone for zone in zones if isinstance(zone, dict))
    return candidates


@dataclass(frozen=True)
class AccountZone:
    """账号服返回的可选择区服，保留原始 ``zid`` 类型。"""

    zone_id: int | str
    name: str


def list_zones(zone_info: Mapping[str, Any]) -> tuple[AccountZone, ...]:
    """从账号服 ``zoneinfo`` 提取去重后的开放区服列表。"""

    zones: list[AccountZone] = []
    seen: set[str] = set()
    for zone in _zone_candidates(zone_info):
        raw_zone_id = zone.get("zid")
        if not isinstance(raw_zone_id, (int, str)) or isinstance(raw_zone_id, bool):
            continue
        if str(zone.get("zopen", 1)) == "0":
            continue
        zone_id = str(raw_zone_id)
        if not zone_id or zone_id in seen:
            continue
        name = zone.get("zname")
        zones.append(AccountZone(raw_zone_id, name if isinstance(name, str) and name else zone_id))
        seen.add(zone_id)
    if not zones:
        raise HarvestError("账号没有可用区服")
    return tuple(zones)


def _choose_zone(zone_info: Mapping[str, Any], requested_zone_id: str | None) -> Mapping[str, Any]:
    raw_list = zone_info.get("list")
    if not isinstance(raw_list, dict):
        raise HarvestError("账号服响应缺少 zoneinfo.list")

    candidates = _zone_candidates(zone_info)

    if requested_zone_id is not None:
        for zone in candidates:
            if str(zone.get("zid")) == requested_zone_id:
                return zone
        raise HarvestError(
            f"未在当前账号区服列表中找到{zone_name(requested_zone_id, '')}"
        )

    for zone in candidates:
        if str(zone.get("ztype", "")) == "300":
            return zone
    for bucket in ("Historys", "Recos", "Sps"):
        zones = raw_list.get(bucket, [])
        if isinstance(zones, list) and zones:
            zone = zones[0]
            if isinstance(zone, dict):
                return zone
    raise HarvestError("账号没有可用区服")


def _extract_game_url(response: Mapping[str, Any]) -> str:
    
    for value in response.values():
        if not isinstance(value, dict):
            continue
        servers = value.get("servers")
        if not isinstance(servers, list) or not servers:
            continue
        first = servers[0]
        if isinstance(first, dict):
            url = first.get("url")
            if isinstance(url, str) and url:
                return url
    raise HarvestError("区服网关没有返回游戏 WebSocket 地址")


def _build_client_blob(tokens: Mapping[str, str], args: argparse.Namespace) -> str:
    client = {
        "devices_id": args.device_id,
        "devices_id2": args.device_id,
        "package_name": "com.zygames.dungeon4",
        "system_ver": args.system_version,
        "phone_model": args.device_model,
        "plat_ver": "1.4.1",
        "plat_buld": "30",
        "distinct_id": "",
        "astc": False,
        "hdr": False,
        "hsr": False,
        "copytexture": False,
        "astc_sam": False,
        "astc_getp": False,
        "astc_setp": False,
        "check_ios": "",
        "resWidth": 0,
        "resHeight": 0,
        "dpi": 0,
        "frameWidth": 0,
        "frameHeight": 0,
        "cpuArchitecture": "",
        "gpu": "",
        "ram": 0,
        "graphicsApi": "",
        "refreshRate": 0,
        "quality": 0,
        "frame_rate": 0,
        "build_ver": args.build_version,
        "update_ver": args.update_version,
        "channel": f"{args.channel_name}-{args.channel_id}",
        "countryCode": "CN",
        "devices_imei": args.imei,
        "wlan_mac": args.mac,
        "os": "Android",
        "oaid": args.oaid,
        "androidId": args.android_id,
        "terminInfo": args.terminal_info,
        "osVer": args.system_version,
        "media": args.media,
        "isGuest": False,
        "thirdLogin": {
            "type": 5,
            "userId": tokens["userid"],
            "channelNo": args.channel_id,
            "token": tokens["verify_token"],
        },
    }
    return pack1_encode(json.dumps(client, ensure_ascii=False, separators=(",", ":")), ACCOUNT_PACK_KEY)


@dataclass(frozen=True)
class GameEndpoint:
    url: str
    game_token: str
    zone_id: str
    zone_name: str


@dataclass(frozen=True)
class AccountSession:
    """账号服 Logincheck 返回的临时游戏令牌、网关和区服列表。"""

    game_token: str
    gate_url: str
    zone_info: Mapping[str, Any]


def request_account_session(
    tokens: Mapping[str, str], args: argparse.Namespace
) -> AccountSession:
    """执行 Logincheck，但不选择区服或连接游戏服。"""

    # JavaScript 的 Math.round 对正数的 .5 向上取整。
    timestamp = int(time.time() + 0.5)
    checksum_source = json.dumps({"ts": timestamp}, separators=(",", ":")) + ACCOUNT_PACK_KEY
    account_request = {
        "aid": 0,
        "cmd": "Logincheck",
        "ts": timestamp,
        "chk_sum": hashlib.md5(checksum_source.encode("utf-8")).hexdigest(),
        "b_str": _build_client_blob(tokens, args),
        "e_key": "0",
        "extend": {
            "osVer": args.system_version,
            "terminInfo": args.terminal_info,
            "ip": args.client_ip,
            "mac": args.mac,
            "imei": args.imei,
            "oaid": args.oaid,
            "idfa": "",
            "extend": args.device_extend,
        },
    }
    account_response = post_json(
        args.account_url, account_request, args.http_timeout, args.post_format
    )
    if account_response.get("ret") != 0:
        raise HarvestError(f"账号服 Logincheck 失败：{_message_from_response(account_response)}")
    account_data = account_response.get("data")
    if not isinstance(account_data, dict):
        raise HarvestError("账号服 Logincheck 成功响应缺少 data")
    zone_info = account_data.get("zoneinfo")
    if not isinstance(zone_info, dict):
        raise HarvestError("账号服响应缺少 zoneinfo")
    return AccountSession(
        game_token=_required_string(account_data, "token", "账号服响应"),
        gate_url=_required_string(account_data, "gate_url", "账号服响应"),
        zone_info=zone_info,
    )


def resolve_game_endpoint(tokens: Mapping[str, str], args: argparse.Namespace) -> GameEndpoint:
    account_session = request_account_session(tokens, args)
    zone = _choose_zone(account_session.zone_info, args.zone_id)
    if str(zone.get("zopen", 1)) == "0":
        raise HarvestError(f"所选区服未开放：{zone.get('zname', zone.get('zid'))}")
    raw_zone_id = zone.get("zid")
    if not isinstance(raw_zone_id, (int, str)) or isinstance(raw_zone_id, bool):
        raise HarvestError("区服数据的 zid 不是字符串或整数")
    gate_response = post_json(
        account_session.gate_url,
        # 客户端将选区对象中的 zid 原样传给网关；当前账号服返回的是 number。
        {"zid": raw_zone_id, "token": account_session.game_token},
        args.http_timeout,
        args.post_format,
    )
    return GameEndpoint(
        url=_extract_game_url(gate_response),
        game_token=account_session.game_token,
        zone_id=str(raw_zone_id),
        zone_name=str(zone.get("zname", "")),
    )


class NativeWebSocket:
    """最小 RFC 6455 客户端，仅使用标准库并保留 TLS 主机名校验。"""

    def __init__(self, sock: socket.socket) -> None:
        self.sock = sock
        self.buffer = bytearray()
        self.fragments: list[bytes] = []
        self.fragment_opcode: int | None = None

    @classmethod
    def connect(cls, url: str, timeout: float) -> "NativeWebSocket":
        parsed = urlsplit(url)
        if parsed.scheme not in {"ws", "wss"} or not parsed.hostname:
            raise HarvestError("游戏服 URL 不是有效的 ws:// 或 wss:// 地址")
        port = parsed.port or (443 if parsed.scheme == "wss" else 80)
        try:
            raw_socket = socket.create_connection((parsed.hostname, port), timeout=timeout)
            if parsed.scheme == "wss":
                context = ssl.create_default_context()
                stream: socket.socket = context.wrap_socket(raw_socket, server_hostname=parsed.hostname)
            else:
                stream = raw_socket
        except OSError as exc:
            raise HarvestError(f"连接游戏服失败：{exc}") from exc

        websocket = cls(stream)
        path = parsed.path or "/"
        if parsed.query:
            path += f"?{parsed.query}"
        host = parsed.hostname if parsed.port is None else f"{parsed.hostname}:{parsed.port}"
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        request = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {host}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            "User-Agent: dungeon4-fief-harvest/1.0\r\n"
            "\r\n"
        )
        try:
            websocket.sock.sendall(request.encode("ascii"))
            header_data = websocket._read_http_headers()
        except OSError as exc:
            websocket.close()
            raise HarvestError(f"WebSocket 握手失败：{exc}") from exc

        lines = header_data.decode("iso-8859-1").split("\r\n")
        if not lines or " 101 " not in f" {lines[0]} ":
            websocket.close()
            raise HarvestError(f"WebSocket 升级被拒绝：{lines[0] if lines else '空响应'}")
        headers: dict[str, str] = {}
        for line in lines[1:]:
            if ":" in line:
                key_name, value = line.split(":", 1)
                headers[key_name.strip().lower()] = value.strip()
        expected_accept = base64.b64encode(
            hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode("ascii")).digest()
        ).decode("ascii")
        if headers.get("sec-websocket-accept") != expected_accept:
            websocket.close()
            raise HarvestError("WebSocket Sec-WebSocket-Accept 校验失败")
        return websocket

    def _read_http_headers(self) -> bytes:
        while b"\r\n\r\n" not in self.buffer:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise HarvestError("WebSocket 握手连接被关闭")
            self.buffer.extend(chunk)
            if len(self.buffer) > 65536:
                raise HarvestError("WebSocket 握手响应过长")
        separator = self.buffer.index(b"\r\n\r\n") + 4
        response = bytes(self.buffer[:separator])
        del self.buffer[:separator]
        return response

    def _recv_exact(self, length: int) -> bytes:
        while len(self.buffer) < length:
            chunk = self.sock.recv(max(4096, length - len(self.buffer)))
            if not chunk:
                raise HarvestError("游戏服关闭了 WebSocket 连接")
            self.buffer.extend(chunk)
        value = bytes(self.buffer[:length])
        del self.buffer[:length]
        return value

    def _send_frame(self, opcode: int, payload: bytes) -> None:
        header = bytearray([0x80 | opcode])
        payload_length = len(payload)
        if payload_length < 126:
            header.append(0x80 | payload_length)
        elif payload_length <= 0xFFFF:
            header.append(0x80 | 126)
            header.extend(struct.pack("!H", payload_length))
        else:
            header.append(0x80 | 127)
            header.extend(struct.pack("!Q", payload_length))
        mask = os.urandom(4)
        header.extend(mask)
        masked = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
        self.sock.sendall(bytes(header) + masked)

    def send_binary(self, payload: bytes) -> None:
        self._send_frame(0x2, payload)

    def send_text(self, payload: str) -> None:
        self._send_frame(0x1, payload.encode("utf-8"))

    def recv_message(self, timeout: float) -> tuple[int, bytes]:
        self.sock.settimeout(timeout)
        while True:
            header = self._recv_exact(2)
            final = bool(header[0] & 0x80)
            opcode = header[0] & 0x0F
            masked = bool(header[1] & 0x80)
            payload_length = header[1] & 0x7F
            if payload_length == 126:
                payload_length = struct.unpack("!H", self._recv_exact(2))[0]
            elif payload_length == 127:
                payload_length = struct.unpack("!Q", self._recv_exact(8))[0]
            if payload_length > 16 * 1024 * 1024:
                raise HarvestError("WebSocket 单帧超过 16 MiB")
            mask = self._recv_exact(4) if masked else b""
            payload = self._recv_exact(payload_length)
            if masked:
                payload = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))

            if opcode == 0x8:
                self._send_frame(0x8, payload[:125])
                close_code = struct.unpack("!H", payload[:2])[0] if len(payload) >= 2 else None
                detail = f"（关闭码 {close_code}）" if close_code is not None else ""
                raise HarvestError(f"游戏服关闭了 WebSocket 会话{detail}")
            if opcode == 0x9:
                self._send_frame(0xA, payload)
                continue
            if opcode == 0xA:
                continue
            if opcode == 0x0:
                if self.fragment_opcode is None:
                    raise HarvestError("收到了没有起始帧的 WebSocket 分片")
                self.fragments.append(payload)
                if final:
                    complete = b"".join(self.fragments)
                    complete_opcode = self.fragment_opcode
                    self.fragments = []
                    self.fragment_opcode = None
                    return complete_opcode, complete
                continue
            if opcode not in (0x1, 0x2):
                raise HarvestError(f"不支持的 WebSocket opcode：{opcode}")
            if final:
                return opcode, payload
            self.fragment_opcode = opcode
            self.fragments = [payload]

    def close(self) -> None:
        try:
            self._send_frame(0x8, b"")
        except OSError:
            pass
        finally:
            try:
                self.sock.close()
            except OSError:
                pass


@dataclass(frozen=True)
class HarvestResult:
    response: FiefHarvestResponse
    changes: tuple[ItemChange, ...]
    props: tuple[RewardProp, ...]


class FiefClient:
    def __init__(
        self,
        endpoint: GameEndpoint,
        timeout: float,
        socket_factory: Callable[[str, float], NativeWebSocket] = NativeWebSocket.connect,
    ) -> None:
        self.endpoint = endpoint
        self.timeout = timeout
        self.socket_factory = socket_factory
        self.socket: NativeWebSocket | None = None
        self.password: str | None = None

    def _send_message(self, message_id: int, data: bytes = b"", *, encrypted: bool) -> None:
        if self.socket is None:
            raise HarvestError("WebSocket 尚未连接")
        packet = encode_message_header(message_id, data)
        if encrypted:
            if not self.password:
                raise HarvestError("游戏服尚未下发会话密码")
            self.socket.send_text(pack1_encode(packet, self.password))
        else:
            self.socket.send_binary(packet)

    def _decode_frame(self, opcode: int, payload: bytes) -> MessageHeader:
        if self.password is not None:
            # NativeWebSocketImpl accepts either a JavaScript string or a
            # Uint8Array and lets Pack1Decode convert the latter to Base64 text.
            # The native bridge used by this build can therefore surface server
            # text frames as binary WebSocket frames.
            if opcode not in (0x1, 0x2):
                raise HarvestError(f"加密游戏报文 opcode 异常：{opcode}")
            payload = pack1_decode(payload, self.password)
        return decode_message_header(payload)

    def _handle_pre_harvest_message(self, header: MessageHeader) -> bool:
        if header.message_id == PACK_PASSWORD_MESSAGE_ID:
            encrypted_password = decode_pack_password(header.data)
            try:
                self.password = pack1_decode(encrypted_password, SOCKET_PACK_KEY).decode("utf-8")
            except UnicodeDecodeError as exc:
                raise HarvestError("游戏服会话密码不是 UTF-8 文本") from exc
            return False
        if header.message_id == HEARTBEAT_MESSAGE_ID:
            self._send_message(
                HEARTBEAT_RET_MESSAGE_ID, b"", encrypted=self.password is not None
            )
            return False
        if header.message_id == LOGIN_FAIL_MESSAGE_ID:
            raise HarvestError("游戏服 Login 失败")
        return header.message_id == LOGIN_REUNIQUE_MESSAGE_ID

    def harvest(self) -> HarvestResult:
        self.socket = self.socket_factory(self.endpoint.url, self.timeout)
        try:
            self._send_message(
                LOGIN_MESSAGE_ID,
                encode_login_payload(self.endpoint.game_token),
                encrypted=False,
            )
            login_deadline = time.monotonic() + self.timeout
            while True:
                remaining = login_deadline - time.monotonic()
                if remaining <= 0:
                    raise HarvestError("等待游戏服登录完成超时")
                try:
                    opcode, frame = self.socket.recv_message(remaining)
                except socket.timeout as exc:
                    raise HarvestError("等待游戏服登录完成超时") from exc
                except OSError as exc:
                    raise HarvestError(f"读取游戏服登录报文失败：{exc}") from exc
                if self._handle_pre_harvest_message(self._decode_frame(opcode, frame)):
                    break

            # SocketManager.throwCachedMsg() dispatches cached business traffic
            # 100 ms after Login_reunique.  Mirror that ordering before sending
            # the standalone client request.
            time.sleep(0.1)
            # HARVEST_NORMAL=0; the generated Protobuf encoder omits it, so the
            # inner message and MsgHdr.data are both empty.
            self._send_message(FIEF_HARVEST_MESSAGE_ID, b"", encrypted=True)
            response: FiefHarvestResponse | None = None
            item_changes: tuple[ItemChange, ...] = ()
            props: tuple[RewardProp, ...] = ()
            deadline = time.monotonic() + self.timeout
            reward_deadline: float | None = None
            while True:
                now = time.monotonic()
                current_deadline = reward_deadline if reward_deadline is not None else deadline
                if now >= current_deadline:
                    if response is not None:
                        return HarvestResult(response, item_changes, props)
                    raise HarvestError("等待 Fief_harvest_res 响应超时")
                try:
                    opcode, frame = self.socket.recv_message(current_deadline - now)
                except socket.timeout as exc:
                    if response is not None:
                        return HarvestResult(response, item_changes, props)
                    raise HarvestError("等待 Fief_harvest_res 响应超时") from exc
                except OSError as exc:
                    raise HarvestError(f"读取庄园收获报文失败：{exc}") from exc
                header = self._decode_frame(opcode, frame)
                if header.message_id == HEARTBEAT_MESSAGE_ID:
                    self._send_message(HEARTBEAT_RET_MESSAGE_ID, b"", encrypted=True)
                    continue
                if header.message_id == FIEF_HARVEST_MESSAGE_ID:
                    response = decode_fief_harvest_response(header.data)
                    if response.ret != 0:
                        raise FiefHarvestRejected(response.ret)
                    reward_deadline = min(deadline, time.monotonic() + 1.0)
                    continue
                if header.message_id == STORAGE_ITEM_CHANGE_MESSAGE_ID:
                    notice = decode_item_change_notify(header.data)
                    if notice.source == FIEF_HARVEST_SOURCE:
                        item_changes = notice.items
                        props = notice.props
                        if response is not None:
                            return HarvestResult(response, item_changes, props)
        finally:
            self.socket.close()


def harvest_normal_times(
    endpoint: GameEndpoint,
    timeout: float,
    count: int,
    *,
    client_factory: Callable[[GameEndpoint, float], FiefClient] = FiefClient,
) -> tuple[HarvestResult, ...]:
    """按日常剩余次数执行普通庄园收取，单次调用最多两次。"""

    if not 0 <= count <= 2:
        raise HarvestError("普通庄园收取次数必须在 0 到 2 之间")
    return tuple(
        client_factory(endpoint, timeout).harvest()
        for _ in range(count)
    )


def _format_item_change(change: ItemChange) -> str:
    return item_change_text(change.item_id, change.delta, change.total)


def _format_reward_prop(prop: RewardProp) -> str:
    return reward_text(prop.kind, prop.item_id, prop.amount)


def print_harvest_result(endpoint: GameEndpoint, result: HarvestResult) -> None:
    print(f"庄园收获成功，区服：{zone_name(endpoint.zone_id, endpoint.zone_name)}")
    if result.changes:
        print("材料变动：")
        for change in result.changes:
            print(f"  {_format_item_change(change)}")
    if result.props:
        print("附加奖励：")
        for prop in result.props:
            print(f"  {_format_reward_prop(prop)}")
    if not result.changes and not result.props:
        print("服务器已确认收获；本次没有收到可列出的材料变动通知。")


def run_self_tests() -> None:
    key = bytes.fromhex("133457799BBCDFF1")
    plain = bytes.fromhex("0123456789ABCDEF")
    expected = bytes.fromhex("85E813540F0AB405")
    assert _des_block(plain, key) == expected
    assert _des_block(expected, key, decrypt=True) == plain

    for payload in (b"short payload", b"x" * 101):
        encoded = pack1_encode(payload, "12345678")
        assert pack1_decode(encoded, "12345678") == payload

    assert encode_login_payload("token") == b"\x20\x01\x2a\x05token"
    assert encode_message_header(FIEF_HARVEST_MESSAGE_ID) == b"\x08\xd2\x95\x01"
    assert decode_message_header(encode_message_header(123, b"abc")) == MessageHeader(123, 0, b"abc")

    item = encode_int_field(1, 7001) + encode_int_field(2, 12) + encode_int_field(3, 99)
    notice = encode_int_field(1, FIEF_HARVEST_SOURCE) + encode_bytes_field(2, item)
    decoded_notice = decode_item_change_notify(notice)
    assert decoded_notice.source == FIEF_HARVEST_SOURCE
    assert decoded_notice.items == (ItemChange(7001, 12, 99),)
    negative_item = encode_int_field(1, 7002) + encode_int_field(2, -12) + encode_int_field(3, 87)
    negative_notice = encode_int_field(1, FIEF_HARVEST_SOURCE) + encode_bytes_field(2, negative_item)
    assert decode_item_change_notify(negative_notice).items == (ItemChange(7002, -12, 87),)

    class TestSocket:
        def __init__(self, frames: list[tuple[int, bytes]]) -> None:
            self.frames = frames
            self.binary_frames: list[bytes] = []
            self.text_frames: list[str] = []
            self.closed = False

        def send_binary(self, payload: bytes) -> None:
            self.binary_frames.append(payload)

        def send_text(self, payload: str) -> None:
            self.text_frames.append(payload)

        def recv_message(self, timeout: float) -> tuple[int, bytes]:
            if not self.frames:
                raise socket.timeout()
            return self.frames.pop(0)

        def close(self) -> None:
            self.closed = True

    session_password = "87654321"
    password_payload = encode_bytes_field(
        1, pack1_encode(session_password.encode("utf-8"), SOCKET_PACK_KEY).encode("utf-8")
    )
    notify_payload = encode_int_field(1, FIEF_HARVEST_SOURCE) + encode_bytes_field(2, item)
    fake_socket = TestSocket(
        [
            (0x2, encode_message_header(PACK_PASSWORD_MESSAGE_ID, password_payload)),
            (0x2, pack1_encode(encode_message_header(LOGIN_REUNIQUE_MESSAGE_ID), session_password).encode("utf-8")),
            (0x2, pack1_encode(encode_message_header(FIEF_HARVEST_MESSAGE_ID), session_password).encode("utf-8")),
            (0x2, pack1_encode(encode_message_header(STORAGE_ITEM_CHANGE_MESSAGE_ID, notify_payload), session_password).encode("utf-8")),
        ]
    )
    endpoint = GameEndpoint("ws://test.invalid", "game-token", "1", "test")
    result = FiefClient(endpoint, 1.0, lambda _url, _timeout: fake_socket).harvest()
    assert result.response.ret == 0
    assert result.changes == (ItemChange(7001, 12, 99),)
    assert fake_socket.closed
    assert decode_message_header(fake_socket.binary_frames[0]).message_id == LOGIN_MESSAGE_ID
    harvest_packet = pack1_decode(fake_socket.text_frames[0], session_password)
    assert decode_message_header(harvest_packet) == MessageHeader(FIEF_HARVEST_MESSAGE_ID, 0, b"")
    assert build_parser().parse_args([]).channel_name == "taojin_android_zhuyue"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--token-file", type=Path, default=DEFAULT_TOKEN_FILE)
    parser.add_argument("--account-url", default=DEFAULT_ACCOUNT_URL)
    parser.add_argument("--post-format", choices=("json", "form"), default="json")
    parser.add_argument("--zone-id", help="指定区服 ID；默认优先最近登录区服")
    parser.add_argument("--http-timeout", type=float, default=15.0)
    parser.add_argument("--timeout", type=float, default=15.0, help="WebSocket 登录和响应超时秒数")
    parser.add_argument("--channel-name", default="taojin_android_zhuyue")
    parser.add_argument("--channel-id", default="110001")
    parser.add_argument("--media", default="M521957")
    parser.add_argument("--device-id", default="2c54fe7b2fe5f0fe")
    parser.add_argument("--device-model", default="HONOR REP-AN00")
    parser.add_argument("--system-version", default="Android")
    parser.add_argument("--terminal-info", default="HONOR REP-AN00")
    parser.add_argument("--client-ip", default="112.10.204.243")
    parser.add_argument("--imei", default="i am imei")
    parser.add_argument("--mac", default="i am mac")
    parser.add_argument("--oaid", default="")
    parser.add_argument("--android-id", default="")
    parser.add_argument("--device-extend", default="{}")
    parser.add_argument("--build-version", default="1.4.1.30")
    parser.add_argument("--update-version", default="1.4.1")
    parser.add_argument("--self-test", action="store_true", help="只运行本地协议自检，不访问网络")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.self_test:
        run_self_tests()
        print("本地协议自检通过")
        return 0
    try:
        tokens = load_tokens(args.token_file)
        endpoint = resolve_game_endpoint(tokens, args)
        result = FiefClient(endpoint, args.timeout).harvest()
    except HarvestError as exc:
        print(f"庄园收获失败：{exc}", file=sys.stderr)
        return 1
    print_harvest_result(endpoint, result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
