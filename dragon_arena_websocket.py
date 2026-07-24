"""龙痕竞技场 WebSocket 收发、Pack1 编解码与日志输出。"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol

from harvest_fief import (
    HarvestError,
    MessageHeader,
    NativeWebSocket,
    decode_message_header,
    encode_message_header,
    pack1_decode,
    pack1_encode,
)
from logging_store import MANAGED_DESTINATION, LogPersistenceError, write_standard_log
from id_descriptions import business_name
from ws_traffic_log import (
    WebSocketTrafficLogger,
    default_ws_raw_log_path,
    resolve_ws_log_path,
)


PROJECT_ROOT = Path(__file__).resolve().parent
DEFAULT_WEBSOCKET_LOG = default_ws_raw_log_path("dragon_arena")
DEFAULT_BUSINESS_LOG = MANAGED_DESTINATION


class WebSocketTransport(Protocol):
    """NativeWebSocket 与本地测试传输共同遵循的最小接口。"""

    def send_binary(self, payload: bytes) -> None: ...

    def send_text(self, payload: str) -> None: ...

    def recv_message(self, timeout: float) -> tuple[int, bytes]: ...

    def close(self) -> None: ...


SocketFactory = Callable[[str, float], WebSocketTransport]
DEFAULT_SOCKET_FACTORY: SocketFactory = NativeWebSocket.connect


class WebSocketBusinessLogger:
    """按 WebSocket 消息 ID 输出固定业务名称，不推断跨消息状态。"""

    def __init__(
        self,
        *,
        path: Path | None = None,
        session_id: str | None = None,
        output: Callable[[str], None] = print,
        managed: bool = False,
        zone: Mapping[str, Any] | None = None,
    ) -> None:
        self.path = path
        self.managed = managed
        self.zone = dict(zone or {"id": "unknown", "name": "unknown"})
        self.session_id = session_id or str(time.time_ns())
        self.output = output
        self._sequence = 0
        if self.path is not None or self.managed:
            self._open()

    def __enter__(self) -> "WebSocketBusinessLogger":
        return self

    def __exit__(self, _exc_type: object, _exc: object, _traceback: object) -> None:
        self.close()

    def _open(self) -> None:
        target = "logs/websocket_business/<日期>.jsonl" if self.managed else str(self.path)
        self.output(f"[业务日志] WebSocket 业务操作追加到 {target}")

    def close(self) -> None:
        return None

    def log_send(self, message_id: int, payload_size: int) -> None:
        self._write("发送", message_id, payload_size)

    def log_receive(self, message_id: int, payload_size: int) -> None:
        self._write("返回", message_id, payload_size)

    def _write(self, direction: str, message_id: int, payload_size: int) -> None:
        name = business_name(message_id)
        self._sequence += 1
        line = (
            f"[WebSocket业务][{direction}] "
            f"session={self.session_id} seq={self._sequence} "
            f"业务={name}，载荷={payload_size} 字节"
        )
        if self.path is not None or self.managed:
            try:
                write_standard_log(
                    event="websocket_business",
                    operation="send" if direction == "发送" else "receive",
                    zone=self.zone,
                    details={"direction": "outbound" if direction == "发送" else "inbound", "sequence": self._sequence, "message_id": message_id, "message_name": name, "payload_size": payload_size},
                    destination=MANAGED_DESTINATION if self.managed else self.path,
                    run_id=self.session_id,
                )
            except LogPersistenceError as exc:
                raise HarvestError(f"写入 WebSocket 业务日志失败：{exc}") from exc
        self.output(line)


class DragonArenaWebSocket:
    """封装游戏服 WebSocket、会话密码以及两类日志。"""

    def __init__(
        self,
        url: str,
        timeout: float,
        *,
        websocket_log: Path | bool | None = True,
        business_log: Path | None = None,
        socket_factory: SocketFactory = DEFAULT_SOCKET_FACTORY,
        output: Callable[[str], None] = print,
        task: str = "dragon_arena",
    ) -> None:
        self.url = url
        self.timeout = timeout
        self.socket_factory = socket_factory
        self.transport: WebSocketTransport | None = None
        self.password: str | None = None
        self.session_id = str(time.time_ns())
        self.business_logger = WebSocketBusinessLogger(
            path=business_log,
            session_id=self.session_id,
            output=output,
        )
        try:
            resolved = resolve_ws_log_path(task, websocket_log)
            self.traffic_logger = WebSocketTrafficLogger(
                path=resolved,
                session_id=self.session_id,
                task=task,
                output=output,
            )
        except Exception:
            self.business_logger.close()
            raise

    @property
    def connected(self) -> bool:
        return self.transport is not None

    def connect(self) -> None:
        if self.transport is None:
            self.transport = self.socket_factory(self.url, self.timeout)

    def close(self) -> None:
        try:
            if self.transport is not None:
                transport = self.transport
                self.transport = None
                transport.close()
        finally:
            try:
                self.traffic_logger.close()
            finally:
                self.business_logger.close()

    def send_message(
        self,
        message_id: int,
        data: bytes = b"",
        *,
        encrypted: bool,
    ) -> None:
        if self.transport is None:
            raise HarvestError("WebSocket 尚未连接")
        packet = encode_message_header(message_id, data)
        if encrypted:
            if not self.password:
                raise HarvestError("游戏服尚未下发会话密码")
            wire_text = pack1_encode(packet, self.password)
            self.transport.send_text(wire_text)
            opcode = 0x1
            wire_payload = wire_text.encode("utf-8")
        else:
            self.transport.send_binary(packet)
            opcode = 0x2
            wire_payload = packet
        self.traffic_logger.write_frame(
            direction="outbound",
            opcode=opcode,
            encrypted=encrypted,
            wire_payload=wire_payload,
            decoded_packet=packet,
            header=MessageHeader(message_id=message_id, sid=0, data=data),
        )
        self.business_logger.log_send(message_id, len(data))

    def receive_header(self, timeout: float) -> MessageHeader:
        if self.transport is None:
            raise HarvestError("WebSocket 尚未连接")
        opcode, payload = self.transport.recv_message(timeout)
        return self.decode_frame(opcode, payload)

    def decode_frame(self, opcode: int, payload: bytes) -> MessageHeader:
        wire_payload = payload
        encrypted = self.password is not None
        decoded_packet: bytes | None = None
        try:
            if self.password is not None:
                if opcode not in (0x1, 0x2):
                    raise HarvestError(f"加密游戏报文 opcode 异常：{opcode}")
                decoded_packet = pack1_decode(payload, self.password)
            else:
                decoded_packet = payload
            header = decode_message_header(decoded_packet)
        except Exception as exc:
            self.traffic_logger.write_frame(
                direction="inbound",
                opcode=opcode,
                encrypted=encrypted,
                wire_payload=wire_payload,
                decoded_packet=decoded_packet,
                header=None,
                decode_error=f"{type(exc).__name__}: {exc}",
            )
            raise
        self.traffic_logger.write_frame(
            direction="inbound",
            opcode=opcode,
            encrypted=encrypted,
            wire_payload=wire_payload,
            decoded_packet=decoded_packet,
            header=header,
        )
        self.business_logger.log_receive(header.message_id, len(header.data))
        return header
