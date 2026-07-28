#!/usr/bin/env python3
"""宫廷棋自动掷骰协议客户端。"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable

from dragon_arena import decode_game_data_item_totals
from dragon_arena_business_map import (
    EVENT_END_MESSAGE_ID,
    EVENT_FUNC_ACTION_MESSAGE_ID,
    EVENT_FUNC_NEXT_MESSAGE_ID,
    EVENT_OPTION_FAILED_MESSAGE_ID,
    EVENT_OPTION_MESSAGE_ID,
    EVENT_START_MESSAGE_ID,
    MONOPOLY_MOVE_MESSAGE_ID,
    MONOPOLY_ROLLDICE_MESSAGE_ID,
)
from game_session import GameSession, GameSessionError
from harvest_fief import (
    GameEndpoint,
    HarvestError,
    MessageHeader,
    ProtoReader,
    decode_int32,
    encode_int_field,
)
from treasure_farm import (
    decode_event_func_action,
    decode_event_start,
    encode_event_func_next,
    encode_event_option,
)


class MonopolyError(HarvestError):
    """宫廷棋会话或协议推进失败。"""


class MonopolyMessageTimeout(MonopolyError):
    """等待一轮宫廷棋消息超时。"""


MONOPOLY_DICE = {
    1: (34001, "基础骰子"),
    2: (34002, "一对基础骰子"),
    3: (34003, "倍率骰子"),
    4: (34004, "遥控骰子"),
}
ROLL_REJECTION_REASONS = {
    3: "所选骰子不足",
}


@dataclass(frozen=True)
class MonopolyRollResponse:
    """``Monopoly_rolldice`` 服务端结果的可展示字段。"""

    ret: int = 0
    dice_id: int = 0
    point: int = 0
    cell_no: int = 0
    current_turn: int = 0
    total_turn: int = 0


@dataclass(frozen=True)
class MonopolyChoice:
    """本轮事件中自动提交的界面按钮。"""

    button_number: int
    option_index: int
    title: str


@dataclass(frozen=True)
class MonopolyTurnResult:
    """一轮掷骰以及其后服务器事件的收敛结果。"""

    roll: MonopolyRollResponse | None = None
    choice: MonopolyChoice | None = None
    display_confirms: int = 0
    cancelled: bool = False
    pending_interaction: bool = False
    interaction_error: str = ""
    blocked_reason: str = ""


@dataclass(frozen=True)
class MonopolyDiceSelection:
    """当前棋盘可自动使用的骰子。"""

    dice_id: int
    item_id: int
    label: str
    available: int | None = None


def encode_monopoly_roll_request(dice_id: int, *, point: int = 0) -> bytes:
    """Encode ``Monopoly_rolldice``: selected dice type and optional point."""

    if dice_id not in MONOPOLY_DICE:
        raise MonopolyError(f"不支持的宫廷棋骰子类型：{dice_id}")
    if point < 0 or point > 12:
        raise MonopolyError("宫廷棋骰子点数必须在 0 到 12 之间")
    payload = encode_int_field(1, dice_id)
    if point:
        payload += encode_int_field(2, point)
    return payload


def describe_roll_rejection(ret: int) -> str:
    return ROLL_REJECTION_REASONS.get(ret, f"服务器停止掷骰（ret={ret}）")


def _first_bytes_field(data: bytes, field_number: int) -> bytes | None:
    for number, wire_type, value in ProtoReader(data).fields():
        if number == field_number and wire_type == 2:
            return bytes(value)
    return None


def _decode_board_level(game_data: bytes) -> int | None:
    monopoly_data = _first_bytes_field(game_data, 34)
    if monopoly_data is None:
        return None
    self_board = _first_bytes_field(monopoly_data, 7)
    if self_board is None:
        return None
    for field_number, wire_type, value in ProtoReader(self_board).fields():
        if field_number == 2 and wire_type == 0:
            return decode_int32(int(value))
    return None


def choose_monopoly_dice(game_data: bytes | None) -> MonopolyDiceSelection:
    """Match the board's normal dice requirement using the login inventory."""

    board_level = _decode_board_level(game_data) if game_data else None
    dice_id = 2 if board_level is not None and board_level >= 2 else 1
    item_id, label = MONOPOLY_DICE[dice_id]
    if not game_data:
        return MonopolyDiceSelection(dice_id, item_id, label)
    item_totals = decode_game_data_item_totals(game_data)
    return MonopolyDiceSelection(dice_id, item_id, label, item_totals.get(item_id, 0))


