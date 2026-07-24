#!/usr/bin/env python3
"""收取萃华仪的普通产出。

默认读取同目录 ``tokens.json`` 的 ``userid`` 与 ``verify_token``，完成账号服、
区服网关和游戏 WebSocket 登录后，发送一次萃华仪普通收获。

用法：
    .venv/bin/python harvest_furnace.py
    .venv/bin/python harvest_furnace.py --zone-id 4101
    .venv/bin/python harvest_furnace.py --self-test
"""

from __future__ import annotations

from pathlib import Path

import argparse
import socket
import sys
import time
from dataclasses import dataclass
from typing import Callable

from harvest_fief import (
    FIEF_HARVEST_SOURCE,
    HEARTBEAT_MESSAGE_ID,
    HEARTBEAT_RET_MESSAGE_ID,
    LOGIN_FAIL_MESSAGE_ID,
    LOGIN_MESSAGE_ID,
    LOGIN_REUNIQUE_MESSAGE_ID,
    PACK_PASSWORD_MESSAGE_ID,
    SOCKET_PACK_KEY,
    STORAGE_ITEM_CHANGE_MESSAGE_ID,
    GameEndpoint,
    HarvestError,
    ItemChange,
    NativeWebSocket,
    ProtoReader,
    decode_item_change_notify,
    decode_message_header,
    decode_pack_password,
    encode_bytes_field,
    encode_int_field,
    encode_login_payload,
    encode_message_header,
    load_tokens,
    pack1_decode,
    pack1_encode,
    resolve_game_endpoint,
)
from ws_traffic_log import bind_traffic_logging
from harvest_fief import build_parser as build_base_parser
from id_descriptions import item_change_text, zone_name


FURNACE_CAPTURE_MESSAGE_ID = 19298
FURNACE_CAPTURE_NORMAL_SOURCE = 460


@dataclass(frozen=True)
class FurnaceState:
    last_refresh_ts: int
    last_quick_ts: int
    quick_refresh_left: int
    quick_count: int
    quick_id: int


@dataclass(frozen=True)
class FurnaceCaptureResponse:
    ret: int
    mode: int
    furnace: FurnaceState | None
    soul_ret: int


@dataclass(frozen=True)
class FurnaceHarvestResult:
    response: FurnaceCaptureResponse
    changes: tuple[ItemChange, ...]


def encode_furnace_capture_payload(mode: int = 0, capture_type: int = 0, times: int = 0) -> bytes:
    """Encode Furnace_capture; normal click uses three omitted zero fields."""

    packet = b""
    if mode:
        packet += encode_int_field(1, mode)
    if capture_type:
        packet += encode_int_field(2, capture_type)
    if times:
        packet += encode_int_field(3, times)
    return packet


def decode_furnace_state(data: bytes) -> FurnaceState:
    values = {
        "last_refresh_ts": 0,
        "last_quick_ts": 0,
        "quick_refresh_left": 0,
        "quick_count": 0,
        "quick_id": 0,
    }
    for field_number, wire_type, value in ProtoReader(data).fields():
        if wire_type != 0:
            continue
        if field_number == 1:
            values["last_refresh_ts"] = int(value)
        elif field_number == 2:
            values["last_quick_ts"] = int(value)
        elif field_number == 3:
            values["quick_refresh_left"] = int(value)
        elif field_number == 4:
            values["quick_count"] = int(value)
        elif field_number == 5:
            values["quick_id"] = int(value)
    return FurnaceState(**values)


def decode_furnace_capture_response(data: bytes) -> FurnaceCaptureResponse:
    ret = 0
    mode = 0
    furnace: FurnaceState | None = None
    soul_ret = 0
    for field_number, wire_type, value in ProtoReader(data).fields():
        if field_number == 1 and wire_type == 0:
            ret = int(value)
        elif field_number == 2 and wire_type == 0:
            mode = int(value)
        elif field_number == 3 and wire_type == 2:
            furnace = decode_furnace_state(bytes(value))
        elif field_number == 5 and wire_type == 0:
            soul_ret = int(value)
    return FurnaceCaptureResponse(ret=ret, mode=mode, furnace=furnace, soul_ret=soul_ret)


