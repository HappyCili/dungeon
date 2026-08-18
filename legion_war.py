#!/usr/bin/env python3
"""军团围攻、税收、招募与募兵的服务端动作客户端。

每日流程以 ``Siege_sync`` 的服务端状态为入口：优先解救被围攻城堡，战斗中从
当前候选战术里随机选择品质最高的一项；全部围攻结算后再领取税收、按军官招募
货币执行一次五连或单抽，并在材料足够时升级募兵等级。
"""

from __future__ import annotations

import json
import random
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Deque, Iterable, Mapping

from game_session import GAME_DATA_MESSAGE_ID, GameSession
from harvest_fief import (
    STORAGE_ITEM_CHANGE_MESSAGE_ID,
    GameEndpoint,
    HarvestError,
    MessageHeader,
    ProtoReader,
    decode_int32,
    decode_item_change_notify,
    encode_bytes_field,
    encode_int_field,
    encode_varint,
)
from project_paths import NATIVE_APP_ROOT


PULL_GACHA_BANNER_V2_MESSAGE_ID = 19532
LEGION_INFO_SYNC_MESSAGE_ID = 20050
LEGION_BATTLE_START_MESSAGE_ID = 20054
LEGION_BATTLE_SYNC_MESSAGE_ID = 20055
LEGION_BATTLE_CHOOSE_STRATEGY_MESSAGE_ID = 20057
LEGION_BATTLE_STRATEGY_EFFECTS_MESSAGE_ID = 20058
LEGION_BATTLE_PROFICIENCY_MESSAGE_ID = 20059
LEGION_BATTLE_TURN_FIGHT_MESSAGE_ID = 20060
LEGION_BATTLE_END_SUMMARY_MESSAGE_ID = 20061
LEGION_BATTLE_RETREAT_MESSAGE_ID = 20062
LEGION_UPGRADE_TROOPS_LEVEL_MESSAGE_ID = 20064
SIEGE_SYNC_MESSAGE_ID = 20074
SIEGE_RESCUE_TOWN_MESSAGE_ID = 20075
SIEGE_COLLECT_TAX_MESSAGE_ID = 20080

GACHA_CATEGORY_NORMAL = 2
LEGION_BATTLE_RESULT_WIN = 1
SIEGE_OCCUPIED_PROGRESS = 100
LEGION_OFFICER_BANNER_ID = 1
LEGION_OFFICER_COST_ITEM_ID = 80
LEGION_OFFICER_SINGLE_COST = 1000
LEGION_OFFICER_FIVE_COST = 5000

DEFAULT_STRATEGY_TABLE = (
    NATIVE_APP_ROOT / "decrypted-data" / "tables" / "legion_strategy_group.json"
)
DEFAULT_TROOP_TABLE = (
    NATIVE_APP_ROOT / "decrypted-data" / "tables" / "legion_recruit_troop.json"
)
DEFAULT_MACROS_TABLE = NATIVE_APP_ROOT / "decrypted-data" / "tables" / "macros.json"
LEGION_OFFICER_SLOT_MACRO = "ma_legion_officer_slot_num"


class LegionWarError(HarvestError):
    """军团战协议或服务端状态不满足当前自动流程。"""


class LegionWarCancelled(LegionWarError):
    """自动任务在军团流程边界收到停止请求。"""


@dataclass(frozen=True)
class LegionOfficer:
    officer_id: int
    level: int
    rarity: int


@dataclass(frozen=True)
class LegionInfo:
    troops_level: int
    officers: tuple[LegionOfficer, ...]


@dataclass(frozen=True)
class SiegeTown:
    town_id: int
    progress: int
    trapped_left_seconds: int
    trapped_legion_battle: int

    @property
    def trapped(self) -> bool:
        return (
            self.progress > SIEGE_OCCUPIED_PROGRESS
            and self.trapped_left_seconds == 0
        )


@dataclass(frozen=True)
class SiegeState:
    towns: tuple[SiegeTown, ...]
    today_tax_collected: bool


