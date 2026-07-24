#!/usr/bin/env python3
"""检查并使用秘法塔的每日免费探索额度。

默认读取同目录 ``tokens.json`` 中的 ``userid`` 和 ``verify_token``，完成账号服、
区服网关和游戏 WebSocket 登录。脚本只对 ``Game_data`` 中
``category=GACHA_CATEGORY_ARTIFACT`` 且 ``freePullTimesLeft > 0`` 的探索池发送
显式免费单次探索；请求不会携带消耗物品 ID。

用法：
    .venv/bin/python explore_arcane_tower_daily_free.py
    .venv/bin/python explore_arcane_tower_daily_free.py --zone-id 4101
    .venv/bin/python explore_arcane_tower_daily_free.py --self-test
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
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
    MessageHeader,
    NativeWebSocket,
    ProtoReader,
    RewardProp,
    decode_int32,
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
from id_descriptions import reward_name, reward_text, unknown_name, zone_name
from logging_store import MANAGED_DESTINATION, LogPersistenceError, write_standard_log


GAME_DATA_MESSAGE_ID = 10490
KICKOUT_MESSAGE_ID = 10030
PULL_GACHA_BANNER_V2_MESSAGE_ID = 19532
GACHA_CATEGORY_ARTIFACT = 1
LOGIN_KICKOUT_RETRY_DELAY = 3.0
PROJECT_ROOT = Path(__file__).resolve().parent
DEFAULT_RESULT_LOG = MANAGED_DESTINATION

RESULT_SUCCESS = 0
RESULT_INVALID_ID = 1
RESULT_BANNER_REMOVED = 2
RESULT_FREEPULL_ALREADY_USED = 3
RESULT_INVALID_PULLTIMES = 4
RESULT_INVALID_PULLMETHOD = 5
RESULT_INVALID_PULL_COSTITEM_ID = 6
RESULT_EXCEED_DAILY_LIMIT = 7
RESULT_COST_NOT_ENOUGH = 8
RESULT_NOT_PULLABLE = 9
RESULT_PULLCOST_UNKNOWN_ERR = 15

RESULT_LABELS = {
    RESULT_SUCCESS: "探索成功",
    RESULT_INVALID_ID: "探索池 ID 无效",
    RESULT_BANNER_REMOVED: "探索池已移除",
    RESULT_FREEPULL_ALREADY_USED: "免费探索额度已使用",
    RESULT_INVALID_PULLTIMES: "探索次数无效",
    RESULT_INVALID_PULLMETHOD: "探索方式无效",
    RESULT_INVALID_PULL_COSTITEM_ID: "消耗物品无效",
    RESULT_EXCEED_DAILY_LIMIT: "超过每日上限",
    RESULT_COST_NOT_ENOUGH: "资源不足",
    RESULT_NOT_PULLABLE: "当前不可探索",
    RESULT_PULLCOST_UNKNOWN_ERR: "探索消耗校验异常",
}

PROP_KIND_LABELS = {
    1: "物品",
    2: "奖励箱",
    3: "装备",
    4: "秘宝",
    5: "英雄",
    6: "符文",
    7: "活动装备",
}


class GameSessionKickout(HarvestError):
    """The game server ended the newly opened session with a reason code."""

    def __init__(self, ret: int, message: str = "") -> None:
        self.ret = ret
        self.message = message
        detail = f"，消息={message}" if message else ""
        super().__init__(f"游戏服终止会话：ret={ret}{detail}")


@dataclass(frozen=True)
class GachaBannerState:
    banner_id: int
    pull_times_today: int
    free_pull_times_left: int
    refresh_free_left_secs: int
    category: int
    pull_times_total: int


@dataclass(frozen=True)
class ArcaneTowerExploreResponse:
    banner_id: int
    pull_times: int
    category: int
    props: tuple[RewardProp, ...]
    state: GachaBannerState | None
    result: int


@dataclass(frozen=True)
class ArcaneTowerExploreAttempt:
    banner_id: int
    sequence: int
    free_before: int
    response: ArcaneTowerExploreResponse


@dataclass(frozen=True)
class ArcaneTowerDailyResult:
    banners: tuple[GachaBannerState, ...]
    attempts: tuple[ArcaneTowerExploreAttempt, ...]


def decode_gacha_banner_state(data: bytes) -> GachaBannerState:
    """Decode the fields used from ``GachaBannerClientData``."""

    values = {
        "banner_id": 0,
        "pull_times_today": 0,
        "free_pull_times_left": 0,
        "refresh_free_left_secs": 0,
        "category": 0,
        "pull_times_total": 0,
    }
    for field_number, wire_type, value in ProtoReader(data).fields():
        if wire_type != 0:
            continue
        if field_number == 1:
            values["banner_id"] = decode_int32(int(value))
        elif field_number == 2:
            values["pull_times_today"] = decode_int32(int(value))
        elif field_number == 3:
            values["free_pull_times_left"] = decode_int32(int(value))
        elif field_number == 4:
            values["refresh_free_left_secs"] = decode_int32(int(value))
        elif field_number == 5:
            values["category"] = decode_int32(int(value))
        elif field_number == 9:
            values["pull_times_total"] = decode_int32(int(value))
    return GachaBannerState(**values)


def decode_arcane_tower_banners(game_data: bytes) -> tuple[GachaBannerState, ...]:
    """Extract artifact-category banners from ``Game_data.banners`` field 21."""

    banners: list[GachaBannerState] = []
    for field_number, wire_type, value in ProtoReader(game_data).fields():
        if field_number != 21 or wire_type != 2:
            continue
        banner = decode_gacha_banner_state(bytes(value))
        if banner.category == GACHA_CATEGORY_ARTIFACT:
            if banner.banner_id <= 0:
                raise HarvestError("Game_data 中秘法塔探索池 ID 无效")
            if banner.free_pull_times_left < 0:
                raise HarvestError(
                    f"秘法塔探索池 {banner.banner_id} 的免费次数为负数"
                )
            banners.append(banner)
    return tuple(sorted(banners, key=lambda banner: banner.banner_id))


def encode_arcane_tower_explore_payload(banner_id: int) -> bytes:
    """Encode a one-time, explicitly free ``Pull_gacha_banner_v2`` request."""

    if banner_id <= 0:
        raise HarvestError("秘法塔探索池 ID 必须为正整数")
    return (
        encode_int_field(1, banner_id)
        + encode_int_field(2, 1)
        + encode_int_field(3, GACHA_CATEGORY_ARTIFACT)
        + encode_int_field(5, 1)
    )


def _decode_reward_prop(data: bytes) -> RewardProp:
    kind = 0
    item_id = 0
    amount = 0
    for field_number, wire_type, value in ProtoReader(data).fields():
        if wire_type != 0:
            continue
        if field_number == 1:
            kind = decode_int32(int(value))
        elif field_number == 2:
            item_id = decode_int32(int(value))
        elif field_number == 3:
            amount = decode_int32(int(value))
    return RewardProp(kind=kind, item_id=item_id, amount=amount)


def decode_kickout(data: bytes) -> tuple[int, str]:
    ret = 0
    message = ""
    for field_number, wire_type, value in ProtoReader(data).fields():
        if field_number == 1 and wire_type == 0:
            ret = decode_int32(int(value))
        elif field_number == 2 and wire_type == 2:
            try:
                message = bytes(value).decode("utf-8")
            except UnicodeDecodeError as exc:
                raise HarvestError("Kickout.msg 不是 UTF-8 文本") from exc
    return ret, message


def decode_arcane_tower_explore_response(data: bytes) -> ArcaneTowerExploreResponse:
    banner_id = 0
    pull_times = 0
    category = 0
    props: list[RewardProp] = []
    state: GachaBannerState | None = None
    result = 0
    for field_number, wire_type, value in ProtoReader(data).fields():
        if field_number == 1 and wire_type == 0:
            banner_id = decode_int32(int(value))
        elif field_number == 2 and wire_type == 0:
            pull_times = decode_int32(int(value))
        elif field_number == 3 and wire_type == 0:
            category = decode_int32(int(value))
        elif field_number == 5 and wire_type == 2:
            props.append(_decode_reward_prop(bytes(value)))
        elif field_number == 6 and wire_type == 2:
            state = decode_gacha_banner_state(bytes(value))
        elif field_number == 20 and wire_type == 0:
            result = decode_int32(int(value))
    return ArcaneTowerExploreResponse(
        banner_id=banner_id,
        pull_times=pull_times,
        category=category,
        props=tuple(props),
        state=state,
        result=result,
    )


class ArcaneTowerClient:
    """Read the server-side free quota and consume only that quota."""

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
            task="arcane_tower",
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

    def _receive_header(self, deadline: float, context: str) -> MessageHeader:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise HarvestError(f"等待{context}超时")
        try:
            assert self.socket is not None
            opcode, frame = self.socket.recv_message(remaining)
        except socket.timeout as exc:
            raise HarvestError(f"等待{context}超时") from exc
        except OSError as exc:
            raise HarvestError(f"读取{context}报文失败：{exc}") from exc
        return self._decode_frame(opcode, frame)

    def _handle_common_message(self, header: MessageHeader) -> bool:
        if header.message_id == HEARTBEAT_MESSAGE_ID:
            self._send_message(
                HEARTBEAT_RET_MESSAGE_ID,
                b"",
                encrypted=self.password is not None,
            )
            return True
        if header.message_id == LOGIN_FAIL_MESSAGE_ID:
            raise HarvestError("游戏服 Login 失败")
        if header.message_id == KICKOUT_MESSAGE_ID:
            ret, message = decode_kickout(header.data)
            raise GameSessionKickout(ret, message)
        return False

    def _login_and_read_banners(self) -> tuple[GachaBannerState, ...]:
        self.socket = self.socket_factory(self.endpoint.url, self.timeout)
        self._send_message(
            LOGIN_MESSAGE_ID,
            encode_login_payload(self.endpoint.game_token),
            encrypted=False,
        )

        login_complete = False
        game_data_received = False
        banners: tuple[GachaBannerState, ...] = ()
        deadline = time.monotonic() + self.timeout
        while not (login_complete and game_data_received):
            header = self._receive_header(deadline, "游戏服登录及 Game_data")
            if header.message_id == PACK_PASSWORD_MESSAGE_ID:
                encrypted_password = decode_pack_password(header.data)
                try:
                    self.password = pack1_decode(
                        encrypted_password, SOCKET_PACK_KEY
                    ).decode("utf-8")
                except UnicodeDecodeError as exc:
                    raise HarvestError("游戏服会话密码不是 UTF-8 文本") from exc
                continue
            if self._handle_common_message(header):
                continue
            if header.message_id == GAME_DATA_MESSAGE_ID:
                banners = decode_arcane_tower_banners(header.data)
                game_data_received = True
            elif header.message_id == LOGIN_REUNIQUE_MESSAGE_ID:
                login_complete = True

        # Native client delays cached business messages by 100 ms here.
        time.sleep(0.1)
        return banners

    def _explore_once(self, banner_id: int) -> ArcaneTowerExploreResponse:
        self._send_message(
            PULL_GACHA_BANNER_V2_MESSAGE_ID,
            encode_arcane_tower_explore_payload(banner_id),
            encrypted=True,
        )
        deadline = time.monotonic() + self.timeout
        while True:
            header = self._receive_header(
                deadline, f"秘法塔探索池 {banner_id} 的响应"
            )
            if self._handle_common_message(header):
                continue
            if header.message_id != PULL_GACHA_BANNER_V2_MESSAGE_ID:
                continue
            response = decode_arcane_tower_explore_response(header.data)
            if response.banner_id not in (0, banner_id):
                raise HarvestError(
                    f"秘法塔响应池 ID 不匹配：请求 {banner_id}，"
                    f"响应 {response.banner_id}"
                )
            if response.category not in (0, GACHA_CATEGORY_ARTIFACT):
                raise HarvestError(
                    f"秘法塔响应分类不匹配：{response.category}"
                )
            return response

    def explore_daily_free(
        self, *, max_attempts: int | None = None
    ) -> ArcaneTowerDailyResult:
        if max_attempts is not None and max_attempts <= 0:
            raise HarvestError("秘法塔探索次数上限必须为正整数")
        try:
            banners = self._login_and_read_banners()
            attempts: list[ArcaneTowerExploreAttempt] = []
            for banner in banners:
                free_left = banner.free_pull_times_left
                for sequence in range(1, free_left + 1):
                    if max_attempts is not None and len(attempts) >= max_attempts:
                        return ArcaneTowerDailyResult(
                            banners=banners,
                            attempts=tuple(attempts),
                        )
                    response = self._explore_once(banner.banner_id)
                    attempts.append(
                        ArcaneTowerExploreAttempt(
                            banner_id=banner.banner_id,
                            sequence=sequence,
                            free_before=free_left - sequence + 1,
                            response=response,
                        )
                    )
                    if response.result != RESULT_SUCCESS:
                        break
                    if (
                        response.state is not None
                        and response.state.free_pull_times_left <= 0
                    ):
                        break
            return ArcaneTowerDailyResult(banners=banners, attempts=tuple(attempts))
        finally:
            if self.socket is not None:
                self.socket.close()


def _format_reward(prop: RewardProp) -> str:
    return reward_text(prop.kind, prop.item_id, prop.amount)


def _free_after(attempt: ArcaneTowerExploreAttempt) -> int:
    response = attempt.response
    if response.state is not None:
        return response.state.free_pull_times_left
    return max(attempt.free_before - int(response.result == RESULT_SUCCESS), 0)


def build_daily_result_log_record(
    endpoint: GameEndpoint,
    result: ArcaneTowerDailyResult,
    *,
    timestamp: str | None = None,
) -> dict[str, object]:
    """Build a token-free JSON record of the quota check and draw responses."""

    return {
        "timestamp": timestamp or datetime.now().astimezone().isoformat(timespec="seconds"),
        "event": "arcane_tower_daily_free",
        "zone": {
            "id": endpoint.zone_id,
            "name": endpoint.zone_name,
        },
        "free_total": sum(
            banner.free_pull_times_left for banner in result.banners
        ),
        "request_count": len(result.attempts),
        "banners": [
            {
                "banner_id": banner.banner_id,
                "name": unknown_name("探索池", banner.banner_id),
                "pull_times_today": banner.pull_times_today,
                "free_pull_times_left": banner.free_pull_times_left,
                "refresh_free_left_secs": banner.refresh_free_left_secs,
                "pull_times_total": banner.pull_times_total,
            }
            for banner in result.banners
        ],
        "attempts": [
            {
                "banner_id": attempt.banner_id,
                "banner_name": unknown_name("探索池", attempt.banner_id),
                "sequence": attempt.sequence,
                "pull_times": attempt.response.pull_times,
                "result": attempt.response.result,
                "result_label": RESULT_LABELS.get(
                    attempt.response.result,
                    f"服务端返回 result={attempt.response.result}",
                ),
                "free_before": attempt.free_before,
                "free_after": _free_after(attempt),
                "rewards": [
                    {
                        "kind": prop.kind,
                        "kind_label": PROP_KIND_LABELS.get(
                            prop.kind, f"类型 {prop.kind}"
                        ),
                        "id": prop.item_id,
                        "name": reward_name(prop.kind, prop.item_id),
                        "amount": prop.amount,
                    }
                    for prop in attempt.response.props
                ],
            }
            for attempt in result.attempts
        ],
    }


def append_daily_result_log(
    path: Path | object | None,
    endpoint: GameEndpoint,
    result: ArcaneTowerDailyResult,
    *,
    timestamp: str | None = None,
) -> None:
    """Append one complete result as JSONL without session credentials."""

    record = build_daily_result_log_record(
        endpoint,
        result,
        timestamp=timestamp,
    )
    failures = [attempt for attempt in result.attempts if attempt.response.result not in (RESULT_SUCCESS, RESULT_FREEPULL_ALREADY_USED)]
    all_used = bool(result.attempts) and all(attempt.response.result == RESULT_FREEPULL_ALREADY_USED for attempt in result.attempts)
    if failures:
        outcome, level, error = "failure", "error", {"type": "ArcaneOutcome", "code": "unexpected_result", "message": "探索出现未接受的服务器结果"}
    elif not result.attempts:
        outcome, level, error = "skipped", "warning", {"type": "ArcaneOutcome", "code": "no_free_attempt", "message": "没有可执行的免费探索"}
    elif all_used:
        outcome, level, error = "skipped", "warning", {"type": "ArcaneOutcome", "code": "free_pull_already_used", "message": "免费探索额度已使用"}
    else:
        outcome, level, error = "success", "info", None
    details = {key: value for key, value in record.items() if key not in {"timestamp", "event", "zone"}}
    try:
        write_standard_log(event="arcane_tower_daily_free", operation="daily_free_explore", zone=record["zone"], details=details, destination=path, timestamp=record["timestamp"], outcome=outcome, level=level, error=error)
    except LogPersistenceError as exc:
        raise HarvestError(f"写入秘法塔结果日志失败：{exc}") from exc


def print_daily_result(endpoint: GameEndpoint, result: ArcaneTowerDailyResult) -> None:
    print(f"秘法塔每日免费探索检查完成，区服：{zone_name(endpoint.zone_id, endpoint.zone_name)}")
    if not result.banners:
        print("服务端未返回秘法塔探索池。")
        return

    free_total = sum(banner.free_pull_times_left for banner in result.banners)
    print(f"探索池数量：{len(result.banners)}；可用免费次数：{free_total}")
    if free_total == 0:
        print("今日没有可用的免费探索额度，未发送探索请求。")
        return

    for banner in result.banners:
        print(
            f"{unknown_name('探索池', banner.banner_id)}：今日已探索 {banner.pull_times_today} 次，"
            f"检查时免费额度 {banner.free_pull_times_left} 次"
        )
    for attempt in result.attempts:
        response = attempt.response
        status = RESULT_LABELS.get(response.result, f"服务端返回 result={response.result}")
        free_after = _free_after(attempt)
        print(
            f"{unknown_name('探索池', attempt.banner_id)} 第 {attempt.sequence} 次：{status}；"
            f"免费额度 {attempt.free_before} -> {free_after}"
        )
        for prop in response.props:
            print(f"  奖励：{_format_reward(prop)}")


def run_self_tests() -> None:
    assert encode_arcane_tower_explore_payload(7) == (
        b"\x08\x07\x10\x01\x18\x01\x28\x01"
    )

    artifact_banner = (
        encode_int_field(1, 7)
        + encode_int_field(2, 4)
        + encode_int_field(3, 1)
        + encode_int_field(4, 3600)
        + encode_int_field(5, GACHA_CATEGORY_ARTIFACT)
        + encode_int_field(9, 12)
    )
    normal_banner = (
        encode_int_field(1, 99)
        + encode_int_field(3, 3)
        + encode_int_field(5, 2)
    )
    game_data = encode_bytes_field(21, artifact_banner) + encode_bytes_field(
        21, normal_banner
    )
    assert decode_arcane_tower_banners(game_data) == (
        GachaBannerState(7, 4, 1, 3600, GACHA_CATEGORY_ARTIFACT, 12),
    )

    reward = (
        encode_int_field(1, 4)
        + encode_int_field(2, 301)
        + encode_int_field(3, 1)
    )
    updated_banner = (
        encode_int_field(1, 7)
        + encode_int_field(2, 5)
        + encode_int_field(3, 0)
        + encode_int_field(4, 86400)
        + encode_int_field(5, GACHA_CATEGORY_ARTIFACT)
        + encode_int_field(9, 13)
    )
    response_payload = (
        encode_int_field(1, 7)
        + encode_int_field(2, 1)
        + encode_int_field(3, GACHA_CATEGORY_ARTIFACT)
        + encode_bytes_field(5, reward)
        + encode_bytes_field(6, updated_banner)
        + encode_int_field(20, RESULT_SUCCESS)
    )
    decoded_response = decode_arcane_tower_explore_response(response_payload)
    assert decoded_response.banner_id == 7
    assert decoded_response.props == (RewardProp(4, 301, 1),)
    assert decoded_response.state == GachaBannerState(
        7, 5, 0, 86400, GACHA_CATEGORY_ARTIFACT, 13
    )
    assert decode_kickout(
        encode_int_field(1, 2)
        + encode_bytes_field(2, "会话已切换".encode("utf-8"))
    ) == (2, "会话已切换")

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
        pack1_encode(session_password.encode("utf-8"), SOCKET_PACK_KEY).encode(
            "utf-8"
        ),
    )

    def encrypted(message_id: int, payload: bytes = b"") -> tuple[int, bytes]:
        packet = encode_message_header(message_id, payload)
        return 0x2, pack1_encode(packet, session_password).encode("utf-8")

    fake_socket = TestSocket(
        [
            (0x2, encode_message_header(PACK_PASSWORD_MESSAGE_ID, password_payload)),
            encrypted(GAME_DATA_MESSAGE_ID, game_data),
            encrypted(LOGIN_REUNIQUE_MESSAGE_ID),
            encrypted(PULL_GACHA_BANNER_V2_MESSAGE_ID, response_payload),
        ]
    )
    endpoint = GameEndpoint("ws://test.invalid", "game-token", "1", "test")
    result = ArcaneTowerClient(
        endpoint,
        1.0,
        socket_factory=lambda _url, _timeout: fake_socket,
    ).explore_daily_free()
    assert result.banners[0].free_pull_times_left == 1
    assert len(result.attempts) == 1
    assert result.attempts[0].response.result == RESULT_SUCCESS
    assert fake_socket.closed
    assert decode_message_header(fake_socket.binary_frames[0]).message_id == LOGIN_MESSAGE_ID
    assert len(fake_socket.text_frames) == 1
    request = decode_message_header(
        pack1_decode(fake_socket.text_frames[0], session_password)
    )
    assert request == MessageHeader(
        PULL_GACHA_BANNER_V2_MESSAGE_ID,
        0,
        encode_arcane_tower_explore_payload(7),
    )

    with tempfile.TemporaryDirectory() as temporary_directory:
        result_log = Path(temporary_directory) / "arcane.jsonl"
        append_daily_result_log(
            result_log,
            endpoint,
            result,
            timestamp="2026-07-19T20:00:00+08:00",
        )
        log_record = json.loads(result_log.read_text(encoding="utf-8"))
        assert log_record["timestamp"] == "2026-07-19T20:00:00+08:00"
        assert log_record["zone"] == {"id": "1", "name": "test"}
        assert log_record["details"]["free_total"] == 1
        assert log_record["details"]["request_count"] == 1
        assert log_record["details"]["attempts"][0]["free_after"] == 0
        assert log_record["details"]["attempts"][0]["rewards"] == [
            {
                "kind": 4,
                "kind_label": "秘宝",
                "id": 301,
                "name": reward_name(4, 301),
                "amount": 1,
            }
        ]
        assert "token" not in result_log.read_text(encoding="utf-8").lower()


def build_parser() -> argparse.ArgumentParser:
    parser = build_base_parser()
    parser.prog = "explore_arcane_tower_daily_free.py"
    parser.description = __doc__
    parser.add_argument(
        "--result-log",
        type=Path,
        default=DEFAULT_RESULT_LOG,
        help="探索结果 JSONL 日志；默认写入项目目录的 arcane_tower_daily_free.jsonl。",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.self_test:
        run_self_tests()
        print("秘法塔每日免费探索本地协议自检通过")
        return 0

    try:
        tokens = load_tokens(args.token_file)
        for attempt in range(2):
            endpoint = resolve_game_endpoint(tokens, args)
            try:
                result = ArcaneTowerClient(
                    endpoint, args.timeout
                ).explore_daily_free()
                break
            except GameSessionKickout as exc:
                if exc.ret != 2 or attempt > 0:
                    raise
                print(
                    "[登录] 收到 Kickout ret=2，"
                    f"{LOGIN_KICKOUT_RETRY_DELAY:g} 秒后重新获取游戏服会话并重试一次。"
                )
                time.sleep(LOGIN_KICKOUT_RETRY_DELAY)
        else:
            raise AssertionError("登录重试循环未返回")
    except HarvestError as exc:
        print(f"秘法塔每日免费探索失败：{exc}", file=sys.stderr)
        return 1

    print_daily_result(endpoint, result)
    try:
        append_daily_result_log(args.result_log, endpoint, result)
    except HarvestError as exc:
        print(f"秘法塔结果记录失败：{exc}", file=sys.stderr)
        return 1
    if args.result_log is MANAGED_DESTINATION:
        print("结果日志：logs/arcane_tower_daily_free/<日期>.jsonl")
    else:
        print(f"结果日志：{args.result_log.expanduser().resolve()}")
    failures = [
        attempt
        for attempt in result.attempts
        if attempt.response.result
        not in (RESULT_SUCCESS, RESULT_FREEPULL_ALREADY_USED)
    ]
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
