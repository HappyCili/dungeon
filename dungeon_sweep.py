#!/usr/bin/env python3
"""地下城扫荡与宝库全部抽取的游戏服协议客户端。

登录后从 ``Game_data`` 读取当前账号已解锁的地下城与历史最高分。扫荡使用
``Dun_sweep``，随后使用 ``Dun_start_draw`` 的 ``all=true`` 请求抽取本次可用
的全部宝库奖励。
"""

from __future__ import annotations

from pathlib import Path

import socket
import time
from dataclasses import dataclass, field, replace
from typing import Callable, Mapping

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
    decode_item_change_notify,
    decode_message_header,
    decode_pack_password,
    encode_bytes_field,
    encode_int_field,
    encode_login_payload,
    encode_message_header,
    pack1_decode,
    pack1_encode,
)
from ws_traffic_log import bind_traffic_logging


GAME_DATA_MESSAGE_ID = 10490
DUN_START_DRAW_MESSAGE_ID = 19604
DUN_SWEEP_MESSAGE_ID = 19642
SHOP_BUY_MESSAGE_ID = 19517
DIRECT_SHOP_ID = 999
DUNGEON_LAMP_ITEM_ID = 901
PROP_KIND_ITEM = 1
KICKOUT_MESSAGE_ID = 10030
# ``ItemChangeSource.DUN_DRAW`` in the generated client protocol.
DUN_DRAW_ITEM_CHANGE_SOURCE = 71
REWARD_NOTIFICATION_GRACE_SECONDS = 1.0

# Native zh-Hans resource: dungeon.sweepRet*.  These are business-state
# responses from Dun_sweep, not transport failures and must not be retried.
DUNGEON_SWEEP_RET_LABELS = {
    1: "未通关",
    2: "无积分记录",
    3: "无扫荡次数",
    5: "进地下城次数达到上限",
    6: "材料不足",
    999: "背包装备已满，请分解后再试",
}


def sweep_rejection_reason(ret: int) -> str | None:
    """Return a locally verified rejection reason, when the client defines one."""

    return DUNGEON_SWEEP_RET_LABELS.get(ret)


class DungeonSweepError(HarvestError):
    """地下城状态、扫荡或宝库抽取不可执行。"""


class DungeonSweepRejected(DungeonSweepError):
    """服务端拒绝地下城扫荡请求。"""

    def __init__(self, ret: int) -> None:
        self.ret = ret
        self.reason = sweep_rejection_reason(ret)
        label = self.reason or "未登记的扫荡拒绝原因"
        super().__init__(f"地下城扫荡失败：{label}（ret={ret}）")


class DungeonDrawRejected(DungeonSweepError):
    """服务端拒绝地下城宝库抽取请求。"""

    def __init__(self, ret: int) -> None:
        self.ret = ret
        super().__init__(f"地下城宝库全部抽取失败：ret={ret}")


class DungeonLampClaimRejected(DungeonSweepError):
    """服务端拒绝领取地下城每日永焰之灯。"""

    def __init__(self, ret: int, message: str = "") -> None:
        self.ret = ret
        self.message = message
        detail = f"：{message}" if message else ""
        super().__init__(f"地下城永焰之灯领取失败{detail}（ret={ret}）")


@dataclass(frozen=True)
class DungeonStatus:
    """从 ``Game_data.dungeon`` 提取的扫荡前状态。"""

    unlocked_ids: tuple[int, ...]
    visible_ids: tuple[int, ...]
    best_scores: Mapping[int, int]
    current_dungeon_id: int
    draw_times: int
    total_draw_times: int
    challenge_times: Mapping[int, int] = field(default_factory=dict)

    @property
    def sweepable_ids(self) -> tuple[int, ...]:
        visible = set(self.visible_ids)
        return tuple(dungeon_id for dungeon_id in self.unlocked_ids if dungeon_id in visible)

    @property
    def pending_draws(self) -> int:
        return max(self.total_draw_times - self.draw_times, 0)

    def can_sweep(self, dungeon_id: int) -> bool:
        return dungeon_id in self.sweepable_ids

    def best_score_for(self, dungeon_id: int) -> int:
        return self.best_scores.get(dungeon_id, 0)

    def challenge_times_for(self, dungeon_id: int) -> int:
        return self.challenge_times.get(dungeon_id, 0)


@dataclass(frozen=True)
class DungeonSweepResponse:
    ret: int


