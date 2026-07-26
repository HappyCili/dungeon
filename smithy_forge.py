#!/usr/bin/env python3
"""铁匠铺普通锻造（``Smithy_forge``）。

日常任务 103：在铁匠铺完成 5 次锻造。脚本登录游戏服后读取 ``Game_data`` 中的
铁匠数据、铁矿数量与装备仓库空位，再发送一次或多次 ``Smithy_forge``。

请求体为 ``{ times }``；消耗来自配置表 ``equip_forgecost``：当前每档均为
「铁」(item id=13) × 100。客户端单次最多连续锻造 20 次。

用法：
    .venv/bin/python smithy_forge.py
    .venv/bin/python smithy_forge.py --times 5
    .venv/bin/python smithy_forge.py --zone-id 4101
    .venv/bin/python smithy_forge.py --self-test
"""

from __future__ import annotations

import argparse
import json
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
    STORAGE_ITEM_CHANGE_MESSAGE_ID,
    GameEndpoint,
    HarvestError,
    ItemChange,
    MessageHeader,
    NativeWebSocket,
    ProtoReader,
    decode_int32,
    decode_item_change_notify,
    decode_message_header,
    decode_pack_password,
    encode_bytes_field,
    encode_int_field,
    encode_login_payload,
    encode_message_header,
    encode_varint,
    load_tokens,
    pack1_decode,
    pack1_encode,
    resolve_game_endpoint,
)
from ws_traffic_log import bind_traffic_logging
from harvest_fief import build_parser as build_base_parser
from id_descriptions import item_name, zone_name
from logging_store import MANAGED_DESTINATION, LogPersistenceError, write_standard_log


GAME_DATA_MESSAGE_ID = 10490
KICKOUT_MESSAGE_ID = 10030
SMITHY_FORGE_MESSAGE_ID = 19136

# 与客户端 ``Np.Iron`` / ``equip_forgecost.cost1`` 一致。
IRON_ITEM_ID = 13
IRON_PER_FORGE = 100
# 客户端 ``getForgeCostMax`` 单次连续锻造上限。
MAX_FORGE_BATCH = 20
LOGIN_KICKOUT_RETRY_DELAY = 3.0
REWARD_NOTIFICATION_GRACE_SECONDS = 0.5

PROJECT_ROOT = Path(__file__).resolve().parent
DEFAULT_RESULT_LOG = MANAGED_DESTINATION

RESULT_SUCCESS = 0
RESULT_LABELS = {
    RESULT_SUCCESS: "锻造成功",
}


class GameSessionKickout(HarvestError):
    """The game server ended the newly opened session with a reason code."""

    def __init__(self, ret: int, message: str = "") -> None:
        self.ret = ret
        self.message = message
        detail = f"，消息={message}" if message else ""
        super().__init__(f"游戏服终止会话：ret={ret}{detail}")


class SmithyForgeRejected(HarvestError):
    """游戏服已响应锻造请求，但拒绝了本次操作。"""

    def __init__(self, ret: int) -> None:
        self.ret = ret
        super().__init__(f"铁匠铺锻造返回 ret={ret}")


@dataclass(frozen=True)
class SmithyState:
    forge_level: int
    forge_times: int
    forge_times_left: int
    draw_level: int
    draw_exp: int


@dataclass(frozen=True)
class SmithyInventory:
    iron: int
    equip_free: int
    equip_capacity: int
    equip_count: int


@dataclass(frozen=True)
class SmithySnapshot:
    smithy: SmithyState
    inventory: SmithyInventory


@dataclass(frozen=True)
class SmithyForgeResponse:
    ret: int
    times: int
    forge_times: int
    forge_times_left: int
    equip_ids: tuple[int, ...]
    draw_level: int
    draw_exp: int


@dataclass(frozen=True)
class SmithyForgeAttempt:
    sequence: int
    requested_times: int
    iron_before: int
    equip_free_before: int
    response: SmithyForgeResponse
    item_changes: tuple[ItemChange, ...]