@dataclass(frozen=True)
class LegionBattleState:
    battle_id: int
    event_id: int
    turn: int
    result: int
    strategy_ids: tuple[int, ...]


@dataclass(frozen=True)
class LegionWarRunResult:
    siege_wins: int
    tax_collected: bool
    officer_pull_times: int
    troops_upgraded: bool

    def summary(self) -> str:
        parts = [f"围攻城堡胜利 {self.siege_wins} 场"]
        parts.append("已领取每日税收" if self.tax_collected else "每日税收已领取")
        if self.officer_pull_times:
            parts.append(f"军官招募 {self.officer_pull_times} 次")
        else:
            parts.append("军官招募货币不足 1000")
        parts.append("已升级募兵" if self.troops_upgraded else "募兵材料不足或已达当前上限")
        return "；".join(parts)


def _first_bytes_field(data: bytes, field_number: int) -> bytes | None:
    for number, wire_type, value in ProtoReader(data).fields():
        if number == field_number and wire_type == 2:
            return bytes(value)
    return None


def _decode_packed_int32(data: bytes) -> tuple[int, ...]:
    reader = ProtoReader(data)
    values: list[int] = []
    while reader.position < len(data):
        values.append(decode_int32(reader.read_varint()))
    return tuple(values)


def _decode_officer(data: bytes) -> LegionOfficer:
    values = {"officer_id": 0, "level": 0, "rarity": 0}
    for field_number, wire_type, value in ProtoReader(data).fields():
        if wire_type != 0:
            continue
        if field_number == 1:
            values["officer_id"] = decode_int32(int(value))
        elif field_number == 2:
            values["level"] = decode_int32(int(value))
        elif field_number == 3:
            values["rarity"] = decode_int32(int(value))
    return LegionOfficer(**values)


def decode_legion_info(data: bytes) -> LegionInfo:
    troops_level = 0
    officers: list[LegionOfficer] = []
    for field_number, wire_type, value in ProtoReader(data).fields():
        if field_number == 1 and wire_type == 0:
            troops_level = decode_int32(int(value))
        elif field_number == 3 and wire_type == 2:
            officers.append(_decode_officer(bytes(value)))
    return LegionInfo(troops_level=troops_level, officers=tuple(officers))


def decode_game_data_legion_info(data: bytes) -> LegionInfo | None:
    legion_info = _first_bytes_field(data, 28)
    return decode_legion_info(legion_info) if legion_info is not None else None


def decode_game_data_item_totals(data: bytes) -> dict[int, int]:
    """Decode ``Game_data.storage.items.list`` item totals for local checks."""

    storage = _first_bytes_field(data, 8)
    if storage is None:
        return {}
    items = _first_bytes_field(storage, 1)
    if items is None:
        return {}

    totals: dict[int, int] = {}
    for field_number, wire_type, value in ProtoReader(items).fields():
        if field_number != 3 or wire_type != 2:
            continue
        item_id = 0
        total = 0
        for item_field, item_wire, item_value in ProtoReader(bytes(value)).fields():
            if item_field == 1 and item_wire == 0:
                item_id = decode_int32(int(item_value))
            elif item_field == 2 and item_wire == 0:
                total = decode_int32(int(item_value))
        if item_id > 0:
            totals[item_id] = total
    return totals


def _decode_siege_town(data: bytes) -> SiegeTown:
    values = {
        "town_id": 0,
        "progress": 0,
        "trapped_left_seconds": 0,
        "trapped_legion_battle": 0,
    }
    for field_number, wire_type, value in ProtoReader(data).fields():
        if wire_type != 0:
            continue
        if field_number == 1:
            values["town_id"] = decode_int32(int(value))
        elif field_number == 2:
            values["progress"] = decode_int32(int(value))
        elif field_number == 25:
            values["trapped_left_seconds"] = decode_int32(int(value))
        elif field_number == 21:
            values["trapped_legion_battle"] = decode_int32(int(value))
    return SiegeTown(**values)


