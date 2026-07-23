#!/usr/bin/env python3
"""聚宝之地扫荡的游戏服协议客户端。

读取服务端 ``Map_treasure_info`` 后，仅对服务端列出的可扫荡区域发起
``Map_treasure_sweep`` 请求。单个请求的扫荡次数固定限制在 1 到 30 次。
"""

from __future__ import annotations

import socket
import time
from dataclasses import dataclass
from typing import Callable

from harvest_fief import (
    HEARTBEAT_MESSAGE_ID,
    HEARTBEAT_RET_MESSAGE_ID,
    LOGIN_FAIL_MESSAGE_ID,
    LOGIN_MESSAGE_ID,
    LOGIN_REUNIQUE_MESSAGE_ID,
    PACK_PASSWORD_MESSAGE_ID,
    SOCKET_PACK_KEY,
    GameEndpoint,
    HarvestError,
    ItemChange,
    MessageHeader,
    NativeWebSocket,
    ProtoReader,
    decode_int32,
    decode_message_header,
    decode_pack_password,
    encode_int_field,
    encode_login_payload,
    encode_message_header,
    pack1_decode,
    pack1_encode,
)


MAP_TREASURE_INFO_MESSAGE_ID = 15570
MAP_TREASURE_SWEEP_MESSAGE_ID = 15571
MAP_TREASURE_CLEAR_RESULT_MESSAGE_ID = 15572
KICKOUT_MESSAGE_ID = 10030
MAX_SWEEP_TIMES_PER_REQUEST = 30

# 客户端 ``tbX`` 扫荡拒绝码（见 decrypted-js Map_treasure_sweep 处理）。
SWEEP_RET_HAS_RESULT = 1
SWEEP_RET_AREA_CANNOT_SWEEP = 2
SWEEP_RET_ITEM_LACK = 3
SWEEP_RET_TIMES_LACK = 4
SWEEP_RET_EQUIP_STORAGE_FULL = 5

SWEEP_RET_LABELS = {
    SWEEP_RET_HAS_RESULT: "尚有未领取的扫荡结果",
    SWEEP_RET_AREA_CANNOT_SWEEP: "所选地图当前不可扫荡",
    SWEEP_RET_ITEM_LACK: "寻宝卷轴不足",
    SWEEP_RET_TIMES_LACK: "今日扫荡次数不足",
    SWEEP_RET_EQUIP_STORAGE_FULL: "装备仓库已满",
}


class TreasureAreaError(HarvestError):
    """聚宝之地会话或服务端状态不可用于扫荡。"""


class TreasureAreaRejected(TreasureAreaError):
    """服务端拒绝了扫荡请求。"""

    def __init__(self, ret: int) -> None:
        self.ret = ret
        label = SWEEP_RET_LABELS.get(ret, "未登记的拒绝原因")
        super().__init__(f"聚宝之地扫荡失败：{label}（ret {ret}）")


@dataclass(frozen=True)
class TreasureSweepLoot:
    """扫荡结算 ``results``：区域与物品变动（已去掉密文载荷）。"""

    area_id: int
    items: tuple[ItemChange, ...]
    multi: bool = False


@dataclass(frozen=True)
class TreasureAreaStatus:
    """``Map_treasure_info`` / 扫荡成功响应中的服务端状态。"""

    open_times: int
    refresh_seconds: int
    swept_today: int
    daily_sweep_limit: int
    area_ids: tuple[int, ...]
    has_pending_results: bool = False
    sweep_loot: TreasureSweepLoot | None = None

    @property
    def sweep_remaining(self) -> int:
        return self.daily_sweep_limit - self.swept_today

    def can_sweep(self, area_id: int) -> bool:
        return area_id in self.area_ids


@dataclass(frozen=True)
class TreasureSweepResponse:
    ret: int
    status: TreasureAreaStatus | None


def _decode_packed_int32(data: bytes) -> tuple[int, ...]:
    reader = ProtoReader(data)
    values: list[int] = []
    while reader.position < len(data):
        values.append(decode_int32(reader.read_varint()))
    return tuple(values)


def _decode_loot_item_change(data: bytes) -> ItemChange:
    """Decode Zn ``{id, num, sum}`` as an ``ItemChange``."""

    item_id = 0
    delta = 0
    total = 0
    for field_number, wire_type, value in ProtoReader(data).fields():
        if wire_type != 0:
            continue
        if field_number == 1:
            item_id = decode_int32(int(value))
        elif field_number == 2:
            delta = decode_int32(int(value))
        elif field_number == 3:
            total = decode_int32(int(value))
    return ItemChange(item_id=item_id, delta=delta, total=total)


def _decode_loot_reward_package(data: bytes) -> tuple[ItemChange, ...]:
    """Decode one Jn reward package; only surface item deltas."""

    items: list[ItemChange] = []
    for field_number, wire_type, value in ProtoReader(data).fields():
        if field_number == 2 and wire_type == 2:
            change = _decode_loot_item_change(bytes(value))
            if change.item_id > 0 and change.delta != 0:
                items.append(change)
    return tuple(items)


