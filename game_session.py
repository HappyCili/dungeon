#!/usr/bin/env python3
"""Process-wide game-server WebSocket session (aligns with native SocketManager).

Native client keeps a single ``SocketManager.instance`` connection for all
features.  This module provides the same lifecycle for the local console:

* one transport + session password per zone endpoint
* Login → Pack_password → Game_data → Login_reunique (``ready``)
* feature clients send business messages without owning connect/close
* close on logout / zone change / fatal Kickout, not after each task
"""

from __future__ import annotations

from contextlib import contextmanager
import socket
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Iterator, Protocol

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
    decode_int32,
    decode_message_header,
    decode_pack_password,
    encode_login_payload,
    encode_message_header,
    pack1_decode,
    pack1_encode,
)

# Same id as daily_quest / dragon_arena; defined here to avoid import cycles.
GAME_DATA_MESSAGE_ID = 10490
KICKOUT_MESSAGE_ID = 10030
LOGIN_OK_MESSAGE_ID = 10012

# Login-time pushes that represent an unfinished server-side interaction.  These
# are deliberately kept in the session layer instead of being owned by the
# feature that happens to connect next.
BATTLE_INFO_MESSAGE_ID = 18002
BATTLE_UNIT_INFO_MESSAGE_ID = 18004
BATTLE_OFFLINE_MESSAGE_ID = 18006
BATTLE_S2C_START_MESSAGE_ID = 18012
BATTLE_S2C_END_MESSAGE_ID = 18090
BATTLE_S2C_FRAME_BROADCAST_MESSAGE_ID = 18500
BATTLE_S2C_FRAME_HASH_VERIFY_MESSAGE_ID = 18502
EVENT_START_MESSAGE_ID = 13300
EVENT_OPTION_FAILED_MESSAGE_ID = 13310
EVENT_END_MESSAGE_ID = 13315
EVENT_FUNC_ACTION_MESSAGE_ID = 13320

BATTLE_RECOVERY_OPEN_MESSAGE_IDS = frozenset(
    {
        BATTLE_INFO_MESSAGE_ID,
        BATTLE_UNIT_INFO_MESSAGE_ID,
        BATTLE_OFFLINE_MESSAGE_ID,
        BATTLE_S2C_START_MESSAGE_ID,
        BATTLE_S2C_FRAME_BROADCAST_MESSAGE_ID,
        BATTLE_S2C_FRAME_HASH_VERIFY_MESSAGE_ID,
    }
)
EVENT_RECOVERY_OPEN_MESSAGE_IDS = frozenset(
    {
        EVENT_START_MESSAGE_ID,
        EVENT_OPTION_FAILED_MESSAGE_ID,
        EVENT_FUNC_ACTION_MESSAGE_ID,
    }
)

# Delay after Login_reunique before business traffic (native throwCachedMsg).
POST_LOGIN_BUSINESS_DELAY_SECONDS = 0.1
# Drain packets that the server already pushed during the native delay.  This is
# intentionally short: feature-specific recovery may still issue its own probe
# and wait for additional packets.
POST_LOGIN_RECOVERY_DRAIN_SECONDS = 0.05

SocketFactory = Callable[[str, float], "WebSocketTransport"]


class WebSocketTransport(Protocol):
    def send_binary(self, payload: bytes) -> None: ...

    def send_text(self, payload: str) -> None: ...

    def recv_message(self, timeout: float) -> tuple[int, bytes]: ...

    def close(self) -> None: ...


class GameSessionError(HarvestError):
    """Shared session protocol or lifecycle failure."""


class GameSessionKickout(GameSessionError):
    """Server terminated the session (Kickout)."""

    def __init__(self, ret: int, message: str = "") -> None:
        self.ret = ret
        self.message = message
        detail = f"，消息={message}" if message else ""
        super().__init__(f"游戏服终止会话：ret={ret}{detail}")


@dataclass(frozen=True)
class SessionRecoveryIssue:
    """One unfinished server-side state observed while establishing a session."""

    kind: str
    message_ids: tuple[int, ...] = ()
    battle_state: int = 0
    battle_type: int = 0

    @property
    def label(self) -> str:
        if self.kind == "battle":
            return "遗留战斗"
        if self.kind == "event":
            return "遗留地图事件/对话"
        return self.kind