@dataclass(frozen=True)
class SmithyForgeResult:
    snapshot: SmithySnapshot | None
    attempts: tuple[SmithyForgeAttempt, ...]
    stop_reason: str


def encode_smithy_forge_payload(times: int) -> bytes:
    """Encode ``Smithy_forge`` C2S ``{ times }`` (field 1)."""

    if times <= 0:
        raise HarvestError("锻造次数必须为正整数")
    return encode_int_field(1, times)


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


def _decode_item_entry(data: bytes) -> tuple[int, int]:
    item_id = 0
    amount = 0
    for field_number, wire_type, value in ProtoReader(data).fields():
        if wire_type != 0:
            continue
        if field_number == 1:
            item_id = int(value)
        elif field_number == 2:
            amount = int(value)
    return item_id, amount


def _decode_bag_section(data: bytes) -> tuple[int, int, list[tuple[int, int]]]:
    """Decode storage bag section ``{ capacity, free, list[] }``."""

    capacity = 0
    free = 0
    entries: list[tuple[int, int]] = []
    for field_number, wire_type, value in ProtoReader(data).fields():
        if field_number == 1 and wire_type == 0:
            capacity = decode_int32(int(value))
        elif field_number == 2 and wire_type == 0:
            free = decode_int32(int(value))
        elif field_number == 3 and wire_type == 2:
            entries.append(_decode_item_entry(bytes(value)))
    return capacity, free, entries


def decode_smithy_state(data: bytes) -> SmithyState:
    """Decode ``Building.smithy`` fields used by ordinary forge."""

    values = {
        "forge_level": 0,
        "forge_times": 0,
        "forge_times_left": 0,
        "draw_level": 0,
        "draw_exp": 0,
    }
    for field_number, wire_type, value in ProtoReader(data).fields():
        if wire_type != 0:
            continue
        if field_number == 1:
            values["forge_level"] = decode_int32(int(value))
        elif field_number == 11:
            values["forge_times"] = decode_int32(int(value))
        elif field_number == 12:
            values["forge_times_left"] = decode_int32(int(value))
        elif field_number == 16:
            values["draw_level"] = decode_int32(int(value))
        elif field_number == 17:
            values["draw_exp"] = decode_int32(int(value))
    return SmithyState(**values)


def decode_smithy_snapshot(game_data: bytes) -> SmithySnapshot:
    """Extract smithy state plus iron / equip free slots from ``Game_data``."""

    smithy: SmithyState | None = None
    iron = 0
    equip_free = 0
    equip_capacity = 0
    equip_count = 0

    for field_number, wire_type, value in ProtoReader(game_data).fields():
        if wire_type != 2:
            continue
        blob = bytes(value)
        if field_number == 8:
            for s_field, s_wire, s_value in ProtoReader(blob).fields():
                if s_wire != 2:
                    continue
                section = bytes(s_value)
                if s_field == 1:
                    _capacity, _free, items = _decode_bag_section(section)
                    for item_id, amount in items:
                        if item_id == IRON_ITEM_ID:
                            iron = int(amount)
                elif s_field == 2:
                    equip_capacity, free_field, equips = _decode_bag_section(section)
                    equip_count = len(equips)
                    if free_field > 0:
                        equip_free = free_field
                    elif equip_capacity > 0:
                        equip_free = max(equip_capacity - equip_count, 0)
        elif field_number == 10:
            for b_field, b_wire, b_value in ProtoReader(blob).fields():
                if b_field == 2 and b_wire == 2:
                    smithy = decode_smithy_state(bytes(b_value))

    if smithy is None:
        raise HarvestError("Game_data 中缺少铁匠铺 smithy 数据")
    return SmithySnapshot(
        smithy=smithy,
        inventory=SmithyInventory(
            iron=iron,
            equip_free=equip_free,
            equip_capacity=equip_capacity,
            equip_count=equip_count,
        ),
    )