def decode_siege_state(data: bytes) -> SiegeState:
    towns: list[SiegeTown] = []
    today_tax_collected = False
    for field_number, wire_type, value in ProtoReader(data).fields():
        if field_number == 1 and wire_type == 2:
            towns.append(_decode_siege_town(bytes(value)))
        elif field_number == 2 and wire_type == 0:
            today_tax_collected = bool(value)
    return SiegeState(tuple(towns), today_tax_collected)


def _decode_strategy_options(data: bytes) -> tuple[int, ...]:
    strategy_ids: list[int] = []
    for field_number, wire_type, value in ProtoReader(data).fields():
        if field_number != 1:
            continue
        if wire_type == 0:
            strategy_ids.append(decode_int32(int(value)))
        elif wire_type == 2:
            strategy_ids.extend(_decode_packed_int32(bytes(value)))
    return tuple(strategy_ids)


def decode_legion_battle_state(data: bytes) -> LegionBattleState:
    values = {"battle_id": 0, "event_id": 0, "turn": 0, "result": 0}
    strategy_ids: tuple[int, ...] = ()
    for field_number, wire_type, value in ProtoReader(data).fields():
        if field_number == 1 and wire_type == 0:
            values["battle_id"] = decode_int32(int(value))
        elif field_number == 4 and wire_type == 0:
            values["result"] = decode_int32(int(value))
        elif field_number == 12 and wire_type == 0:
            values["turn"] = decode_int32(int(value))
        elif field_number == 14 and wire_type == 2:
            strategy_ids = _decode_strategy_options(bytes(value))
        elif field_number == 20 and wire_type == 0:
            values["event_id"] = decode_int32(int(value))
    return LegionBattleState(strategy_ids=strategy_ids, **values)


def _decode_response_result(data: bytes, field_number: int) -> int:
    for number, wire_type, value in ProtoReader(data).fields():
        if number == field_number and wire_type == 0:
            return decode_int32(int(value))
    return 0


def _decode_response_message(data: bytes, field_number: int = 11) -> str:
    """Return an optional server diagnostic message from a response packet."""

    for number, wire_type, value in ProtoReader(data).fields():
        if number == field_number and wire_type == 2:
            try:
                return bytes(value).decode("utf-8")
            except UnicodeDecodeError:
                return ""
    return ""


def _decode_rescue_response(data: bytes) -> tuple[int, int]:
    town_id = 0
    result = 0
    for field_number, wire_type, value in ProtoReader(data).fields():
        if field_number == 1 and wire_type == 0:
            town_id = decode_int32(int(value))
        elif field_number == 10 and wire_type == 0:
            result = decode_int32(int(value))
    return town_id, result


def encode_siege_rescue_payload(town_id: int) -> bytes:
    if town_id <= 0:
        raise LegionWarError("围攻城堡 ID 必须为正数")
    return encode_int_field(1, town_id)


def encode_legion_battle_start_payload(
    battle_id: int,
    event_id: int,
    officer_ids: Iterable[int],
) -> bytes:
    if battle_id <= 0:
        raise LegionWarError("军团战 battleId 必须为正数")
    packet = encode_int_field(1, battle_id)
    if event_id:
        packet += encode_int_field(2, event_id)
    packed_officers = b"".join(
        encode_varint(officer_id) for officer_id in officer_ids
    )
    if packed_officers:
        packet += encode_bytes_field(3, packed_officers)
    return packet


def encode_legion_strategy_payload(strategy_id: int) -> bytes:
    if strategy_id <= 0:
        raise LegionWarError("战术 ID 必须为正数")
    return encode_int_field(1, strategy_id)


def encode_officer_pull_payload(pull_times: int) -> bytes:
    if pull_times not in {1, 5}:
        raise LegionWarError("军官招募次数只能为 1 或 5")
    return (
        encode_int_field(1, LEGION_OFFICER_BANNER_ID)
        + encode_int_field(2, pull_times)
        + encode_int_field(3, GACHA_CATEGORY_NORMAL)
        + encode_int_field(6, LEGION_OFFICER_COST_ITEM_ID)
    )