def decode_monopoly_roll_response(data: bytes) -> MonopolyRollResponse:
    """解码客户端静态协议 ``Xb``（``Monopoly_rolldice``）。"""

    values = {
        "ret": 0,
        "dice_id": 0,
        "point": 0,
        "cell_no": 0,
        "current_turn": 0,
        "total_turn": 0,
    }
    for field_number, wire_type, value in ProtoReader(data).fields():
        if wire_type != 0:
            continue
        decoded = decode_int32(int(value))
        if field_number == 1:
            values["ret"] = decoded
        elif field_number == 2:
            values["dice_id"] = decoded
        elif field_number == 3:
            values["point"] = decoded
        elif field_number == 4:
            values["cell_no"] = decoded
        elif field_number == 5:
            values["current_turn"] = decoded
        elif field_number == 8:
            values["total_turn"] = decoded
    return MonopolyRollResponse(**values)


def decode_monopoly_move(data: bytes) -> tuple[int, int, int]:
    """Decode ``Monopoly_move``: total turn, cell number, current turn."""

    total_turn = 0
    cell_no = 0
    current_turn = 0
    for field_number, wire_type, value in ProtoReader(data).fields():
        if wire_type != 0:
            continue
        decoded = decode_int32(int(value))
        if field_number == 1:
            total_turn = decoded
        elif field_number == 2:
            cell_no = decoded
        elif field_number == 3:
            current_turn = decoded
    return total_turn, cell_no, current_turn