@dataclass(frozen=True)
class DungeonDrawResponse:
    ret: int
    dungeon_id: int
    draw_times: int
    total_draw_times: int
    reward_ids: tuple[int, ...]
    probabilities: tuple[int, ...]
    all_drawn: bool
    item_changes: tuple[ItemChange, ...] = ()
    reward_props: tuple[RewardProp, ...] = ()
    reward_notice_received: bool = False


@dataclass(frozen=True)
class DungeonLampClaimResponse:
    ret: int
    shop_id: int
    message: str
    claimed_qty: int


@dataclass(frozen=True)
class DungeonLampOffer:
    stock_qty: int
    goods_data: bytes


def _decode_packed_int32(data: bytes) -> tuple[int, ...]:
    reader = ProtoReader(data)
    values: list[int] = []
    while reader.position < len(data):
        values.append(decode_int32(reader.read_varint()))
    return tuple(values)


def _append_repeated_int32(
    values: list[int], wire_type: int, value: int | bytes
) -> None:
    if wire_type == 0:
        values.append(decode_int32(int(value)))
    elif wire_type == 2:
        values.extend(_decode_packed_int32(bytes(value)))


def _decode_best_score_entry(data: bytes) -> tuple[int, int] | None:
    dungeon_id = 0
    score = 0
    for field_number, wire_type, value in ProtoReader(data).fields():
        if field_number == 1 and wire_type == 0:
            dungeon_id = decode_int32(int(value))
        elif field_number == 2 and wire_type == 0:
            score = int(value)
    if dungeon_id == 0:
        return None
    return dungeon_id, score


def _decode_challenge_time_entry(data: bytes) -> tuple[int, int] | None:
    dungeon_id = 0
    times = 0
    for field_number, wire_type, value in ProtoReader(data).fields():
        if field_number == 1 and wire_type == 0:
            dungeon_id = decode_int32(int(value))
        elif field_number == 2 and wire_type == 0:
            times = decode_int32(int(value))
    if dungeon_id == 0:
        return None
    return dungeon_id, times


def _validate_ids(ids: tuple[int, ...], label: str) -> None:
    if any(value <= 0 for value in ids):
        raise DungeonSweepError(f"Game_data 地下城{label}包含无效 ID")
    if len(ids) != len(set(ids)):
        raise DungeonSweepError(f"Game_data 地下城{label}包含重复 ID")


def decode_dungeon_status(data: bytes) -> DungeonStatus:
    """Decode the generated ``DungeonData`` message from the local client."""

    unlocked_ids: list[int] = []
    visible_ids: list[int] = []
    best_scores: dict[int, int] = {}
    challenge_times: dict[int, int] = {}
    current_dungeon_id = 0
    draw_times = 0
    total_draw_times = 0
    for field_number, wire_type, value in ProtoReader(data).fields():
        if field_number == 1:
            _append_repeated_int32(unlocked_ids, wire_type, value)
        elif field_number == 5 and wire_type == 0:
            current_dungeon_id = decode_int32(int(value))
        elif field_number == 7 and wire_type == 0:
            draw_times = decode_int32(int(value))
        elif field_number == 21 and wire_type == 0:
            total_draw_times = decode_int32(int(value))
        elif field_number == 29 and wire_type == 2:
            entry = _decode_challenge_time_entry(bytes(value))
            if entry is None:
                continue
            dungeon_id, times = entry
            if dungeon_id in challenge_times:
                raise DungeonSweepError("Game_data 地下城挑战次数包含重复地下城")
            challenge_times[dungeon_id] = times
        elif field_number == 30 and wire_type == 2:
            entry = _decode_best_score_entry(bytes(value))
            if entry is None:
                continue
            dungeon_id, score = entry
            if dungeon_id in best_scores:
                raise DungeonSweepError("Game_data 地下城最高分包含重复地下城")
            best_scores[dungeon_id] = score
        elif field_number == 35:
            _append_repeated_int32(visible_ids, wire_type, value)

    unlocked = tuple(unlocked_ids)
    visible = tuple(visible_ids)
    _validate_ids(unlocked, "已解锁列表")
    _validate_ids(visible, "展示列表")
    if current_dungeon_id < 0:
        raise DungeonSweepError("Game_data 当前地下城 ID 无效")
    if draw_times < 0 or total_draw_times < draw_times:
        raise DungeonSweepError("Game_data 地下城抽取次数状态无效")
    if any(dungeon_id <= 0 or score < 0 for dungeon_id, score in best_scores.items()):
        raise DungeonSweepError("Game_data 地下城最高分状态无效")
    if any(
        dungeon_id <= 0 or times < 0
        for dungeon_id, times in challenge_times.items()
    ):
        raise DungeonSweepError("Game_data 地下城挑战次数状态无效")
    return DungeonStatus(
        unlocked_ids=unlocked,
        visible_ids=visible,
        best_scores=best_scores,
        current_dungeon_id=current_dungeon_id,
        draw_times=draw_times,
        total_draw_times=total_draw_times,
        challenge_times=challenge_times,
    )