@dataclass(frozen=True)
class SessionRecoverySnapshot:
    """Server-authoritative residual state retained from the login epoch."""

    generation: int
    game_data_present: bool
    issues: tuple[SessionRecoveryIssue, ...]
    observed_message_ids: tuple[int, ...] = ()

    @property
    def pending(self) -> bool:
        return bool(self.issues)

    @property
    def message_ids(self) -> tuple[int, ...]:
        seen: list[int] = []
        for issue in self.issues:
            for message_id in issue.message_ids:
                if message_id not in seen:
                    seen.append(message_id)
        return tuple(seen)

    def describe(self) -> str:
        if not self.issues:
            return "空闲"
        parts: list[str] = []
        for issue in self.issues:
            suffix = ""
            if issue.kind == "battle" and issue.battle_state:
                suffix = (
                    f"（battleState={issue.battle_state}，"
                    f"battleType={issue.battle_type}）"
                )
            elif issue.message_ids:
                suffix = "（消息=" + "、".join(str(mid) for mid in issue.message_ids) + "）"
            parts.append(issue.label + suffix)
        return "；".join(parts)


class GameSessionRecoveryRequired(GameSessionError):
    """A new task attempted to use a session before residual state was settled."""

    def __init__(self, snapshot: SessionRecoverySnapshot) -> None:
        self.snapshot = snapshot
        super().__init__(
            "游戏服存在未恢复状态："
            f"{snapshot.describe()}；请先完成会话恢复后再执行新任务"
        )


def _decode_kickout(data: bytes) -> tuple[int, str]:
    ret = 0
    message = ""
    for field_number, wire_type, value in ProtoReader(data).fields():
        if field_number == 1 and wire_type == 0:
            ret = decode_int32(int(value))
        elif field_number == 2 and wire_type == 2:
            message = bytes(value).decode("utf-8", errors="replace")
    return ret, message


def _decode_login_battle_marker(data: bytes) -> tuple[int, int]:
    """Return ``Game_data.field35.{battleState,battleType}`` when present.

    This compact decode belongs in the shared layer because it is only a login
    recovery marker.  Feature clients remain responsible for interpreting a
    concrete battle type and sending any game-specific follow-up requests.
    """

    try:
        battle_state = 0
        battle_type = 0
        for field_number, wire_type, value in ProtoReader(data).fields():
            if field_number != 35 or wire_type != 2:
                continue
            for battle_field, battle_wire, battle_value in ProtoReader(bytes(value)).fields():
                if battle_wire != 0:
                    continue
                if battle_field == 3:
                    battle_state = decode_int32(int(battle_value))
                elif battle_field == 4:
                    battle_type = decode_int32(int(battle_value))
            break
        return battle_state, battle_type
    except HarvestError:
        # The snapshot contains many independently evolving tables.  A malformed
        # or newer optional field must not make the shared transport unusable;
        # feature handlers can still recover from concrete pushed packets.
        return 0, 0


def _endpoint_key(endpoint: GameEndpoint) -> tuple[str, str, str]:
    return (str(endpoint.zone_id), str(endpoint.url), str(endpoint.game_token))