def load_strategy_rarities(path: Path = DEFAULT_STRATEGY_TABLE) -> dict[int, int]:
    """Load strategy quality from the checked-in native client table."""

    try:
        rows = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise LegionWarError(f"缺少军团战术配置：{path}") from exc
    except json.JSONDecodeError as exc:
        raise LegionWarError(f"军团战术配置不是有效 JSON：{path}") from exc
    return {int(row["id"]): int(row["rarity"]) for row in rows}


def load_troop_upgrade_costs(path: Path = DEFAULT_TROOP_TABLE) -> dict[int, tuple[int, int]]:
    """Load the material requirement to advance from each troop level."""

    try:
        rows = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise LegionWarError(f"缺少募兵配置：{path}") from exc
    except json.JSONDecodeError as exc:
        raise LegionWarError(f"募兵配置不是有效 JSON：{path}") from exc

    costs: dict[int, tuple[int, int]] = {}
    for row in rows:
        raw_cost = str(row.get("cost", "")).strip()
        # The terminal level has an empty cost and therefore no further upgrade.
        if not raw_cost:
            continue
        try:
            item_id, amount = raw_cost.split(",", maxsplit=1)
            costs[int(row["lv"])] = (int(item_id), int(amount))
        except (KeyError, ValueError) as exc:
            raise LegionWarError(f"募兵配置成本无效：{row!r}") from exc
    return costs


def load_legion_officer_slot_count(path: Path = DEFAULT_MACROS_TABLE) -> int:
    """Load the current battle officer limit from the native macro table."""

    try:
        rows = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise LegionWarError(f"缺少军团宏配置：{path}") from exc
    except json.JSONDecodeError as exc:
        raise LegionWarError(f"军团宏配置不是有效 JSON：{path}") from exc

    if not isinstance(rows, list):
        raise LegionWarError(f"军团宏配置格式无效：{path}")
    for row in rows:
        if not isinstance(row, dict) or row.get("name") != LEGION_OFFICER_SLOT_MACRO:
            continue
        try:
            slot_count = int(row["param"])
        except (KeyError, TypeError, ValueError) as exc:
            raise LegionWarError(f"军团军官槽位配置无效：{row!r}") from exc
        if slot_count <= 0:
            raise LegionWarError(f"军团军官槽位必须为正数：{slot_count}")
        return slot_count
    raise LegionWarError(f"军团宏配置缺少 {LEGION_OFFICER_SLOT_MACRO}")


def choose_highest_rarity_strategy(
    strategy_ids: Iterable[int],
    strategy_rarities: Mapping[int, int],
    *,
    rng: random.Random | None = None,
) -> int:
    """Return a random offered strategy among the current highest rarity."""

    offered = tuple(strategy_ids)
    if not offered:
        raise LegionWarError("服务端未下发可选战术")
    missing = [
        strategy_id for strategy_id in offered if strategy_id not in strategy_rarities
    ]
    if missing:
        raise LegionWarError(f"战术配置缺少候选 ID：{missing}")
    highest_rarity = max(strategy_rarities[strategy_id] for strategy_id in offered)
    best = [
        strategy_id
        for strategy_id in offered
        if strategy_rarities[strategy_id] == highest_rarity
    ]
    return (rng or random.SystemRandom()).choice(best)


def select_battle_officers(
    officers: Iterable[LegionOfficer],
    *,
    max_officers: int | None = None,
) -> tuple[int, ...]:
    """Match native roster ordering and retain only available battle slots."""

    slot_count = (
        load_legion_officer_slot_count()
        if max_officers is None
        else max_officers
    )
    if slot_count <= 0:
        raise LegionWarError(f"军团军官槽位必须为正数：{slot_count}")

    ordered = sorted(
        officers,
        key=lambda officer: (-officer.rarity, -officer.level, officer.officer_id),
    )
    return tuple(
        officer.officer_id for officer in ordered if officer.officer_id > 0
    )[:slot_count]


