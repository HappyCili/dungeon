#!/usr/bin/env python3
"""检查并使用古律院的每日免费铭刻次数。

脚本从 ``Game_data.storage.orderrunes.pullinfo.freepullnum`` 读取服务端状态。
仅当免费次数大于 0 时，才发送 ``Storage_orderrune_pull`` 的免费模式
``PM_NULL``；
每次响应后还会核对免费次数确实减少且物品通知中没有实际成本，再决定是否
继续使用下一次免费次数。

用法：
    .venv/bin/python engrave_ancient_law_court_daily_free.py
    .venv/bin/python engrave_ancient_law_court_daily_free.py --zone-id 4101
    .venv/bin/python engrave_ancient_law_court_daily_free.py --self-test
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
from typing import Callable, Mapping, Sequence

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
    decode_int32,
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
from logging_store import MANAGED_DESTINATION, LogPersistenceError, write_standard_log
from id_descriptions import (
    artifact_name,
    artifact_rarity,
    is_artifact_piece_item,
    item_name,
    item_quality,
    reward_name,
    rune_name,
    zone_name,
)
from project_paths import NATIVE_APP_ROOT


GAME_DATA_MESSAGE_ID = 10490
KICKOUT_MESSAGE_ID = 10030
SYNC_ORDER_RUNE_INFO_MESSAGE_ID = 12640
SYNC_ORDER_RUNE_PULL_MESSAGE_ID = 12641
ORDER_RUNE_PULL_MESSAGE_ID = 12642

ORDER_RUNE_PULL_SOURCE = 410
ORDER_RUNE_ADD = 1
ORDER_RUNE_UPDATE = 2
PULL_MODE_FREE = 0
PROP_KIND_ITEM = 1
PROP_KIND_ORDER_RUNE = 6
PROP_KIND_EQUIPMENT = 3
PROP_KIND_ARTIFACT = 4

LOGIN_KICKOUT_RETRY_DELAY = 3.0
REWARD_NOTIFICATION_GRACE_SECONDS = 1.0
MAX_SERVER_FREE_PULLS = 100

PROJECT_ROOT = Path(__file__).resolve().parent
DEFAULT_RESULT_LOG = MANAGED_DESTINATION
DEFAULT_ITEM_MAP = PROJECT_ROOT / "item_id_map.json"
DEFAULT_RUNE_TABLE = (
    NATIVE_APP_ROOT / "decrypted-data" / "rune-tables" / "orderrune.json"
)

RESULT_SUCCESS = 0
RESULT_LABELS = {RESULT_SUCCESS: "铭刻成功"}

PROP_KIND_LABELS = {
    1: "物品",
    2: "奖励箱",
    3: "装备",
    4: "秘宝",
    5: "英雄",
    6: "律文",
    7: "活动装备",
}

RUNE_RARITY_LABELS = {
    1: "破损/D",
    2: "崭新/C",
    3: "珍稀/B",
    4: "精良/A",
    5: "S",
    6: "SS",
    7: "SSS",
}
# 与客户端品质色板一致：B=#9E80E8 紫，A=#ECB94B 黄。
RUNE_COLOR_LABELS = {
    1: "灰色",
    2: "蓝色",
    3: "紫色",
    4: "黄色",
    5: "绿色",
    6: "橙色",
    7: "橙色",
}
# 律文：B 紫、A 黄、S 绿（客户端 kb 色板）。
HIGHLIGHT_RUNE_RARITIES = frozenset({3, 4, 5})
# 秘宝配置 rarity 映射到 kb[rarity+2]：1→紫、2→黄、3→绿。
ARTIFACT_COLOR_LABELS = {
    1: "紫色",
    2: "黄色",
    3: "绿色",
}
ARTIFACT_RARITY_LABELS = {
    1: "I",
    2: "II",
    3: "III",
}
HIGHLIGHT_ARTIFACT_RARITIES = frozenset({1, 2, 3})
# 秘宝碎片使用物品 quality，色板与装备/律文相同：3 紫、4 黄、5 绿。
HIGHLIGHT_ARTIFACT_PIECE_QUALITIES = frozenset({3, 4, 5})
ARTIFACT_FORM_WHOLE = "整体"
ARTIFACT_FORM_PIECE = "碎片"


class GameSessionKickout(HarvestError):
    """The game server ended the newly opened session with a reason code."""

    def __init__(self, ret: int, message: str = "") -> None:
        self.ret = ret
        self.message = message
        detail = f"，消息={message}" if message else ""
        super().__init__(f"游戏服终止会话：ret={ret}{detail}")


@dataclass(frozen=True)
class RuneAttribute:
    attribute_id: int
    attribute_type: int
    value: int


@dataclass(frozen=True)
class OrderRune:
    instance_id: int
    position: int
    order_rune_id: int
    rarity: int
    locked: int
    level: int
    affix_attributes: tuple[RuneAttribute, ...]
    pattern_attributes: tuple[RuneAttribute, ...]
    stage_level: int
    base_locks: tuple[int, ...]
    temporary_affix_attributes: tuple[RuneAttribute, ...]
    temporary_pattern_attributes: tuple[RuneAttribute, ...]


@dataclass(frozen=True)
class RunePullInfo:
    daily_pull_num: int
    daily_time: int
    pull_id: int
    pity_score: int
    free_pull_num: int
    free_time: int
    wishlist_open: bool


@dataclass(frozen=True)
class RuneStorageState:
    capacity: int
    free_slots: int
    runes: tuple[OrderRune, ...]
    pull_info: RunePullInfo | None


@dataclass(frozen=True)
class RuneInfoSync:
    change_type: int
    rune: OrderRune | None


@dataclass(frozen=True)
class RunePullResponse:
    result: int
    mode: int
    instance_ids: tuple[int, ...]
    pull_info: RunePullInfo | None


@dataclass(frozen=True)
class RuneItemChangeNotify:
    source: int
    items: tuple[ItemChange, ...]
    equipment_instance_ids: tuple[int, ...]
    artifact_instance_ids: tuple[int, ...]
    rune_instance_ids: tuple[int, ...]
    costs: tuple[RewardProp, ...]
    props: tuple[RewardProp, ...]


@dataclass(frozen=True)
class RuneEngravingAttempt:
    sequence: int
    free_before: int
    response: RunePullResponse
    rune_infos: tuple[OrderRune, ...]
    notifications: tuple[RuneItemChangeNotify, ...]
    warnings: tuple[str, ...]
    safety_verified: bool
    can_continue: bool


@dataclass(frozen=True)
class AncientLawCourtDailyResult:
    state: RuneStorageState | None
    attempts: tuple[RuneEngravingAttempt, ...]


@dataclass(frozen=True)
class NameCatalogs:
    item_names: Mapping[int, str]
    rune_names: Mapping[int, str]


@dataclass(frozen=True)
class RewardSummary:
    kind: int
    base_id: int
    quantity: int
    name: str
    instance_id: int
    rarity: int
    level: int
    stage_level: int
    affix_attributes: tuple[RuneAttribute, ...]
    pattern_attributes: tuple[RuneAttribute, ...]
    form: str = ""


def decode_int64(value: int) -> int:
    value &= 0xFFFFFFFFFFFFFFFF
    return value - 0x10000000000000000 if value & 0x8000000000000000 else value


def _decode_packed_varints(data: bytes, *, signed: bool = False) -> tuple[int, ...]:
    reader = ProtoReader(data)
    values: list[int] = []
    while reader.position < len(reader.data):
        value = reader.read_varint()
        values.append(decode_int64(value) if signed else value)
    return tuple(values)


def decode_rune_attribute(data: bytes) -> RuneAttribute:
    values = {"attribute_id": 0, "attribute_type": 0, "value": 0}
    for field_number, wire_type, value in ProtoReader(data).fields():
        if wire_type != 0:
            continue
        if field_number == 1:
            values["attribute_id"] = decode_int32(int(value))
        elif field_number == 2:
            values["attribute_type"] = decode_int32(int(value))
        elif field_number == 3:
            values["value"] = decode_int32(int(value))
    return RuneAttribute(**values)


def decode_order_rune(data: bytes) -> OrderRune:
    values: dict[str, object] = {
        "instance_id": 0,
        "position": 0,
        "order_rune_id": 0,
        "rarity": 0,
        "locked": 0,
        "level": 0,
        "affix_attributes": [],
        "pattern_attributes": [],
        "stage_level": 0,
        "base_locks": [],
        "temporary_affix_attributes": [],
        "temporary_pattern_attributes": [],
    }
    for field_number, wire_type, value in ProtoReader(data).fields():
        if field_number == 1 and wire_type == 0:
            values["instance_id"] = decode_int64(int(value))
        elif field_number == 2 and wire_type == 0:
            values["position"] = decode_int64(int(value))
        elif field_number == 3 and wire_type == 0:
            values["order_rune_id"] = decode_int32(int(value))
        elif field_number == 5 and wire_type == 0:
            values["rarity"] = decode_int32(int(value))
        elif field_number == 6 and wire_type == 0:
            values["locked"] = decode_int32(int(value))
        elif field_number == 7 and wire_type == 0:
            values["level"] = decode_int32(int(value))
        elif field_number == 8 and wire_type == 2:
            values["affix_attributes"].append(decode_rune_attribute(bytes(value)))
        elif field_number == 9 and wire_type == 2:
            values["pattern_attributes"].append(decode_rune_attribute(bytes(value)))
        elif field_number == 10 and wire_type == 0:
            values["stage_level"] = decode_int32(int(value))
        elif field_number == 11 and wire_type == 0:
            values["base_locks"].append(decode_int32(int(value)))
        elif field_number == 11 and wire_type == 2:
            values["base_locks"].extend(
                decode_int32(item) for item in _decode_packed_varints(bytes(value))
            )
        elif field_number == 12 and wire_type == 2:
            values["temporary_affix_attributes"].append(
                decode_rune_attribute(bytes(value))
            )
        elif field_number == 13 and wire_type == 2:
            values["temporary_pattern_attributes"].append(
                decode_rune_attribute(bytes(value))
            )
    return OrderRune(
        instance_id=int(values["instance_id"]),
        position=int(values["position"]),
        order_rune_id=int(values["order_rune_id"]),
        rarity=int(values["rarity"]),
        locked=int(values["locked"]),
        level=int(values["level"]),
        affix_attributes=tuple(values["affix_attributes"]),
        pattern_attributes=tuple(values["pattern_attributes"]),
        stage_level=int(values["stage_level"]),
        base_locks=tuple(values["base_locks"]),
        temporary_affix_attributes=tuple(values["temporary_affix_attributes"]),
        temporary_pattern_attributes=tuple(values["temporary_pattern_attributes"]),
    )


def decode_rune_pull_info(data: bytes) -> RunePullInfo:
    values: dict[str, int | bool] = {
        "daily_pull_num": 0,
        "daily_time": 0,
        "pull_id": 0,
        "pity_score": 0,
        "free_pull_num": 0,
        "free_time": 0,
        "wishlist_open": False,
    }
    for field_number, wire_type, value in ProtoReader(data).fields():
        if field_number == 1 and wire_type == 0:
            values["daily_pull_num"] = decode_int32(int(value))
        elif field_number == 2 and wire_type == 0:
            values["daily_time"] = decode_int32(int(value))
        elif field_number == 3 and wire_type == 0:
            values["pull_id"] = decode_int32(int(value))
        elif field_number == 5 and wire_type == 0:
            values["pity_score"] = decode_int32(int(value))
        elif field_number == 6 and wire_type == 0:
            values["free_pull_num"] = decode_int32(int(value))
        elif field_number == 7 and wire_type == 0:
            values["free_time"] = decode_int32(int(value))
        elif field_number == 9 and wire_type == 0:
            values["wishlist_open"] = bool(value)
    return RunePullInfo(**values)


def decode_rune_storage_state(data: bytes) -> RuneStorageState:
    capacity = 0
    free_slots = 0
    runes: list[OrderRune] = []
    pull_info: RunePullInfo | None = None
    for field_number, wire_type, value in ProtoReader(data).fields():
        if field_number == 1 and wire_type == 0:
            capacity = decode_int32(int(value))
        elif field_number == 2 and wire_type == 0:
            free_slots = decode_int32(int(value))
        elif field_number == 3 and wire_type == 2:
            runes.append(decode_order_rune(bytes(value)))
        elif field_number == 4 and wire_type == 2:
            pull_info = decode_rune_pull_info(bytes(value))
    return RuneStorageState(capacity, free_slots, tuple(runes), pull_info)


def decode_game_data_rune_state(data: bytes) -> RuneStorageState | None:
    """Decode ``Game_data.storage.orderrunes`` (fields 8 -> 3)."""

    storage_payload: bytes | None = None
    for field_number, wire_type, value in ProtoReader(data).fields():
        if field_number == 8 and wire_type == 2:
            storage_payload = bytes(value)
    if storage_payload is None:
        return None

    rune_storage_payload: bytes | None = None
    for field_number, wire_type, value in ProtoReader(storage_payload).fields():
        if field_number == 3 and wire_type == 2:
            rune_storage_payload = bytes(value)
    if rune_storage_payload is None:
        return None
    return decode_rune_storage_state(rune_storage_payload)


def decode_rune_info_sync(data: bytes) -> RuneInfoSync:
    change_type = 0
    rune: OrderRune | None = None
    for field_number, wire_type, value in ProtoReader(data).fields():
        if field_number == 1 and wire_type == 0:
            change_type = decode_int32(int(value))
        elif field_number == 2 and wire_type == 2:
            rune = decode_order_rune(bytes(value))
    return RuneInfoSync(change_type, rune)


def decode_rune_pull_sync(data: bytes) -> RunePullInfo | None:
    for field_number, wire_type, value in ProtoReader(data).fields():
        if field_number == 1 and wire_type == 2:
            return decode_rune_pull_info(bytes(value))
    return None


def encode_free_rune_engraving_payload() -> bytes:
    """Encode ``PM_NULL``; protobuf omits its zero-valued ``mode`` field."""

    return b""


def decode_rune_pull_response(data: bytes) -> RunePullResponse:
    result = 0
    mode = 0
    instance_ids: list[int] = []
    pull_info: RunePullInfo | None = None
    for field_number, wire_type, value in ProtoReader(data).fields():
        if field_number == 1 and wire_type == 0:
            result = decode_int32(int(value))
        elif field_number == 2 and wire_type == 0:
            mode = decode_int32(int(value))
        elif field_number == 3 and wire_type == 0:
            instance_ids.append(decode_int64(int(value)))
        elif field_number == 3 and wire_type == 2:
            instance_ids.extend(
                decode_int64(item) for item in _decode_packed_varints(bytes(value))
            )
        elif field_number == 4 and wire_type == 2:
            pull_info = decode_rune_pull_info(bytes(value))
    return RunePullResponse(result, mode, tuple(instance_ids), pull_info)


def _decode_item_change(data: bytes) -> ItemChange:
    item_id = 0
    delta = 0
    total = 0
    for field_number, wire_type, value in ProtoReader(data).fields():
        if wire_type != 0:
            continue
        if field_number == 1:
            item_id = decode_int64(int(value))
        elif field_number == 2:
            delta = decode_int64(int(value))
        elif field_number == 3:
            total = decode_int64(int(value))
    return ItemChange(item_id, delta, total)


def _decode_prop(data: bytes) -> RewardProp:
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
    return RewardProp(kind, item_id, amount)


def decode_rune_item_change_notify(data: bytes) -> RuneItemChangeNotify:
    source = 0
    items: list[ItemChange] = []
    equipment_instance_ids: list[int] = []
    artifact_instance_ids: list[int] = []
    rune_instance_ids: list[int] = []
    costs: list[RewardProp] = []
    props: list[RewardProp] = []
    for field_number, wire_type, value in ProtoReader(data).fields():
        if field_number == 1 and wire_type == 0:
            source = decode_int32(int(value))
        elif field_number == 2 and wire_type == 2:
            items.append(_decode_item_change(bytes(value)))
        elif field_number == 3 and wire_type == 0:
            equipment_instance_ids.append(decode_int64(int(value)))
        elif field_number == 3 and wire_type == 2:
            equipment_instance_ids.extend(
                decode_int64(item)
                for item in _decode_packed_varints(bytes(value))
            )
        elif field_number == 4 and wire_type == 0:
            artifact_instance_ids.append(decode_int64(int(value)))
        elif field_number == 4 and wire_type == 2:
            artifact_instance_ids.extend(
                decode_int64(item)
                for item in _decode_packed_varints(bytes(value))
            )
        elif field_number == 10 and wire_type == 0:
            rune_instance_ids.append(decode_int64(int(value)))
        elif field_number == 10 and wire_type == 2:
            rune_instance_ids.extend(
                decode_int64(item) for item in _decode_packed_varints(bytes(value))
            )
        elif field_number == 20 and wire_type == 2:
            costs.append(_decode_prop(bytes(value)))
        elif field_number == 21 and wire_type == 2:
            props.append(_decode_prop(bytes(value)))
    return RuneItemChangeNotify(
        source,
        tuple(items),
        tuple(equipment_instance_ids),
        tuple(artifact_instance_ids),
        tuple(rune_instance_ids),
        tuple(costs),
        tuple(props),
    )


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


def _validate_state(state: RuneStorageState) -> None:
    if state.capacity < 0 or state.free_slots < 0:
        raise HarvestError("Game_data 中古律院仓库容量为负数")
    if state.pull_info is None:
        return
    free_count = state.pull_info.free_pull_num
    if free_count < 0:
        raise HarvestError("Game_data 中古律院免费铭刻次数为负数")
    if free_count > MAX_SERVER_FREE_PULLS:
        raise HarvestError(
            f"Game_data 中古律院免费铭刻次数异常：{free_count}"
        )


def _load_name_rows(path: Path, context: str) -> list[Mapping[str, object]]:
    try:
        payload = json.loads(path.expanduser().read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise HarvestError(f"{context}不存在：{path}") from exc
    except json.JSONDecodeError as exc:
        raise HarvestError(f"{context}不是有效 JSON：{path}") from exc
    except OSError as exc:
        raise HarvestError(f"读取{context}失败：{exc}") from exc

    rows = payload.values() if isinstance(payload, dict) else payload
    if not isinstance(rows, (list, tuple, type({}.values()))):
        raise HarvestError(f"{context}根节点必须是对象或数组")
    result: list[Mapping[str, object]] = []
    for row in rows:
        if isinstance(row, dict):
            result.append(row)
    return result


def _rows_to_names(rows: list[Mapping[str, object]]) -> dict[int, str]:
    names: dict[int, str] = {}
    for row in rows:
        item_id = row.get("id")
        name = row.get("name")
        if (
            isinstance(item_id, int)
            and not isinstance(item_id, bool)
            and item_id > 0
            and isinstance(name, str)
            and name
        ):
            names[item_id] = name
    return names


def load_name_catalogs(item_map_path: Path, rune_table_path: Path) -> NameCatalogs:
    item_names = _rows_to_names(_load_name_rows(item_map_path, "物品名称表"))
    rune_names: dict[int, str] = {}
    if rune_table_path.expanduser().is_file():
        rune_names = _rows_to_names(
            _load_name_rows(rune_table_path, "律文名称表")
        )
    return NameCatalogs(item_names, rune_names)


def _kind_label(kind: int) -> str:
    return PROP_KIND_LABELS.get(kind, f"类型 {kind}")


def _reward_name(catalogs: NameCatalogs, kind: int, base_id: int) -> str:
    if kind == PROP_KIND_ORDER_RUNE:
        if base_id > 0:
            return catalogs.rune_names.get(base_id, rune_name(base_id))
        return "未知律文"
    if kind == PROP_KIND_ARTIFACT:
        if base_id > 0:
            return artifact_name(base_id)
        return "未知秘宝"
    if base_id > 0:
        return catalogs.item_names.get(base_id, reward_name(kind, base_id))
    return f"未知{_kind_label(kind)}"


def _source_notifications(
    attempt: RuneEngravingAttempt,
) -> tuple[RuneItemChangeNotify, ...]:
    return tuple(
        notify
        for notify in attempt.notifications
        if notify.source == ORDER_RUNE_PULL_SOURCE
    )


def _attempt_free_after(attempt: RuneEngravingAttempt) -> int | None:
    pull_info = attempt.response.pull_info
    return pull_info.free_pull_num if pull_info is not None else None


def _attempt_has_cost(attempt: RuneEngravingAttempt) -> bool:
    for notify in _source_notifications(attempt):
        if any(cost.amount > 0 for cost in notify.costs):
            return True
        if any(item.delta < 0 for item in notify.items):
            return True
    return False


def resolve_attempt_rewards(
    attempt: RuneEngravingAttempt,
    catalogs: NameCatalogs,
) -> tuple[RewardSummary, ...]:
    notifications = _source_notifications(attempt)
    props = [prop for notify in notifications for prop in notify.props if prop.amount > 0]
    notify_ids = [
        instance_id
        for notify in notifications
        for instance_id in notify.rune_instance_ids
        if instance_id > 0
    ]
    target_ids = list(dict.fromkeys((*attempt.response.instance_ids, *notify_ids)))
    infos = {rune.instance_id: rune for rune in attempt.rune_infos}

    # For rune props, ``num`` is the generated rune level, not a quantity.
    pending_runes: list[list[int]] = [
        [prop.item_id, prop.amount]
        for prop in props
        if prop.kind == PROP_KIND_ORDER_RUNE and prop.item_id > 0
    ]
    rewards: list[RewardSummary] = []
    unresolved_ids: list[int] = []

    for instance_id in target_ids:
        rune = infos.get(instance_id)
        if rune is None:
            unresolved_ids.append(instance_id)
            continue
        rewards.append(
            RewardSummary(
                kind=PROP_KIND_ORDER_RUNE,
                base_id=rune.order_rune_id,
                quantity=1,
                name=_reward_name(
                    catalogs, PROP_KIND_ORDER_RUNE, rune.order_rune_id
                ),
                instance_id=rune.instance_id,
                rarity=rune.rarity,
                level=rune.level,
                stage_level=rune.stage_level,
                affix_attributes=rune.affix_attributes,
                pattern_attributes=rune.pattern_attributes,
            )
        )
        for index, pending in enumerate(pending_runes):
            if pending[0] == rune.order_rune_id:
                pending_runes.pop(index)
                break

    for instance_id in unresolved_ids:
        base_id = 0
        reported_level = 0
        if pending_runes:
            base_id, reported_level = pending_runes.pop(0)
        rewards.append(
            RewardSummary(
                kind=PROP_KIND_ORDER_RUNE,
                base_id=base_id,
                quantity=1,
                name=_reward_name(catalogs, PROP_KIND_ORDER_RUNE, base_id),
                instance_id=instance_id,
                rarity=0,
                level=max(reported_level, 0),
                stage_level=0,
                affix_attributes=(),
                pattern_attributes=(),
            )
        )

    if not target_ids:
        for base_id, reported_level in pending_runes:
            rewards.append(
                RewardSummary(
                    kind=PROP_KIND_ORDER_RUNE,
                    base_id=base_id,
                    quantity=1,
                    name=_reward_name(catalogs, PROP_KIND_ORDER_RUNE, base_id),
                    instance_id=0,
                    rarity=0,
                    level=max(reported_level, 0),
                    stage_level=0,
                    affix_attributes=(),
                    pattern_attributes=(),
                )
            )

    pending_artifacts: list[int] = [
        prop.item_id
        for prop in props
        if prop.kind == PROP_KIND_ARTIFACT and prop.item_id > 0
    ]
    artifact_instance_ids = list(
        dict.fromkeys(
            instance_id
            for notify in notifications
            for instance_id in notify.artifact_instance_ids
            if instance_id > 0
        )
    )
    for instance_id in artifact_instance_ids:
        base_id = pending_artifacts.pop(0) if pending_artifacts else 0
        rewards.append(
            RewardSummary(
                kind=PROP_KIND_ARTIFACT,
                base_id=base_id,
                quantity=1,
                name=_reward_name(catalogs, PROP_KIND_ARTIFACT, base_id),
                instance_id=instance_id,
                rarity=artifact_rarity(base_id) if base_id > 0 else 0,
                level=0,
                stage_level=0,
                affix_attributes=(),
                pattern_attributes=(),
                form=ARTIFACT_FORM_WHOLE,
            )
        )
    for base_id in pending_artifacts:
        rewards.append(
            RewardSummary(
                kind=PROP_KIND_ARTIFACT,
                base_id=base_id,
                quantity=1,
                name=_reward_name(catalogs, PROP_KIND_ARTIFACT, base_id),
                instance_id=0,
                rarity=artifact_rarity(base_id),
                level=0,
                stage_level=0,
                affix_attributes=(),
                pattern_attributes=(),
                form=ARTIFACT_FORM_WHOLE,
            )
        )

    for prop in props:
        if prop.kind in {PROP_KIND_ORDER_RUNE, PROP_KIND_ARTIFACT}:
            continue
        if prop.kind == PROP_KIND_ITEM and is_artifact_piece_item(prop.item_id):
            rewards.append(
                RewardSummary(
                    kind=PROP_KIND_ITEM,
                    base_id=prop.item_id,
                    quantity=prop.amount,
                    name=_reward_name(catalogs, PROP_KIND_ITEM, prop.item_id),
                    instance_id=0,
                    rarity=item_quality(prop.item_id),
                    level=0,
                    stage_level=0,
                    affix_attributes=(),
                    pattern_attributes=(),
                    form=ARTIFACT_FORM_PIECE,
                )
            )
            continue
        rewards.append(
            RewardSummary(
                kind=prop.kind,
                base_id=prop.item_id,
                quantity=prop.amount,
                name=_reward_name(catalogs, prop.kind, prop.item_id),
                instance_id=0,
                rarity=0,
                level=0,
                stage_level=0,
                affix_attributes=(),
                pattern_attributes=(),
            )
        )

    equipment_instance_ids = list(
        dict.fromkeys(
            instance_id
            for notify in notifications
            for instance_id in notify.equipment_instance_ids
            if instance_id > 0
        )
    )
    for instance_id in equipment_instance_ids:
        rewards.append(
            RewardSummary(
                kind=PROP_KIND_EQUIPMENT,
                base_id=0,
                quantity=1,
                name=_reward_name(catalogs, PROP_KIND_EQUIPMENT, 0),
                instance_id=instance_id,
                rarity=0,
                level=0,
                stage_level=0,
                affix_attributes=(),
                pattern_attributes=(),
            )
        )

    remaining_prop_items: dict[int, int] = {}
    for prop in props:
        if prop.kind == PROP_KIND_ITEM:
            remaining_prop_items[prop.item_id] = (
                remaining_prop_items.get(prop.item_id, 0) + prop.amount
            )
    for item in (item for notify in notifications for item in notify.items):
        if item.delta <= 0:
            continue
        covered = min(item.delta, remaining_prop_items.get(item.item_id, 0))
        remaining_prop_items[item.item_id] = (
            remaining_prop_items.get(item.item_id, 0) - covered
        )
        missing_quantity = item.delta - covered
        if missing_quantity <= 0:
            continue
        if is_artifact_piece_item(item.item_id):
            rewards.append(
                RewardSummary(
                    kind=PROP_KIND_ITEM,
                    base_id=item.item_id,
                    quantity=missing_quantity,
                    name=_reward_name(catalogs, PROP_KIND_ITEM, item.item_id),
                    instance_id=0,
                    rarity=item_quality(item.item_id),
                    level=0,
                    stage_level=0,
                    affix_attributes=(),
                    pattern_attributes=(),
                    form=ARTIFACT_FORM_PIECE,
                )
            )
            continue
        rewards.append(
            RewardSummary(
                kind=PROP_KIND_ITEM,
                base_id=item.item_id,
                quantity=missing_quantity,
                name=_reward_name(catalogs, PROP_KIND_ITEM, item.item_id),
                instance_id=0,
                rarity=0,
                level=0,
                stage_level=0,
                affix_attributes=(),
                pattern_attributes=(),
            )
        )

    if not rewards:
        for instance_id in target_ids:
            rewards.append(
                RewardSummary(
                    kind=PROP_KIND_ORDER_RUNE,
                    base_id=0,
                    quantity=1,
                    name=_reward_name(catalogs, PROP_KIND_ORDER_RUNE, 0),
                    instance_id=instance_id,
                    rarity=0,
                    level=0,
                    stage_level=0,
                    affix_attributes=(),
                    pattern_attributes=(),
                )
            )
    return tuple(rewards)


class AncientLawCourtClient:
    """Read and consume only the server-advertised free rune pull quota."""

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
            task="ancient_law_court",
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

    def _receive_optional_header(
        self, deadline: float, context: str
    ) -> MessageHeader | None:
        from game_session import try_session_receive_header

        handled, header = try_session_receive_header(
            self, deadline, context, allow_timeout=True
        )
        if handled:
            return header
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        try:
            assert self.socket is not None
            opcode, frame = self.socket.recv_message(remaining)
        except socket.timeout:
            return None
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

    def _sync_missing_pull_info(self, state: RuneStorageState) -> RuneStorageState:
        self._send_message(
            SYNC_ORDER_RUNE_PULL_MESSAGE_ID,
            b"",
            encrypted=True,
        )
        deadline = time.monotonic() + self.timeout
        while True:
            header = self._receive_header(deadline, "古律院免费铭刻状态同步")
            if self._handle_common_message(header):
                continue
            if header.message_id != SYNC_ORDER_RUNE_PULL_MESSAGE_ID:
                continue
            pull_info = decode_rune_pull_sync(header.data)
            if pull_info is None:
                raise HarvestError("古律院状态同步响应缺少 pullinfo")
            synced = RuneStorageState(
                state.capacity,
                state.free_slots,
                state.runes,
                pull_info,
            )
            _validate_state(synced)
            return synced

    def _login_and_read_state(self) -> RuneStorageState | None:
        from game_session import try_session_ensure_ready

        if try_session_ensure_ready(self, self.endpoint):
            game_data = getattr(self._session, "game_data", None)
            if not game_data:
                return None
            state = decode_game_data_rune_state(bytes(game_data))
            if state is not None:
                _validate_state(state)
                if state.pull_info is None:
                    state = self._sync_missing_pull_info(state)
            return state

        self.socket = self.socket_factory(self.endpoint.url, self.timeout)
        self._send_message(
            LOGIN_MESSAGE_ID,
            encode_login_payload(self.endpoint.game_token),
            encrypted=False,
        )

        login_complete = False
        game_data_received = False
        state: RuneStorageState | None = None
        synced_pull_info: RunePullInfo | None = None
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
                state = decode_game_data_rune_state(header.data)
                if state is not None:
                    _validate_state(state)
                game_data_received = True
            elif header.message_id == SYNC_ORDER_RUNE_PULL_MESSAGE_ID:
                synced_pull_info = decode_rune_pull_sync(header.data)
                if synced_pull_info is None:
                    raise HarvestError("古律院状态同步消息缺少 pullinfo")
                _validate_state(RuneStorageState(0, 0, (), synced_pull_info))
            elif header.message_id == LOGIN_REUNIQUE_MESSAGE_ID:
                login_complete = True

        if state is not None:
            if synced_pull_info is not None:
                state = RuneStorageState(
                    state.capacity,
                    state.free_slots,
                    state.runes,
                    synced_pull_info,
                )
            elif state.pull_info is None:
                state = self._sync_missing_pull_info(state)
        return state

    @staticmethod
    def _draw_messages_complete(
        response: RunePullResponse,
        rune_infos: Mapping[int, OrderRune],
        notifications: list[RuneItemChangeNotify],
    ) -> bool:
        source_notifications = [
            notify
            for notify in notifications
            if notify.source == ORDER_RUNE_PULL_SOURCE
        ]
        if not source_notifications:
            return False
        rune_instance_ids = set(response.instance_ids)
        rune_instance_ids.update(
            instance_id
            for notify in source_notifications
            for instance_id in notify.rune_instance_ids
        )
        has_reward_reference = bool(rune_instance_ids) or any(
            notify.equipment_instance_ids
            or notify.artifact_instance_ids
            or any(item.delta > 0 for item in notify.items)
            or any(prop.amount > 0 for prop in notify.props)
            for notify in source_notifications
        )
        return has_reward_reference and all(
            instance_id in rune_infos for instance_id in rune_instance_ids
        )

    def _engrave_once(self, sequence: int, free_before: int) -> RuneEngravingAttempt:
        if free_before <= 0:
            raise HarvestError("发送免费铭刻请求前，免费次数必须大于 0")
        self._send_message(
            ORDER_RUNE_PULL_MESSAGE_ID,
            encode_free_rune_engraving_payload(),
            encrypted=True,
        )
        deadline = time.monotonic() + self.timeout
        response: RunePullResponse | None = None
        rune_infos: dict[int, OrderRune] = {}
        notifications: list[RuneItemChangeNotify] = []

        def handle_business_message(header: MessageHeader) -> None:
            nonlocal response
            if header.message_id == SYNC_ORDER_RUNE_INFO_MESSAGE_ID:
                sync = decode_rune_info_sync(header.data)
                if (
                    sync.rune is not None
                    and sync.rune.instance_id > 0
                    and sync.change_type in (0, ORDER_RUNE_ADD, ORDER_RUNE_UPDATE)
                ):
                    rune_infos[sync.rune.instance_id] = sync.rune
            elif header.message_id == STORAGE_ITEM_CHANGE_MESSAGE_ID:
                notify = decode_rune_item_change_notify(header.data)
                if notify.source == ORDER_RUNE_PULL_SOURCE:
                    notifications.append(notify)
            elif header.message_id == ORDER_RUNE_PULL_MESSAGE_ID:
                response = decode_rune_pull_response(header.data)

        while response is None:
            header = self._receive_header(deadline, "古律院免费铭刻响应")
            if self._handle_common_message(header):
                continue
            handle_business_message(header)

        if response.result == RESULT_SUCCESS:
            grace_deadline = min(
                deadline,
                time.monotonic() + REWARD_NOTIFICATION_GRACE_SECONDS,
            )
            while not self._draw_messages_complete(
                response, rune_infos, notifications
            ):
                header = self._receive_optional_header(
                    grace_deadline, "古律院铭刻奖励通知"
                )
                if header is None:
                    break
                if self._handle_common_message(header):
                    continue
                handle_business_message(header)

        source_notifications = [
            notify
            for notify in notifications
            if notify.source == ORDER_RUNE_PULL_SOURCE
        ]
        notify_ids = {
            instance_id
            for notify in source_notifications
            for instance_id in notify.rune_instance_ids
        }
        response_ids = set(response.instance_ids)
        expected_rune_ids = response_ids.union(notify_ids)
        ids_consistent = (
            not response_ids or not notify_ids or response_ids == notify_ids
        )
        costs_detected = any(
            cost.amount > 0
            for notify in source_notifications
            for cost in notify.costs
        ) or any(
            item.delta < 0
            for notify in source_notifications
            for item in notify.items
        )
        free_after = (
            response.pull_info.free_pull_num
            if response.pull_info is not None
            else None
        )
        has_reward_reference = bool(expected_rune_ids) or any(
            notify.equipment_instance_ids
            or notify.artifact_instance_ids
            or any(item.delta > 0 for item in notify.items)
            or any(prop.amount > 0 for prop in notify.props)
            for notify in source_notifications
        )
        missing_infos = sorted(expected_rune_ids.difference(rune_infos))

        warnings: list[str] = []
        if response.result == RESULT_SUCCESS:
            if response.mode != PULL_MODE_FREE:
                warnings.append(
                    f"响应模式异常：期望 {PULL_MODE_FREE}，实际 {response.mode}"
                )
            if not source_notifications:
                warnings.append("未收到 source=410 的古律院物品变更通知")
            if not ids_consistent:
                warnings.append("铭刻响应实例 ID 与物品变更通知不一致")
            if missing_infos:
                warnings.append(
                    "未收到部分律文实例同步：" + ",".join(map(str, missing_infos))
                )
            if costs_detected:
                warnings.append("免费铭刻通知中出现实际物品成本")
            if free_after is None:
                warnings.append("铭刻响应缺少更新后的免费次数")
            elif free_after != free_before - 1:
                warnings.append(
                    f"免费次数变化异常：期望 {free_before - 1}，实际 {free_after}"
                )
            if not has_reward_reference:
                warnings.append("铭刻成功响应未提供抽中物品")

        safety_verified = (
            response.result == RESULT_SUCCESS
            and response.mode == PULL_MODE_FREE
            and bool(source_notifications)
            and has_reward_reference
            and ids_consistent
            and not missing_infos
            and not costs_detected
            and free_after == free_before - 1
        )
        can_continue = safety_verified and free_after is not None and free_after > 0
        return RuneEngravingAttempt(
            sequence=sequence,
            free_before=free_before,
            response=response,
            rune_infos=tuple(rune_infos.values()),
            notifications=tuple(notifications),
            warnings=tuple(warnings),
            safety_verified=safety_verified,
            can_continue=can_continue,
        )

    def engrave_daily_free(
        self, *, max_attempts: int | None = None
    ) -> AncientLawCourtDailyResult:
        if max_attempts is not None and max_attempts <= 0:
            raise HarvestError("古律院铭刻次数上限必须为正整数")
        try:
            state = self._login_and_read_state()
            if state is None or state.pull_info is None:
                return AncientLawCourtDailyResult(state, ())

            free_count = state.pull_info.free_pull_num
            attempts: list[RuneEngravingAttempt] = []
            current_free = free_count
            for sequence in range(1, free_count + 1):
                if max_attempts is not None and len(attempts) >= max_attempts:
                    break
                if current_free <= 0:
                    break
                attempt = self._engrave_once(sequence, current_free)
                attempts.append(attempt)
                if not attempt.can_continue:
                    break
                free_after = _attempt_free_after(attempt)
                assert free_after is not None
                current_free = free_after
            return AncientLawCourtDailyResult(state, tuple(attempts))
        finally:
            from game_session import shared_close

            if shared_close(self) and self.socket is not None:
                self.socket.close()
                self.socket = None


def _attribute_record(attribute: RuneAttribute) -> dict[str, int]:
    return {
        "id": attribute.attribute_id,
        "type": attribute.attribute_type,
        "value": attribute.value,
    }


def _reward_record(reward: RewardSummary) -> dict[str, object]:
    form = reward.form
    if reward.kind == PROP_KIND_ARTIFACT or form == ARTIFACT_FORM_WHOLE:
        rarity_label = ARTIFACT_RARITY_LABELS.get(
            reward.rarity, "" if reward.rarity == 0 else str(reward.rarity)
        )
        color_label = ARTIFACT_COLOR_LABELS.get(reward.rarity, "")
        form = form or ARTIFACT_FORM_WHOLE
    elif form == ARTIFACT_FORM_PIECE or (
        reward.kind == PROP_KIND_ITEM and is_artifact_piece_item(reward.base_id)
    ):
        rarity_label = RUNE_RARITY_LABELS.get(
            reward.rarity, "" if reward.rarity == 0 else str(reward.rarity)
        )
        color_label = RUNE_COLOR_LABELS.get(reward.rarity, "")
        form = ARTIFACT_FORM_PIECE
    else:
        rarity_label = RUNE_RARITY_LABELS.get(
            reward.rarity, "" if reward.rarity == 0 else str(reward.rarity)
        )
        color_label = RUNE_COLOR_LABELS.get(reward.rarity, "")
    return {
        "kind": reward.kind,
        "kind_label": _kind_label(reward.kind),
        "id": reward.base_id,
        "name": reward.name,
        "quantity": reward.quantity,
        "instance_id": reward.instance_id,
        "rarity": reward.rarity,
        "rarity_label": rarity_label,
        "color_label": color_label,
        "form": form,
        "level": reward.level,
        "stage_level": reward.stage_level,
        "affix_attributes": [
            _attribute_record(attribute) for attribute in reward.affix_attributes
        ],
        "pattern_attributes": [
            _attribute_record(attribute) for attribute in reward.pattern_attributes
        ],
    }


def _prop_record(prop: RewardProp, catalogs: NameCatalogs) -> dict[str, object]:
    return {
        "kind": prop.kind,
        "kind_label": _kind_label(prop.kind),
        "id": prop.item_id,
        "name": _reward_name(catalogs, prop.kind, prop.item_id),
        "quantity": prop.amount,
    }


def build_daily_result_log_record(
    endpoint: GameEndpoint,
    result: AncientLawCourtDailyResult,
    catalogs: NameCatalogs,
    *,
    timestamp: str | None = None,
) -> dict[str, object]:
    state = result.state
    pull_info = state.pull_info if state is not None else None
    return {
        "timestamp": timestamp
        or datetime.now().astimezone().isoformat(timespec="seconds"),
        "event": "ancient_law_court_daily_free_engraving",
        "zone": {"id": endpoint.zone_id, "name": endpoint.zone_name},
        "state_present": state is not None,
        "free_checked": pull_info.free_pull_num if pull_info is not None else 0,
        "request_count": len(result.attempts),
        "storage": None
        if state is None
        else {
            "capacity": state.capacity,
            "free_slots": state.free_slots,
            "rune_count": len(state.runes),
            "pull_info": None
            if pull_info is None
            else {
                "daily_pull_num": pull_info.daily_pull_num,
                "daily_time": pull_info.daily_time,
                "pull_id": pull_info.pull_id,
                "pity_score": pull_info.pity_score,
                "free_pull_num": pull_info.free_pull_num,
                "free_time": pull_info.free_time,
                "wishlist_open": pull_info.wishlist_open,
            },
        },
        "attempts": [
            {
                "sequence": attempt.sequence,
                "mode": attempt.response.mode,
                "result": attempt.response.result,
                "result_label": RESULT_LABELS.get(
                    attempt.response.result,
                    f"服务端返回 ret={attempt.response.result}",
                ),
                "free_before": attempt.free_before,
                "free_after": _attempt_free_after(attempt),
                "response_instance_ids": list(attempt.response.instance_ids),
                "notification_equipment_instance_ids": [
                    instance_id
                    for notify in _source_notifications(attempt)
                    for instance_id in notify.equipment_instance_ids
                ],
                "notification_artifact_instance_ids": [
                    instance_id
                    for notify in _source_notifications(attempt)
                    for instance_id in notify.artifact_instance_ids
                ],
                "notification_instance_ids": [
                    instance_id
                    for notify in _source_notifications(attempt)
                    for instance_id in notify.rune_instance_ids
                ],
                "safety_verified": attempt.safety_verified,
                "unexpected_cost_detected": _attempt_has_cost(attempt),
                "warnings": list(attempt.warnings),
                "costs": [
                    _prop_record(cost, catalogs)
                    for notify in _source_notifications(attempt)
                    for cost in notify.costs
                ],
                "item_changes": [
                    {
                        "id": item.item_id,
                        "name": catalogs.item_names.get(
                            item.item_id, item_name(item.item_id)
                        ),
                        "delta": item.delta,
                        "total": item.total,
                    }
                    for notify in _source_notifications(attempt)
                    for item in notify.items
                ],
                "rewards": [
                    _reward_record(reward)
                    for reward in resolve_attempt_rewards(attempt, catalogs)
                ],
            }
            for attempt in result.attempts
        ],
        "highlight_runes": [
            {
                **_reward_record(reward),
                "kind_label": (
                    "秘宝"
                    if reward.kind == PROP_KIND_ARTIFACT
                    or reward.form in {ARTIFACT_FORM_WHOLE, ARTIFACT_FORM_PIECE}
                    else _kind_label(reward.kind)
                ),
            }
            for reward in collect_highlight_runes(result, catalogs)
        ],
    }


def append_daily_result_log(
    path: Path | object | None,
    endpoint: GameEndpoint,
    result: AncientLawCourtDailyResult,
    catalogs: NameCatalogs,
    *,
    timestamp: str | None = None,
) -> None:
    record = build_daily_result_log_record(
        endpoint, result, catalogs, timestamp=timestamp
    )
    failed_results = any(attempt.response.result != RESULT_SUCCESS for attempt in result.attempts)
    failed_safety = any(not attempt.safety_verified for attempt in result.attempts)
    if failed_results:
        outcome, level, error = "failure", "error", {"type": "AncientLawCourtOutcome", "code": "server_result_failed", "message": "铭刻响应失败"}
    elif failed_safety:
        outcome, level, error = "failure", "error", {"type": "AncientLawCourtOutcome", "code": "safety_verification_failed", "message": "免费铭刻安全校验未通过"}
    elif not result.attempts:
        outcome, level, error = "skipped", "warning", {"type": "AncientLawCourtOutcome", "code": "no_free_attempt", "message": "没有可执行的免费铭刻"}
    else:
        outcome, level, error = "success", "info", None
    details = {key: value for key, value in record.items() if key not in {"timestamp", "event", "zone"}}
    try:
        write_standard_log(event="ancient_law_court_daily_free_engraving", operation="daily_free_engraving", zone=record["zone"], details=details, destination=path, timestamp=record["timestamp"], outcome=outcome, level=level, error=error)
    except LogPersistenceError as exc:
        raise HarvestError(f"写入古律院铭刻结果日志失败：{exc}") from exc


def _artifact_color_and_quality(reward: RewardSummary) -> tuple[str, str]:
    form = reward.form or (
        ARTIFACT_FORM_WHOLE if reward.kind == PROP_KIND_ARTIFACT else ARTIFACT_FORM_PIECE
    )
    if form == ARTIFACT_FORM_PIECE:
        color = RUNE_COLOR_LABELS.get(reward.rarity, "")
        quality = RUNE_RARITY_LABELS.get(
            reward.rarity, str(reward.rarity) if reward.rarity else "未知"
        )
        return color, quality
    color = ARTIFACT_COLOR_LABELS.get(reward.rarity, "")
    quality = ARTIFACT_RARITY_LABELS.get(
        reward.rarity, str(reward.rarity) if reward.rarity else "未知"
    )
    return color, quality


def _format_reward(reward: RewardSummary) -> str:
    amount = f" x{reward.quantity}" if reward.quantity != 1 else ""
    if reward.kind == PROP_KIND_ORDER_RUNE:
        details: list[str] = []
        if reward.rarity > 0:
            color = RUNE_COLOR_LABELS.get(reward.rarity, "")
            quality = RUNE_RARITY_LABELS.get(reward.rarity, str(reward.rarity))
            if color:
                details.append(f"品质 {color}·{quality}")
            else:
                details.append(f"品质 {quality}")
        if reward.level > 0:
            details.append(f"Lv.{reward.level}")
        if reward.stage_level > 0:
            details.append(f"淬阶 +{reward.stage_level}")
        suffix = f"（{'；'.join(details)}）" if details else ""
        return f"{reward.name}{amount}{suffix}"
    if reward.kind == PROP_KIND_ARTIFACT or reward.form in {
        ARTIFACT_FORM_WHOLE,
        ARTIFACT_FORM_PIECE,
    }:
        form = reward.form or (
            ARTIFACT_FORM_WHOLE
            if reward.kind == PROP_KIND_ARTIFACT
            else ARTIFACT_FORM_PIECE
        )
        details = [f"秘宝·{form}"]
        if reward.rarity > 0:
            color, quality = _artifact_color_and_quality(reward)
            if color:
                details.append(f"品质 {color}·{quality}")
            else:
                details.append(f"品质 {quality}")
        return f"{reward.name}{amount}（{'；'.join(details)}）"
    return f"{reward.name}{amount}"


def is_highlight_rune_rarity(rarity: int) -> bool:
    """紫色（B）、黄色（A）或绿色（S）律文需要在日志中单独强调。"""

    return rarity in HIGHLIGHT_RUNE_RARITIES


def is_highlight_artifact_rarity(rarity: int) -> bool:
    """紫色、黄色或绿色秘宝整体需要在日志中单独强调。"""

    return rarity in HIGHLIGHT_ARTIFACT_RARITIES


def is_highlight_artifact_piece_quality(quality: int) -> bool:
    """紫色、黄色或绿色秘宝碎片（物品 quality）需要在日志中单独强调。"""

    return quality in HIGHLIGHT_ARTIFACT_PIECE_QUALITIES


def is_highlight_reward(reward: RewardSummary | object) -> bool:
    kind = int(getattr(reward, "kind", 0) or 0)
    rarity = int(getattr(reward, "rarity", 0) or 0)
    form = str(getattr(reward, "form", "") or "")
    if kind == PROP_KIND_ORDER_RUNE:
        return is_highlight_rune_rarity(rarity)
    if kind == PROP_KIND_ARTIFACT or form == ARTIFACT_FORM_WHOLE:
        return is_highlight_artifact_rarity(rarity)
    if form == ARTIFACT_FORM_PIECE or (
        kind == PROP_KIND_ITEM and is_artifact_piece_item(getattr(reward, "base_id", 0))
    ):
        return is_highlight_artifact_piece_quality(rarity)
    return False


def collect_highlight_runes(
    result: AncientLawCourtDailyResult,
    catalogs: NameCatalogs | None = None,
) -> tuple[RewardSummary, ...]:
    """从铭刻结果中提取紫/黄/绿品质律文与秘宝（按实例去重）。"""

    highlights: list[RewardSummary] = []
    seen: set[tuple[int, int, int, int]] = set()

    def consider(reward: RewardSummary) -> None:
        if not is_highlight_reward(reward):
            return
        key = (reward.kind, reward.instance_id, reward.base_id, reward.rarity)
        if key in seen:
            return
        seen.add(key)
        highlights.append(reward)

    for attempt in result.attempts:
        rune_infos = getattr(attempt, "rune_infos", ()) or ()
        if catalogs is not None and hasattr(attempt, "notifications"):
            for reward in resolve_attempt_rewards(attempt, catalogs):
                consider(reward)
        # 同步下来的律文详情是品质判定的权威来源，即使奖励解析未带上也会纳入。
        for rune in rune_infos:
            consider(
                RewardSummary(
                    kind=PROP_KIND_ORDER_RUNE,
                    base_id=rune.order_rune_id,
                    quantity=1,
                    name=(
                        catalogs.rune_names.get(rune.order_rune_id, rune_name(rune.order_rune_id))
                        if catalogs is not None
                        else rune_name(rune.order_rune_id)
                    ),
                    instance_id=rune.instance_id,
                    rarity=rune.rarity,
                    level=rune.level,
                    stage_level=rune.stage_level,
                    affix_attributes=getattr(rune, "affix_attributes", ()) or (),
                    pattern_attributes=getattr(rune, "pattern_attributes", ()) or (),
                )
            )
    return tuple(highlights)


def format_highlight_rune_summary(
    highlights: Sequence[RewardSummary] | Sequence[object],
) -> str:
    """格式化为日志片段。

    例如：百胜（律文·紫色·珍稀/B）、永恒之心（秘宝·整体·黄色·II）、
    邪力水晶残片（秘宝·碎片·紫色·珍稀/B）。
    """

    parts: list[str] = []
    for reward in highlights:
        kind = int(getattr(reward, "kind", PROP_KIND_ORDER_RUNE) or PROP_KIND_ORDER_RUNE)
        rarity = int(getattr(reward, "rarity", 0) or 0)
        base_id = int(getattr(reward, "base_id", 0) or 0)
        form = str(getattr(reward, "form", "") or "")
        name = str(getattr(reward, "name", "") or "")
        if kind == PROP_KIND_ARTIFACT or form in {
            ARTIFACT_FORM_WHOLE,
            ARTIFACT_FORM_PIECE,
        }:
            if not name:
                name = (
                    item_name(base_id)
                    if form == ARTIFACT_FORM_PIECE or kind == PROP_KIND_ITEM
                    else artifact_name(base_id)
                )
            if not form:
                form = (
                    ARTIFACT_FORM_WHOLE
                    if kind == PROP_KIND_ARTIFACT
                    else ARTIFACT_FORM_PIECE
                )
            # 碎片用装备色板（quality），整体用秘宝 rarity 色板。
            if form == ARTIFACT_FORM_PIECE:
                color = RUNE_COLOR_LABELS.get(rarity, "")
                quality = RUNE_RARITY_LABELS.get(
                    rarity, str(rarity) if rarity else "未知"
                )
            else:
                color = ARTIFACT_COLOR_LABELS.get(rarity, "")
                quality = ARTIFACT_RARITY_LABELS.get(
                    rarity, str(rarity) if rarity else "未知"
                )
            if color:
                parts.append(f"{name}（秘宝·{form}·{color}·{quality}）")
            else:
                parts.append(f"{name}（秘宝·{form}·{quality}）")
            continue

        if not name:
            name = rune_name(base_id)
        color = RUNE_COLOR_LABELS.get(rarity, "")
        quality = RUNE_RARITY_LABELS.get(rarity, str(rarity) if rarity else "未知")
        if color:
            parts.append(f"{name}（律文·{color}·{quality}）")
        else:
            parts.append(f"{name}（律文·{quality}）")
    return "、".join(parts)


def print_daily_result(
    endpoint: GameEndpoint,
    result: AncientLawCourtDailyResult,
    catalogs: NameCatalogs,
) -> None:
    print(
        f"古律院每日免费铭刻检查完成，"
        f"区服：{zone_name(endpoint.zone_id, endpoint.zone_name)}"
    )
    if result.state is None:
        print("Game_data 未返回古律院状态，未发送铭刻请求。")
        return
    if result.state.pull_info is None:
        print("古律院状态缺少免费次数，未发送铭刻请求。")
        return

    pull_info = result.state.pull_info
    print(
        f"检查时免费铭刻次数：{pull_info.free_pull_num}；"
        f"今日已铭刻：{pull_info.daily_pull_num}"
    )
    if pull_info.free_pull_num == 0:
        print("今日没有可用的免费铭刻次数，未发送铭刻请求。")
        return

    for attempt in result.attempts:
        status = RESULT_LABELS.get(
            attempt.response.result,
            f"服务端返回 ret={attempt.response.result}",
        )
        free_after = _attempt_free_after(attempt)
        after_label = "未知" if free_after is None else str(free_after)
        print(
            f"第 {attempt.sequence} 次免费铭刻：{status}；"
            f"免费次数 {attempt.free_before} -> {after_label}"
        )
        rewards = resolve_attempt_rewards(attempt, catalogs)
        if rewards:
            for reward in rewards:
                print(f"  铭刻抽到的物品：{_format_reward(reward)}")
        else:
            print("  铭刻抽到的物品：服务端未返回可解析的奖励")
        for warning in attempt.warnings:
            print(f"  校验提示：{warning}")

    highlights = collect_highlight_runes(result, catalogs)
    if highlights:
        print(
            "高品质掉落（紫/黄/绿律文或秘宝）："
            + format_highlight_rune_summary(highlights)
        )


def run_self_tests() -> None:
    assert encode_free_rune_engraving_payload() == b""

    def encoded_pull_info(free: int, daily: int = 4) -> bytes:
        payload = encode_int_field(1, daily)
        payload += encode_int_field(5, 17)
        if free:
            payload += encode_int_field(6, free)
        payload += encode_int_field(7, 3600)
        return payload

    attribute = (
        encode_int_field(1, 7001)
        + encode_int_field(2, 101)
        + encode_int_field(3, 250)
    )
    rune_instance_id = 900000000001
    rune_payload = (
        encode_int_field(1, rune_instance_id)
        + encode_int_field(3, 9001)
        + encode_int_field(5, 4)
        + encode_int_field(7, 1)
        + encode_bytes_field(8, attribute)
    )
    rune_storage = (
        encode_int_field(1, 200)
        + encode_int_field(2, 199)
        + encode_bytes_field(3, rune_payload)
        + encode_bytes_field(4, encoded_pull_info(1))
    )
    game_data = encode_bytes_field(8, encode_bytes_field(3, rune_storage))
    decoded_state = decode_game_data_rune_state(game_data)
    assert decoded_state is not None
    assert decoded_state.capacity == 200
    assert decoded_state.free_slots == 199
    assert decoded_state.pull_info is not None
    assert decoded_state.pull_info.free_pull_num == 1
    assert decoded_state.runes[0].order_rune_id == 9001
    assert decoded_state.runes[0].affix_attributes == (
        RuneAttribute(7001, 101, 250),
    )

    updated_pull_info = encoded_pull_info(0, daily=5)
    response_payload = (
        encode_bytes_field(3, encode_varint(rune_instance_id))
        + encode_bytes_field(4, updated_pull_info)
    )
    decoded_response = decode_rune_pull_response(response_payload)
    assert decoded_response.result == RESULT_SUCCESS
    assert decoded_response.mode == PULL_MODE_FREE
    assert decoded_response.instance_ids == (rune_instance_id,)
    assert decoded_response.pull_info is not None
    assert decoded_response.pull_info.free_pull_num == 0

    sync_payload = encode_int_field(1, ORDER_RUNE_ADD) + encode_bytes_field(
        2, rune_payload
    )
    assert decode_rune_info_sync(sync_payload) == RuneInfoSync(
        ORDER_RUNE_ADD, decode_order_rune(rune_payload)
    )

    reward_prop = (
        encode_int_field(1, PROP_KIND_ORDER_RUNE)
        + encode_int_field(2, 9001)
        + encode_int_field(3, 1)
    )
    notify_payload = (
        encode_int_field(1, ORDER_RUNE_PULL_SOURCE)
        + encode_bytes_field(10, encode_varint(rune_instance_id))
        + encode_bytes_field(21, reward_prop)
        + encode_bytes_field(21, reward_prop)
    )
    decoded_notify = decode_rune_item_change_notify(notify_payload)
    assert decoded_notify.source == ORDER_RUNE_PULL_SOURCE
    assert decoded_notify.equipment_instance_ids == ()
    assert decoded_notify.artifact_instance_ids == ()
    assert decoded_notify.rune_instance_ids == (rune_instance_id,)
    assert decoded_notify.props == (
        RewardProp(PROP_KIND_ORDER_RUNE, 9001, 1),
        RewardProp(PROP_KIND_ORDER_RUNE, 9001, 1),
    )

    generic_instance_notify = decode_rune_item_change_notify(
        encode_int_field(1, ORDER_RUNE_PULL_SOURCE)
        + encode_bytes_field(3, encode_varint(800000000001))
        + encode_int_field(4, 700000000001)
    )
    assert generic_instance_notify.equipment_instance_ids == (800000000001,)
    assert generic_instance_notify.artifact_instance_ids == (700000000001,)

    negative_item = (
        encode_int_field(1, 302)
        + encode_int_field(2, -1)
        + encode_int_field(3, 9)
    )
    cost_notify = decode_rune_item_change_notify(
        encode_int_field(1, ORDER_RUNE_PULL_SOURCE)
        + encode_bytes_field(2, negative_item)
        + encode_bytes_field(
            20,
            encode_int_field(1, 1)
            + encode_int_field(2, 302)
            + encode_int_field(3, 1),
        )
    )
    assert cost_notify.items == (ItemChange(302, -1, 9),)
    assert cost_notify.costs == (RewardProp(1, 302, 1),)

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
            encrypted(ORDER_RUNE_PULL_MESSAGE_ID, response_payload),
            encrypted(SYNC_ORDER_RUNE_INFO_MESSAGE_ID, sync_payload),
            encrypted(STORAGE_ITEM_CHANGE_MESSAGE_ID, notify_payload),
        ]
    )
    endpoint = GameEndpoint("ws://test.invalid", "game-token", "1", "test")
    result = AncientLawCourtClient(
        endpoint,
        1.0,
        socket_factory=lambda _url, _timeout: fake_socket,
    ).engrave_daily_free()
    assert result.state is not None
    assert len(result.attempts) == 1
    assert result.attempts[0].safety_verified
    assert not result.attempts[0].warnings
    assert fake_socket.closed
    assert decode_message_header(fake_socket.binary_frames[0]).message_id == LOGIN_MESSAGE_ID
    assert len(fake_socket.text_frames) == 1
    request = decode_message_header(
        pack1_decode(fake_socket.text_frames[0], session_password)
    )
    assert request == MessageHeader(
        ORDER_RUNE_PULL_MESSAGE_ID,
        0,
        encode_free_rune_engraving_payload(),
    )

    catalogs = NameCatalogs({302: "古文物"}, {9001: "守序律文"})
    rewards = resolve_attempt_rewards(result.attempts[0], catalogs)
    assert len(rewards) == 1
    assert rewards[0].name == "守序律文"
    assert rewards[0].instance_id == rune_instance_id
    assert rewards[0].rarity == 4
    highlights = collect_highlight_runes(result, catalogs)
    assert len(highlights) == 1
    assert highlights[0].name == "守序律文"
    assert "黄色" in format_highlight_rune_summary(highlights)

    with tempfile.TemporaryDirectory() as temporary_directory:
        result_log = Path(temporary_directory) / "ancient-law-court.jsonl"
        append_daily_result_log(
            result_log,
            endpoint,
            result,
            catalogs,
            timestamp="2026-07-19T22:00:00+08:00",
        )
        log_text = result_log.read_text(encoding="utf-8")
        log_record = json.loads(log_text)
        assert log_record["timestamp"] == "2026-07-19T22:00:00+08:00"
        assert log_record["details"]["free_checked"] == 1
        assert log_record["details"]["request_count"] == 1
        assert log_record["details"]["attempts"][0]["free_after"] == 0
        assert len(log_record["details"]["attempts"][0]["rewards"]) == 1
        assert log_record["details"]["attempts"][0]["rewards"][0]["name"] == "守序律文"
        assert log_record["details"]["highlight_runes"][0]["color_label"] == "黄色"
        assert "token" not in log_text.lower()

    no_free_storage = (
        encode_int_field(1, 200)
        + encode_int_field(2, 199)
        + encode_bytes_field(4, encoded_pull_info(0))
    )
    no_free_game_data = encode_bytes_field(
        8, encode_bytes_field(3, no_free_storage)
    )
    no_free_socket = TestSocket(
        [
            (0x2, encode_message_header(PACK_PASSWORD_MESSAGE_ID, password_payload)),
            encrypted(GAME_DATA_MESSAGE_ID, no_free_game_data),
            encrypted(LOGIN_REUNIQUE_MESSAGE_ID),
        ]
    )
    no_free_result = AncientLawCourtClient(
        endpoint,
        1.0,
        socket_factory=lambda _url, _timeout: no_free_socket,
    ).engrave_daily_free()
    assert no_free_result.attempts == ()
    assert no_free_socket.text_frames == []

    missing_pull_info_storage = (
        encode_int_field(1, 200) + encode_int_field(2, 199)
    )
    missing_pull_info_game_data = encode_bytes_field(
        8, encode_bytes_field(3, missing_pull_info_storage)
    )
    status_sync_socket = TestSocket(
        [
            (0x2, encode_message_header(PACK_PASSWORD_MESSAGE_ID, password_payload)),
            encrypted(GAME_DATA_MESSAGE_ID, missing_pull_info_game_data),
            encrypted(LOGIN_REUNIQUE_MESSAGE_ID),
            encrypted(
                SYNC_ORDER_RUNE_PULL_MESSAGE_ID,
                encode_bytes_field(1, encoded_pull_info(0)),
            ),
        ]
    )
    status_sync_result = AncientLawCourtClient(
        endpoint,
        1.0,
        socket_factory=lambda _url, _timeout: status_sync_socket,
    ).engrave_daily_free()
    assert status_sync_result.state is not None
    assert status_sync_result.state.pull_info is not None
    assert status_sync_result.state.pull_info.free_pull_num == 0
    assert status_sync_result.attempts == ()
    assert len(status_sync_socket.text_frames) == 1
    status_request = decode_message_header(
        pack1_decode(status_sync_socket.text_frames[0], session_password)
    )
    assert status_request == MessageHeader(
        SYNC_ORDER_RUNE_PULL_MESSAGE_ID,
        0,
        b"",
    )

    send_guard_socket = TestSocket([])
    send_guard_client = AncientLawCourtClient(
        endpoint,
        1.0,
        socket_factory=lambda _url, _timeout: send_guard_socket,
    )
    send_guard_client.socket = send_guard_socket
    send_guard_client.password = session_password
    try:
        send_guard_client._engrave_once(1, 0)
    except HarvestError:
        pass
    else:
        raise AssertionError("零免费次数未触发发包保护")
    assert send_guard_socket.text_frames == []

    two_free_storage = (
        encode_int_field(1, 200)
        + encode_int_field(2, 199)
        + encode_bytes_field(4, encoded_pull_info(2))
    )
    two_free_game_data = encode_bytes_field(
        8, encode_bytes_field(3, two_free_storage)
    )
    unchanged_response = (
        encode_bytes_field(3, encode_varint(rune_instance_id))
        + encode_bytes_field(4, encoded_pull_info(2, daily=5))
    )
    unchanged_socket = TestSocket(
        [
            (0x2, encode_message_header(PACK_PASSWORD_MESSAGE_ID, password_payload)),
            encrypted(GAME_DATA_MESSAGE_ID, two_free_game_data),
            encrypted(LOGIN_REUNIQUE_MESSAGE_ID),
            encrypted(ORDER_RUNE_PULL_MESSAGE_ID, unchanged_response),
            encrypted(SYNC_ORDER_RUNE_INFO_MESSAGE_ID, sync_payload),
            encrypted(STORAGE_ITEM_CHANGE_MESSAGE_ID, notify_payload),
        ]
    )
    unchanged_result = AncientLawCourtClient(
        endpoint,
        1.0,
        socket_factory=lambda _url, _timeout: unchanged_socket,
    ).engrave_daily_free()
    assert len(unchanged_result.attempts) == 1
    assert not unchanged_result.attempts[0].safety_verified
    assert len(unchanged_socket.text_frames) == 1
    assert any(
        "免费次数变化异常" in warning
        for warning in unchanged_result.attempts[0].warnings
    )


def build_parser() -> argparse.ArgumentParser:
    parser = build_base_parser()
    parser.prog = "engrave_ancient_law_court_daily_free.py"
    parser.description = __doc__
    parser.add_argument(
        "--result-log",
        type=Path,
        default=DEFAULT_RESULT_LOG,
        help=(
            "铭刻结果 JSONL 日志；默认写入项目目录的 "
            "ancient_law_court_daily_free.jsonl。"
        ),
    )
    parser.add_argument(
        "--item-map",
        type=Path,
        default=DEFAULT_ITEM_MAP,
        help="物品名称表；默认使用项目目录的 item_id_map.json。",
    )
    parser.add_argument(
        "--rune-table",
        type=Path,
        default=DEFAULT_RUNE_TABLE,
        help="可选的 orderrune.json，用于把律文基础 ID 转为名称。",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.self_test:
        run_self_tests()
        print("古律院每日免费铭刻本地协议自检通过")
        return 0

    try:
        catalogs = load_name_catalogs(args.item_map, args.rune_table)
        tokens = load_tokens(args.token_file)
        for attempt_number in range(2):
            endpoint = resolve_game_endpoint(tokens, args)
            try:
                result = AncientLawCourtClient(
                    endpoint, args.timeout
                ).engrave_daily_free()
                break
            except GameSessionKickout as exc:
                if exc.ret != 2 or attempt_number > 0:
                    raise
                print(
                    "[登录] 收到 Kickout ret=2，"
                    f"{LOGIN_KICKOUT_RETRY_DELAY:g} 秒后重新获取游戏服会话"
                    "并重试一次。"
                )
                time.sleep(LOGIN_KICKOUT_RETRY_DELAY)
        else:
            raise AssertionError("登录重试循环未返回")
    except HarvestError as exc:
        print(f"古律院每日免费铭刻失败：{exc}", file=sys.stderr)
        return 1

    print_daily_result(endpoint, result, catalogs)
    try:
        append_daily_result_log(args.result_log, endpoint, result, catalogs)
    except HarvestError as exc:
        print(f"古律院铭刻结果记录失败：{exc}", file=sys.stderr)
        return 1
    if args.result_log is MANAGED_DESTINATION:
        print("结果日志：logs/ancient_law_court_daily_free_engraving/<日期>.jsonl")
    else:
        print(f"结果日志：{args.result_log.expanduser().resolve()}")

    failures = [
        attempt
        for attempt in result.attempts
        if attempt.response.result != RESULT_SUCCESS or not attempt.safety_verified
    ]
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
