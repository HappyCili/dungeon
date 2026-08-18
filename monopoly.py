#!/usr/bin/env python3
"""宫廷棋自动掷骰协议客户端。"""

from __future__ import annotations

import time
from dataclasses import dataclass, replace
from typing import Callable

from dragon_arena import decode_game_data_item_totals
from dragon_arena_business_map import (
    EVENT_END_MESSAGE_ID,
    EVENT_FUNC_ACTION_MESSAGE_ID,
    EVENT_FUNC_NEXT_MESSAGE_ID,
    EVENT_OPTION_FAILED_MESSAGE_ID,
    EVENT_OPTION_MESSAGE_ID,
    EVENT_START_MESSAGE_ID,
    MONOPOLY_CAPTURED_MESSAGE_ID,
    MONOPOLY_CONFIRM_OTHER_MESSAGE_ID,
    MONOPOLY_EXEMPTION_PUNISH_MESSAGE_ID,
    MONOPOLY_INFO_MESSAGE_ID,
    MONOPOLY_EXIT_OTHER_MESSAGE_ID,
    MONOPOLY_MOVE_MESSAGE_ID,
    MONOPOLY_RESET_LAYOUT_MESSAGE_ID,
    MONOPOLY_ROLLDICE_MESSAGE_ID,
    MONOPOLY_SELECT_LAYOUT_MESSAGE_ID,
    MONOPOLY_SYN_VISITOR_LIST_MESSAGE_ID,
    MONOPOLY_TRANS_LIMIT_MESSAGE_ID,
    MONOPOLY_TRANS_OTHER_MESSAGE_ID,
    STORAGE_ITEM_CHANGE_MESSAGE_ID,
)
from game_session import GameSession, GameSessionError
from harvest_fief import (
    GameEndpoint,
    HarvestError,
    MessageHeader,
    ProtoReader,
    decode_item_change_notify,
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
    34001: "基础骰子",
    34002: "一对基础骰子",
    34003: "倍率骰子",
    34004: "遥控骰子",
}
# ``monopoly_level``: group * 1000 + level -> dice item ID. The UI must switch
# to the new board's configured dice immediately after a board upgrade.
MONOPOLY_BOARD_DICE = {
    (1, 1): 34001,
    (1, 2): 34002,
    (1, 3): 34002,
    (2, 1): 34001,
    (2, 2): 34002,
    (2, 3): 34002,
    (3, 1): 34001,
    (3, 2): 34001,
    (3, 3): 34001,
    (3, 4): 34002,
    (3, 5): 34002,
}
# ``monopoly_level.layout`` in the native client. The layout picker displays
# these in declaration order; the automation follows its second button rule.
MONOPOLY_LAYOUT_OPTIONS = {
    (1, 1): (101,),
    (1, 2): (202, 203, 204),
    (1, 3): (301, 302, 303),
    (2, 1): (101,),
    (2, 2): (202, 203, 204),
    (2, 3): (301, 302, 303),
    (3, 1): (1201,),
    (3, 2): (101,),
    (3, 3): (2001,),
    (3, 4): (202, 203, 204),
    (3, 5): (301, 302, 303),
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
    """本轮事件中按界面顺序取末项后自动提交的按钮。"""

    button_number: int
    option_index: int
    title: str


@dataclass(frozen=True)
class MonopolyBoard:
    """The fields needed to choose a dice item and reset layout."""

    group: int = 0
    level: int = 0
    layout: int = 0


@dataclass(frozen=True)
class MonopolyGameState:
    """Current Monopoly boards plus the server's layout-reset gate."""

    board: MonopolyBoard | None = None
    reset_status: int = 0
    other_uid: int = 0
    other_board: MonopolyBoard | None = None


@dataclass(frozen=True)
class MonopolyTransOtherResponse:
    """``Monopoly_transother`` destination board used for a visit."""

    other_uid: int = 0
    board: MonopolyBoard | None = None


@dataclass(frozen=True)
class MonopolyVisitor:
    """A server-provided target displayed by ``MonopolyPayVisitPanel``."""

    uid: int = 0
    name: str = ""


@dataclass(frozen=True)
class MonopolyVisitorListResponse:
    """``Monopoly_syn_visitorlist`` candidates and visit state."""

    visitors: tuple[MonopolyVisitor, ...] = ()
    status: int = 0


@dataclass(frozen=True)
class MonopolyConfirmOtherResponse:
    """``Monopoly_confirm_other`` result used before the board transfer push."""

    ret: int = 0
    status: int = 0


@dataclass(frozen=True)
class MonopolyExemptionResponse:
    """``Monopoly_exemption_punish`` response after a visit capture."""

    ret: int = 0
    status: int = 0


@dataclass(frozen=True)
class MonopolyVisitChoice:
    """A default-second choice made in the visit or capture UI."""

    button_number: int
    target_id: int = 0
    title: str = ""


@dataclass(frozen=True)
class MonopolyLayoutChoice:
    """The board layout button submitted after a level-up reset."""

    button_number: int
    layout_id: int


@dataclass(frozen=True)
class MonopolyResetLayoutResponse:
    """``Monopoly_reset_layout`` response fields used by automation."""

    ret: int = 0
    reset_status: int = 0
    board: MonopolyBoard | None = None


@dataclass(frozen=True)
class MonopolySelectLayoutResponse:
    """``Monopoly_select_layout`` response fields."""

    ret: int = 0
    reset_status: int = 0


@dataclass(frozen=True)
class MonopolyTurnResult:
    """一轮掷骰以及其后服务器事件的收敛结果。"""

    roll: MonopolyRollResponse | None = None
    choice: MonopolyChoice | None = None
    visit_choices: tuple[MonopolyVisitChoice, ...] = ()
    layout_choice: MonopolyLayoutChoice | None = None
    display_confirms: int = 0
    dice_remaining: int | None = None
    dice_depleted: bool = False
    cancelled: bool = False
    pending_interaction: bool = False
    interaction_error: str = ""


@dataclass(frozen=True)
class MonopolyDiceSelection:
    """当前棋盘可自动使用的骰子。"""

    dice_id: int
    label: str
    available: int | None = None


def _first_bytes_field(data: bytes, field_number: int) -> bytes | None:
    for number, wire_type, value in ProtoReader(data).fields():
        if number == field_number and wire_type == 2:
            return bytes(value)
    return None


def decode_monopoly_board(data: bytes) -> MonopolyBoard:
    """Decode the small, stable subset of the native ``MonopolyBoard`` type."""

    values = {"group": 0, "level": 0, "layout": 0}
    for field_number, wire_type, value in ProtoReader(data).fields():
        if wire_type != 0:
            continue
        decoded = decode_int32(int(value))
        if field_number == 1:
            values["group"] = decoded
        elif field_number == 2:
            values["level"] = decoded
        elif field_number == 10:
            values["layout"] = decoded
    return MonopolyBoard(**values)


def decode_monopoly_game_board(game_data: bytes | None) -> MonopolyBoard | None:
    """Return the self board embedded in ``Game_data`` when it is available."""

    return decode_monopoly_game_state(game_data).board


def decode_monopoly_game_state(game_data: bytes | None) -> MonopolyGameState:
    """Decode the Monopoly subset shared by ``Game_data`` and ``Monopoly_info``."""

    if not game_data:
        return MonopolyGameState()
    monopoly_data = _first_bytes_field(game_data, 34)
    data = monopoly_data if monopoly_data is not None else game_data
    self_board = _first_bytes_field(data, 7)
    other_board = _first_bytes_field(data, 9)
    reset_status = 0
    other_uid = 0
    for field_number, wire_type, value in ProtoReader(data).fields():
        if field_number == 8 and wire_type == 0:
            other_uid = int(value)
        elif field_number == 16 and wire_type == 0:
            reset_status = decode_int32(int(value))
    return MonopolyGameState(
        board=decode_monopoly_board(self_board) if self_board is not None else None,
        reset_status=reset_status,
        other_uid=other_uid,
        other_board=decode_monopoly_board(other_board) if other_board is not None else None,
    )


def encode_monopoly_roll_request(dice_id: int, *, point: int = 0) -> bytes:
    """Encode ``Monopoly_rolldice`` with the selected dice *item ID*."""

    if dice_id not in MONOPOLY_DICE:
        raise MonopolyError(f"不支持的宫廷棋骰子道具：{dice_id}")
    if point < 0 or point > 12:
        raise MonopolyError("宫廷棋骰子点数必须在 0 到 12 之间")
    payload = encode_int_field(1, dice_id)
    if point:
        payload += encode_int_field(2, point)
    return payload


def encode_monopoly_select_layout_request(layout_id: int) -> bytes:
    """Encode native ``Monopoly_select_layout`` request field ``layout``."""

    if layout_id <= 0:
        raise MonopolyError("宫廷棋布局 ID 必须大于 0")
    return encode_int_field(1, layout_id)


def encode_monopoly_confirm_other_request(visitor_id: int) -> bytes:
    """Encode the native default ``VISITOR_LIST`` confirmation request.

    ``VISITOR_LIST`` is enum zero, so its first protobuf field is deliberately
    omitted by the original client. The target player ID is field two.
    """

    if visitor_id <= 0:
        raise MonopolyError("宫廷棋拜访目标 ID 必须大于 0")
    return encode_int_field(2, visitor_id)


def describe_roll_rejection(ret: int) -> str:
    return ROLL_REJECTION_REASONS.get(ret, f"服务器停止掷骰（ret={ret}）")


def choose_monopoly_dice(game_data: bytes | None) -> MonopolyDiceSelection:
    """Select the dice configured for the current board, with an inventory fallback."""

    item_totals = decode_game_data_item_totals(game_data) if game_data else {}
    board = decode_monopoly_game_board(game_data)
    configured_dice = (
        MONOPOLY_BOARD_DICE.get((board.group, board.level)) if board else None
    )
    if configured_dice is not None:
        return MonopolyDiceSelection(
            configured_dice,
            MONOPOLY_DICE[configured_dice],
            item_totals.get(configured_dice, 0),
        )
    for dice_id, label in MONOPOLY_DICE.items():
        available = item_totals.get(dice_id, 0)
        if available > 0:
            return MonopolyDiceSelection(dice_id, label, available)

    label = MONOPOLY_DICE[34001]
    available = item_totals.get(34001) if game_data else None
    return MonopolyDiceSelection(34001, label, available)


def choose_monopoly_layout(board: MonopolyBoard) -> MonopolyLayoutChoice | None:
    """Choose the second configured layout, or the sole layout when only one exists."""

    layouts = MONOPOLY_LAYOUT_OPTIONS.get((board.group, board.level), ())
    if not layouts:
        return None
    index = 1 if len(layouts) >= 2 else 0
    return MonopolyLayoutChoice(button_number=index + 1, layout_id=layouts[index])


def decode_monopoly_reset_layout_response(data: bytes) -> MonopolyResetLayoutResponse:
    ret = 0
    reset_status = 0
    board: MonopolyBoard | None = None
    for field_number, wire_type, value in ProtoReader(data).fields():
        if field_number == 1 and wire_type == 0:
            ret = decode_int32(int(value))
        elif field_number == 3 and wire_type == 2:
            board = decode_monopoly_board(bytes(value))
        elif field_number == 4 and wire_type == 0:
            reset_status = decode_int32(int(value))
    return MonopolyResetLayoutResponse(ret, reset_status, board)


def decode_monopoly_select_layout_response(data: bytes) -> MonopolySelectLayoutResponse:
    ret = 0
    reset_status = 0
    for field_number, wire_type, value in ProtoReader(data).fields():
        if wire_type != 0:
            continue
        if field_number == 1:
            ret = decode_int32(int(value))
        elif field_number == 2:
            reset_status = decode_int32(int(value))
    return MonopolySelectLayoutResponse(ret, reset_status)


def decode_monopoly_transother_response(data: bytes) -> MonopolyTransOtherResponse:
    """Decode the visited board from ``Monopoly_transother``."""

    other_uid = 0
    board: MonopolyBoard | None = None
    for field_number, wire_type, value in ProtoReader(data).fields():
        if field_number == 1 and wire_type == 0:
            other_uid = int(value)
        elif field_number == 2 and wire_type == 2:
            board = decode_monopoly_board(bytes(value))
    return MonopolyTransOtherResponse(other_uid, board)


def decode_monopoly_visitor_list_response(data: bytes) -> MonopolyVisitorListResponse:
    """Decode ``Monopoly_syn_visitorlist`` (native codec ``vk``)."""

    visitors: list[MonopolyVisitor] = []
    status = 0
    for field_number, wire_type, value in ProtoReader(data).fields():
        if field_number == 1 and wire_type == 2:
            uid = 0
            name = ""
            for nested_field, nested_wire, nested_value in ProtoReader(bytes(value)).fields():
                if nested_field == 1 and nested_wire == 0:
                    uid = int(nested_value)
                elif nested_field == 5 and nested_wire == 2:
                    name = bytes(nested_value).decode("utf-8", errors="replace")
            visitors.append(MonopolyVisitor(uid=uid, name=name))
        elif field_number == 2 and wire_type == 0:
            status = decode_int32(int(value))
    return MonopolyVisitorListResponse(tuple(visitors), status)


def decode_monopoly_confirm_other_response(data: bytes) -> MonopolyConfirmOtherResponse:
    """Decode ``Monopoly_confirm_other`` (native codec ``kk``)."""

    ret = 0
    status = 0
    for field_number, wire_type, value in ProtoReader(data).fields():
        if wire_type != 0:
            continue
        if field_number == 1:
            ret = decode_int32(int(value))
        elif field_number == 4:
            status = decode_int32(int(value))
    return MonopolyConfirmOtherResponse(ret=ret, status=status)


def decode_monopoly_exemption_response(data: bytes) -> MonopolyExemptionResponse:
    """Decode ``Monopoly_exemption_punish`` (native codec ``Ak``)."""

    ret = 0
    status = 0
    for field_number, wire_type, value in ProtoReader(data).fields():
        if wire_type != 0:
            continue
        if field_number == 1:
            ret = decode_int32(int(value))
        elif field_number == 3:
            status = decode_int32(int(value))
    return MonopolyExemptionResponse(ret=ret, status=status)


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

    # The native board waits for the movement callback before it starts the
    # next event. Keep a comparable receive window so a delayed reset is not
    # overtaken by the next dice request.
    _POST_ROLL_SETTLE_SECONDS = 1.25
    _STATE_RETRY_SECONDS = 1.0

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
        self._dice = MonopolyDiceSelection(34001, "基础骰子")
        self._self_board: MonopolyBoard | None = None
        self._active_board: MonopolyBoard | None = None
        self._visiting_other = False
        self._reset_status = 0
        self._item_totals: dict[int, int] = {}
        self._inventory_known = False

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
            if isinstance(game_data, bytes):
                self._inventory_known = True
                self._item_totals = decode_game_data_item_totals(game_data)
                self._apply_game_state(decode_monopoly_game_state(game_data))
                if self._active_board is None:
                    self._dice = choose_monopoly_dice(game_data)
            else:
                self._dice = choose_monopoly_dice(None)
        except MonopolyError:
            raise
        except Exception as exc:
            raise MonopolyError("连接宫廷棋游戏服失败") from exc

    def dice_status(self) -> MonopolyDiceSelection:
        """Return the selected dice item and its latest known server total."""

        return self._dice

    def _select_dice_for_board(self, board: MonopolyBoard) -> None:
        """Synchronize the active dice to the board received from the server."""

        self._active_board = board
        dice_id = MONOPOLY_BOARD_DICE.get((board.group, board.level))
        if dice_id is None:
            return
        available = self._item_totals.get(dice_id) if self._inventory_known else None
        self._dice = MonopolyDiceSelection(dice_id, MONOPOLY_DICE[dice_id], available)

    def _apply_game_state(self, state: MonopolyGameState) -> None:
        self._reset_status = state.reset_status
        if state.board is not None:
            self._self_board = state.board
        if state.other_uid and state.other_board is not None:
            self._visiting_other = True
            self._select_dice_for_board(state.other_board)
        elif self._self_board is not None:
            self._visiting_other = False
            self._select_dice_for_board(self._self_board)

    def _apply_reset_layout(self, reset: MonopolyResetLayoutResponse) -> None:
        self._reset_status = reset.reset_status
        if reset.board is None:
            return
        self._self_board = reset.board
        if not self._visiting_other:
            self._select_dice_for_board(reset.board)

    def _enter_other_board(self, response: MonopolyTransOtherResponse) -> None:
        if response.other_uid == 0 or response.board is None:
            raise MonopolyError("拜访棋盘未返回玩家或棋盘信息")
        self._visiting_other = True
        self._select_dice_for_board(response.board)

    def _leave_other_board(self) -> None:
        self._visiting_other = False
        if self._self_board is not None:
            self._select_dice_for_board(self._self_board)

    def _refresh_game_state(self) -> MonopolyGameState:
        """Read the authoritative layout gate after the server rejects a roll."""

        self._send(MONOPOLY_INFO_MESSAGE_ID)
        deadline = time.monotonic() + self.timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise MonopolyMessageTimeout("等待宫廷棋状态超时")
            header = self._next_header(remaining)
            if header.message_id == STORAGE_ITEM_CHANGE_MESSAGE_ID:
                self._record_inventory_change(
                    header.data,
                    watched_dice_id=self._dice.dice_id,
                )
                continue
            if header.message_id == MONOPOLY_INFO_MESSAGE_ID:
                state = decode_monopoly_game_state(header.data)
                self._apply_game_state(state)
                return state
            if header.message_id == MONOPOLY_RESET_LAYOUT_MESSAGE_ID:
                reset = decode_monopoly_reset_layout_response(header.data)
                if reset.ret != 0:
                    raise MonopolyError(f"棋盘重置失败（ret={reset.ret}）")
                self._apply_reset_layout(reset)
                return MonopolyGameState(
                    board=self._self_board,
                    reset_status=self._reset_status,
                )
            if header.message_id == MONOPOLY_TRANS_OTHER_MESSAGE_ID:
                self._enter_other_board(decode_monopoly_transother_response(header.data))
                continue
            if header.message_id == MONOPOLY_EXIT_OTHER_MESSAGE_ID:
                self._leave_other_board()
                continue

    def _begin_pending_layout(self) -> MonopolyLayoutChoice | None:
        """Submit the native layout selection without consuming unrelated pushes."""

        if self._reset_status != 1:
            return None
        if self._self_board is None:
            raise MonopolyError("棋盘布局选择缺少棋盘信息")
        selected_layout = choose_monopoly_layout(self._self_board)
        if selected_layout is None:
            raise MonopolyError(
                f"未找到棋盘组 {self._self_board.group} 等级 {self._self_board.level} 的布局"
            )
        self._self_board = replace(self._self_board, layout=selected_layout.layout_id)
        self._send(
            MONOPOLY_SELECT_LAYOUT_MESSAGE_ID,
            encode_monopoly_select_layout_request(selected_layout.layout_id),
        )
        return selected_layout

    def _select_pending_layout(self) -> MonopolyLayoutChoice | None:
        """Submit and settle the pending server-side layout picker, when present."""

        selected_layout = self._begin_pending_layout()
        if selected_layout is None:
            return None
        deadline = time.monotonic() + self.timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise MonopolyMessageTimeout("等待宫廷棋布局选择结果超时")
            header = self._next_header(remaining)
            if header.message_id == STORAGE_ITEM_CHANGE_MESSAGE_ID:
                self._record_inventory_change(
                    header.data,
                    watched_dice_id=self._dice.dice_id,
                )
                continue
            if header.message_id == MONOPOLY_SELECT_LAYOUT_MESSAGE_ID:
                response = decode_monopoly_select_layout_response(header.data)
                if response.ret != 0:
                    raise MonopolyError(f"服务端未接受棋盘布局（ret={response.ret}）")
                if response.reset_status != 0:
                    raise MonopolyError("棋盘布局选择后仍在等待重置")
                self._reset_status = 0
                return selected_layout

    def _wait_for_state_retry(self, should_stop: Callable[[], bool]) -> bool:
        """Wait out the native client's board-transition interval."""

        deadline = time.monotonic() + self._STATE_RETRY_SECONDS
        while True:
            if should_stop():
                return False
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return True
            time.sleep(min(remaining, 0.1))

    def _record_inventory_change(self, data: bytes, *, watched_dice_id: int) -> bool:
        """Apply authoritative inventory totals and report the consumed dice update."""

        try:
            notice = decode_item_change_notify(data)
        except Exception as exc:
            raise MonopolyError("解析宫廷棋骰子库存变动失败") from exc
        watched_updated = False
        for change in notice.items:
            self._inventory_known = True
            self._item_totals[change.item_id] = max(0, change.total)
            if change.item_id == self._dice.dice_id:
                self._dice = replace(self._dice, available=max(0, change.total))
            if change.item_id == watched_dice_id:
                watched_updated = True
        return watched_updated

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
        # The native Event_start list retains the displayed button order. Event
        # automation intentionally takes the last choice; visit and layout
        # selectors keep their separate default-second policies.
        position = len(options) - 1
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
                dice_remaining=self._dice.available,
                dice_depleted=True,
            )

        roll: MonopolyRollResponse | None = None
        choice: MonopolyChoice | None = None
        visit_choices: list[MonopolyVisitChoice] = []
        layout_choice: MonopolyLayoutChoice | None = None
        display_confirms = 0
        event_active = False
        layout_active = False
        layout_pending = False
        waiting_visit_candidates = False
        waiting_visit_board = False
        waiting_visit_exit = False
        waiting_capture_result = False
        settle_deadline: float | None = None
        dice_inventory_updated = False
        rolled_dice_id = 0
        deadline = 0.0

        def begin_roll() -> bool:
            nonlocal deadline, dice_inventory_updated, rolled_dice_id, settle_deadline
            if self._dice.available is not None and self._dice.available <= 0:
                return False
            rolled_dice_id = self._dice.dice_id
            dice_inventory_updated = False
            settle_deadline = None
            deadline = time.monotonic() + self.timeout
            self._send(
                MONOPOLY_ROLLDICE_MESSAGE_ID,
                encode_monopoly_roll_request(rolled_dice_id),
            )
            return True

        def wait_error() -> str:
            if waiting_visit_candidates:
                return "等待拜访候选玩家列表超时"
            if waiting_visit_board:
                return "等待拜访棋盘切换超时"
            if waiting_capture_result:
                return "等待拜访捕获处理结果超时"
            if waiting_visit_exit:
                return "等待返回自身棋盘超时"
            return ""

        def finish_turn(
            *,
            cancelled: bool = False,
            pending_interaction: bool = False,
            interaction_error: str = "",
        ) -> MonopolyTurnResult:
            nonlocal dice_inventory_updated
            # The server normally pushes Storage_notify_itemchange immediately
            # after each successful roll. If that push arrives late, retain an
            # exact local decrement until the next authoritative total replaces
            # it; a successful roll always consumes one selected dice item.
            if (
                roll is not None
                and roll.ret == 0
                and not dice_inventory_updated
            ):
                previous_total = self._item_totals.get(rolled_dice_id)
                if previous_total is not None:
                    updated_total = max(0, previous_total - 1)
                    self._item_totals[rolled_dice_id] = updated_total
                    if self._dice.dice_id == rolled_dice_id:
                        self._dice = replace(self._dice, available=updated_total)
                elif self._dice.dice_id == rolled_dice_id and self._dice.available is not None:
                    self._dice = replace(
                        self._dice,
                        available=max(0, self._dice.available - 1),
                    )
                dice_inventory_updated = True
            remaining = self._dice.available
            return MonopolyTurnResult(
                roll=roll,
                choice=choice,
                visit_choices=tuple(visit_choices),
                layout_choice=layout_choice,
                display_confirms=display_confirms,
                dice_remaining=remaining,
                dice_depleted=remaining == 0,
                cancelled=cancelled,
                pending_interaction=pending_interaction,
                interaction_error=interaction_error,
            )

        if not begin_roll():
            return MonopolyTurnResult(
                dice_remaining=self._dice.available,
                dice_depleted=True,
            )

        while True:
            if should_stop():
                return finish_turn(cancelled=True)

            now = time.monotonic()
            server_transition_pending = (
                waiting_visit_candidates
                or waiting_visit_board
                or waiting_visit_exit
                or waiting_capture_result
            )
            if (event_active or layout_active or server_transition_pending) and now >= deadline:
                error = wait_error()
                if error:
                    return finish_turn(interaction_error=error)
                return finish_turn(pending_interaction=True)
            if (
                roll is not None
                and not event_active
                and not layout_active
                and not server_transition_pending
                and settle_deadline is not None
            ):
                if now >= settle_deadline:
                    return finish_turn()

            wait_until = deadline
            if (
                settle_deadline is not None
                and not event_active
                and not layout_active
                and not server_transition_pending
            ):
                wait_until = min(wait_until, settle_deadline)
            remaining = wait_until - now
            if remaining <= 0:
                if roll is not None and not event_active and not server_transition_pending:
                    return finish_turn()
                raise MonopolyMessageTimeout("等待宫廷棋掷骰结果超时")

            try:
                header = self._next_header(remaining)
            except MonopolyMessageTimeout:
                if (
                    roll is not None
                    and not event_active
                    and not layout_active
                    and not server_transition_pending
                ):
                    return finish_turn()
                error = wait_error()
                if error:
                    return finish_turn(interaction_error=error)
                if event_active or layout_active:
                    return finish_turn(pending_interaction=True)
                raise

            if header.message_id == STORAGE_ITEM_CHANGE_MESSAGE_ID:
                dice_inventory_updated = (
                    self._record_inventory_change(
                        header.data,
                        watched_dice_id=rolled_dice_id,
                    )
                    or dice_inventory_updated
                )
                continue

            if header.message_id == MONOPOLY_ROLLDICE_MESSAGE_ID:
                roll = decode_monopoly_roll_response(header.data)
                if roll.ret != 0:
                    if roll.ret == 6:
                        # The native client does not re-query Monopoly_info for
                        # this result. The server follows with a visitor list;
                        # its panel submits 22330 and then waits for 22316.
                        roll = None
                        waiting_visit_candidates = True
                        deadline = time.monotonic() + self.timeout
                        continue
                    if roll.ret == 7:
                        # A board upgrade may race the dice request. Unlike a
                        # visit, this response is resolved by the authoritative
                        # reset-status query and the normal layout picker.
                        self._refresh_game_state()
                        recovered_layout = self._select_pending_layout()
                        if recovered_layout is not None:
                            layout_choice = recovered_layout
                            if (
                                self._dice.available is not None
                                and self._dice.available <= 0
                            ):
                                return MonopolyTurnResult(
                                    layout_choice=layout_choice,
                                    dice_remaining=self._dice.available,
                                    dice_depleted=True,
                                )
                            roll = None
                            event_active = False
                            layout_active = False
                            settle_deadline = None
                            if not self._wait_for_state_retry(should_stop):
                                return finish_turn(cancelled=True)
                            if begin_roll():
                                continue
                    return finish_turn()
                waiting_visit_exit = (
                    self._visiting_other
                    and roll.total_turn > 0
                    and roll.current_turn >= roll.total_turn
                )
                if waiting_visit_exit:
                    deadline = time.monotonic() + self.timeout
                    settle_deadline = None
                    continue
                settle_deadline = time.monotonic() + self._POST_ROLL_SETTLE_SECONDS
                continue

            if header.message_id == MONOPOLY_RESET_LAYOUT_MESSAGE_ID:
                reset = decode_monopoly_reset_layout_response(header.data)
                if reset.ret != 0:
                    return finish_turn(interaction_error=f"棋盘重置失败（ret={reset.ret}）")
                self._apply_reset_layout(reset)
                if reset.reset_status != 1:
                    settle_deadline = time.monotonic() + self._POST_ROLL_SETTLE_SECONDS
                    continue
                # StageMonopoly only opens the layout picker once the current
                # event has ended. Selecting during Event_func_action can
                # overtake its server-side release.
                layout_pending = True
                if not event_active:
                    try:
                        layout_choice = self._begin_pending_layout()
                    except MonopolyError as exc:
                        return finish_turn(interaction_error=str(exc))
                    layout_pending = False
                    layout_active = layout_choice is not None
                    settle_deadline = None
                continue

            if header.message_id == MONOPOLY_SELECT_LAYOUT_MESSAGE_ID and layout_active:
                selected = decode_monopoly_select_layout_response(header.data)
                if selected.ret != 0:
                    return finish_turn(
                        interaction_error=f"服务端未接受棋盘布局（ret={selected.ret}）"
                    )
                if selected.reset_status != 0:
                    return finish_turn(interaction_error="棋盘布局选择后仍在等待重置")
                layout_active = False
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

            if header.message_id == MONOPOLY_INFO_MESSAGE_ID:
                self._apply_game_state(decode_monopoly_game_state(header.data))
                if self._reset_status == 1 and not event_active and not layout_active:
                    try:
                        layout_choice = self._begin_pending_layout()
                    except MonopolyError as exc:
                        return finish_turn(interaction_error=str(exc))
                    layout_active = layout_choice is not None
                    layout_pending = False
                if waiting_visit_board and self._visiting_other and roll is None:
                    waiting_visit_board = False
                    if not begin_roll():
                        return finish_turn()
                elif waiting_visit_exit and not self._visiting_other:
                    waiting_visit_exit = False
                    if roll is None and not begin_roll():
                        return finish_turn()
                continue

            if header.message_id == MONOPOLY_SYN_VISITOR_LIST_MESSAGE_ID:
                visitor_list = decode_monopoly_visitor_list_response(header.data)
                if visitor_list.status == 1:  # VS_CONFIRM
                    if not visitor_list.visitors:
                        return finish_turn(
                            interaction_error="服务器未返回可拜访的宫廷棋玩家"
                        )
                    position = 1 if len(visitor_list.visitors) >= 2 else 0
                    visitor = visitor_list.visitors[position]
                    if visitor.uid <= 0:
                        return finish_turn(
                            interaction_error="服务器返回的宫廷棋拜访玩家无效"
                        )
                    self._send(
                        MONOPOLY_CONFIRM_OTHER_MESSAGE_ID,
                        encode_monopoly_confirm_other_request(visitor.uid),
                    )
                    visit_choices.append(
                        MonopolyVisitChoice(position + 1, visitor.uid, visitor.name)
                    )
                    waiting_visit_candidates = False
                    waiting_visit_board = True
                    deadline = time.monotonic() + self.timeout
                    continue
                if visitor_list.status == 3:  # VS_CAPTURED
                    self._send(MONOPOLY_EXEMPTION_PUNISH_MESSAGE_ID)
                    visit_choices.append(MonopolyVisitChoice(2, 0, "返回自身棋盘"))
                    waiting_visit_candidates = False
                    waiting_capture_result = True
                    deadline = time.monotonic() + self.timeout
                    continue
                if visitor_list.status == 2:  # VS_GOING
                    waiting_visit_candidates = False
                    waiting_visit_board = True
                    deadline = time.monotonic() + self.timeout
                    continue
                return finish_turn(
                    interaction_error=f"宫廷棋拜访状态异常（status={visitor_list.status}）"
                )

            if header.message_id == MONOPOLY_CONFIRM_OTHER_MESSAGE_ID:
                confirmed = decode_monopoly_confirm_other_response(header.data)
                if confirmed.ret != 0:
                    return finish_turn(
                        interaction_error=f"服务器未接受拜访玩家（ret={confirmed.ret}）"
                    )
                if confirmed.status == 3:  # VS_CAPTURED
                    self._send(MONOPOLY_EXEMPTION_PUNISH_MESSAGE_ID)
                    visit_choices.append(MonopolyVisitChoice(2, 0, "返回自身棋盘"))
                    waiting_visit_board = False
                    waiting_capture_result = True
                elif confirmed.status == 2:  # VS_GOING
                    waiting_visit_board = True
                else:
                    return finish_turn(
                        interaction_error=(
                            f"拜访玩家后未进入棋盘（status={confirmed.status}）"
                        )
                    )
                deadline = time.monotonic() + self.timeout
                continue

            if header.message_id == MONOPOLY_TRANS_LIMIT_MESSAGE_ID:
                return finish_turn(interaction_error="宫廷棋拜访次数已达今日上限")

            if header.message_id == MONOPOLY_CAPTURED_MESSAGE_ID:
                # Native capture dialog has two buttons. The configured default
                # is its second button: do not spend currency to escape.
                self._send(MONOPOLY_EXEMPTION_PUNISH_MESSAGE_ID)
                visit_choices.append(MonopolyVisitChoice(2, 0, "返回自身棋盘"))
                waiting_visit_board = False
                waiting_capture_result = True
                deadline = time.monotonic() + self.timeout
                continue

            if header.message_id == MONOPOLY_EXEMPTION_PUNISH_MESSAGE_ID:
                exemption = decode_monopoly_exemption_response(header.data)
                if exemption.ret != 0:
                    return finish_turn(
                        interaction_error=f"宫廷棋捕获处理失败（ret={exemption.ret}）"
                    )
                waiting_capture_result = False
                waiting_visit_exit = True
                deadline = time.monotonic() + self.timeout
                continue

            if header.message_id == MONOPOLY_TRANS_OTHER_MESSAGE_ID:
                self._enter_other_board(decode_monopoly_transother_response(header.data))
                waiting_visit_candidates = False
                waiting_visit_board = False
                waiting_capture_result = False
                if roll is None:
                    if not begin_roll():
                        return finish_turn()
                else:
                    settle_deadline = time.monotonic() + self._POST_ROLL_SETTLE_SECONDS
                continue

            if header.message_id == MONOPOLY_EXIT_OTHER_MESSAGE_ID:
                self._leave_other_board()
                waiting_visit_exit = False
                waiting_capture_result = False
                if roll is None:
                    if not begin_roll():
                        return finish_turn()
                else:
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
                    return finish_turn(pending_interaction=True)
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
                # EventModule ultimately clears every wait/waitAction by
                # sending Event_func_next. This includes a non-zero LineText
                # panel, which is the branch that previously left dice unused.
                if action.wait or action.wait_action_id:
                    self._send(EVENT_FUNC_NEXT_MESSAGE_ID, encode_event_func_next())
                    display_confirms += 1
                    continue
                # A function action with no wait is already complete in the
                # native client; wait for its Event_end rather than sending a
                # speculative acknowledgement.
                continue

            if header.message_id == EVENT_OPTION_FAILED_MESSAGE_ID:
                return finish_turn(interaction_error="服务端未接受宫廷棋事件选项")

            if header.message_id == EVENT_END_MESSAGE_ID:
                event_active = False
                if layout_pending:
                    try:
                        layout_choice = self._begin_pending_layout()
                    except MonopolyError as exc:
                        return finish_turn(interaction_error=str(exc))
                    layout_pending = False
                    layout_active = layout_choice is not None
                    settle_deadline = None
                else:
                    settle_deadline = time.monotonic() + self._POST_ROLL_SETTLE_SECONDS