@dataclass
class GameSession:
    """One game-server WebSocket after Login_reunique (msgReady)."""

    timeout: float
    socket_factory: SocketFactory = NativeWebSocket.connect
    # When True, close() destroys the transport (CLI one-shot). Shared manager
    # sessions keep owned=True at the session level; clients must not close them.
    owned: bool = True
    # Raw frame log under logs/websocket_raw/game_session/ (UI shared path).
    # True → dated default path; Path → that file; False/None → disabled.
    websocket_log: Any = True

    def __post_init__(self) -> None:
        self._lock = threading.RLock()
        self._transport: WebSocketTransport | None = None
        self._password: str | None = None
        self._ready = False
        self._endpoint: GameEndpoint | None = None
        self._endpoint_key: tuple[str, str, str] | None = None
        self._game_data: bytes | None = None
        self._queued: list[MessageHeader] = []
        self._traffic_logger: Any = None
        self._ws_session_id = str(time.time_ns())
        self._recovery_generation = 0
        self._recovery_battle_state = 0
        self._recovery_battle_type = 0
        self._recovery_battle_message_ids: list[int] = []
        self._recovery_event_message_ids: list[int] = []
        self._recovery_observed_message_ids: list[int] = []
        self._recovery_scope_depth = 0
        self._recovery_checked_generation = -1

    @property
    def ready(self) -> bool:
        return self._ready

    @property
    def password(self) -> str | None:
        return self._password

    @property
    def game_data(self) -> bytes | None:
        return self._game_data

    @property
    def endpoint(self) -> GameEndpoint | None:
        return self._endpoint

    @property
    def transport(self) -> WebSocketTransport | None:
        return self._transport

    @property
    def recovery_snapshot(self) -> SessionRecoverySnapshot:
        """Return the residual state observed for the current login epoch."""

        with self._lock:
            return self._recovery_snapshot_unlocked()

    @property
    def recovery_pending(self) -> bool:
        return self.recovery_snapshot.pending

    @property
    def recovery_checked(self) -> bool:
        with self._lock:
            return self._recovery_checked_generation == self._recovery_generation

    @contextmanager
    def recovery_scope(self) -> Iterator["GameSession"]:
        """Allow a registered recovery handler to send its continuation packets.

        Normal feature requests are held while the login snapshot contains an
        unfinished battle or event.  A recovery handler enters this scope after
        it has been selected by the coordinator; heartbeats remain handled by
        the session itself.
        """

        with self._lock:
            self._recovery_scope_depth += 1
        try:
            yield self
        finally:
            with self._lock:
                self._recovery_scope_depth -= 1

    def resolve_recovery_issue(self, kind: str) -> None:
        """Mark a handler-verified recovery branch complete.

        This is only for states whose terminal acknowledgement is represented
        outside the common battle/event message set.  Handlers normally rely on
        ``Battle_S2C_end`` or ``Event_end`` to clear the state automatically.
        """

        with self._lock:
            if self._recovery_scope_depth <= 0:
                raise GameSessionError("只能在会话恢复处理器中确认恢复状态")
            if kind == "battle":
                self._clear_battle_recovery_unlocked()
            elif kind == "event":
                self._recovery_event_message_ids.clear()
            else:
                raise ValueError(f"未知恢复状态类型：{kind}")

    def collect_recovery_messages(self) -> SessionRecoverySnapshot:
        """Drain packets already available after login into the recovery snapshot.

        A server may emit recovery packets just after ``Login_reunique``.  The
        coordinator calls this inside ``recovery_scope`` before choosing a
        handler; ordinary task code should never need to poll the transport as
        a state probe.
        """

        with self._lock:
            if not self._ready:
                raise GameSessionError("游戏服会话尚未就绪（msgReady=false）")
            self._drain_login_pushes_unlocked()
            return self._recovery_snapshot_unlocked()

    def mark_recovery_checked(self) -> None:
        """Record that the coordinator settled this login generation."""

        with self._lock:
            snapshot = self._recovery_snapshot_unlocked()
            if snapshot.pending:
                raise GameSessionRecoveryRequired(snapshot)
            self._recovery_checked_generation = self._recovery_generation

    def _reset_recovery_state_unlocked(self) -> None:
        self._recovery_generation += 1
        self._recovery_checked_generation = -1
        self._recovery_battle_state = 0
        self._recovery_battle_type = 0
        self._recovery_battle_message_ids.clear()
        self._recovery_event_message_ids.clear()
        self._recovery_observed_message_ids.clear()

    @staticmethod
    def _remember_message_id(target: list[int], message_id: int) -> None:
        if message_id not in target:
            target.append(message_id)

    def _remember_game_data_unlocked(self, data: bytes) -> None:
        self._game_data = data
        battle_state, battle_type = _decode_login_battle_marker(data)
        self._recovery_battle_state = battle_state
        self._recovery_battle_type = battle_type

    def _clear_battle_recovery_unlocked(self) -> None:
        self._recovery_battle_state = 0
        self._recovery_battle_type = 0
        self._recovery_battle_message_ids.clear()

    def _observe_recovery_header_unlocked(self, header: MessageHeader) -> None:
        """Reduce one login/recovery packet into the shared residual snapshot."""

        message_id = header.message_id
        self._remember_message_id(self._recovery_observed_message_ids, message_id)
        if message_id in BATTLE_RECOVERY_OPEN_MESSAGE_IDS:
            self._remember_message_id(self._recovery_battle_message_ids, message_id)
        elif message_id == BATTLE_S2C_END_MESSAGE_ID:
            self._clear_battle_recovery_unlocked()

        if message_id in EVENT_RECOVERY_OPEN_MESSAGE_IDS:
            self._remember_message_id(self._recovery_event_message_ids, message_id)
        elif message_id == EVENT_END_MESSAGE_ID:
            self._recovery_event_message_ids.clear()

    def _recovery_snapshot_unlocked(self) -> SessionRecoverySnapshot:
        issues: list[SessionRecoveryIssue] = []
        if self._recovery_battle_message_ids or self._recovery_battle_state:
            issues.append(
                SessionRecoveryIssue(
                    kind="battle",
                    message_ids=tuple(self._recovery_battle_message_ids),
                    battle_state=self._recovery_battle_state,
                    battle_type=self._recovery_battle_type,
                )
            )
        if self._recovery_event_message_ids:
            issues.append(
                SessionRecoveryIssue(
                    kind="event",
                    message_ids=tuple(self._recovery_event_message_ids),
                )
            )
        return SessionRecoverySnapshot(
            generation=self._recovery_generation,
            game_data_present=self._game_data is not None,
            issues=tuple(issues),
            observed_message_ids=tuple(self._recovery_observed_message_ids),
        )

    def _queue_login_header_unlocked(self, header: MessageHeader) -> None:
        """Preserve all business pushes until a recovery handler consumes them."""

        self._queued.append(header)
        self._observe_recovery_header_unlocked(header)

    def ensure_ready(self, endpoint: GameEndpoint) -> None:
        """Connect and login if needed; reuse when endpoint identity matches."""

        with self._lock:
            key = _endpoint_key(endpoint)
            if self._ready and self._transport is not None and self._endpoint_key == key:
                return
            self._connect_and_login(endpoint)

    def ensure_recovered(self, endpoint: GameEndpoint, *, coordinator: Any | None = None) -> None:
        """Connect, then settle the current login epoch before feature work.

        ``GameSessionManager`` is the normal entry point, but some command-line
        callers bind a shared session directly to a feature client.  Keeping the
        recovery hook on the session makes both paths obey the same barrier.
        Nested feature clients created by a recovery handler only need the
        connection to remain ready, so they do not recursively start another
        coordinator pass.
        """

        self.ensure_ready(endpoint)
        with self._lock:
            if (
                self._recovery_checked_generation == self._recovery_generation
                or self._recovery_scope_depth > 0
            ):
                return

        if coordinator is None:
            # Keep the transport layer importable without feature clients until
            # a direct shared-session caller actually needs recovery.
            from session_recovery import build_default_recovery_coordinator

            coordinator = build_default_recovery_coordinator()
        coordinator.recover(self, endpoint)

    def _ensure_traffic_logger(self) -> None:
        """Open shared-session raw WS log (separate from per-client CLI logs)."""

        if self._traffic_logger is not None:
            return
        if self.websocket_log is False or self.websocket_log is None:
            return
        from ws_traffic_log import WebSocketTrafficLogger, resolve_ws_log_path

        path = resolve_ws_log_path("game_session", self.websocket_log)
        if path is None:
            return
        self._traffic_logger = WebSocketTrafficLogger(
            path=path,
            session_id=self._ws_session_id,
            task="game_session",
        )

    def _log_traffic_frame(
        self,
        *,
        direction: str,
        opcode: int,
        encrypted: bool,
        wire_payload: bytes,
        decoded_packet: bytes | None,
        header: MessageHeader | None,
        decode_error: str | None = None,
    ) -> None:
        traffic = self._traffic_logger
        if traffic is None:
            return
        traffic.write_frame(
            direction=direction,
            opcode=opcode,
            encrypted=encrypted,
            wire_payload=wire_payload,
            decoded_packet=decoded_packet,
            header=header,
            decode_error=decode_error,
        )

    def _connect_and_login(self, endpoint: GameEndpoint) -> None:
        # Match SocketManager.connect: destroy existing before opening a new one.
        self._teardown_transport()
        self._password = None
        self._ready = False
        self._game_data = None
        self._queued.clear()
        self._reset_recovery_state_unlocked()
        self._endpoint = endpoint
        self._endpoint_key = _endpoint_key(endpoint)
        self._ensure_traffic_logger()

        try:
            self._transport = self.socket_factory(endpoint.url, self.timeout)
            self._send_message_unlocked(
                LOGIN_MESSAGE_ID,
                encode_login_payload(endpoint.game_token),
                encrypted=False,
            )
            login_complete = False
            deferred_login_headers: list[MessageHeader] = []
            deadline = time.monotonic() + self.timeout
            while not (login_complete and self._password is not None):
                header = self._receive_header_unlocked(
                    deadline, "游戏服登录", allow_before_ready=True
                )
                if header.message_id == PACK_PASSWORD_MESSAGE_ID:
                    encrypted_password = decode_pack_password(header.data)
                    try:
                        self._password = pack1_decode(
                            encrypted_password, SOCKET_PACK_KEY
                        ).decode("utf-8")
                    except UnicodeDecodeError as exc:
                        raise GameSessionError("游戏服会话密码不是 UTF-8 文本") from exc
                    continue
                if header.message_id == GAME_DATA_MESSAGE_ID:
                    self._remember_game_data_unlocked(header.data)
                    continue
                if header.message_id == LOGIN_REUNIQUE_MESSAGE_ID:
                    login_complete = True
                    continue
                if header.message_id == LOGIN_OK_MESSAGE_ID:
                    continue
                # The native client dispatches these after Login_reunique.  Do
                # not discard them: a prior process can have left a battle,
                # dialog, reward confirmation, or another feature state here.
                deferred_login_headers.append(header)
                self._observe_recovery_header_unlocked(header)

            # Game_data often arrives before reunique; if late, drain briefly.
            if self._game_data is None:
                extra_deadline = time.monotonic() + min(2.0, max(self.timeout, 0.1))
                while self._game_data is None and time.monotonic() < extra_deadline:
                    try:
                        header = self._receive_header_unlocked(
                            extra_deadline, "Game_data", allow_before_ready=True
                        )
                    except GameSessionError:
                        break
                    if header.message_id == GAME_DATA_MESSAGE_ID:
                        self._remember_game_data_unlocked(header.data)
                    else:
                        deferred_login_headers.append(header)
                        self._observe_recovery_header_unlocked(header)

            self._queued.extend(deferred_login_headers)
            self._ready = True
            # Native throwCachedMsg dispatches after 100 ms.
            time.sleep(POST_LOGIN_BUSINESS_DELAY_SECONDS)
        except Exception:
            self._teardown_transport()
            self._password = None
            self._ready = False
            self._game_data = None
            raise

    def _drain_login_pushes_unlocked(self) -> None:
        """Capture messages already waiting after ``Login_reunique``.

        The drain never consumes the pre-existing queue; it only performs a
        short receive window against the transport after the native client's
        normal cached-message delay.
        """

        if self._transport is None or POST_LOGIN_RECOVERY_DRAIN_SECONDS <= 0:
            return
        queued = self._queued
        self._queued = []
        try:
            deadline = time.monotonic() + POST_LOGIN_RECOVERY_DRAIN_SECONDS
            while time.monotonic() < deadline:
                try:
                    header = self._receive_header_unlocked(
                        deadline,
                        "登录后恢复消息",
                    )
                except GameSessionError as exc:
                    if "超时" in str(exc):
                        break
                    raise
                if header.message_id == GAME_DATA_MESSAGE_ID:
                    self._remember_game_data_unlocked(header.data)
                elif header.message_id not in {
                    LOGIN_OK_MESSAGE_ID,
                    LOGIN_REUNIQUE_MESSAGE_ID,
                }:
                    self._queue_login_header_unlocked(header)
        finally:
            # Existing packets always precede packets received in this drain.
            self._queued[0:0] = queued

    def send_message(
        self, message_id: int, data: bytes = b"", *, encrypted: bool = True
    ) -> None:
        with self._lock:
            if encrypted and not self._ready:
                raise GameSessionError("游戏服会话尚未就绪（msgReady=false）")
            if encrypted and self._recovery_scope_depth == 0:
                snapshot = self._recovery_snapshot_unlocked()
                if snapshot.pending:
                    raise GameSessionRecoveryRequired(snapshot)
            self._send_message_unlocked(message_id, data, encrypted=encrypted)

    def _send_message_unlocked(
        self, message_id: int, data: bytes = b"", *, encrypted: bool
    ) -> None:
        if self._transport is None:
            raise GameSessionError("WebSocket 尚未连接")
        packet = encode_message_header(message_id, data)
        if encrypted:
            if not self._password:
                raise GameSessionError("游戏服尚未下发会话密码")
            wire_text = pack1_encode(packet, self._password)
            self._transport.send_text(wire_text)
            self._log_traffic_frame(
                direction="outbound",
                opcode=0x1,
                encrypted=True,
                wire_payload=wire_text.encode("utf-8"),
                decoded_packet=packet,
                header=MessageHeader(message_id=message_id, sid=0, data=data),
            )
        else:
            self._transport.send_binary(packet)
            self._log_traffic_frame(
                direction="outbound",
                opcode=0x2,
                encrypted=False,
                wire_payload=packet,
                decoded_packet=packet,
                header=MessageHeader(message_id=message_id, sid=0, data=data),
            )

    def receive_header(self, timeout: float) -> MessageHeader:
        with self._lock:
            deadline = time.monotonic() + max(timeout, 0.0)
            return self._receive_header_unlocked(deadline, "游戏服报文")

    def wait_for(
        self,
        message_ids: set[int],
        timeout: float,
        *,
        context: str,
    ) -> MessageHeader:
        """Read until one of ``message_ids``; auto-answer heartbeats."""

        if not message_ids:
            raise ValueError("message_ids 不能为空")
        with self._lock:
            deadline = time.monotonic() + max(timeout, 0.0)
            while True:
                header = self._receive_header_unlocked(deadline, context)
                if header.message_id in message_ids:
                    return header

    def _receive_header_unlocked(
        self,
        deadline: float,
        context: str,
        *,
        allow_before_ready: bool = False,
    ) -> MessageHeader:
        if self._queued:
            return self._queued.pop(0)

        if not allow_before_ready and not self._ready:
            raise GameSessionError("游戏服会话尚未就绪（msgReady=false）")
        if self._transport is None:
            raise GameSessionError("WebSocket 尚未连接")

        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise GameSessionError(f"等待{context}超时")
            try:
                opcode, payload = self._transport.recv_message(remaining)
            except socket.timeout as exc:
                raise GameSessionError(f"等待{context}超时") from exc
            except OSError as exc:
                self.invalidate()
                raise GameSessionError(f"读取{context}报文失败：{exc}") from exc

            try:
                header, decoded_packet = self._decode_frame_with_packet(opcode, payload)
            except Exception as exc:
                self._log_traffic_frame(
                    direction="inbound",
                    opcode=opcode,
                    encrypted=self._password is not None,
                    wire_payload=payload,
                    decoded_packet=None,
                    header=None,
                    decode_error=f"{type(exc).__name__}: {exc}",
                )
                self.invalidate()
                raise

            self._log_traffic_frame(
                direction="inbound",
                opcode=opcode,
                encrypted=self._password is not None,
                wire_payload=payload,
                decoded_packet=decoded_packet,
                header=header,
            )

            if header.message_id == HEARTBEAT_MESSAGE_ID:
                self._send_message_unlocked(
                    HEARTBEAT_RET_MESSAGE_ID,
                    b"",
                    encrypted=self._password is not None,
                )
                continue
            if header.message_id == HEARTBEAT_RET_MESSAGE_ID:
                continue
            if header.message_id == LOGIN_FAIL_MESSAGE_ID:
                self.invalidate()
                raise GameSessionError("游戏服 Login 失败")
            if header.message_id == KICKOUT_MESSAGE_ID:
                ret, message = _decode_kickout(header.data)
                self.invalidate()
                raise GameSessionKickout(ret, message)
            if self._recovery_scope_depth > 0:
                if header.message_id == GAME_DATA_MESSAGE_ID:
                    self._remember_game_data_unlocked(header.data)
                else:
                    self._observe_recovery_header_unlocked(header)
            return header

    def _decode_frame(self, opcode: int, payload: bytes) -> MessageHeader:
        header, _packet = self._decode_frame_with_packet(opcode, payload)
        return header

    def _decode_frame_with_packet(
        self, opcode: int, payload: bytes
    ) -> tuple[MessageHeader, bytes]:
        packet = payload
        if self._password is not None:
            if opcode not in (0x1, 0x2):
                raise GameSessionError(f"加密游戏报文 opcode 异常：{opcode}")
            packet = pack1_decode(payload, self._password)
        return decode_message_header(packet), packet

    def push_header(self, header: MessageHeader) -> None:
        """Re-queue a header already read by a feature client."""

        with self._lock:
            self._queued.append(header)
            if self._recovery_scope_depth > 0:
                self._observe_recovery_header_unlocked(header)

    def push_headers(self, headers: Iterable[MessageHeader]) -> None:
        """Restore a batch ahead of later queued traffic, preserving its order."""

        pending = list(headers)
        if not pending:
            return
        with self._lock:
            self._queued[0:0] = pending
            if self._recovery_scope_depth > 0:
                for header in pending:
                    self._observe_recovery_header_unlocked(header)

    def close(self) -> None:
        """Destroy the transport (SocketManager.destroy / logout)."""

        with self._lock:
            self._teardown_transport()
            self._password = None
            self._ready = False
            self._game_data = None
            self._endpoint = None
            self._endpoint_key = None
            self._queued.clear()
            self._reset_recovery_state_unlocked()
            self._close_traffic_logger()

    def invalidate(self) -> None:
        """Mark dead without raising (Kickout / IO failure)."""

        with self._lock:
            self._teardown_transport()
            self._password = None
            self._ready = False
            # Keep endpoint_key cleared so ensure_ready reconnects.
            self._endpoint_key = None
            self._queued.clear()
            self._reset_recovery_state_unlocked()

    def _close_traffic_logger(self) -> None:
        traffic = self._traffic_logger
        self._traffic_logger = None
        if traffic is None:
            return
        try:
            traffic.close()
        except OSError:
            pass

    def _teardown_transport(self) -> None:
        transport = self._transport
        self._transport = None
        if transport is None:
            return
        try:
            transport.close()
        except OSError:
            pass


