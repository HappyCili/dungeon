"""双生螺旋挑战客户端。

秽肉之塔使用 Rogue 协议族而非地下城扫荡协议。这个模块保留首场
手动触发式的状态转换：首场胜利结算后才开始自动连战，任一失败结算
或取消请求都会停止后续挑战。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field, replace
from typing import Callable, Mapping

from dragon_arena import (
    BATTLE_C2S_START_MESSAGE_ID,
    BATTLE_INFO_MESSAGE_ID,
    BATTLE_S2C_END_MESSAGE_ID,
    BATTLE_S2C_FRAME_BROADCAST_MESSAGE_ID,
    BATTLE_S2C_FRAME_HASH_VERIFY_MESSAGE_ID,
    BATTLE_S2C_START_MESSAGE_ID,
    GAME_DATA_MESSAGE_ID,
    DragonArenaClient,
    GameMessageTimeout,
    GameSessionClosed,
    decode_battle_info,
    decode_battle_start_response,
)
from harvest_fief import (
    GameEndpoint,
    HarvestError,
    MessageHeader,
    ProtoReader,
    decode_int32,
    encode_int_field,
)


ROGUE_TRIGGER_NODE_MESSAGE_ID = 22034
ROGUE_TRIGGER_NODE_NOTIFY_MESSAGE_ID = 22036
ROGUE_HERO_STATUS_NOTIFY_MESSAGE_ID = 22040
ROGUE_SETTLE_NOTIFY_MESSAGE_ID = 22042

BATTLE_END_RESULT_LOSE = 1
BATTLE_END_RESULT_WIN = 2
TWIN_SPIRAL_TOWER_NAME = "秽肉之塔"


@dataclass(frozen=True)
class TwinSpiralPosition:
    area_id: int = 0
    node_id: int = 0
    steps: int = 0
    level: int = 0
    battle_count: int = 0
    step_limit: int = 0


@dataclass(frozen=True)
class TwinSpiralState:
    current: TwinSpiralPosition = field(default_factory=TwinSpiralPosition)
    areas: Mapping[int, Mapping[int, int]] = field(default_factory=dict)


@dataclass(frozen=True)
class TwinSpiralStatus:
    tower_name: str
    active: bool
    current_area_id: int
    current_node_id: int
    available_node_ids: tuple[int, ...]
    node_states: Mapping[int, int]
    battle_count: int
    steps: int
    step_limit: int


@dataclass(frozen=True)
class TwinSpiralTriggerResponse:
    ret: int
    node_id: int


@dataclass(frozen=True)
class TwinSpiralBattleResult:
    node_id: int
    win: bool
    result_code: int | None
    round_number: int | None


@dataclass(frozen=True)
class TwinSpiralRoundResult:
    node_id: int
    start: TwinSpiralTriggerResponse
    battle: TwinSpiralBattleResult | None
    automatic: bool
    auto_enabled: bool


def _first_bytes_field(data: bytes, field_number: int) -> bytes | None:
    for number, wire_type, value in ProtoReader(data).fields():
        if number == field_number and wire_type == 2:
            return bytes(value)
    return None


def _decode_int_map_entry(data: bytes) -> tuple[int, int] | None:
    key: int | None = None
    value: int | None = None
    for field_number, wire_type, field_value in ProtoReader(data).fields():
        if wire_type != 0:
            continue
        if field_number == 1:
            key = decode_int32(int(field_value))
        elif field_number == 2:
            value = decode_int32(int(field_value))
    if key is None or value is None:
        return None
    return key, value


def decode_twin_spiral_position(data: bytes) -> TwinSpiralPosition:
    values = [0] * 6
    for field_number, wire_type, value in ProtoReader(data).fields():
        if wire_type == 0 and 1 <= field_number <= 6:
            values[field_number - 1] = decode_int32(int(value))
    return TwinSpiralPosition(*values)


def _decode_area_node_states(data: bytes) -> tuple[int, dict[int, int]]:
    area_id = 0
    nodes: dict[int, int] = {}
    for field_number, wire_type, value in ProtoReader(data).fields():
        if field_number == 1 and wire_type == 0:
            area_id = decode_int32(int(value))
        elif field_number == 2 and wire_type == 2:
            entry = _decode_int_map_entry(bytes(value))
            if entry is not None:
                nodes[entry[0]] = entry[1]
    return area_id, nodes


def decode_twin_spiral_state(data: bytes) -> TwinSpiralState:
    """Decode the required subset of ``Game_data.rogue`` (field 31)."""

    current = TwinSpiralPosition()
    areas: dict[int, Mapping[int, int]] = {}
    for field_number, wire_type, value in ProtoReader(data).fields():
        if field_number == 3 and wire_type == 2:
            # unlockAreas map entry: field 1 is the area ID, field 2 is Hy.
            area_key = 0
            area_value: bytes | None = None
            for entry_field, entry_wire, entry_value in ProtoReader(bytes(value)).fields():
                if entry_field == 1 and entry_wire == 0:
                    area_key = decode_int32(int(entry_value))
                elif entry_field == 2 and entry_wire == 2:
                    area_value = bytes(entry_value)
            if area_value is not None:
                decoded_area_id, nodes = _decode_area_node_states(area_value)
                areas[area_key or decoded_area_id] = nodes
        elif field_number == 11 and wire_type == 2:
            current = decode_twin_spiral_position(bytes(value))
    return TwinSpiralState(current=current, areas=areas)


def decode_game_data_twin_spiral(data: bytes) -> TwinSpiralState | None:
    rogue = _first_bytes_field(data, 31)
    return decode_twin_spiral_state(rogue) if rogue is not None else None


def decode_trigger_response(data: bytes) -> TwinSpiralTriggerResponse:
    ret = 0
    node_id = 0
    for field_number, wire_type, value in ProtoReader(data).fields():
        if wire_type != 0:
            continue
        if field_number == 1:
            ret = decode_int32(int(value))
        elif field_number == 2:
            node_id = decode_int32(int(value))
    return TwinSpiralTriggerResponse(ret=ret, node_id=node_id)


def decode_trigger_notify(data: bytes) -> tuple[int, int, int]:
    node_id = 0
    state = 0
    area_id = 0
    for field_number, wire_type, value in ProtoReader(data).fields():
        if wire_type != 0:
            continue
        if field_number == 1:
            node_id = decode_int32(int(value))
        elif field_number == 2:
            state = decode_int32(int(value))
        elif field_number == 3:
            area_id = decode_int32(int(value))
    return node_id, state, area_id


def decode_battle_end_result(data: bytes) -> tuple[bool, int | None, int | None]:
    win_flag: int | None = None
    result_code: int | None = None
    round_number: int | None = None
    for field_number, wire_type, value in ProtoReader(data).fields():
        if wire_type != 0:
            continue
        if field_number == 1:
            round_number = decode_int32(int(value))
        elif field_number == 2:
            win_flag = int(value)
        elif field_number == 10:
            result_code = decode_int32(int(value))
    if result_code == BATTLE_END_RESULT_WIN:
        win = True
    elif result_code == BATTLE_END_RESULT_LOSE:
        win = False
    else:
        win = bool(win_flag)
    return win, result_code, round_number


def decode_settle_state(data: bytes) -> tuple[bool, TwinSpiralState | None]:
    win = False
    rogue: bytes | None = None
    for field_number, wire_type, value in ProtoReader(data).fields():
        if field_number == 1 and wire_type == 0:
            win = bool(value)
        elif field_number == 3 and wire_type == 2:
            rogue = bytes(value)
    return win, decode_twin_spiral_state(rogue) if rogue is not None else None


class TwinSpiralClient(DragonArenaClient):
    """Rogue challenge client with an explicit post-first-battle auto loop."""

    def __init__(
        self,
        endpoint: GameEndpoint,
        timeout: float,
        *,
        battle_timeout: float = 180.0,
        log: Callable[[str], None] = print,
        task: str = "twin_spiral",
        **kwargs: object,
    ) -> None:
        self._twin_spiral_state: TwinSpiralState | None = None
        super().__init__(
            endpoint,
            timeout,
            battle_timeout=battle_timeout,
            log=log,
            task=task,
            **kwargs,
        )

    def _remember_header(self, header: MessageHeader) -> None:
        super()._remember_header(header)
        if header.message_id == GAME_DATA_MESSAGE_ID:
            self._twin_spiral_state = decode_game_data_twin_spiral(header.data)
        elif header.message_id == ROGUE_TRIGGER_NODE_NOTIFY_MESSAGE_ID:
            self._apply_trigger_notify(header.data)
        elif header.message_id == ROGUE_HERO_STATUS_NOTIFY_MESSAGE_ID:
            self._apply_battle_count(header.data)
        elif header.message_id == ROGUE_SETTLE_NOTIFY_MESSAGE_ID:
            _win, state = decode_settle_state(header.data)
            if state is not None:
                self._twin_spiral_state = state

    def _apply_trigger_notify(self, data: bytes) -> None:
        if self._twin_spiral_state is None:
            return
        node_id, state, area_id = decode_trigger_notify(data)
        if not node_id:
            return
        target_area_id = area_id or self._twin_spiral_state.current.area_id
        areas = {key: dict(value) for key, value in self._twin_spiral_state.areas.items()}
        nodes = areas.setdefault(target_area_id, {})
        nodes[node_id] = state
        self._twin_spiral_state = replace(self._twin_spiral_state, areas=areas)

    def _apply_battle_count(self, data: bytes) -> None:
        if self._twin_spiral_state is None:
            return
        battle_count: int | None = None
        for field_number, wire_type, value in ProtoReader(data).fields():
            if field_number == 2 and wire_type == 0:
                battle_count = decode_int32(int(value))
        if battle_count is not None:
            self._twin_spiral_state = replace(
                self._twin_spiral_state,
                current=replace(
                    self._twin_spiral_state.current,
                    battle_count=battle_count,
                ),
            )

    def get_status(self) -> TwinSpiralStatus:
        state = self._twin_spiral_state
        if state is None:
            return TwinSpiralStatus(
                tower_name=TWIN_SPIRAL_TOWER_NAME,
                active=False,
                current_area_id=0,
                current_node_id=0,
                available_node_ids=(),
                node_states={},
                battle_count=0,
                steps=0,
                step_limit=0,
            )
        current = state.current
        node_states = dict(state.areas.get(current.area_id, {}))
        return TwinSpiralStatus(
            tower_name=TWIN_SPIRAL_TOWER_NAME,
            active=current.area_id > 0,
            current_area_id=current.area_id,
            current_node_id=current.node_id,
            available_node_ids=tuple(sorted(node_states)),
            node_states=node_states,
            battle_count=current.battle_count,
            steps=current.steps,
            step_limit=current.step_limit,
        )

    def trigger_challenge(self, node_id: int) -> TwinSpiralTriggerResponse:
        self._send_message(
            ROGUE_TRIGGER_NODE_MESSAGE_ID,
            encode_int_field(1, node_id),
            encrypted=True,
        )
        for header in self._wait_for(
            {ROGUE_TRIGGER_NODE_MESSAGE_ID, BATTLE_INFO_MESSAGE_ID}, self.timeout
        ):
            if header.message_id == ROGUE_TRIGGER_NODE_MESSAGE_ID:
                return decode_trigger_response(header.data)
            if header.message_id == BATTLE_INFO_MESSAGE_ID:
                info = decode_battle_info(header.data)
                if info.ret != 0:
                    raise HarvestError(f"Battle_info 返回 ret={info.ret}")
                self._queued_headers.appendleft(header)
                return TwinSpiralTriggerResponse(ret=0, node_id=node_id)
        raise AssertionError("_wait_for 未返回双生螺旋挑战响应")

    def await_battle_result(self, node_id: int) -> TwinSpiralBattleResult:
        configured = False
        battle_start_sent = False
        deadline = time.monotonic() + self.battle_timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise HarvestError("等待双生螺旋战斗结算超时")
            try:
                header = self._next_header(min(remaining, 5.0))
            except GameMessageTimeout:
                continue
            except GameSessionClosed:
                raise

            if header.message_id == BATTLE_INFO_MESSAGE_ID:
                info = decode_battle_info(header.data)
                if info.ret != 0:
                    raise HarvestError(f"Battle_info 返回 ret={info.ret}")
                if not battle_start_sent:
                    self.start_battle(info)
                    battle_start_sent = True
                continue
            if header.message_id == ROGUE_TRIGGER_NODE_MESSAGE_ID:
                response = decode_trigger_response(header.data)
                if response.ret != 0:
                    raise HarvestError(f"双生螺旋挑战开始失败：ret={response.ret}")
                continue
            if header.message_id == BATTLE_S2C_START_MESSAGE_ID:
                start = decode_battle_start_response(header.data)
                if start.ret != 0:
                    raise HarvestError(f"Battle_S2C_start 返回 ret={start.ret}")
                if not configured:
                    self.configure_battle()
                    configured = True
                continue
            if header.message_id in {
                BATTLE_S2C_FRAME_BROADCAST_MESSAGE_ID,
                BATTLE_S2C_FRAME_HASH_VERIFY_MESSAGE_ID,
            }:
                continue
            if header.message_id == BATTLE_S2C_END_MESSAGE_ID:
                win, result_code, round_number = decode_battle_end_result(header.data)
                self._drain_post_battle_notices()
                return TwinSpiralBattleResult(
                    node_id=node_id,
                    win=win,
                    result_code=result_code,
                    round_number=round_number,
                )
            if battle_start_sent and header.message_id == BATTLE_C2S_START_MESSAGE_ID:
                continue

    def _drain_post_battle_notices(self) -> None:
        deadline = time.monotonic() + min(0.5, self.timeout)
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            try:
                header = self._next_header(remaining)
            except (GameMessageTimeout, GameSessionClosed):
                return
            if header.message_id in {
                GAME_DATA_MESSAGE_ID,
                ROGUE_TRIGGER_NODE_NOTIFY_MESSAGE_ID,
                ROGUE_HERO_STATUS_NOTIFY_MESSAGE_ID,
                ROGUE_SETTLE_NOTIFY_MESSAGE_ID,
            }:
                continue
            self._queued_headers.appendleft(header)
            return

    def run_round(self, node_id: int, *, automatic: bool) -> TwinSpiralRoundResult:
        start = self.trigger_challenge(node_id)
        if start.ret != 0:
            return TwinSpiralRoundResult(
                node_id=node_id,
                start=start,
                battle=None,
                automatic=automatic,
                auto_enabled=automatic,
            )
        battle = self.await_battle_result(node_id)
        return TwinSpiralRoundResult(
            node_id=node_id,
            start=start,
            battle=battle,
            automatic=automatic,
            auto_enabled=automatic,
        )

    def run_after_first(
        self,
        node_id: int,
        *,
        stop_requested: Callable[[], bool] | None = None,
        on_round: Callable[[TwinSpiralRoundResult], None] | None = None,
    ) -> tuple[TwinSpiralRoundResult, ...]:
        """Run the first fight, then enable repeats only after its win result."""

        results: list[TwinSpiralRoundResult] = []
        automatic = False
        while not (stop_requested and stop_requested()):
            result = self.run_round(node_id, automatic=automatic)
            if not automatic and result.battle is not None and result.battle.win:
                result = replace(result, auto_enabled=True)
                automatic = True
            elif automatic:
                result = replace(result, auto_enabled=True)
            results.append(result)
            if on_round is not None:
                on_round(result)
            if result.battle is None or not result.battle.win:
                break
        return tuple(results)
