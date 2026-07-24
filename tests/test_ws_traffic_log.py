"""WebSocket raw traffic logger unit tests."""

from __future__ import annotations

import base64
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

from harvest_fief import (
    MessageHeader,
    encode_int_field,
    encode_message_header,
)
from ws_traffic_log import (
    WebSocketTrafficLogger,
    bind_traffic_logging,
    default_ws_raw_log_path,
    resolve_ws_log_path,
)


class ResolvePathTestCase(unittest.TestCase):
    def test_default_path_under_logs_websocket_raw(self) -> None:
        path = default_ws_raw_log_path("treasure_farm")
        self.assertEqual(path.parent.name, "treasure_farm")
        self.assertEqual(path.parent.parent.name, "websocket_raw")
        self.assertTrue(path.name.endswith(".jsonl"))

    def test_resolve_false_disables(self) -> None:
        self.assertIsNone(resolve_ws_log_path("x", False))
        self.assertIsNone(resolve_ws_log_path("x", None))

    def test_resolve_true_uses_default(self) -> None:
        path = resolve_ws_log_path("smithy_forge", True)
        assert path is not None
        self.assertEqual(path.parent.name, "smithy_forge")


class BindTrafficLoggingTestCase(unittest.TestCase):
    def test_bind_records_outbound_and_inbound_frames(self) -> None:
        sent: list[tuple[str, object]] = []

        class FakeSocket:
            def send_binary(self, payload: bytes) -> None:
                sent.append(("binary", payload))

            def send_text(self, payload: str) -> None:
                sent.append(("text", payload))

            def recv_message(self, _timeout: float) -> tuple[int, bytes]:
                return (0x2, encode_message_header(10020, encode_int_field(1, 1)))

            def close(self) -> None:
                sent.append(("close", None))

        client = SimpleNamespace(socket=FakeSocket(), password=None)

        with TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "ws.jsonl"
            bound = bind_traffic_logging(
                client,
                task="unit_test",
                path=log_path,
            )
            self.assertEqual(bound, log_path)

            client._send_message(10010, encode_int_field(1, 7), encrypted=False)
            header = client._decode_frame(
                0x2, encode_message_header(10020, encode_int_field(1, 1))
            )
            self.assertEqual(header.message_id, 10020)
            client.close()

            lines = log_path.read_text(encoding="utf-8").strip().splitlines()
            self.assertEqual(len(lines), 2)
            outbound = json.loads(lines[0])
            inbound = json.loads(lines[1])
            self.assertEqual(outbound["direction"], "outbound")
            self.assertEqual(outbound["message_id"], 10010)
            self.assertEqual(outbound["task"], "unit_test")
            self.assertTrue(outbound["wire_payload_base64"])
            self.assertEqual(inbound["direction"], "inbound")
            self.assertEqual(inbound["message_id"], 10020)
            # Wire bytes round-trip.
            wire = base64.b64decode(outbound["wire_payload_base64"])
            self.assertEqual(wire[:2], encode_message_header(10010, b"")[:2])

    def test_bind_disabled_is_noop(self) -> None:
        client = SimpleNamespace(socket=None, password=None)
        self.assertIsNone(
            bind_traffic_logging(client, task="unit_test", path=False)
        )
        self.assertIsNone(client._traffic_logger)


class LoggerWriteTestCase(unittest.TestCase):
    def test_write_frame_includes_payloads(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "a.jsonl"
            logger = WebSocketTrafficLogger(
                path=path, session_id="s1", task="demo", output=lambda _m: None
            )
            header = MessageHeader(message_id=15516, sid=3, data=b"\x08\x01")
            logger.write_frame(
                direction="outbound",
                opcode=2,
                encrypted=False,
                wire_payload=b"abc",
                decoded_packet=b"abc",
                header=header,
            )
            logger.close()
            record = json.loads(path.read_text(encoding="utf-8").strip())
            self.assertEqual(record["message_id"], 15516)
            self.assertEqual(record["sid"], 3)
            self.assertEqual(
                base64.b64decode(record["message_payload_base64"]), b"\x08\x01"
            )


if __name__ == "__main__":
    unittest.main()