class GameSessionManager:
    """Thread-safe holder for the process-wide shared session."""

    def __init__(
        self,
        *,
        timeout: float = 15.0,
        socket_factory: SocketFactory = NativeWebSocket.connect,
        websocket_log: Any = True,
        recovery_coordinator: Any | None = None,
    ) -> None:
        self._timeout = timeout
        self._socket_factory = socket_factory
        self._websocket_log = websocket_log
        self._recovery_coordinator = recovery_coordinator
        self._lock = threading.RLock()
        self._session: GameSession | None = None

    @property
    def timeout(self) -> float:
        return self._timeout

    def session_for(self, endpoint: GameEndpoint) -> GameSession:
        """Return a ready shared session for ``endpoint`` (reconnect if needed)."""

        with self._lock:
            if self._session is None:
                self._session = GameSession(
                    timeout=self._timeout,
                    socket_factory=self._socket_factory,
                    owned=True,
                    websocket_log=self._websocket_log,
                )
            self._session.ensure_ready(endpoint)
            if not self._session.recovery_checked:
                self._recover_session_unlocked(self._session, endpoint)
            return self._session

    def session_for_snapshot(self, endpoint: GameEndpoint) -> GameSession:
        """Return a ready session for a read-only server snapshot.

        Dashboard refreshes need to remain responsive when the login stream
        reports a residual battle or dialog.  They may inspect the
        server-provided ``Game_data`` snapshot, but must not start a potentially
        long feature recovery as a side effect.  Task execution continues to
        use :meth:`session_for`, which retains the recovery barrier.
        """

        with self._lock:
            if self._session is None:
                self._session = GameSession(
                    timeout=self._timeout,
                    socket_factory=self._socket_factory,
                    owned=True,
                    websocket_log=self._websocket_log,
                )
            self._session.ensure_ready(endpoint)
            # Capture post-login pushes before deciding whether the snapshot is
            # safe to supplement with a normal business query.
            self._session.collect_recovery_messages()
            return self._session

    def _recover_session_unlocked(
        self,
        session: GameSession,
        endpoint: GameEndpoint,
    ) -> None:
        coordinator = self._recovery_coordinator
        if coordinator is None:
            # Import lazily so the low-level transport remains usable by CLI
            # scripts and tests without feature modules at import time.
            from session_recovery import build_default_recovery_coordinator

            coordinator = build_default_recovery_coordinator()
            self._recovery_coordinator = coordinator
        coordinator.recover(session, endpoint)

    def get_if_ready(self) -> GameSession | None:
        with self._lock:
            if self._session is not None and self._session.ready:
                return self._session
            return None

    def close(self) -> None:
        with self._lock:
            if self._session is not None:
                self._session.close()
                self._session = None

    def invalidate(self) -> None:
        with self._lock:
            if self._session is not None:
                self._session.invalidate()