def decode_game_data_dungeon_status(data: bytes) -> DungeonStatus:
    """从 ``Game_data`` 中提取地下城状态。"""

    for field_number, wire_type, value in ProtoReader(data).fields():
        if field_number == 18 and wire_type == 2:
            return decode_dungeon_status(bytes(value))
    raise DungeonSweepError("Game_data 缺少 dungeon 状态")


def encode_dungeon_sweep_request(dungeon_id: int) -> bytes:
    if (
        not isinstance(dungeon_id, int)
        or isinstance(dungeon_id, bool)
        or dungeon_id <= 0
    ):
        raise ValueError("地下城 ID 必须是正整数")
    return encode_int_field(1, dungeon_id)


def decode_dungeon_sweep_response(data: bytes) -> DungeonSweepResponse:
    ret = 0
    for field_number, wire_type, value in ProtoReader(data).fields():
        if field_number == 1 and wire_type == 0:
            ret = decode_int32(int(value))
    return DungeonSweepResponse(ret=ret)


def encode_dungeon_draw_all_request(dungeon_id: int) -> bytes:
    if (
        not isinstance(dungeon_id, int)
        or isinstance(dungeon_id, bool)
        or dungeon_id <= 0
    ):
        raise ValueError("地下城 ID 必须是正整数")
    return encode_int_field(1, dungeon_id) + encode_int_field(2, 1)


def decode_dungeon_draw_response(data: bytes) -> DungeonDrawResponse:
    ret = 0
    dungeon_id = 0
    draw_times = 0
    total_draw_times = 0
    reward_ids: list[int] = []
    probabilities: list[int] = []
    all_drawn = False
    for field_number, wire_type, value in ProtoReader(data).fields():
        if field_number == 1 and wire_type == 0:
            ret = decode_int32(int(value))
        elif field_number == 2 and wire_type == 0:
            dungeon_id = decode_int32(int(value))
        elif field_number == 3 and wire_type == 0:
            draw_times = decode_int32(int(value))
        elif field_number == 4 and wire_type == 0:
            total_draw_times = decode_int32(int(value))
        elif field_number == 5:
            _append_repeated_int32(reward_ids, wire_type, value)
        elif field_number == 6:
            _append_repeated_int32(probabilities, wire_type, value)
        elif field_number == 7 and wire_type == 0:
            all_drawn = bool(value)
    if ret == 0:
        if dungeon_id <= 0:
            raise DungeonSweepError("地下城全部抽取响应缺少地下城 ID")
        if draw_times < 0 or total_draw_times < draw_times:
            raise DungeonSweepError("地下城全部抽取响应中的次数状态无效")
        if any(reward_id <= 0 for reward_id in reward_ids):
            raise DungeonSweepError("地下城全部抽取响应包含无效奖励 ID")
    return DungeonDrawResponse(
        ret=ret,
        dungeon_id=dungeon_id,
        draw_times=draw_times,
        total_draw_times=total_draw_times,
        reward_ids=tuple(reward_ids),
        probabilities=tuple(probabilities),
        all_drawn=all_drawn,
    )


def encode_dungeon_lamp_claim_request(quantity: int, goods_data: bytes) -> bytes:
    """Encode the native ``Shop_buy`` request for direct-shop goods."""

    if (
        not isinstance(quantity, int)
        or isinstance(quantity, bool)
        or quantity <= 0
    ):
        raise ValueError("永焰之灯领取数量必须是正整数")
    if not isinstance(goods_data, bytes) or not goods_data:
        raise ValueError("直购商品数据不能为空")
    return (
        encode_int_field(1, DIRECT_SHOP_ID)
        + encode_int_field(2, quantity)
        + encode_bytes_field(3, goods_data)
    )