class FurnaceClient:
    def __init__(
        self,
        endpoint: GameEndpoint,
        timeout: float,
        *,
        last_sid: int = 0,
        unique: int = 0,
        socket_factory: Callable[[str, float], NativeWebSocket] = NativeWebSocket.connect,
        websocket_log: Path | bool | None = True,
    ) -> None:
        self.endpoint = endpoint
        self.timeout = timeout
        self.last_sid = last_sid
        self.unique = unique
        self.socket_factory = socket_factory
        self.socket: NativeWebSocket | None = None
        self.password: str | None = None
        bind_traffic_logging(
            self,
            task="furnace_harvest",
            path=websocket_log,
            error_cls=HarvestError,
        )

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

    def _decode_frame(self, opcode: int, payload: bytes):
        if self.password is not None:
            if opcode not in (0x1, 0x2):
                raise HarvestError(f"加密游戏报文 opcode 异常：{opcode}")
            payload = pack1_decode(payload, self.password)
        return decode_message_header(payload)

    def _handle_login_message(self, header) -> bool:
        if header.message_id == PACK_PASSWORD_MESSAGE_ID:
            encrypted_password = decode_pack_password(header.data)
            try:
                self.password = pack1_decode(encrypted_password, SOCKET_PACK_KEY).decode("utf-8")
            except UnicodeDecodeError as exc:
                raise HarvestError("游戏服会话密码不是 UTF-8 文本") from exc
            return False
        if header.message_id == HEARTBEAT_MESSAGE_ID:
            self._send_message(
                HEARTBEAT_RET_MESSAGE_ID,
                b"",
                encrypted=self.password is not None,
            )
            return False
        if header.message_id == LOGIN_FAIL_MESSAGE_ID:
            raise HarvestError("游戏服 Login 失败")
        return header.message_id == LOGIN_REUNIQUE_MESSAGE_ID

    def harvest(self) -> FurnaceHarvestResult:
        self.socket = self.socket_factory(self.endpoint.url, self.timeout)
        try:
            self._send_message(
                LOGIN_MESSAGE_ID,
                encode_login_payload(
                    self.endpoint.game_token,
                    last_sid=self.last_sid,
                    unique=self.unique,
                ),
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
                if self._handle_login_message(self._decode_frame(opcode, frame)):
                    break

            # SocketManager sends business traffic 100 ms after Login_reunique.
            time.sleep(0.1)
            self._send_message(
                FURNACE_CAPTURE_MESSAGE_ID,
                encode_furnace_capture_payload(),
                encrypted=True,
            )

            response: FurnaceCaptureResponse | None = None
            changes: tuple[ItemChange, ...] = ()
            deadline = time.monotonic() + self.timeout
            reward_deadline: float | None = None
            while True:
                now = time.monotonic()
                current_deadline = reward_deadline if reward_deadline is not None else deadline
                if now >= current_deadline:
                    if response is not None:
                        return FurnaceHarvestResult(response, changes)
                    raise HarvestError("等待 Furnace_capture 响应超时")
                try:
                    opcode, frame = self.socket.recv_message(current_deadline - now)
                except socket.timeout as exc:
                    if response is not None:
                        return FurnaceHarvestResult(response, changes)
                    raise HarvestError("等待 Furnace_capture 响应超时") from exc
                header = self._decode_frame(opcode, frame)
                if header.message_id == HEARTBEAT_MESSAGE_ID:
                    self._send_message(HEARTBEAT_RET_MESSAGE_ID, b"", encrypted=True)
                    continue
                if header.message_id == FURNACE_CAPTURE_MESSAGE_ID:
                    response = decode_furnace_capture_response(header.data)
                    if response.ret != 0:
                        raise HarvestError(f"萃华仪收获返回 ret={response.ret}")
                    reward_deadline = min(deadline, time.monotonic() + 1.0)
                    continue
                if header.message_id == STORAGE_ITEM_CHANGE_MESSAGE_ID:
                    notice = decode_item_change_notify(header.data)
                    if notice.source == FURNACE_CAPTURE_NORMAL_SOURCE:
                        changes = notice.items
                        if response is not None:
                            return FurnaceHarvestResult(response, changes)
        finally:
            self.socket.close()


def _format_item_change(change: ItemChange) -> str:
    return item_change_text(change.item_id, change.delta, change.total)


def print_harvest_result(endpoint: GameEndpoint, result: FurnaceHarvestResult) -> None:
    print(f"萃华仪收获成功，区服：{zone_name(endpoint.zone_id, endpoint.zone_name)}")
    if result.changes:
        print("材料变动：")
        for change in result.changes:
            print(f"  {_format_item_change(change)}")
    else:
        print("服务器已确认收获；本次没有收到可列出的材料变动通知。")


def run_self_tests() -> None:
    assert encode_furnace_capture_payload() == b""
    assert encode_furnace_capture_payload(1, 2, 3) == b"\x08\x01\x10\x02\x18\x03"

    furnace = (
        encode_int_field(1, 100)
        + encode_int_field(2, 200)
        + encode_int_field(3, 300)
        + encode_int_field(4, 4)
        + encode_int_field(5, 5)
    )
    response = decode_furnace_capture_response(
        encode_int_field(1, 0) + encode_bytes_field(3, furnace)
    )
    assert response.ret == 0
    assert response.furnace == FurnaceState(100, 200, 300, 4, 5)

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
        1,
        pack1_encode(session_password.encode("utf-8"), SOCKET_PACK_KEY).encode("utf-8"),
    )
    item = encode_int_field(1, 303) + encode_int_field(2, 10) + encode_int_field(3, 20)
    notice_payload = encode_int_field(1, FURNACE_CAPTURE_NORMAL_SOURCE) + encode_bytes_field(2, item)
    fake_socket = TestSocket(
        [
            (0x2, encode_message_header(PACK_PASSWORD_MESSAGE_ID, password_payload)),
            (0x2, pack1_encode(encode_message_header(LOGIN_REUNIQUE_MESSAGE_ID), session_password).encode("utf-8")),
            (0x2, pack1_encode(encode_message_header(FURNACE_CAPTURE_MESSAGE_ID), session_password).encode("utf-8")),
            (0x2, pack1_encode(encode_message_header(STORAGE_ITEM_CHANGE_MESSAGE_ID, notice_payload), session_password).encode("utf-8")),
        ]
    )
    endpoint = GameEndpoint("ws://test.invalid", "game-token", "1", "test")
    result = FurnaceClient(endpoint, 1.0, socket_factory=lambda _url, _timeout: fake_socket).harvest()
    assert result.response.ret == 0
    assert result.changes == (ItemChange(303, 10, 20),)
    assert fake_socket.closed
    assert decode_message_header(fake_socket.binary_frames[0]).message_id == LOGIN_MESSAGE_ID
    furnace_packet = pack1_decode(fake_socket.text_frames[0], session_password)
    assert decode_message_header(furnace_packet).message_id == FURNACE_CAPTURE_MESSAGE_ID
    assert decode_message_header(furnace_packet).data == b""


def build_parser() -> argparse.ArgumentParser:
    parser = build_base_parser()
    parser.prog = "harvest_furnace.py"
    parser.description = __doc__
    parser.add_argument("--last-sid", type=int, default=0, help="恢复会话时使用的 lastSId")
    parser.add_argument("--unique", type=int, default=0, help="恢复会话时使用的 unique")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.self_test:
        run_self_tests()
        print("萃华仪本地协议自检通过")
        return 0
    try:
        tokens = load_tokens(args.token_file)
        endpoint = resolve_game_endpoint(tokens, args)
        result = FurnaceClient(
            endpoint,
            args.timeout,
            last_sid=args.last_sid,
            unique=args.unique,
        ).harvest()
    except HarvestError as exc:
        print(f"萃华仪收获失败：{exc}", file=sys.stderr)
        return 1
    print_harvest_result(endpoint, result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