def decode_treasure_sweep_loot(data: bytes) -> TreasureSweepLoot:
    """Decode TreasureData.results (area + reward packages)."""

    area_id = 0
    items: list[ItemChange] = []
    multi = False
    for field_number, wire_type, value in ProtoReader(data).fields():
        if field_number == 1 and wire_type == 0:
            area_id = decode_int32(int(value))
        elif field_number == 2 and wire_type == 2:
            items.extend(_decode_loot_reward_package(bytes(value)))
        elif field_number == 4 and wire_type == 2:
            # rare 额外奖励包
            items.extend(_decode_loot_reward_package(bytes(value)))
        elif field_number == 5 and wire_type == 0:
            multi = bool(value)
    return TreasureSweepLoot(area_id=area_id, items=tuple(items), multi=multi)


def decode_treasure_area_status(data: bytes) -> TreasureAreaStatus:
    """Decode the generated ``TreasureData`` message from the client bundle."""

    open_times = 0
    refresh_seconds = 0
    swept_today = 0
    daily_sweep_limit = 0
    area_ids: list[int] = []
    has_pending_results = False
    sweep_loot: TreasureSweepLoot | None = None
    for field_number, wire_type, value in ProtoReader(data).fields():
        if field_number == 1 and wire_type == 0:
            open_times = decode_int32(int(value))
        elif field_number == 2 and wire_type == 0:
            refresh_seconds = decode_int32(int(value))
        elif field_number == 3 and wire_type == 0:
            swept_today = decode_int32(int(value))
        elif field_number == 4 and wire_type == 0:
            daily_sweep_limit = decode_int32(int(value))
        elif field_number == 5 and wire_type == 0:
            area_ids.append(decode_int32(int(value)))
        elif field_number == 5 and wire_type == 2:
            area_ids.extend(_decode_packed_int32(bytes(value)))
        elif field_number == 6 and wire_type == 2:
            # 原生客户端在再次扫荡前会清除未读 results。
            has_pending_results = True
            sweep_loot = decode_treasure_sweep_loot(bytes(value))

    if swept_today < 0 or daily_sweep_limit < swept_today:
        raise TreasureAreaError("聚宝之地当日扫荡次数状态无效")
    if any(area_id <= 0 for area_id in area_ids):
        raise TreasureAreaError("聚宝之地包含无效地图 ID")
    if len(set(area_ids)) != len(area_ids):
        raise TreasureAreaError("聚宝之地返回重复的可扫荡地图")
    return TreasureAreaStatus(
        open_times=open_times,
        refresh_seconds=refresh_seconds,
        swept_today=swept_today,
        daily_sweep_limit=daily_sweep_limit,
        area_ids=tuple(area_ids),
        has_pending_results=has_pending_results,
        sweep_loot=sweep_loot,
    )


def encode_treasure_sweep_request(area_id: int, times: int) -> bytes:
    """Encode ``Map_treasure_sweep`` with the same fields as the native client."""

    if not isinstance(area_id, int) or isinstance(area_id, bool) or area_id <= 0:
        raise ValueError("聚宝之地地图 ID 必须是正整数")
    if not isinstance(times, int) or isinstance(times, bool) or not 1 <= times <= MAX_SWEEP_TIMES_PER_REQUEST:
        raise ValueError(f"聚宝之地单次扫荡次数必须是 1 到 {MAX_SWEEP_TIMES_PER_REQUEST} 之间的整数")
    return (
        encode_int_field(1, area_id)
        + encode_int_field(2, 1)
        + encode_int_field(3, times)
    )


def decode_treasure_sweep_response(data: bytes) -> TreasureSweepResponse:
    ret = 0
    status: TreasureAreaStatus | None = None
    for field_number, wire_type, value in ProtoReader(data).fields():
        if field_number == 1 and wire_type == 0:
            ret = decode_int32(int(value))
        elif field_number == 2 and wire_type == 2:
            status = decode_treasure_area_status(bytes(value))
    return TreasureSweepResponse(ret=ret, status=status)


