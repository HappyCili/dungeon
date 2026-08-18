from __future__ import annotations

import socket
import unittest

from dungeon_sweep import (
    DUN_START_DRAW_MESSAGE_ID,
    DUN_DRAW_ITEM_CHANGE_SOURCE,
    DUN_SWEEP_MESSAGE_ID,
    DUNGEON_LAMP_ITEM_ID,
    DIRECT_SHOP_ID,
    GAME_DATA_MESSAGE_ID,
    SHOP_BUY_MESSAGE_ID,
    DungeonSweepClient,
    DungeonSweepRejected,
    decode_dungeon_status,
    encode_dungeon_lamp_claim_request,
    encode_dungeon_draw_all_request,
    encode_dungeon_sweep_request,
    find_dungeon_lamp_offer,
    sweep_rejection_reason,
)
from harvest_fief import (
    HarvestError,
    LOGIN_MESSAGE_ID,
    LOGIN_REUNIQUE_MESSAGE_ID,
    PACK_PASSWORD_MESSAGE_ID,
    SOCKET_PACK_KEY,
    STORAGE_ITEM_CHANGE_MESSAGE_ID,
    GameEndpoint,
    ItemChange,
    RewardProp,
    decode_message_header,
    encode_bytes_field,
    encode_int_field,
    encode_message_header,
    encode_varint,
    pack1_decode,
    pack1_encode,
)


def _dungeon_status_payload(
    *,
    unlocked_ids: tuple[int, ...],
    visible_ids: tuple[int, ...],
    best_scores: dict[int, int],
    challenge_times: dict[int, int] | None = None,
    current_dungeon_id: int,
    draw_times: int,
    total_draw_times: int,
) -> bytes:
    packed_unlocked = b"".join(encode_varint(value) for value in unlocked_ids)
    packed_visible = b"".join(encode_varint(value) for value in visible_ids)
    best_entries = b"".join(
        encode_bytes_field(
            30,
            encode_int_field(1, dungeon_id) + encode_int_field(2, score),
        )
        for dungeon_id, score in best_scores.items()
    )
    challenge_entries = b"".join(
        encode_bytes_field(
            29,
            encode_int_field(1, dungeon_id) + encode_int_field(2, times),
        )
        for dungeon_id, times in (challenge_times or {}).items()
    )
    return (
        encode_bytes_field(1, packed_unlocked)
        + encode_int_field(5, current_dungeon_id)
        + encode_int_field(7, draw_times)
        + encode_int_field(21, total_draw_times)
        + challenge_entries
        + best_entries
        + encode_bytes_field(35, packed_visible)
    )


class FakeSocket:
    def __init__(
        self,
        frames: list[tuple[int, bytes]],
        *,
        after_frames_error: Exception | None = None,
    ) -> None:
        self.frames = list(frames)
        self.after_frames_error = after_frames_error
        self.binary_frames: list[bytes] = []
        self.text_frames: list[str] = []
        self.closed = False

    def send_binary(self, payload: bytes) -> None:
        self.binary_frames.append(payload)

    def send_text(self, payload: str) -> None:
        self.text_frames.append(payload)

    def recv_message(self, _timeout: float) -> tuple[int, bytes]:
        if not self.frames:
            if self.after_frames_error is not None:
                raise self.after_frames_error
            raise socket.timeout()
        return self.frames.pop(0)

    def close(self) -> None:
        self.closed = True


class DungeonSweepProtocolTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.endpoint = GameEndpoint("ws://test.invalid", "game-token", "1", "测试区")

    def test_status_decodes_sweepable_dungeons_and_highest_scores(self) -> None:
        status = decode_dungeon_status(
            _dungeon_status_payload(
                unlocked_ids=(2301, 2302, 2303),
                visible_ids=(2301, 2302),
                best_scores={2301: 88, 2302: 165, 2303: 7},
                challenge_times={2301: 1, 2302: 3},
                current_dungeon_id=2301,
                draw_times=1,
                total_draw_times=3,
            )
        )

        self.assertEqual(status.sweepable_ids, (2301, 2302))
        self.assertEqual(status.best_score_for(2302), 165)
        self.assertEqual(status.pending_draws, 2)
        self.assertEqual(status.challenge_times_for(2301), 1)
        self.assertEqual(status.challenge_times_for(2302), 3)
        self.assertFalse(status.can_sweep(2303))

    def test_sweep_and_all_draw_requests_use_native_fields(self) -> None:
        self.assertEqual(encode_dungeon_sweep_request(2302), b"\x08\xfe\x11")
        self.assertEqual(
            encode_dungeon_draw_all_request(2302), b"\x08\xfe\x11\x10\x01"
        )

    def test_direct_shop_lamp_offer_is_found_and_encoded(self) -> None:
        goods = (
            encode_int_field(1, 7001)
            + encode_int_field(2, 3)
            + encode_bytes_field(
                7,
                encode_int_field(1, 1)
                + encode_int_field(2, DUNGEON_LAMP_ITEM_ID)
                + encode_int_field(3, 1),
            )
        )
        game_data = encode_bytes_field(
            17,
            encode_int_field(1, DIRECT_SHOP_ID) + encode_bytes_field(9, goods),
        )

        offer = find_dungeon_lamp_offer(game_data)

        self.assertIsNotNone(offer)
        assert offer is not None
        self.assertEqual(offer.stock_qty, 3)
        self.assertEqual(
            encode_dungeon_lamp_claim_request(offer.stock_qty, offer.goods_data),
            encode_int_field(1, DIRECT_SHOP_ID)
            + encode_int_field(2, 3)
            + encode_bytes_field(3, goods),
        )

    def test_sweep_rejection_uses_native_reason_when_defined(self) -> None:
        rejection = DungeonSweepRejected(3)

        self.assertEqual(rejection.ret, 3)
        self.assertEqual(rejection.reason, "无扫荡次数")
        self.assertEqual(str(rejection), "地下城扫荡失败：无扫荡次数（ret=3）")
        self.assertIsNone(sweep_rejection_reason(4))

    def test_client_sweeps_then_draws_all_and_decodes_rewards(self) -> None:
        session_password = "87654321"
        password_payload = encode_bytes_field(
            1,
            pack1_encode(session_password.encode("utf-8"), SOCKET_PACK_KEY).encode(
                "utf-8"
            ),
        )

        def encrypted(message_id: int, data: bytes = b"") -> tuple[int, bytes]:
            packet = encode_message_header(message_id, data)
            return 0x2, pack1_encode(packet, session_password).encode("utf-8")

        status_payload = _dungeon_status_payload(
            unlocked_ids=(2301, 2302),
            visible_ids=(2301, 2302),
            best_scores={2301: 88, 2302: 165},
            current_dungeon_id=2301,
            draw_times=1,
            total_draw_times=2,
        )
        draw_payload = (
            encode_int_field(1, 0)
            + encode_int_field(2, 2302)
            + encode_int_field(3, 3)
            + encode_int_field(4, 3)
            + encode_bytes_field(5, encode_varint(9001) + encode_varint(9002))
            + encode_bytes_field(6, encode_varint(250) + encode_varint(750))
            + encode_int_field(7, 1)
        )
        draw_reward_notice = (
            encode_int_field(1, DUN_DRAW_ITEM_CHANGE_SOURCE)
            + encode_bytes_field(
                2,
                encode_int_field(1, 9001)
                + encode_int_field(2, 2)
                + encode_int_field(3, 12),
            )
            + encode_bytes_field(
                21,
                encode_int_field(1, 1)
                + encode_int_field(2, 9001)
                + encode_int_field(3, 2),
            )
        )
        second_draw_reward_notice = (
            encode_int_field(1, DUN_DRAW_ITEM_CHANGE_SOURCE)
            + encode_bytes_field(
                2,
                encode_int_field(1, 9002)
                + encode_int_field(2, 1)
                + encode_int_field(3, 4),
            )
            + encode_bytes_field(
                21,
                encode_int_field(1, 2)
                + encode_int_field(2, 9002)
                + encode_int_field(3, 1),
            )
        )
        fake_socket = FakeSocket(
            [
                (0x2, encode_message_header(PACK_PASSWORD_MESSAGE_ID, password_payload)),
                encrypted(LOGIN_REUNIQUE_MESSAGE_ID),
                encrypted(GAME_DATA_MESSAGE_ID, encode_bytes_field(18, status_payload)),
                encrypted(DUN_SWEEP_MESSAGE_ID, encode_int_field(1, 0)),
                encrypted(DUN_START_DRAW_MESSAGE_ID, draw_payload),
                encrypted(STORAGE_ITEM_CHANGE_MESSAGE_ID, draw_reward_notice),
                encrypted(
                    STORAGE_ITEM_CHANGE_MESSAGE_ID, second_draw_reward_notice
                ),
            ],
            after_frames_error=HarvestError("游戏服关闭了 WebSocket 连接"),
        )
        client = DungeonSweepClient(
            self.endpoint,
            1.0,
            socket_factory=lambda _url, _timeout: fake_socket,
        )

        status = client.get_status()
        client.sweep(2302)
        draw = client.draw_all(2302)

        self.assertEqual(status.sweepable_ids, (2301, 2302))
        self.assertEqual(draw.reward_ids, (9001, 9002))
        self.assertEqual(draw.probabilities, (250, 750))
        self.assertTrue(draw.all_drawn)
        self.assertTrue(draw.reward_notice_received)
        self.assertEqual(
            draw.item_changes,
            (ItemChange(9001, 2, 12), ItemChange(9002, 1, 4)),
        )
        self.assertEqual(
            draw.reward_props,
            (RewardProp(1, 9001, 2), RewardProp(2, 9002, 1)),
        )
        self.assertEqual(
            decode_message_header(fake_socket.binary_frames[0]).message_id,
            LOGIN_MESSAGE_ID,
        )
        sweep_packet = decode_message_header(
            pack1_decode(fake_socket.text_frames[0], session_password)
        )
        draw_packet = decode_message_header(
            pack1_decode(fake_socket.text_frames[1], session_password)
        )
        self.assertEqual(sweep_packet.message_id, DUN_SWEEP_MESSAGE_ID)
        self.assertEqual(sweep_packet.data, encode_dungeon_sweep_request(2302))
        self.assertEqual(draw_packet.message_id, DUN_START_DRAW_MESSAGE_ID)
        self.assertEqual(draw_packet.data, encode_dungeon_draw_all_request(2302))

    def test_client_raises_when_sweep_is_rejected(self) -> None:
        session_password = "87654321"
        password_payload = encode_bytes_field(
            1,
            pack1_encode(session_password.encode("utf-8"), SOCKET_PACK_KEY).encode(
                "utf-8"
            ),
        )

        def encrypted(message_id: int, data: bytes = b"") -> tuple[int, bytes]:
            return (
                0x2,
                pack1_encode(
                    encode_message_header(message_id, data), session_password
                ).encode("utf-8"),
            )

        status_payload = _dungeon_status_payload(
            unlocked_ids=(2301,),
            visible_ids=(2301,),
            best_scores={2301: 88},
            current_dungeon_id=2301,
            draw_times=0,
            total_draw_times=0,
        )
        fake_socket = FakeSocket(
            [
                (0x2, encode_message_header(PACK_PASSWORD_MESSAGE_ID, password_payload)),
                encrypted(LOGIN_REUNIQUE_MESSAGE_ID),
                encrypted(GAME_DATA_MESSAGE_ID, encode_bytes_field(18, status_payload)),
                encrypted(DUN_SWEEP_MESSAGE_ID, encode_int_field(1, 4)),
            ]
        )
        client = DungeonSweepClient(
            self.endpoint,
            1.0,
            socket_factory=lambda _url, _timeout: fake_socket,
        )

        with self.assertRaises(DungeonSweepRejected) as context:
            client.sweep(2301)

        self.assertEqual(context.exception.ret, 4)
        self.assertIsNone(context.exception.reason)

    def test_client_claims_available_daily_lamp_from_direct_shop(self) -> None:
        session_password = "87654321"
        password_payload = encode_bytes_field(
            1,
            pack1_encode(session_password.encode("utf-8"), SOCKET_PACK_KEY).encode(
                "utf-8"
            ),
        )

        def encrypted(message_id: int, data: bytes = b"") -> tuple[int, bytes]:
            return (
                0x2,
                pack1_encode(
                    encode_message_header(message_id, data), session_password
                ).encode("utf-8"),
            )

        status_payload = _dungeon_status_payload(
            unlocked_ids=(2301,),
            visible_ids=(2301,),
            best_scores={2301: 88},
            current_dungeon_id=2301,
            draw_times=0,
            total_draw_times=0,
        )
        goods = (
            encode_int_field(1, 7001)
            + encode_int_field(2, 3)
            + encode_bytes_field(
                7,
                encode_int_field(1, 1)
                + encode_int_field(2, DUNGEON_LAMP_ITEM_ID)
                + encode_int_field(3, 1),
            )
        )
        game_data = (
            encode_bytes_field(17, encode_int_field(1, DIRECT_SHOP_ID) + encode_bytes_field(9, goods))
            + encode_bytes_field(18, status_payload)
        )
        fake_socket = FakeSocket(
            [
                (0x2, encode_message_header(PACK_PASSWORD_MESSAGE_ID, password_payload)),
                encrypted(LOGIN_REUNIQUE_MESSAGE_ID),
                encrypted(GAME_DATA_MESSAGE_ID, game_data),
                encrypted(SHOP_BUY_MESSAGE_ID, encode_int_field(1, DIRECT_SHOP_ID)),
            ]
        )
        client = DungeonSweepClient(
            self.endpoint,
            1.0,
            socket_factory=lambda _url, _timeout: fake_socket,
        )

        result = client.claim_daily_lamp()

        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.claimed_qty, 3)
        request = decode_message_header(
            pack1_decode(fake_socket.text_frames[0], session_password)
        )
        self.assertEqual(request.message_id, SHOP_BUY_MESSAGE_ID)
        self.assertEqual(
            request.data,
            encode_int_field(1, DIRECT_SHOP_ID)
            + encode_int_field(2, 3)
            + encode_bytes_field(3, goods),
        )


if __name__ == "__main__":
    unittest.main()
