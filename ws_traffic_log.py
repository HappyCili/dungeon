#!/usr/bin/env python3
"""Shared WebSocket raw-frame logging for all game-server task clients.

Raw frames are written under ``logs/websocket_raw/<task>/YYYY-MM-DD.jsonl`` by
default.  Each line includes wire bytes, Pack1-decoded packets, and decoded
``MsgHdr`` payloads (base64).  This is intentionally separate from the redacted
managed operational log in ``logging_store``.
"""

from __future__ import annotations

import base64
import json
import time
import weakref
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, TextIO

from project_paths import UI_APP_ROOT


PROJECT_ROOT = UI_APP_ROOT
DEFAULT_WS_RAW_ROOT = PROJECT_ROOT / "logs" / "websocket_raw"

# Quiet by default so batch jobs are not flooded; paths still appear once.
_DEFAULT_OUTPUT: Callable[[str], None] = lambda _message: None


def _message_name(message_id: int | None) -> str | None:
    """Lazy lookup to avoid import cycles with harvest_fief / business_map."""

    if message_id is None:
        return None
    try:
        from dragon_arena_business_map import message_name
    except Exception:
        return None
    try:
        return message_name(message_id)
    except Exception:
        return None


def default_ws_raw_log_path(task: str, *, when: datetime | None = None) -> Path:
    """Return ``logs/websocket_raw/<task>/<date>.jsonl``."""

    task_name = (task or "unknown").strip().replace("/", "_").replace("\\", "_")
    if not task_name:
        task_name = "unknown"
    stamp = (when or datetime.now().astimezone()).date().isoformat()
    return DEFAULT_WS_RAW_ROOT / task_name / f"{stamp}.jsonl"


def resolve_ws_log_path(task: str, path: Path | bool | None) -> Path | None:
    """Normalize the ``websocket_log`` constructor argument.

    * ``True`` / omitted default → dated file under ``logs/websocket_raw/<task>/``
    * ``Path`` → that file
    * ``False`` / ``None`` → disabled
    """

    if path is False or path is None:
        return None
    if path is True:
        return default_ws_raw_log_path(task)
    return Path(path)


class WebSocketTrafficLogger:
    """Append one JSON object per WebSocket frame (wire + decoded)."""

    def __init__(
        self,
        *,
        path: Path | None,
        session_id: str,
        task: str = "",
        output: Callable[[str], None] = _DEFAULT_OUTPUT,
    ) -> None:
        self.path = path
        self.session_id = session_id
        self.task = task
        self.output = output
        self._file: TextIO | None = None
        self._sequence = 0
        if self.path is not None:
            self._open()

    def _open(self) -> None:
        assert self.path is not None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._file = self.path.open("a", encoding="utf-8", buffering=1)
        except OSError as exc:
            from harvest_fief import HarvestError

            raise HarvestError(f"打开 WebSocket 原始日志失败：{self.path}：{exc}") from exc
        label = f" task={self.task}" if self.task else ""
        self.output(f"[WebSocket日志] 原始收发{label} 追加到 {self.path}")

    def close(self) -> None:
        if self._file is not None:
            self._file.close()
            self._file = None

    def write_frame(
        self,
        *,
        direction: str,
        opcode: int,
        encrypted: bool,
        wire_payload: bytes,
        decoded_packet: bytes | None,
        header: object | None,
        decode_error: str | None = None,
    ) -> None:
        if self._file is None:
            return
        self._sequence += 1
        message_id = getattr(header, "message_id", None) if header is not None else None
        sid = getattr(header, "sid", None) if header is not None else None
        data = getattr(header, "data", None) if header is not None else None
        if not isinstance(data, (bytes, bytearray)):
            data = None
        record = {
            "timestamp": datetime.now().astimezone().isoformat(timespec="milliseconds"),
            "session_id": self.session_id,
            "task": self.task or None,
            "sequence": self._sequence,
            "direction": direction,
            "opcode": opcode,
            "frame_type": "text" if opcode == 0x1 else "binary",
            "encrypted": encrypted,
            "message_id": message_id,
            "message_name": _message_name(
                int(message_id) if isinstance(message_id, int) else None
            ),
            "sid": sid,
            "wire_size": len(wire_payload),
            "wire_payload_base64": base64.b64encode(wire_payload).decode("ascii"),
            "decoded_size": len(decoded_packet) if decoded_packet is not None else None,
            "decoded_packet_base64": (
                base64.b64encode(decoded_packet).decode("ascii")
                if decoded_packet is not None
                else None
            ),
            "payload_size": len(data) if data is not None else None,
            "message_payload_base64": (
                base64.b64encode(bytes(data)).decode("ascii") if data is not None else None
            ),
            "decode_error": decode_error,
        }
        try:
            self._file.write(
                json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
            )
            self._file.flush()
        except OSError as exc:
            from harvest_fief import HarvestError

            raise HarvestError(f"写入 WebSocket 原始日志失败：{self.path}：{exc}") from exc