def _decode_shop_ticket(data: bytes) -> tuple[int, int]:
    ticket_id = 0
    status = 0
    for field_number, wire_type, value in ProtoReader(data).fields():
        if field_number == 1 and wire_type == 0:
            ticket_id = decode_int32(int(value))
        elif field_number == 2 and wire_type == 0:
            status = decode_int32(int(value))
    return ticket_id, status


def _decode_shop_prop(data: bytes) -> tuple[int, int] | None:
    kind = 0
    item_id = 0
    for field_number, wire_type, value in ProtoReader(data).fields():
        if field_number == 1 and wire_type == 0:
            kind = decode_int32(int(value))
        elif field_number == 2 and wire_type == 0:
            item_id = decode_int32(int(value))
    if kind <= 0 or item_id <= 0:
        return None
    return kind, item_id


def _decode_direct_shop_offer(data: bytes) -> DungeonLampOffer | None:
    stock_qty = 0
    prop: tuple[int, int] | None = None
    purchase_ticket: tuple[int, int] | None = None
    remove_ticket: tuple[int, int] | None = None
    for field_number, wire_type, value in ProtoReader(data).fields():
        if field_number == 2 and wire_type == 0:
            stock_qty = decode_int32(int(value))
        elif field_number == 7 and wire_type == 2:
            prop = _decode_shop_prop(bytes(value))
        elif field_number == 20 and wire_type == 2:
            purchase_ticket = _decode_shop_ticket(bytes(value))
        elif field_number == 21 and wire_type == 2:
            remove_ticket = _decode_shop_ticket(bytes(value))
    if prop != (PROP_KIND_ITEM, DUNGEON_LAMP_ITEM_ID) or stock_qty <= 0:
        return None
    # Match Storage.getDirectlyGoodsData: ticket-gated offers are not usable.
    if purchase_ticket is not None:
        ticket_id, status = purchase_ticket
        if ticket_id > 0 and status != 1:
            return None
    if remove_ticket is not None and remove_ticket[1] == 1:
        return None
    return DungeonLampOffer(stock_qty=stock_qty, goods_data=data)


def find_dungeon_lamp_offer(game_data: bytes) -> DungeonLampOffer | None:
    """Find the available daily lamp offer in ``Game_data.shopData``."""

    for field_number, wire_type, value in ProtoReader(game_data).fields():
        if field_number != 17 or wire_type != 2:
            continue
        shop_id = 0
        goods: list[bytes] = []
        for shop_field, shop_wire, shop_value in ProtoReader(bytes(value)).fields():
            if shop_field == 1 and shop_wire == 0:
                shop_id = decode_int32(int(shop_value))
            elif shop_field == 9 and shop_wire == 2:
                goods.append(bytes(shop_value))
        if shop_id != DIRECT_SHOP_ID:
            continue
        for goods_data in goods:
            offer = _decode_direct_shop_offer(goods_data)
            if offer is not None:
                return offer
    return None


def decode_dungeon_lamp_claim_response(data: bytes) -> DungeonLampClaimResponse:
    shop_id = 0
    ret = 0
    message = ""
    for field_number, wire_type, value in ProtoReader(data).fields():
        if field_number == 1 and wire_type == 0:
            shop_id = decode_int32(int(value))
        elif field_number == 2 and wire_type == 0:
            ret = decode_int32(int(value))
        elif field_number == 3 and wire_type == 2:
            message = bytes(value).decode("utf-8", errors="replace")
    return DungeonLampClaimResponse(
        ret=ret,
        shop_id=shop_id,
        message=message,
        claimed_qty=0,
    )


