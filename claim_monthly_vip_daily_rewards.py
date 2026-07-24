#!/usr/bin/env python3
"""领取盈月之仪（月度权益）的每日奖励。

默认读取同目录 ``tokens.json`` 中的 ``userid`` 和 ``verify_token``，复用当前
Android 客户端的账号服、区服网关与游戏服登录流程，然后为基础和进阶月度权益各
发送一次每日奖励请求。服务端会独立校验月度权益状态与当日领取状态。

用法：
    venv/bin/python claim_monthly_vip_daily_rewards.py
    venv/bin/python claim_monthly_vip_daily_rewards.py --vip-id 1
    venv/bin/python claim_monthly_vip_daily_rewards.py --self-test
"""

from __future__ import annotations

from pathlib import Path

import argparse
import socket
import sys
import time
from dataclasses import dataclass
from typing import Callable, Iterable

from harvest_fief import (
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
    MessageHeader,
    NativeWebSocket,
    ProtoReader,
    RewardProp,
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
from id_descriptions import item_change_text, reward_text, unknown_name, zone_name


MONTHLY_VIP_DAILY_REWARDS_MESSAGE_ID = 12416
MONTHLY_VIP_DAILY_REWARD_SOURCE = 142
VIP_BASE_ID = 1
VIP_PRO_ID = 2

RESULT_SUCCESS = 0
RESULT_INVALID_ID = 1
RESULT_NOT_ENABLED = 2
RESULT_TODAY_ALREADY_GAINED = 3

RESULT_LABELS = {
    RESULT_SUCCESS: "领取成功",
    RESULT_INVALID_ID: "月度权益 ID 无效",
    RESULT_NOT_ENABLED: "月度权益未启用",
    RESULT_TODAY_ALREADY_GAINED: "今日已领取",
}
VIP_LABELS = {
    VIP_BASE_ID: "基础月度权益",
    VIP_PRO_ID: "进阶月度权益",
}


@dataclass(frozen=True)
class MonthlyVipDailyRewardResponse:
    vip_id: int
    result: int


@dataclass(frozen=True)
class MonthlyVipDailyRewardResult:
    response: MonthlyVipDailyRewardResponse
    changes: tuple[ItemChange, ...]
    props: tuple[RewardProp, ...]


def encode_monthly_vip_daily_rewards_payload(vip_id: int) -> bytes:
    """Encode ``Pay_get_monthly_vip_dailyrewards`` C2S: ``id`` is field 1."""

    if vip_id <= 0:
        raise HarvestError("月度权益 ID 必须为正整数")
    return encode_int_field(1, vip_id)


def decode_monthly_vip_daily_rewards_response(data: bytes) -> MonthlyVipDailyRewardResponse:
    """Decode S2C fields: ``id`` is field 1 and ``result`` is field 10."""

    vip_id = 0
    result = 0
    for field_number, wire_type, value in ProtoReader(data).fields():
        if wire_type != 0:
            continue
        if field_number == 1:
            vip_id = int(value)
        elif field_number == 10:
            result = int(value)
    return MonthlyVipDailyRewardResponse(vip_id=vip_id, result=result)


class MonthlyVipDailyRewardsClient:
    """建立游戏会话并顺序领取指定月度权益的每日奖励。"""

    def __init__(
        self,
        endpoint: GameEndpoint,
        timeout: float,
        *,
        socket_factory: Callable[[str, float], NativeWebSocket] = NativeWebSocket.connect,
        websocket_log: Path | bool | None = True,
    ) -> None:
        self.endpoint = endpoint
        self.timeout = timeout
        self.socket_factory = socket_factory
        self.socket: NativeWebSocket | None = None
        self.password: str | None = None
        bind_traffic_logging(
            self,
            task="monthly_vip_daily",
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

    def _decode_frame(self, opcode: int, payload: bytes) -> MessageHeader:
        if self.password is not None:
            if opcode not in (0x1, 0x2):
                raise HarvestError(f"加密游戏报文 opcode 异常：{opcode}")
            payload = pack1_decode(payload, self.password)
        return decode_message_header(payload)

    def _handle_login_message(self, header: MessageHeader) -> bool:
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

    def _login(self) -> None:
        self.socket = self.socket_factory(self.endpoint.url, self.timeout)
        self._send_message(
            LOGIN_MESSAGE_ID,
            encode_login_payload(self.endpoint.game_token),
            encrypted=False,
        )
        deadline = time.monotonic() + self.timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise HarvestError("等待游戏服登录完成超时")
            try:
                opcode, frame = self.socket.recv_message(remaining)
            except socket.timeout as exc:
                raise HarvestError("等待游戏服登录完成超时") from exc
            except OSError as exc:
                raise HarvestError(f"读取游戏服登录报文失败：{exc}") from exc
            if self._handle_login_message(self._decode_frame(opcode, frame)):
                break

        # 客户端会在 Login_reunique 后延迟发送缓存的业务消息。
        time.sleep(0.1)

    def _claim_one(self, vip_id: int) -> MonthlyVipDailyRewardResult:
        self._send_message(
            MONTHLY_VIP_DAILY_REWARDS_MESSAGE_ID,
            encode_monthly_vip_daily_rewards_payload(vip_id),
            encrypted=True,
        )
        response: MonthlyVipDailyRewardResponse | None = None
        changes: tuple[ItemChange, ...] = ()
        props: tuple[RewardProp, ...] = ()
        deadline = time.monotonic() + self.timeout
        reward_deadline: float | None = None

        while True:
            now = time.monotonic()
            current_deadline = reward_deadline if reward_deadline is not None else deadline
            if now >= current_deadline:
                if response is not None:
                    return MonthlyVipDailyRewardResult(response, changes, props)
                raise HarvestError("等待盈月之仪每日奖励响应超时")
            try:
                assert self.socket is not None
                opcode, frame = self.socket.recv_message(current_deadline - now)
            except socket.timeout as exc:
                if response is not None:
                    return MonthlyVipDailyRewardResult(response, changes, props)
                raise HarvestError("等待盈月之仪每日奖励响应超时") from exc
            except OSError as exc:
                raise HarvestError(f"读取盈月之仪每日奖励报文失败：{exc}") from exc

            header = self._decode_frame(opcode, frame)
            if header.message_id == HEARTBEAT_MESSAGE_ID:
                self._send_message(HEARTBEAT_RET_MESSAGE_ID, b"", encrypted=True)
                continue
            if header.message_id == MONTHLY_VIP_DAILY_REWARDS_MESSAGE_ID:
                response = decode_monthly_vip_daily_rewards_response(header.data)
                if response.vip_id not in (0, vip_id):
                    raise HarvestError(
                        f"每日奖励响应权益 ID 不匹配：请求 {vip_id}，响应 {response.vip_id}"
                    )
                if response.result != RESULT_SUCCESS:
                    return MonthlyVipDailyRewardResult(response, changes, props)
                reward_deadline = min(deadline, time.monotonic() + 1.0)
                if changes or props:
                    return MonthlyVipDailyRewardResult(response, changes, props)
                continue
            if header.message_id == STORAGE_ITEM_CHANGE_MESSAGE_ID:
                notice = decode_item_change_notify(header.data)
                if notice.source == MONTHLY_VIP_DAILY_REWARD_SOURCE:
                    changes = notice.items
                    props = notice.props
                    if response is not None:
                        return MonthlyVipDailyRewardResult(response, changes, props)

    def claim(self, vip_ids: Iterable[int]) -> tuple[MonthlyVipDailyRewardResult, ...]:
        requested_ids = tuple(vip_ids)
        if not requested_ids:
            raise HarvestError("至少指定一个月度权益 ID")
        if len(set(requested_ids)) != len(requested_ids):
            raise HarvestError("月度权益 ID 不能重复")
        for vip_id in requested_ids:
            if vip_id <= 0:
                raise HarvestError("月度权益 ID 必须为正整数")

        try:
            self._login()
            return tuple(self._claim_one(vip_id) for vip_id in requested_ids)
        finally:
            if self.socket is not None:
                self.socket.close()


def _format_item_change(change: ItemChange) -> str:
    return item_change_text(change.item_id, change.delta, change.total)


def _format_reward_prop(prop: RewardProp) -> str:
    return reward_text(prop.kind, prop.item_id, prop.amount)


def print_claim_results(endpoint: GameEndpoint, results: Iterable[MonthlyVipDailyRewardResult]) -> None:
    print(f"盈月之仪每日奖励请求完成，区服：{zone_name(endpoint.zone_id, endpoint.zone_name)}")
    for item in results:
        response = item.response
        vip_id = response.vip_id or 0
        label = VIP_LABELS.get(vip_id, unknown_name("月度权益", vip_id))
        status = RESULT_LABELS.get(response.result, f"服务端返回 result={response.result}")
        print(f"{label}：{status}")
        for change in item.changes:
            print(f"  {_format_item_change(change)}")
        for prop in item.props:
            print(f"  {_format_reward_prop(prop)}")


def run_self_tests() -> None:
    assert encode_monthly_vip_daily_rewards_payload(1) == b"\x08\x01"
    assert decode_monthly_vip_daily_rewards_response(
        encode_int_field(1, 2) + encode_int_field(10, RESULT_TODAY_ALREADY_GAINED)
    ) == MonthlyVipDailyRewardResponse(2, RESULT_TODAY_ALREADY_GAINED)

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
    item = encode_int_field(1, 120) + encode_int_field(2, 1) + encode_int_field(3, 31)
    notice_payload = encode_int_field(1, MONTHLY_VIP_DAILY_REWARD_SOURCE) + encode_bytes_field(
        2, item
    )
    success_payload = encode_int_field(1, VIP_BASE_ID)
    already_claimed_payload = encode_int_field(1, VIP_PRO_ID) + encode_int_field(
        10, RESULT_TODAY_ALREADY_GAINED
    )
    fake_socket = TestSocket(
        [
            (0x2, encode_message_header(PACK_PASSWORD_MESSAGE_ID, password_payload)),
            (
                0x2,
                pack1_encode(
                    encode_message_header(LOGIN_REUNIQUE_MESSAGE_ID), session_password
                ).encode("utf-8"),
            ),
            (
                0x2,
                pack1_encode(
                    encode_message_header(
                        MONTHLY_VIP_DAILY_REWARDS_MESSAGE_ID, success_payload
                    ),
                    session_password,
                ).encode("utf-8"),
            ),
            (
                0x2,
                pack1_encode(
                    encode_message_header(STORAGE_ITEM_CHANGE_MESSAGE_ID, notice_payload),
                    session_password,
                ).encode("utf-8"),
            ),
            (
                0x2,
                pack1_encode(
                    encode_message_header(
                        MONTHLY_VIP_DAILY_REWARDS_MESSAGE_ID, already_claimed_payload
                    ),
                    session_password,
                ).encode("utf-8"),
            ),
        ]
    )
    endpoint = GameEndpoint("ws://test.invalid", "game-token", "1", "test")
    results = MonthlyVipDailyRewardsClient(
        endpoint, 1.0, socket_factory=lambda _url, _timeout: fake_socket
    ).claim((VIP_BASE_ID, VIP_PRO_ID))

    assert results[0].response == MonthlyVipDailyRewardResponse(VIP_BASE_ID, RESULT_SUCCESS)
    assert results[0].changes == (ItemChange(120, 1, 31),)
    assert results[1].response == MonthlyVipDailyRewardResponse(
        VIP_PRO_ID, RESULT_TODAY_ALREADY_GAINED
    )
    assert fake_socket.closed
    assert decode_message_header(fake_socket.binary_frames[0]).message_id == LOGIN_MESSAGE_ID
    assert len(fake_socket.text_frames) == 2
    first_request = pack1_decode(fake_socket.text_frames[0], session_password)
    second_request = pack1_decode(fake_socket.text_frames[1], session_password)
    assert decode_message_header(first_request) == MessageHeader(
        MONTHLY_VIP_DAILY_REWARDS_MESSAGE_ID,
        0,
        encode_monthly_vip_daily_rewards_payload(VIP_BASE_ID),
    )
    assert decode_message_header(second_request) == MessageHeader(
        MONTHLY_VIP_DAILY_REWARDS_MESSAGE_ID,
        0,
        encode_monthly_vip_daily_rewards_payload(VIP_PRO_ID),
    )


def build_parser() -> argparse.ArgumentParser:
    parser = build_base_parser()
    parser.prog = "claim_monthly_vip_daily_rewards.py"
    parser.description = __doc__
    parser.add_argument(
        "--vip-id",
        type=int,
        action="append",
        help="领取指定月度权益 ID；可重复传入。默认按客户端逻辑依次处理 1 和 2。",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.self_test:
        run_self_tests()
        print("盈月之仪每日奖励本地协议自检通过")
        return 0

    vip_ids = tuple(args.vip_id) if args.vip_id else (VIP_BASE_ID, VIP_PRO_ID)
    try:
        tokens = load_tokens(args.token_file)
        endpoint = resolve_game_endpoint(tokens, args)
        results = MonthlyVipDailyRewardsClient(endpoint, args.timeout).claim(vip_ids)
    except HarvestError as exc:
        print(f"盈月之仪每日奖励领取失败：{exc}", file=sys.stderr)
        return 1
    print_claim_results(endpoint, results)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