def bind_shared_session(
    client: Any,
    session: GameSession | None,
    *,
    error_cls: type[Exception],
    task: str,
    websocket_log: Any = True,
    traffic: bool = True,
) -> None:
    """Attach optional shared session fields used by feature clients."""

    client._session = session
    client._owns_connection = session is None
    client._session_error_cls = error_cls
    if session is None and traffic:
        from ws_traffic_log import bind_traffic_logging

        bind_traffic_logging(
            client,
            task=task,
            path=websocket_log,
            error_cls=error_cls,
        )


def session_send_message(
    client: Any,
    message_id: int,
    data: bytes = b"",
    *,
    encrypted: bool = True,
) -> bool:
    """Send via shared session when bound. Returns True if handled."""

    session = getattr(client, "_session", None)
    if session is None:
        return False
    error_cls = getattr(client, "_session_error_cls", HarvestError)
    try:
        session.send_message(message_id, data, encrypted=encrypted)
    except GameSessionKickout:
        raise
    except Exception as exc:
        if isinstance(exc, error_cls):
            raise
        raise error_cls(str(exc)) from exc
    client.password = session.password
    return True


def try_session_receive_header(
    client: Any,
    deadline: float,
    context: str,
    *,
    allow_timeout: bool = False,
) -> tuple[bool, MessageHeader | None]:
    """Return ``(True, header)`` when shared session handled the read.

    When ``allow_timeout`` is True and the wait times out, returns
    ``(True, None)``. When no session is bound, returns ``(False, None)``.
    """

    session = getattr(client, "_session", None)
    if session is None:
        return False, None
    error_cls = getattr(client, "_session_error_cls", HarvestError)
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        if allow_timeout:
            return True, None
        raise error_cls(f"等待{context}超时")
    try:
        return True, session.receive_header(remaining)
    except GameSessionKickout:
        raise
    except GameSessionError as exc:
        if allow_timeout and "超时" in str(exc):
            return True, None
        raise error_cls(str(exc)) from exc
    except Exception as exc:
        if isinstance(exc, error_cls):
            raise
        raise error_cls(str(exc)) from exc


def try_session_ensure_ready(client: Any, endpoint: GameEndpoint) -> bool:
    """Ensure shared session is ready. Returns True if session-bound."""

    session = getattr(client, "_session", None)
    if session is None:
        return False
    error_cls = getattr(client, "_session_error_cls", HarvestError)
    try:
        ensure_recovered = getattr(session, "ensure_recovered", None)
        if callable(ensure_recovered):
            ensure_recovered(endpoint)
        else:
            # Preserve compatibility with test doubles and older session-like
            # integrations while real GameSession instances use the barrier.
            session.ensure_ready(endpoint)
    except GameSessionKickout:
        raise
    except Exception as exc:
        if isinstance(exc, error_cls):
            raise
        raise error_cls(str(exc)) from exc
    client.password = session.password
    return True


def shared_close(client: Any) -> bool:
    """Close owned socket only. Returns True if caller should close owned socket."""

    if not getattr(client, "_owns_connection", True):
        client.socket = None
        return False
    return True
