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

import socket
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Protocol

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

# Delay after Login_reunique before business traffic (native throwCachedMsg).
POST_LOGIN_BUSINESS_DELAY_SECONDS = 0.1

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


def _decode_kickout(data: bytes) -> tuple[int, str]:
    ret = 0
    message = ""
    for field_number, wire_type, value in ProtoReader(data).fields():
        if field_number == 1 and wire_type == 0:
            ret = decode_int32(int(value))
        elif field_number == 2 and wire_type == 2:
            message = bytes(value).decode("utf-8", errors="replace")
    return ret, message


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

    def ensure_ready(self, endpoint: GameEndpoint) -> None:
        """Connect and login if needed; reuse when endpoint identity matches."""

        with self._lock:
            key = _endpoint_key(endpoint)
            if self._ready and self._transport is not None and self._endpoint_key == key:
                return
            self._connect_and_login(endpoint)

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
                    self._game_data = header.data
                    continue
                if header.message_id == LOGIN_REUNIQUE_MESSAGE_ID:
                    login_complete = True
                    continue
                # Other login-phase pushes are ignored (mail counts, etc.).

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
                        self._game_data = header.data
                    else:
                        self._queued.append(header)

            self._ready = True
            # Native throwCachedMsg dispatches after 100 ms.
            time.sleep(POST_LOGIN_BUSINESS_DELAY_SECONDS)
        except Exception:
            self._teardown_transport()
            self._password = None
            self._ready = False
            self._game_data = None
            raise

    def send_message(
        self, message_id: int, data: bytes = b"", *, encrypted: bool = True
    ) -> None:
        with self._lock:
            if encrypted and not self._ready:
                raise GameSessionError("游戏服会话尚未就绪（msgReady=false）")
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

    def push_headers(self, headers: Iterable[MessageHeader]) -> None:
        """Restore a batch ahead of later queued traffic, preserving its order."""

        pending = list(headers)
        if not pending:
            return
        with self._lock:
            self._queued[0:0] = pending

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
    ) -> None:
        self._timeout = timeout
        self._socket_factory = socket_factory
        self._websocket_log = websocket_log
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
            return self._session

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