def _decode_packed_int64s(data: bytes) -> tuple[int, ...]:
    reader = ProtoReader(data)
    values: list[int] = []
    while reader.position < len(reader.data):
        values.append(int(reader.read_varint()))
    return tuple(values)


def decode_smithy_forge_response(data: bytes) -> SmithyForgeResponse:
    """Decode ``Smithy_forge`` S2C response (client codec ``Kmm`` / ``bl``)."""

    ret = 0
    times = 0
    forge_times = 0
    forge_times_left = 0
    equip_ids: list[int] = []
    draw_level = 0
    draw_exp = 0
    for field_number, wire_type, value in ProtoReader(data).fields():
        if field_number == 1 and wire_type == 0:
            ret = decode_int32(int(value))
        elif field_number == 2 and wire_type == 0:
            times = decode_int32(int(value))
        elif field_number == 3 and wire_type == 0:
            forge_times = decode_int32(int(value))
        elif field_number == 4 and wire_type == 0:
            forge_times_left = decode_int32(int(value))
        elif field_number == 6 and wire_type == 0:
            equip_ids.append(int(value))
        elif field_number == 6 and wire_type == 2:
            equip_ids.extend(_decode_packed_int64s(bytes(value)))
        elif field_number == 7 and wire_type == 0:
            draw_level = decode_int32(int(value))
        elif field_number == 8 and wire_type == 0:
            draw_exp = decode_int32(int(value))
    return SmithyForgeResponse(
        ret=ret,
        times=times,
        forge_times=forge_times,
        forge_times_left=forge_times_left,
        equip_ids=tuple(equip_ids),
        draw_level=draw_level,
        draw_exp=draw_exp,
    )


def max_affordable_forges(
    inventory: SmithyInventory,
    *,
    remaining: int,
    iron_per_forge: int = IRON_PER_FORGE,
    batch_limit: int = MAX_FORGE_BATCH,
) -> int:
    """How many ordinary forges can be requested right now."""

    if remaining <= 0:
        return 0
    if iron_per_forge <= 0:
        raise HarvestError("单次锻造铁矿消耗必须为正整数")
    by_iron = inventory.iron // iron_per_forge
    # free/capacity 都缺失时不按仓库拦截，避免误伤；明确 free=0 且已知容量则拦截。
    if inventory.equip_free > 0:
        by_slots = inventory.equip_free
    elif inventory.equip_capacity > 0:
        by_slots = 0
    else:
        by_slots = by_iron
    return max(0, min(remaining, by_iron, by_slots, batch_limit))


def describe_forge_ret(ret: int) -> str:
    return RESULT_LABELS.get(ret, f"服务端返回 ret={ret}")