class TreasureAreaClient:
    """短生命周期的聚宝之地查询与扫荡会话。"""

    def __init__(
        self,
        endpoint: GameEndpoint,
        timeout: float,
        *,
        socket_factory: Callable[[str, float], NativeWebSocket] = NativeWebSocket.connect,
    ) -> None:
        self.endpoint = endpoint
        self.timeout = timeout
        self.socket_factory = socket_factory
        self.socket: NativeWebSocket | None = None
        self.password: str | None = None

    def __enter__(self) -> "TreasureAreaClient":
        self.login()
        return self

    def __exit__(self, _exc_type: object, _exc: object, _traceback: object) -> None:
        self.close()

    def close(self) -> None:
        if self.socket is not None:
            self.socket.close()
            self.socket = None

    def _send_message(self, message_id: int, data: bytes = b"", *, encrypted: bool) -> None:
        if self.socket is None:
            raise TreasureAreaError("WebSocket 尚未连接")
        packet = encode_message_header(message_id, data)
        if encrypted:
            if not self.password:
                raise TreasureAreaError("游戏服尚未下发会话密码")
            self.socket.send_text(pack1_encode(packet, self.password))
        else:
            self.socket.send_binary(packet)

    def _decode_frame(self, opcode: int, payload: bytes) -> MessageHeader:
        if self.password is not None:
            if opcode not in (0x1, 0x2):
                raise TreasureAreaError(f"加密游戏报文 opcode 异常：{opcode}")
            payload = pack1_decode(payload, self.password)
        return decode_message_header(payload)

    def _receive_header(self, deadline: float, context: str) -> MessageHeader:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TreasureAreaError(f"等待{context}超时")
        try:
            assert self.socket is not None
            opcode, payload = self.socket.recv_message(remaining)
        except socket.timeout as exc:
            raise TreasureAreaError(f"等待{context}超时") from exc
        except OSError as exc:
            raise TreasureAreaError(f"读取{context}报文失败：{exc}") from exc
        return self._decode_frame(opcode, payload)

    def _handle_common_message(self, header: MessageHeader) -> bool:
        if header.message_id == HEARTBEAT_MESSAGE_ID:
            self._send_message(
                HEARTBEAT_RET_MESSAGE_ID,
                encrypted=self.password is not None,
            )
            return True
        if header.message_id == LOGIN_FAIL_MESSAGE_ID:
            raise TreasureAreaError("游戏服 Login 失败")
        if header.message_id == KICKOUT_MESSAGE_ID:
            ret = 0
            for field_number, wire_type, value in ProtoReader(header.data).fields():
                if field_number == 1 and wire_type == 0:
                    ret = decode_int32(int(value))
                    break
            raise TreasureAreaError(f"游戏服终止聚宝之地会话：ret={ret}")
        return False

    def login(self) -> None:
        if self.socket is not None:
            return
        self.socket = self.socket_factory(self.endpoint.url, self.timeout)
        try:
            self._send_message(
                LOGIN_MESSAGE_ID,
                encode_login_payload(self.endpoint.game_token),
                encrypted=False,
            )
            deadline = time.monotonic() + self.timeout
            while True:
                header = self._receive_header(deadline, "游戏服登录")
                if header.message_id == PACK_PASSWORD_MESSAGE_ID:
                    encrypted_password = decode_pack_password(header.data)
                    try:
                        self.password = pack1_decode(
                            encrypted_password, SOCKET_PACK_KEY
                        ).decode("utf-8")
                    except UnicodeDecodeError as exc:
                        raise TreasureAreaError("游戏服会话密码不是 UTF-8 文本") from exc
                    continue
                if self._handle_common_message(header):
                    continue
                if header.message_id == LOGIN_REUNIQUE_MESSAGE_ID:
                    break
            # SocketManager 在 Login_reunique 后再调度缓存业务消息。
            time.sleep(0.1)
        except Exception:
            self.close()
            raise

    def get_status(self) -> TreasureAreaStatus:
        self.login()
        self._send_message(MAP_TREASURE_INFO_MESSAGE_ID, encrypted=True)
        deadline = time.monotonic() + self.timeout
        while True:
            header = self._receive_header(deadline, "Map_treasure_info 响应")
            if self._handle_common_message(header):
                continue
            if header.message_id == MAP_TREASURE_INFO_MESSAGE_ID:
                status = decode_treasure_area_status(header.data)
                if not status.has_pending_results:
                    return status
                return self._clear_pending_results()

    def _clear_pending_results(self) -> TreasureAreaStatus:
        self._send_message(MAP_TREASURE_CLEAR_RESULT_MESSAGE_ID, encrypted=True)
        deadline = time.monotonic() + self.timeout
        while True:
            header = self._receive_header(deadline, "Map_treasure_clear_result 响应")
            if self._handle_common_message(header):
                continue
            if header.message_id == MAP_TREASURE_CLEAR_RESULT_MESSAGE_ID:
                return decode_treasure_area_status(header.data)

    def sweep(self, area_id: int, times: int) -> TreasureSweepResponse:
        self.login()
        self._send_message(
            MAP_TREASURE_SWEEP_MESSAGE_ID,
            encode_treasure_sweep_request(area_id, times),
            encrypted=True,
        )
        deadline = time.monotonic() + self.timeout
        while True:
            header = self._receive_header(deadline, "Map_treasure_sweep 响应")
            if self._handle_common_message(header):
                continue
            if header.message_id == MAP_TREASURE_SWEEP_MESSAGE_ID:
                response = decode_treasure_sweep_response(header.data)
                if response.ret != 0:
                    raise TreasureAreaRejected(response.ret)
                if response.status is None:
                    raise TreasureAreaError("聚宝之地扫荡成功响应缺少最新状态")
                return response