class MonopolyClient:
    """通过 ``GameSession`` 执行一轮宫廷棋掷骰和自动事件选择。"""

    _POST_ROLL_SETTLE_SECONDS = 0.35

    def __init__(
        self,
        endpoint: GameEndpoint,
        timeout: float,
        *,
        session: GameSession | object | None = None,
    ) -> None:
        if timeout <= 0:
            raise MonopolyError("timeout 必须大于 0")
        self.endpoint = endpoint
        self.timeout = timeout
        self._owns_session = session is None
        self._session = session or GameSession(timeout=timeout)
        self._dice = MonopolyDiceSelection(1, 34001, "基础骰子")

    def __enter__(self) -> "MonopolyClient":
        self.login()
        return self

    def __exit__(self, _exc_type: object, _exc: object, _traceback: object) -> None:
        self.close()

    def close(self) -> None:
        if self._owns_session:
            close = getattr(self._session, "close", None)
            if callable(close):
                close()

    def login(self) -> None:
        try:
            ensure_recovered = getattr(self._session, "ensure_recovered", None)
            if callable(ensure_recovered):
                ensure_recovered(self.endpoint)
            else:
                self._session.ensure_ready(self.endpoint)
            game_data = getattr(self._session, "game_data", None)
            self._dice = choose_monopoly_dice(
                game_data if isinstance(game_data, bytes) else None
            )
        except MonopolyError:
            raise
        except Exception as exc:
            raise MonopolyError("连接宫廷棋游戏服失败") from exc

    def _send(self, message_id: int, data: bytes = b"") -> None:
        try:
            self._session.send_message(message_id, data, encrypted=True)
        except MonopolyError:
            raise
        except Exception as exc:
            raise MonopolyError("发送宫廷棋请求失败") from exc

    def _next_header(self, timeout: float) -> MessageHeader:
        try:
            return self._session.receive_header(timeout)
        except GameSessionError as exc:
            if "超时" in str(exc):
                raise MonopolyMessageTimeout("等待宫廷棋消息超时") from exc
            raise MonopolyError("读取宫廷棋消息失败") from exc
        except MonopolyError:
            raise
        except Exception as exc:
            if "超时" in str(exc):
                raise MonopolyMessageTimeout("等待宫廷棋消息超时") from exc
            raise MonopolyError("读取宫廷棋消息失败") from exc

    @staticmethod
    def _choose_event_option(start: object) -> MonopolyChoice | None:
        options = tuple(getattr(start, "options", ()))
        if not options:
            return None
        # Options retain the UI display order. A single button is only used to
        # acknowledge an event that has no real alternative.
        position = 1 if len(options) >= 2 else 0
        option = options[position]
        return MonopolyChoice(
            button_number=position + 1,
            option_index=int(getattr(option, "optidx", 0)),
            title=str(getattr(option, "title", "")),
        )

    def roll_once(
        self,
        *,
        stop_requested: Callable[[], bool] | None = None,
    ) -> MonopolyTurnResult:
        """Roll once and settle any immediate Monopoly event before returning."""

        should_stop = stop_requested or (lambda: False)
        if should_stop():
            return MonopolyTurnResult(cancelled=True)
        if self._dice.available is not None and self._dice.available <= 0:
            return MonopolyTurnResult(
                blocked_reason=f"{self._dice.label}不足（当前 0），等待游戏内补给后再试"
            )

        self._send(
            MONOPOLY_ROLLDICE_MESSAGE_ID,
            encode_monopoly_roll_request(self._dice.dice_id),
        )
        deadline = time.monotonic() + self.timeout
        roll: MonopolyRollResponse | None = None
        choice: MonopolyChoice | None = None
        display_confirms = 0
        event_active = False
        settle_deadline: float | None = None

        while True:
            if should_stop():
                return MonopolyTurnResult(
                    roll=roll,
                    choice=choice,
                    display_confirms=display_confirms,
                    cancelled=True,
                )

            now = time.monotonic()
            if event_active and now >= deadline:
                return MonopolyTurnResult(
                    roll=roll,
                    choice=choice,
                    display_confirms=display_confirms,
                    pending_interaction=True,
                )
            if roll is not None and not event_active and settle_deadline is not None:
                if now >= settle_deadline:
                    return MonopolyTurnResult(
                        roll=roll,
                        choice=choice,
                        display_confirms=display_confirms,
                    )

            wait_until = deadline
            if settle_deadline is not None and not event_active:
                wait_until = min(wait_until, settle_deadline)
            remaining = wait_until - now
            if remaining <= 0:
                if roll is not None and not event_active:
                    return MonopolyTurnResult(
                        roll=roll,
                        choice=choice,
                        display_confirms=display_confirms,
                    )
                raise MonopolyMessageTimeout("等待宫廷棋掷骰结果超时")

            try:
                header = self._next_header(remaining)
            except MonopolyMessageTimeout:
                if roll is not None and not event_active:
                    return MonopolyTurnResult(
                        roll=roll,
                        choice=choice,
                        display_confirms=display_confirms,
                    )
                if event_active:
                    return MonopolyTurnResult(
                        roll=roll,
                        choice=choice,
                        display_confirms=display_confirms,
                        pending_interaction=True,
                    )
                raise

            if header.message_id == MONOPOLY_ROLLDICE_MESSAGE_ID:
                roll = decode_monopoly_roll_response(header.data)
                if roll.ret != 0:
                    return MonopolyTurnResult(
                        roll=roll,
                        choice=choice,
                        display_confirms=display_confirms,
                    )
                settle_deadline = time.monotonic() + self._POST_ROLL_SETTLE_SECONDS
                continue

            if header.message_id == MONOPOLY_MOVE_MESSAGE_ID and roll is not None:
                total_turn, cell_no, current_turn = decode_monopoly_move(header.data)
                roll = MonopolyRollResponse(
                    ret=roll.ret,
                    dice_id=roll.dice_id,
                    point=roll.point,
                    cell_no=cell_no or roll.cell_no,
                    current_turn=current_turn or roll.current_turn,
                    total_turn=total_turn or roll.total_turn,
                )
                settle_deadline = time.monotonic() + self._POST_ROLL_SETTLE_SECONDS
                continue

            if header.message_id == EVENT_START_MESSAGE_ID:
                event_active = True
                start = decode_event_start(header.data)
                if start.auto_option >= 100:
                    self._send(
                        EVENT_OPTION_MESSAGE_ID,
                        encode_event_option(
                            start.auto_option,
                            start.dialog_id,
                            start.event_id,
                        ),
                    )
                    continue

                selected = self._choose_event_option(start)
                if selected is None or selected.option_index == 0:
                    return MonopolyTurnResult(
                        roll=roll,
                        choice=choice,
                        display_confirms=display_confirms,
                        pending_interaction=True,
                    )
                option = start.options[selected.button_number - 1]
                self._send(
                    EVENT_OPTION_MESSAGE_ID,
                    encode_event_option(
                        selected.option_index,
                        start.dialog_id,
                        start.event_id,
                        itemcommit=option.icommit,
                    ),
                )
                choice = selected
                continue

            if header.message_id == EVENT_FUNC_ACTION_MESSAGE_ID:
                event_active = True
                action = decode_event_func_action(header.data)
                if action.auto_confirmable:
                    self._send(EVENT_FUNC_NEXT_MESSAGE_ID, encode_event_func_next())
                    display_confirms += 1
                    continue
                return MonopolyTurnResult(
                    roll=roll,
                    choice=choice,
                    display_confirms=display_confirms,
                    pending_interaction=True,
                )

            if header.message_id == EVENT_OPTION_FAILED_MESSAGE_ID:
                return MonopolyTurnResult(
                    roll=roll,
                    choice=choice,
                    display_confirms=display_confirms,
                    interaction_error="服务端未接受宫廷棋事件选项",
                )

            if header.message_id == EVENT_END_MESSAGE_ID:
                event_active = False
                settle_deadline = time.monotonic() + self._POST_ROLL_SETTLE_SECONDS