class SmithyForgeClient:
    """Login, inspect materials, and run ordinary smithy forge batches."""

    def __init__(
        self,
        endpoint: GameEndpoint,
        timeout: float,
        *,
        session: object | None = None,
        socket_factory: Callable[[str, float], NativeWebSocket] = NativeWebSocket.connect,
        websocket_log: Path | bool | None = True,
    ) -> None:
        from game_session import bind_shared_session

        self.endpoint = endpoint
        self.timeout = timeout
        self.socket_factory = socket_factory
        self.socket: NativeWebSocket | None = None
        self.password: str | None = None
        bind_shared_session(
            self,
            session,  # type: ignore[arg-type]
            error_cls=HarvestError,
            task="smithy_forge",
            websocket_log=websocket_log,
        )

    def _send_message(self, message_id: int, data: bytes = b"", *, encrypted: bool) -> None:
        from game_session import session_send_message

        if session_send_message(self, message_id, data, encrypted=encrypted):
            return
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
        from game_session import try_session_receive_header

        handled, header = try_session_receive_header(self, deadline, context)
        if handled:
            assert header is not None
            return header
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

    def _login_and_read_snapshot(self) -> SmithySnapshot:
        from game_session import try_session_ensure_ready

        if try_session_ensure_ready(self, self.endpoint):
            game_data = getattr(self._session, "game_data", None)
            if not game_data:
                raise HarvestError("未收到 Game_data")
            return decode_smithy_snapshot(bytes(game_data))

        self.socket = self.socket_factory(self.endpoint.url, self.timeout)
        self._send_message(
            LOGIN_MESSAGE_ID,
            encode_login_payload(self.endpoint.game_token),
            encrypted=False,
        )

        login_complete = False
        game_data_received = False
        snapshot: SmithySnapshot | None = None
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
                snapshot = decode_smithy_snapshot(header.data)
                game_data_received = True
            elif header.message_id == LOGIN_REUNIQUE_MESSAGE_ID:
                login_complete = True

        if snapshot is None:
            raise HarvestError("未收到 Game_data")
        # Native client delays cached business messages by 100 ms here.
        time.sleep(0.1)
        return snapshot

    def _forge_once(
        self, times: int
    ) -> tuple[SmithyForgeResponse, tuple[ItemChange, ...]]:
        self._send_message(
            SMITHY_FORGE_MESSAGE_ID,
            encode_smithy_forge_payload(times),
            encrypted=True,
        )
        deadline = time.monotonic() + self.timeout
        response: SmithyForgeResponse | None = None
        changes: list[ItemChange] = []
        grace_deadline: float | None = None
        while True:
            if response is not None and grace_deadline is not None:
                if time.monotonic() >= grace_deadline:
                    break
                header_deadline = grace_deadline
            else:
                header_deadline = deadline
            try:
                header = self._receive_header(header_deadline, "铁匠铺锻造响应")
            except HarvestError:
                if response is not None:
                    break
                raise
            if self._handle_common_message(header):
                continue
            if header.message_id == STORAGE_ITEM_CHANGE_MESSAGE_ID:
                notice = decode_item_change_notify(header.data)
                changes.extend(notice.items)
                continue
            if header.message_id != SMITHY_FORGE_MESSAGE_ID:
                continue
            response = decode_smithy_forge_response(header.data)
            grace_deadline = time.monotonic() + REWARD_NOTIFICATION_GRACE_SECONDS
        if response is None:
            raise HarvestError("未收到 Smithy_forge 响应")
        return response, tuple(changes)

    def forge(
        self,
        *,
        times: int,
        max_batch: int = MAX_FORGE_BATCH,
    ) -> SmithyForgeResult:
        """Forge up to ``times`` times, batching when materials allow."""

        if times <= 0:
            raise HarvestError("锻造目标次数必须为正整数")
        if max_batch <= 0:
            raise HarvestError("单次锻造批次上限必须为正整数")

        try:
            snapshot = self._login_and_read_snapshot()
            attempts: list[SmithyForgeAttempt] = []
            remaining = times
            iron = snapshot.inventory.iron
            equip_free = snapshot.inventory.equip_free
            stop_reason = "completed"

            sequence = 0
            while remaining > 0:
                inventory = SmithyInventory(
                    iron=iron,
                    equip_free=equip_free,
                    equip_capacity=snapshot.inventory.equip_capacity,
                    equip_count=snapshot.inventory.equip_count,
                )
                batch = max_affordable_forges(
                    inventory,
                    remaining=remaining,
                    batch_limit=max_batch,
                )
                if batch <= 0:
                    if iron < IRON_PER_FORGE:
                        stop_reason = "insufficient_iron"
                    elif equip_free <= 0:
                        stop_reason = "equip_storage_full"
                    else:
                        stop_reason = "no_affordable"
                    break

                sequence += 1
                response, item_changes = self._forge_once(batch)
                attempts.append(
                    SmithyForgeAttempt(
                        sequence=sequence,
                        requested_times=batch,
                        iron_before=iron,
                        equip_free_before=equip_free,
                        response=response,
                        item_changes=item_changes,
                    )
                )
                if response.ret != RESULT_SUCCESS:
                    stop_reason = f"server_ret_{response.ret}"
                    break

                forged = response.times if response.times > 0 else batch
                remaining -= forged
                iron = max(iron - forged * IRON_PER_FORGE, 0)
                equip_free = max(equip_free - forged, 0)
                for change in item_changes:
                    if change.item_id == IRON_ITEM_ID:
                        iron = max(int(change.total), 0)

            if remaining <= 0:
                stop_reason = "completed"
            return SmithyForgeResult(
                snapshot=snapshot,
                attempts=tuple(attempts),
                stop_reason=stop_reason,
            )
        finally:
            from game_session import shared_close

            if shared_close(self) and self.socket is not None:
                self.socket.close()
                self.socket = None

    def forge_for_daily(self, *, max_times: int) -> SmithyForgeResult:
        """Daily task entry: forge at most ``max_times`` ordinary times."""

        if max_times <= 0:
            raise HarvestError("日常锻造次数上限必须为正整数")
        return self.forge(times=max_times)