def bind_traffic_logging(
    client: Any,
    *,
    task: str,
    path: Path | bool | None = True,
    error_cls: type[Exception] | None = None,
    output: Callable[[str], None] = _DEFAULT_OUTPUT,
) -> Path | None:
    """Attach raw-frame logging by replacing ``_send_message`` / ``_decode_frame``.

    Expects ``client.socket`` and ``client.password`` (same shape as existing
    harvest/treasure clients).  Safe to call with ``path=False`` to disable.
    """

    # Lazy imports avoid harvest_fief ↔ ws_traffic_log import cycles.
    from harvest_fief import (
        HarvestError,
        MessageHeader,
        decode_message_header,
        encode_message_header,
        pack1_decode,
        pack1_encode,
    )

    err_type: type[Exception] = error_cls or HarvestError
    log_path = resolve_ws_log_path(task, path)
    session_id = str(getattr(client, "_ws_session_id", None) or time.time_ns())
    client._ws_session_id = session_id
    client._ws_error_cls = err_type

    if log_path is None:
        client._traffic_logger = None
        return None

    logger = WebSocketTrafficLogger(
        path=log_path,
        session_id=session_id,
        task=task,
        output=output,
    )
    client._traffic_logger = logger
    # Close the log handle if the client is GC'd without an explicit close().
    try:
        weakref.finalize(client, logger.close)
    except TypeError:
        # e.g. SimpleNamespace in unit tests cannot be weak-referenced.
        pass

    def _send_message(
        message_id: int, data: bytes = b"", *, encrypted: bool
    ) -> None:
        socket = getattr(client, "socket", None)
        if socket is None:
            raise err_type("WebSocket 尚未连接")
        packet = encode_message_header(message_id, data)
        password = getattr(client, "password", None)
        if encrypted:
            if not password:
                raise err_type("游戏服尚未下发会话密码")
            wire_text = pack1_encode(packet, password)
            socket.send_text(wire_text)
            opcode = 0x1
            wire_payload = wire_text.encode("utf-8")
        else:
            socket.send_binary(packet)
            opcode = 0x2
            wire_payload = packet
        traffic = getattr(client, "_traffic_logger", None)
        if traffic is not None:
            traffic.write_frame(
                direction="outbound",
                opcode=opcode,
                encrypted=encrypted,
                wire_payload=wire_payload,
                decoded_packet=packet,
                header=MessageHeader(message_id=message_id, sid=0, data=data),
            )

    def _decode_frame(opcode: int, payload: bytes) -> MessageHeader:
        wire_payload = payload
        password = getattr(client, "password", None)
        encrypted = password is not None
        decoded_packet: bytes | None = None
        traffic = getattr(client, "_traffic_logger", None)
        try:
            if password is not None:
                # Native bridge may surface Pack1 text frames as binary opcodes.
                if opcode not in (0x1, 0x2):
                    raise err_type(f"加密游戏报文 opcode 异常：{opcode}")
                decoded_packet = pack1_decode(payload, password)
            else:
                decoded_packet = payload
            header = decode_message_header(decoded_packet)
        except Exception as exc:
            if traffic is not None:
                traffic.write_frame(
                    direction="inbound",
                    opcode=opcode,
                    encrypted=encrypted,
                    wire_payload=wire_payload,
                    decoded_packet=decoded_packet,
                    header=None,
                    decode_error=f"{type(exc).__name__}: {exc}",
                )
            raise
        if traffic is not None:
            traffic.write_frame(
                direction="inbound",
                opcode=opcode,
                encrypted=encrypted,
                wire_payload=wire_payload,
                decoded_packet=decoded_packet,
                header=header,
            )
        return header

    client._send_message = _send_message
    client._decode_frame = _decode_frame

    original_close = getattr(client, "close", None)

    def close() -> None:
        try:
            if callable(original_close):
                original_close()
        finally:
            traffic = getattr(client, "_traffic_logger", None)
            if traffic is not None:
                traffic.close()
                client._traffic_logger = None

    client.close = close
    return log_path