class LegionWarClient:
    """One daily run bound to a shared :class:`GameSession` when available."""

    _DEFERRED_WORKFLOW_MESSAGES = frozenset(
        {
            PULL_GACHA_BANNER_V2_MESSAGE_ID,
            LEGION_BATTLE_START_MESSAGE_ID,
            LEGION_BATTLE_SYNC_MESSAGE_ID,
            LEGION_BATTLE_CHOOSE_STRATEGY_MESSAGE_ID,
            LEGION_BATTLE_STRATEGY_EFFECTS_MESSAGE_ID,
            LEGION_BATTLE_PROFICIENCY_MESSAGE_ID,
            LEGION_BATTLE_TURN_FIGHT_MESSAGE_ID,
            LEGION_BATTLE_END_SUMMARY_MESSAGE_ID,
            LEGION_BATTLE_RETREAT_MESSAGE_ID,
            LEGION_UPGRADE_TROOPS_LEVEL_MESSAGE_ID,
            SIEGE_SYNC_MESSAGE_ID,
            SIEGE_RESCUE_TOWN_MESSAGE_ID,
            SIEGE_COLLECT_TAX_MESSAGE_ID,
        }
    )

    def __init__(
        self,
        endpoint: GameEndpoint,
        timeout: float,
        *,
        session: object | None = None,
        strategy_rarities: Mapping[int, int] | None = None,
        troop_upgrade_costs: Mapping[int, tuple[int, int]] | None = None,
        officer_slot_count: int | None = None,
        rng: random.Random | None = None,
        stop_requested: Callable[[], bool] | None = None,
    ) -> None:
        self.endpoint = endpoint
        self.timeout = timeout
        self._owns_session = session is None
        self._session = session or GameSession(timeout=timeout)
        self._strategy_rarities = dict(
            strategy_rarities
            if strategy_rarities is not None
            else load_strategy_rarities()
        )
        self._troop_upgrade_costs = dict(
            troop_upgrade_costs
            if troop_upgrade_costs is not None
            else load_troop_upgrade_costs()
        )
        self._officer_slot_count = (
            load_legion_officer_slot_count()
            if officer_slot_count is None
            else officer_slot_count
        )
        if self._officer_slot_count <= 0:
            raise LegionWarError(
                f"军团军官槽位必须为正数：{self._officer_slot_count}"
            )
        self._rng = rng or random.SystemRandom()
        self._stop_requested = stop_requested or (lambda: False)
        self._item_totals: dict[int, int] = {}
        self._legion_info: LegionInfo | None = None
        self._deferred: Deque[MessageHeader] = deque()
        self._preserved: list[MessageHeader] = []
        self._closed = False

    def _check_stop(self) -> None:
        if self._stop_requested():
            raise LegionWarCancelled("军团日常已停止")

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        retained = [*self._deferred, *self._preserved]
        push_headers = getattr(self._session, "push_headers", None)
        if retained and callable(push_headers):
            push_headers(retained)
        if self._owns_session:
            close = getattr(self._session, "close", None)
            if callable(close):
                close()

    def _ensure_session(self) -> None:
        self._check_stop()
        ensure_recovered = getattr(self._session, "ensure_recovered", None)
        ensure_ready = getattr(self._session, "ensure_ready", None)
        try:
            if callable(ensure_recovered):
                ensure_recovered(self.endpoint)
            elif callable(ensure_ready):
                ensure_ready(self.endpoint)
        except Exception as exc:
            raise LegionWarError(f"军团战会话未就绪：{exc}") from exc

        game_data = getattr(self._session, "game_data", None)
        if game_data:
            self._refresh_game_data(bytes(game_data))
        if self._legion_info is None:
            raise LegionWarError("Game_data 缺少军团信息")

    def _refresh_game_data(self, data: bytes) -> None:
        self._item_totals.update(decode_game_data_item_totals(data))
        legion_info = decode_game_data_legion_info(data)
        if legion_info is not None:
            self._legion_info = legion_info

    def _consume_background(self, header: MessageHeader) -> bool:
        if header.message_id == GAME_DATA_MESSAGE_ID:
            self._refresh_game_data(header.data)
            return True
        if header.message_id == STORAGE_ITEM_CHANGE_MESSAGE_ID:
            notification = decode_item_change_notify(header.data)
            for item in notification.items:
                self._item_totals[item.item_id] = item.total
            return True
        if header.message_id == LEGION_INFO_SYNC_MESSAGE_ID:
            self._legion_info = decode_legion_info(header.data)
            return True
        return False

    def _take_deferred(self, expected: set[int]) -> MessageHeader | None:
        for index, header in enumerate(self._deferred):
            if header.message_id in expected:
                del self._deferred[index]
                return header
        return None

    def _wait_for(self, expected: set[int], context: str) -> MessageHeader:
        self._check_stop()
        header = self._take_deferred(expected)
        if header is not None:
            return header

        deadline = time.monotonic() + self.timeout
        while True:
            self._check_stop()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise LegionWarError(f"等待{context}超时")
            try:
                header = self._session.receive_header(remaining)
            except Exception as exc:
                raise LegionWarError(f"等待{context}失败：{exc}") from exc
            if self._consume_background(header):
                continue
            if header.message_id in expected:
                return header
            if header.message_id in self._DEFERRED_WORKFLOW_MESSAGES:
                self._deferred.append(header)
            else:
                self._preserved.append(header)

    def _send(self, message_id: int, data: bytes = b"") -> None:
        self._check_stop()
        try:
            self._session.send_message(message_id, data)
        except Exception as exc:
            raise LegionWarError(f"发送军团消息 {message_id} 失败：{exc}") from exc

    def _sync_siege(self) -> SiegeState:
        self._send(SIEGE_SYNC_MESSAGE_ID)
        response = self._wait_for({SIEGE_SYNC_MESSAGE_ID}, "围攻城堡状态")
        return decode_siege_state(response.data)

    def _rescue_and_fight(self, town: SiegeTown) -> bool:
        for attempt in range(2):
            self._send(
                SIEGE_RESCUE_TOWN_MESSAGE_ID,
                encode_siege_rescue_payload(town.town_id),
            )
            rescue = self._wait_for(
                {SIEGE_RESCUE_TOWN_MESSAGE_ID}, "城堡出击响应"
            )
            response_town_id, result = _decode_rescue_response(rescue.data)
            if result == 5:
                resumed = self._resume_deferred_battle()
                if resumed is not None:
                    return resumed
                if attempt == 1:
                    detail = _decode_response_message(rescue.data)
                    suffix = f"，{detail}" if detail else ""
                    raise LegionWarError(
                        f"城堡 {town.town_id} 出击被服务端拒绝（ret=5{suffix}）"
                    )
                self._retreat_stale_battle()
                resumed = self._resume_deferred_battle()
                if resumed is not None:
                    return resumed
                continue
            if result != 0:
                detail = _decode_response_message(rescue.data)
                suffix = f"，{detail}" if detail else ""
                raise LegionWarError(
                    f"城堡 {town.town_id} 出击被服务端拒绝（ret={result}{suffix}）"
                )
            if response_town_id not in (0, town.town_id):
                raise LegionWarError(
                    "城堡出击响应 ID 不匹配："
                    f"请求 {town.town_id}，响应 {response_town_id}"
                )

            battle_header = self._wait_for(
                {LEGION_BATTLE_SYNC_MESSAGE_ID},
                f"城堡 {town.town_id} 军团战状态",
            )
            return self._continue_battle(
                decode_legion_battle_state(battle_header.data)
            )
        raise LegionWarError(f"城堡 {town.town_id} 出击恢复次数已用尽")

    def _retreat_stale_battle(self) -> None:
        self._send(LEGION_BATTLE_RETREAT_MESSAGE_ID)
        response = self._wait_for(
            {LEGION_BATTLE_RETREAT_MESSAGE_ID}, "遗留军团战撤退响应"
        )
        result = _decode_response_result(response.data, 1)
        if result != 0:
            detail = _decode_response_message(response.data, field_number=2)
            suffix = f"，{detail}" if detail else ""
            raise LegionWarError(
                f"遗留军团战撤退被服务端拒绝（ret={result}{suffix}）"
            )

    def _continue_battle(self, battle: LegionBattleState) -> bool:
        if battle.battle_id <= 0:
            raise LegionWarError("服务端下发了无效的军团战 battleId")
        if battle.turn <= 0:
            self._start_battle(battle)
        return self._fight_battle(battle)

    def _resume_deferred_battle(self) -> bool | None:
        """Resume a battle push received before the current siege response."""

        header = self._take_deferred(
            {LEGION_BATTLE_SYNC_MESSAGE_ID, LEGION_BATTLE_END_SUMMARY_MESSAGE_ID}
        )
        if header is None:
            return None
        if header.message_id == LEGION_BATTLE_END_SUMMARY_MESSAGE_ID:
            return _decode_response_result(header.data, 1) == LEGION_BATTLE_RESULT_WIN
        return self._continue_battle(decode_legion_battle_state(header.data))

    def _start_battle(self, battle: LegionBattleState) -> None:
        assert self._legion_info is not None
        officer_ids = select_battle_officers(
            self._legion_info.officers,
            max_officers=self._officer_slot_count,
        )
        if not officer_ids:
            raise LegionWarError("没有可用于军团出击的军官")
        self._send(
            LEGION_BATTLE_START_MESSAGE_ID,
            encode_legion_battle_start_payload(
                battle.battle_id, battle.event_id, officer_ids
            ),
        )
        response = self._wait_for({LEGION_BATTLE_START_MESSAGE_ID}, "军团出击响应")
        result = _decode_response_result(response.data, 10)
        if result != 0:
            detail = _decode_response_message(response.data)
            suffix = f"，{detail}" if detail else ""
            raise LegionWarError(
                f"军团出击被服务端拒绝（ret={result}{suffix}）"
            )

    def _fight_battle(self, initial: LegionBattleState) -> bool:
        battle = initial
        if battle.turn <= 0:
            header = self._wait_for_battle_state("军团战开始")
            if header.message_id == LEGION_BATTLE_END_SUMMARY_MESSAGE_ID:
                return _decode_response_result(header.data, 1) == LEGION_BATTLE_RESULT_WIN
            battle = decode_legion_battle_state(header.data)

        while True:
            if battle.turn > 0:
                if not battle.strategy_ids:
                    header = self._wait_for_battle_state("军团战术候选")
                    if header.message_id == LEGION_BATTLE_END_SUMMARY_MESSAGE_ID:
                        return _decode_response_result(header.data, 1) == LEGION_BATTLE_RESULT_WIN
                    battle = decode_legion_battle_state(header.data)
                    continue
                strategy_id = choose_highest_rarity_strategy(
                    battle.strategy_ids, self._strategy_rarities, rng=self._rng
                )
                self._send(
                    LEGION_BATTLE_CHOOSE_STRATEGY_MESSAGE_ID,
                    encode_legion_strategy_payload(strategy_id),
                )
                response = self._wait_for(
                    {LEGION_BATTLE_CHOOSE_STRATEGY_MESSAGE_ID}, "军团战术选择响应"
                )
                result = _decode_response_result(response.data, 2)
                if result != 0:
                    raise LegionWarError(
                        f"战术 {strategy_id} 被服务端拒绝（ret={result}）"
                    )

            header = self._wait_for_battle_state("军团战回合结算")
            if header.message_id == LEGION_BATTLE_END_SUMMARY_MESSAGE_ID:
                return _decode_response_result(header.data, 1) == LEGION_BATTLE_RESULT_WIN
            battle = decode_legion_battle_state(header.data)

    def _wait_for_battle_state(self, context: str) -> MessageHeader:
        expected = {
            LEGION_BATTLE_SYNC_MESSAGE_ID,
            LEGION_BATTLE_END_SUMMARY_MESSAGE_ID,
            LEGION_BATTLE_STRATEGY_EFFECTS_MESSAGE_ID,
            LEGION_BATTLE_PROFICIENCY_MESSAGE_ID,
            LEGION_BATTLE_TURN_FIGHT_MESSAGE_ID,
        }
        while True:
            header = self._wait_for(expected, context)
            if header.message_id in {
                LEGION_BATTLE_STRATEGY_EFFECTS_MESSAGE_ID,
                LEGION_BATTLE_PROFICIENCY_MESSAGE_ID,
                LEGION_BATTLE_TURN_FIGHT_MESSAGE_ID,
            }:
                continue
            return header

    def _collect_tax(self, state: SiegeState) -> bool:
        if state.today_tax_collected:
            return False
        self._send(SIEGE_COLLECT_TAX_MESSAGE_ID)
        response = self._wait_for({SIEGE_COLLECT_TAX_MESSAGE_ID}, "每日税收响应")
        result = _decode_response_result(response.data, 10)
        if result != 0:
            raise LegionWarError(f"每日税收被服务端拒绝（ret={result}）")
        return True

    def _recruit_officers(self) -> int:
        balance = self._item_totals.get(LEGION_OFFICER_COST_ITEM_ID, 0)
        pull_times = (
            5
            if balance >= LEGION_OFFICER_FIVE_COST
            else 1
            if balance >= LEGION_OFFICER_SINGLE_COST
            else 0
        )
        if pull_times == 0:
            return 0
        self._send(
            PULL_GACHA_BANNER_V2_MESSAGE_ID,
            encode_officer_pull_payload(pull_times),
        )
        response = self._wait_for(
            {PULL_GACHA_BANNER_V2_MESSAGE_ID}, "军官招募响应"
        )
        result = _decode_response_result(response.data, 20)
        if result != 0:
            raise LegionWarError(f"军官招募被服务端拒绝（ret={result}）")
        return pull_times

    def _upgrade_troops(self) -> bool:
        assert self._legion_info is not None
        cost = self._troop_upgrade_costs.get(self._legion_info.troops_level)
        if cost is None:
            return False
        item_id, amount = cost
        if self._item_totals.get(item_id, 0) < amount:
            return False
        self._send(LEGION_UPGRADE_TROOPS_LEVEL_MESSAGE_ID)
        response = self._wait_for(
            {LEGION_UPGRADE_TROOPS_LEVEL_MESSAGE_ID}, "募兵升级响应"
        )
        result = _decode_response_result(response.data, 10)
        if result != 0:
            raise LegionWarError(f"募兵升级被服务端拒绝（ret={result}）")
        return True

    def run_daily(self) -> LegionWarRunResult:
        try:
            self._ensure_session()
            siege_wins = 0
            while True:
                self._check_stop()
                siege_state = self._sync_siege()
                resumed = self._resume_deferred_battle()
                if resumed is not None:
                    if not resumed:
                        raise LegionWarError(
                            "待恢复的军团战未获胜，已停止后续税收与招募"
                        )
                    siege_wins += 1
                    continue
                trapped_town = next(
                    (town for town in siege_state.towns if town.trapped), None
                )
                if trapped_town is None:
                    break
                if not self._rescue_and_fight(trapped_town):
                    raise LegionWarError(
                        f"城堡 {trapped_town.town_id} 军团战未获胜，已停止后续税收与招募"
                    )
                siege_wins += 1

            self._check_stop()
            tax_collected = self._collect_tax(siege_state)
            self._check_stop()
            officer_pull_times = self._recruit_officers()
            self._check_stop()
            troops_upgraded = self._upgrade_troops()
            return LegionWarRunResult(
                siege_wins=siege_wins,
                tax_collected=tax_collected,
                officer_pull_times=officer_pull_times,
                troops_upgraded=troops_upgraded,
            )
        finally:
            self.close()