def forged_count(result: SmithyForgeResult) -> int:
    total = 0
    for attempt in result.attempts:
        if attempt.response.ret != RESULT_SUCCESS:
            continue
        if attempt.response.times > 0:
            total += attempt.response.times
        else:
            total += attempt.requested_times
    return total


def stop_reason_label(stop_reason: str) -> str:
    labels = {
        "completed": "目标次数已完成",
        "insufficient_iron": f"{item_name(IRON_ITEM_ID)}不足",
        "equip_storage_full": "装备仓库空位不足",
        "no_affordable": "当前无法继续锻造",
    }
    if stop_reason in labels:
        return labels[stop_reason]
    if stop_reason.startswith("server_ret_"):
        try:
            ret = int(stop_reason.removeprefix("server_ret_"))
        except ValueError:
            return stop_reason
        return describe_forge_ret(ret)
    return stop_reason


def build_result_log_record(
    endpoint: GameEndpoint,
    result: SmithyForgeResult,
    *,
    timestamp: str | None = None,
) -> dict[str, object]:
    snapshot = result.snapshot
    return {
        "timestamp": timestamp
        or datetime.now().astimezone().isoformat(timespec="seconds"),
        "event": "smithy_forge",
        "zone": {
            "id": endpoint.zone_id,
            "name": endpoint.zone_name,
        },
        "forged": forged_count(result),
        "request_count": len(result.attempts),
        "stop_reason": result.stop_reason,
        "stop_reason_label": stop_reason_label(result.stop_reason),
        "snapshot": None
        if snapshot is None
        else {
            "forge_level": snapshot.smithy.forge_level,
            "forge_times": snapshot.smithy.forge_times,
            "iron": snapshot.inventory.iron,
            "equip_free": snapshot.inventory.equip_free,
            "equip_capacity": snapshot.inventory.equip_capacity,
        },
        "attempts": [
            {
                "sequence": attempt.sequence,
                "requested_times": attempt.requested_times,
                "iron_before": attempt.iron_before,
                "equip_free_before": attempt.equip_free_before,
                "ret": attempt.response.ret,
                "result_label": describe_forge_ret(attempt.response.ret),
                "times": attempt.response.times,
                "forge_times": attempt.response.forge_times,
                "equip_count": len(attempt.response.equip_ids),
                "draw_level": attempt.response.draw_level,
                "draw_exp": attempt.response.draw_exp,
            }
            for attempt in result.attempts
        ],
    }