class DungeonSweepClient:
    """地下城查询、扫荡与全部抽取会话；可注入共享 GameSession。"""

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
            error_cls=DungeonSweepError,
            task="dungeon_sweep",
            websocket_log=websocket_log,
        )
        self._status: DungeonStatus | None = None
        self._game_data: bytes | None = None

    def __enter__(self) -> "DungeonSweepClient":
        self.login()
        return self

    def __exit__(self, _exc_type: object, _exc: object, _traceback: object) -> None:
        self.close()

    def close(self) -> None:
        from game_session import shared_close

        if not shared_close(self):
            return
        if self.socket is not None:
            self.socket.close()
            self.socket = None

    def _send_message(
        self, message_id: int, data: bytes = b"", *, encrypted: bool
    ) -> None:
        from game_session import session_send_message

        if session_send_message(self, message_id, data, encrypted=encrypted):
            return
        if self.socket is None:
            raise DungeonSweepError("WebSocket 尚未连接")
        packet = encode_message_header(message_id, data)
        if encrypted:
            if not self.password:
                raise DungeonSweepError("游戏服尚未下发会话密码")
            self.socket.send_text(pack1_encode(packet, self.password))
        else:
            self.socket.send_binary(packet)

    def _decode_frame(self, opcode: int, payload: bytes) -> MessageHeader:
        if self.password is not None:
            if opcode not in (0x1, 0x2):
                raise DungeonSweepError(f"加密游戏报文 opcode 异常：{opcode}")
            payload = pack1_decode(payload, self.password)
        return decode_message_header(payload)

    def _receive_header(
        self, deadline: float, context: str, *, allow_timeout: bool = False
    ) -> MessageHeader | None:
        from game_session import try_session_receive_header

        handled, header = try_session_receive_header(
            self, deadline, context, allow_timeout=allow_timeout
        )
        if handled:
            return header
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            if allow_timeout:
                return None
            raise DungeonSweepError(f"等待{context}超时")
        try:
            assert self.socket is not None
            opcode, payload = self.socket.recv_message(remaining)
        except socket.timeout as exc:
            if allow_timeout:
                return None
            raise DungeonSweepError(f"等待{context}超时") from exc
        except OSError as exc:
            raise DungeonSweepError(f"读取{context}报文失败：{exc}") from exc
        try:
            return self._decode_frame(opcode, payload)
        except HarvestError as exc:
            raise DungeonSweepError(f"解析{context}报文失败：{exc}") from exc

    def _handle_common_message(self, header: MessageHeader) -> bool:
        if header.message_id == HEARTBEAT_MESSAGE_ID:
            self._send_message(
                HEARTBEAT_RET_MESSAGE_ID,
                encrypted=self.password is not None,
            )
            return True
        if header.message_id == LOGIN_FAIL_MESSAGE_ID:
            raise DungeonSweepError("游戏服 Login 失败")
        if header.message_id == KICKOUT_MESSAGE_ID:
            ret = 0
            for field_number, wire_type, value in ProtoReader(header.data).fields():
                if field_number == 1 and wire_type == 0:
                    ret = decode_int32(int(value))
                    break
            raise DungeonSweepError(f"游戏服终止地下城会话：ret={ret}")
        return False

    def login(self) -> DungeonStatus:
        from game_session import try_session_ensure_ready

        if try_session_ensure_ready(self, self.endpoint):
            game_data = getattr(self._session, "game_data", None)
            if not game_data:
                raise DungeonSweepError("地下城会话缺少 Game_data")
            self._game_data = bytes(game_data)
            self._status = decode_game_data_dungeon_status(self._game_data)
            return self._status

        if self.socket is not None:
            if self._status is None:
                raise DungeonSweepError("地下城会话缺少 Game_data")
            return self._status

        self.socket = self.socket_factory(self.endpoint.url, self.timeout)
        try:
            self._send_message(
                LOGIN_MESSAGE_ID,
                encode_login_payload(self.endpoint.game_token),
                encrypted=False,
            )
            login_complete = False
            status: DungeonStatus | None = None
            deadline = time.monotonic() + self.timeout
            while not (login_complete and status is not None):
                header = self._receive_header(deadline, "游戏服登录及 Game_data")
                assert header is not None
                if header.message_id == PACK_PASSWORD_MESSAGE_ID:
                    encrypted_password = decode_pack_password(header.data)
                    try:
                        self.password = pack1_decode(
                            encrypted_password, SOCKET_PACK_KEY
                        ).decode("utf-8")
                    except UnicodeDecodeError as exc:
                        raise DungeonSweepError(
                            "游戏服会话密码不是 UTF-8 文本"
                        ) from exc
                    continue
                if self._handle_common_message(header):
                    continue
                if header.message_id == GAME_DATA_MESSAGE_ID:
                    self._game_data = bytes(header.data)
                    status = decode_game_data_dungeon_status(self._game_data)
                elif header.message_id == LOGIN_REUNIQUE_MESSAGE_ID:
                    login_complete = True
            self._status = status
            # 与客户端 SocketManager 的缓存业务消息调度顺序保持一致。
            time.sleep(0.1)
            return status
        except Exception:
            self.close()
            raise

    def get_status(self) -> DungeonStatus:
        return self.login()

    def claim_daily_lamp(self) -> DungeonLampClaimResponse | None:
        """Claim the available direct-shop lamp before entering the dungeon."""

        self.login()
        game_data = self._game_data
        if game_data is None:
            game_data = getattr(self._session, "game_data", None)
        if not game_data:
            raise DungeonSweepError("地下城会话缺少直购商店状态")
        offer = find_dungeon_lamp_offer(bytes(game_data))
        if offer is None:
            return None

        self._send_message(
            SHOP_BUY_MESSAGE_ID,
            encode_dungeon_lamp_claim_request(offer.stock_qty, offer.goods_data),
            encrypted=True,
        )
        deadline = time.monotonic() + self.timeout
        while True:
            header = self._receive_header(deadline, "地下城永焰之灯领取响应")
            assert header is not None
            if self._handle_common_message(header):
                continue
            if header.message_id != SHOP_BUY_MESSAGE_ID:
                continue
            response = decode_dungeon_lamp_claim_response(header.data)
            if response.ret != 0:
                raise DungeonLampClaimRejected(response.ret, response.message)
            return replace(response, claimed_qty=offer.stock_qty)

    def sweep(self, dungeon_id: int) -> DungeonSweepResponse:
        self.login()
        self._send_message(
            DUN_SWEEP_MESSAGE_ID,
            encode_dungeon_sweep_request(dungeon_id),
            encrypted=True,
        )
        deadline = time.monotonic() + self.timeout
        while True:
            header = self._receive_header(deadline, "Dun_sweep 响应")
            assert header is not None
            if self._handle_common_message(header):
                continue
            if header.message_id == DUN_SWEEP_MESSAGE_ID:
                response = decode_dungeon_sweep_response(header.data)
                if response.ret != 0:
                    raise DungeonSweepRejected(response.ret)
                return response

    def draw_all(self, dungeon_id: int) -> DungeonDrawResponse:
        self.login()
        self._send_message(
            DUN_START_DRAW_MESSAGE_ID,
            encode_dungeon_draw_all_request(dungeon_id),
            encrypted=True,
        )
        deadline = time.monotonic() + self.timeout
        reward_deadline: float | None = None
        response: DungeonDrawResponse | None = None
        item_changes: list[ItemChange] = []
        reward_props: list[RewardProp] = []
        reward_notice_received = False

        def completed_response() -> DungeonDrawResponse:
            assert response is not None
            return replace(
                response,
                item_changes=tuple(item_changes),
                reward_props=tuple(reward_props),
                reward_notice_received=reward_notice_received,
            )

        while True:
            current_deadline = reward_deadline or deadline
            try:
                header = self._receive_header(
                    current_deadline,
                    "Dun_start_draw 响应",
                    allow_timeout=response is not None,
                )
            except HarvestError:
                # A successful Dun_start_draw response is the committed draw result.
                # Some game servers close the short-lived socket before the optional
                # item-change notification arrives, so retain the response IDs as
                # a displayable fallback instead of reporting the draw as failed.
                if response is not None:
                    return completed_response()
                raise
            if header is None:
                return completed_response()
            if self._handle_common_message(header):
                continue
            if header.message_id == DUN_START_DRAW_MESSAGE_ID:
                response = decode_dungeon_draw_response(header.data)
                if response.ret != 0:
                    raise DungeonDrawRejected(response.ret)
                if response.dungeon_id != dungeon_id:
                    raise DungeonSweepError("地下城全部抽取响应的地下城 ID 不匹配")
                reward_deadline = min(
                    deadline, time.monotonic() + REWARD_NOTIFICATION_GRACE_SECONDS
                )
                continue
            if header.message_id == STORAGE_ITEM_CHANGE_MESSAGE_ID:
                notice = decode_item_change_notify(header.data)
                if notice.source != DUN_DRAW_ITEM_CHANGE_SOURCE:
                    continue
                item_changes.extend(notice.items)
                reward_props.extend(notice.props)
                reward_notice_received = True
                if response is not None:
                    reward_deadline = min(
                        deadline,
                        time.monotonic() + REWARD_NOTIFICATION_GRACE_SECONDS,
                    )