def append_result_log(
    path: Path | object | None,
    endpoint: GameEndpoint,
    result: SmithyForgeResult,
    *,
    timestamp: str | None = None,
) -> None:
    record = build_result_log_record(endpoint, result, timestamp=timestamp)
    forged = forged_count(result)
    failures = [
        attempt
        for attempt in result.attempts
        if attempt.response.ret != RESULT_SUCCESS
    ]
    if failures:
        outcome, level, error = (
            "failure",
            "error",
            {
                "type": "SmithyOutcome",
                "code": "unexpected_result",
                "message": "锻造出现未接受的服务器结果",
            },
        )
    elif forged <= 0:
        outcome, level, error = (
            "skipped",
            "warning",
            {
                "type": "SmithyOutcome",
                "code": result.stop_reason,
                "message": stop_reason_label(result.stop_reason),
            },
        )
    else:
        outcome, level, error = "success", "info", None
    details = {
        key: value
        for key, value in record.items()
        if key not in {"timestamp", "event", "zone"}
    }
    try:
        write_standard_log(
            event="smithy_forge",
            operation="ordinary_forge",
            zone=record["zone"],
            details=details,
            destination=path,
            timestamp=record["timestamp"],
            outcome=outcome,
            level=level,
            error=error,
        )
    except LogPersistenceError as exc:
        raise HarvestError(f"写入铁匠铺锻造结果日志失败：{exc}") from exc


def print_result(endpoint: GameEndpoint, result: SmithyForgeResult) -> None:
    print(
        f"铁匠铺普通锻造完成，区服：{zone_name(endpoint.zone_id, endpoint.zone_name)}"
    )
    if result.snapshot is None:
        print("未取得锻造快照。")
        return
    inv = result.snapshot.inventory
    smithy = result.snapshot.smithy
    print(
        f"锻造等级 {smithy.forge_level}；"
        f"{item_name(IRON_ITEM_ID)} {inv.iron}；"
        f"装备空位 {inv.equip_free}"
        + (f"/{inv.equip_capacity}" if inv.equip_capacity else "")
    )
    if not result.attempts:
        print(f"未发送锻造请求：{stop_reason_label(result.stop_reason)}")
        return
    print(
        f"锻造成功 {forged_count(result)} 次；"
        f"停止原因：{stop_reason_label(result.stop_reason)}"
    )
    for attempt in result.attempts:
        status = describe_forge_ret(attempt.response.ret)
        print(
            f"第 {attempt.sequence} 批：请求 {attempt.requested_times} 次，"
            f"{status}，响应 times={attempt.response.times}，"
            f"产出装备 {len(attempt.response.equip_ids)} 件"
        )


def run_self_tests() -> None:
    assert encode_smithy_forge_payload(5) == encode_int_field(1, 5)
    try:
        encode_smithy_forge_payload(0)
        raise AssertionError("times=0 应被拒绝")
    except HarvestError:
        pass

    item_iron = (
        encode_int_field(1, IRON_ITEM_ID) + encode_int_field(2, 550)
    )
    items_bag = (
        encode_int_field(1, 200)
        + encode_int_field(2, 100)
        + encode_bytes_field(3, item_iron)
    )
    equips_bag = encode_int_field(1, 50) + encode_int_field(2, 12)
    storage = encode_bytes_field(1, items_bag) + encode_bytes_field(2, equips_bag)
    smithy = (
        encode_int_field(1, 16)
        + encode_int_field(11, 3)
        + encode_int_field(12, 0)
        + encode_int_field(16, 2)
        + encode_int_field(17, 40)
    )
    building = encode_bytes_field(2, smithy)
    game_data = encode_bytes_field(8, storage) + encode_bytes_field(10, building)
    snapshot = decode_smithy_snapshot(game_data)
    assert snapshot.smithy == SmithyState(16, 3, 0, 2, 40)
    assert snapshot.inventory.iron == 550
    assert snapshot.inventory.equip_free == 12
    assert max_affordable_forges(snapshot.inventory, remaining=5) == 5
    assert max_affordable_forges(snapshot.inventory, remaining=20) == 5  # iron 550/100

    low_iron = SmithyInventory(iron=99, equip_free=10, equip_capacity=50, equip_count=0)
    assert max_affordable_forges(low_iron, remaining=5) == 0
    full_bag = SmithyInventory(iron=1000, equip_free=0, equip_capacity=50, equip_count=50)
    assert max_affordable_forges(full_bag, remaining=5) == 0

    equip_ids = encode_varint(101) + encode_varint(102)
    response_payload = (
        encode_int_field(1, RESULT_SUCCESS)
        + encode_int_field(2, 5)
        + encode_int_field(3, 8)
        + encode_bytes_field(6, equip_ids)
        + encode_int_field(7, 2)
        + encode_int_field(8, 65)
    )
    decoded = decode_smithy_forge_response(response_payload)
    assert decoded.ret == RESULT_SUCCESS
    assert decoded.times == 5
    assert decoded.forge_times == 8
    assert decoded.equip_ids == (101, 102)
    assert decoded.draw_level == 2
    assert decoded.draw_exp == 65
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
            encrypted(SMITHY_FORGE_MESSAGE_ID, response_payload),
        ]
    )
    endpoint = GameEndpoint("ws://test.invalid", "game-token", "1", "test")
    result = SmithyForgeClient(
        endpoint,
        1.0,
        socket_factory=lambda _url, _timeout: fake_socket,
    ).forge_for_daily(max_times=5)
    assert result.snapshot is not None
    assert result.snapshot.inventory.iron == 550
    assert len(result.attempts) == 1
    assert result.attempts[0].requested_times == 5
    assert result.attempts[0].response.times == 5
    assert forged_count(result) == 5
    assert result.stop_reason == "completed"
    assert fake_socket.closed
    assert decode_message_header(fake_socket.binary_frames[0]).message_id == LOGIN_MESSAGE_ID
    assert len(fake_socket.text_frames) == 1
    request = decode_message_header(
        pack1_decode(fake_socket.text_frames[0], session_password)
    )
    assert request == MessageHeader(
        SMITHY_FORGE_MESSAGE_ID,
        0,
        encode_smithy_forge_payload(5),
    )

    with tempfile.TemporaryDirectory() as temporary_directory:
        result_log = Path(temporary_directory) / "smithy.jsonl"
        append_result_log(
            result_log,
            endpoint,
            result,
            timestamp="2026-07-21T12:00:00+08:00",
        )
        log_record = json.loads(result_log.read_text(encoding="utf-8"))
        assert log_record["timestamp"] == "2026-07-21T12:00:00+08:00"
        assert log_record["zone"] == {"id": "1", "name": "test"}
        assert log_record["details"]["forged"] == 5
        assert log_record["details"]["request_count"] == 1
        assert "token" not in result_log.read_text(encoding="utf-8").lower()


def build_parser() -> argparse.ArgumentParser:
    parser = build_base_parser()
    parser.prog = "smithy_forge.py"
    parser.description = __doc__
    parser.add_argument(
        "--times",
        type=int,
        default=5,
        help="目标锻造次数（默认 5，对应日常任务 103）。",
    )
    parser.add_argument(
        "--result-log",
        type=Path,
        default=DEFAULT_RESULT_LOG,
        help="锻造结果 JSONL 日志；默认写入项目托管日志目录。",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.self_test:
        run_self_tests()
        print("铁匠铺普通锻造本地协议自检通过")
        return 0

    if args.times <= 0:
        print("锻造次数必须为正整数", file=sys.stderr)
        return 2

    try:
        tokens = load_tokens(args.token_file)
        for attempt in range(2):
            endpoint = resolve_game_endpoint(tokens, args)
            try:
                result = SmithyForgeClient(endpoint, args.timeout).forge(
                    times=args.times
                )
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
        print(f"铁匠铺普通锻造失败：{exc}", file=sys.stderr)
        return 1

    print_result(endpoint, result)
    try:
        append_result_log(args.result_log, endpoint, result)
    except HarvestError as exc:
        print(f"写入结果日志失败：{exc}", file=sys.stderr)
        return 1
    return 0 if forged_count(result) > 0 or result.stop_reason == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
