from __future__ import annotations

import socket
import unittest

from harvest_fief import (
    LOGIN_MESSAGE_ID,
    LOGIN_REUNIQUE_MESSAGE_ID,
    PACK_PASSWORD_MESSAGE_ID,
    SOCKET_PACK_KEY,
    GameEndpoint,
    MessageHeader,
    decode_message_header,
    encode_bytes_field,
    encode_int_field,
    encode_message_header,
    encode_varint,
    pack1_decode,
    pack1_encode,
)
from harvest_fief import ItemChange
from dragon_arena import (
    BattleInfo,
    BattleTeamUnit,
    ProtoReader,
    decode_game_data_battle_context,
)
from game_session import (
    BATTLE_INFO_MESSAGE_ID,
    BATTLE_S2C_START_MESSAGE_ID,
    GAME_DATA_MESSAGE_ID,
    GameSession,
    GameSessionManager,
    SessionRecoveryIssue,
    SessionRecoverySnapshot,
)
from session_recovery import (
    RecoveryResult,
    SessionRecoveryCoordinator,
    recovery_content_area,
)
from knight_arena import (
    ArenaChallengeResponse,
    ArenaChallengeResult,
    ArenaInfo,
    ArenaOpponent,
    ArenaRoundResult,
    KnightArenaClient,
)
from treasure_area import (
    MAP_TREASURE_CLEAR_RESULT_MESSAGE_ID,
    MAP_TREASURE_INFO_MESSAGE_ID,
    MAP_TREASURE_SWEEP_MESSAGE_ID,
    MAX_SWEEP_TIMES_PER_REQUEST,
    SWEEP_RET_TIMES_LACK,
    TreasureAreaClient,
    TreasureAreaRejected,
    TreasureSweepLoot,
    decode_treasure_area_status,
    decode_treasure_sweep_loot,
    encode_treasure_sweep_request,
)


def _treasure_status_payload(
    *,
    swept_today: int,
    daily_sweep_limit: int,
    area_ids: tuple[int, ...],
    has_pending_results: bool = False,
    results: bytes | None = None,
) -> bytes:
    packed_areas = b"".join(encode_varint(area_id) for area_id in area_ids)
    payload = (
        encode_int_field(1, 3)
        + encode_int_field(2, 120)
        + encode_int_field(3, swept_today)
        + encode_int_field(4, daily_sweep_limit)
        + encode_bytes_field(5, packed_areas)
    )
    if results is not None:
        payload += encode_bytes_field(6, results)
    elif has_pending_results:
        payload += encode_bytes_field(6, b"")
    return payload


class FakeSocket:
    def __init__(self, frames: list[tuple[int, bytes]]) -> None:
        self.frames = list(frames)
        self.binary_frames: list[bytes] = []
        self.text_frames: list[str] = []
        self.closed = False

    def send_binary(self, payload: bytes) -> None:
        self.binary_frames.append(payload)

    def send_text(self, payload: str) -> None:
        self.text_frames.append(payload)

    def recv_message(self, _timeout: float) -> tuple[int, bytes]:
        if not self.frames:
            raise socket.timeout()
        return self.frames.pop(0)

    def close(self) -> None:
        self.closed = True


class TreasureAreaProtocolTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.endpoint = GameEndpoint("ws://test.invalid", "game-token", "1", "测试区")

    def test_status_decodes_available_maps_and_daily_remaining_count(self) -> None:
        status = decode_treasure_area_status(
            _treasure_status_payload(
                swept_today=4,
                daily_sweep_limit=9,
                area_ids=(1001, 1002),
            )
        )

        self.assertEqual(status.area_ids, (1001, 1002))
        self.assertEqual(status.swept_today, 4)
        self.assertEqual(status.daily_sweep_limit, 9)
        self.assertEqual(status.sweep_remaining, 5)
        self.assertFalse(status.has_pending_results)

    def test_status_clears_pending_sweep_results_before_returning(self) -> None:
        session_password = "87654321"
        password_payload = encode_bytes_field(
            1, pack1_encode(session_password.encode("utf-8"), SOCKET_PACK_KEY).encode("utf-8")
        )

        def encrypted(message_id: int, data: bytes = b"") -> tuple[int, bytes]:
            return (
                0x2,
                pack1_encode(
                    encode_message_header(message_id, data), session_password
                ).encode("utf-8"),
            )

        change = (
            encode_int_field(1, 1)
            + encode_int_field(2, 3)
            + encode_int_field(3, 10)
        )
        reward_package = encode_bytes_field(2, change)
        pending_results = encode_int_field(1, 1001) + encode_bytes_field(2, reward_package)
        pending = _treasure_status_payload(
            swept_today=2,
            daily_sweep_limit=10,
            area_ids=(1001,),
            results=pending_results,
        )
        cleared = _treasure_status_payload(
            swept_today=2,
            daily_sweep_limit=10,
            area_ids=(1001,),
        )
        fake_socket = FakeSocket(
            [
                (0x2, encode_message_header(PACK_PASSWORD_MESSAGE_ID, password_payload)),
                encrypted(LOGIN_REUNIQUE_MESSAGE_ID),
                encrypted(MAP_TREASURE_INFO_MESSAGE_ID, pending),
                encrypted(MAP_TREASURE_CLEAR_RESULT_MESSAGE_ID, cleared),
            ]
        )
        client = TreasureAreaClient(
            self.endpoint,
            1.0,
            socket_factory=lambda _url, _timeout: fake_socket,
        )

        status = client.get_status()

        self.assertFalse(status.has_pending_results)
        self.assertEqual(status.sweep_remaining, 8)
        self.assertEqual(
            status.cleared_sweep_loot,
            TreasureSweepLoot(
                area_id=1001,
                items=(ItemChange(item_id=1, delta=3, total=10),),
            ),
        )
        sent_ids = [
            decode_message_header(pack1_decode(frame, session_password)).message_id
            for frame in fake_socket.text_frames
        ]
        self.assertEqual(
            sent_ids,
            [MAP_TREASURE_INFO_MESSAGE_ID, MAP_TREASURE_CLEAR_RESULT_MESSAGE_ID],
        )

    def test_shared_session_preserves_unrelated_pushes_while_waiting_for_status(self) -> None:
        class SharedSession:
            def __init__(self, headers: list[MessageHeader]) -> None:
                self.headers = list(headers)
                self.sent: list[int] = []
                self.password = "shared-password"

            def ensure_ready(self, _endpoint: GameEndpoint) -> None:
                return None

            def send_message(
                self, message_id: int, _data: bytes = b"", *, encrypted: bool
            ) -> None:
                self.sent.append(message_id)
                assert encrypted

            def receive_header(self, _timeout: float) -> MessageHeader:
                if not self.headers:
                    raise socket.timeout()
                return self.headers.pop(0)

            def push_headers(self, headers: list[MessageHeader]) -> None:
                self.headers[0:0] = headers

        unrelated = MessageHeader(message_id=10490, sid=0, data=b"game-data")
        status_header = MessageHeader(
            message_id=MAP_TREASURE_INFO_MESSAGE_ID,
            sid=0,
            data=_treasure_status_payload(
                swept_today=2,
                daily_sweep_limit=10,
                area_ids=(1001,),
            ),
        )
        session = SharedSession([unrelated, status_header])
        client = TreasureAreaClient(self.endpoint, 1.0, session=session)

        status = client.get_status()

        self.assertEqual(status.sweep_remaining, 8)
        self.assertEqual(session.sent, [MAP_TREASURE_INFO_MESSAGE_ID])
        self.assertEqual(session.receive_header(0), unrelated)

    def test_shared_status_query_uses_the_recovery_barrier(self) -> None:
        class SharedSession:
            def __init__(self) -> None:
                self.password = "shared-password"
                self.recovered_for: GameEndpoint | None = None
                self.sent: list[int] = []

            def ensure_recovered(self, endpoint: GameEndpoint) -> None:
                self.recovered_for = endpoint

            def send_message(
                self, message_id: int, _data: bytes = b"", *, encrypted: bool
            ) -> None:
                assert encrypted
                self.sent.append(message_id)

            def receive_header(self, _timeout: float) -> MessageHeader:
                return MessageHeader(
                    message_id=MAP_TREASURE_INFO_MESSAGE_ID,
                    sid=0,
                    data=_treasure_status_payload(
                        swept_today=2,
                        daily_sweep_limit=10,
                        area_ids=(1001,),
                    ),
                )

        session = SharedSession()
        client = TreasureAreaClient(self.endpoint, 1.0, session=session)

        client.get_status()

        self.assertIs(session.recovered_for, self.endpoint)
        self.assertEqual(session.sent, [MAP_TREASURE_INFO_MESSAGE_ID])

    def test_manager_recovers_login_battle_before_returning_shared_session(self) -> None:
        class ClearBattleHandler:
            name = "test_battle"

            def __init__(self) -> None:
                self.seen_message_id: int | None = None

            def can_handle(self, snapshot: object) -> bool:
                return bool(getattr(snapshot, "pending", False))

            def recover(
                self,
                session: GameSession,
                _endpoint: GameEndpoint,
                _snapshot: object,
            ) -> RecoveryResult:
                self.seen_message_id = session.receive_header(0).message_id
                session.resolve_recovery_issue("battle")
                return RecoveryResult(self.name, "battle cleared")

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

        fake_socket = FakeSocket(
            [
                (0x2, encode_message_header(PACK_PASSWORD_MESSAGE_ID, password_payload)),
                encrypted(BATTLE_INFO_MESSAGE_ID, b"battle"),
                encrypted(LOGIN_REUNIQUE_MESSAGE_ID),
            ]
        )
        handler = ClearBattleHandler()
        import game_session as gs

        original = gs.POST_LOGIN_BUSINESS_DELAY_SECONDS
        gs.POST_LOGIN_BUSINESS_DELAY_SECONDS = 0
        try:
            manager = GameSessionManager(
                timeout=1.0,
                socket_factory=lambda _url, _timeout: fake_socket,
                websocket_log=False,
                recovery_coordinator=SessionRecoveryCoordinator((handler,)),
            )
            session = manager.session_for(self.endpoint)
        finally:
            gs.POST_LOGIN_BUSINESS_DELAY_SECONDS = original

        self.assertEqual(handler.seen_message_id, BATTLE_INFO_MESSAGE_ID)
        self.assertFalse(session.recovery_pending)
        self.assertTrue(session.recovery_checked)

    def test_knight_arena_preparation_battle_info_does_not_start_battle(self) -> None:
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

        marker = encode_int_field(3, 1) + encode_int_field(4, 5)
        fake_socket = FakeSocket(
            [
                (0x2, encode_message_header(PACK_PASSWORD_MESSAGE_ID, password_payload)),
                encrypted(GAME_DATA_MESSAGE_ID, encode_bytes_field(35, marker)),
                encrypted(BATTLE_INFO_MESSAGE_ID, b"battle-prepare"),
                encrypted(19800, b"arena-info"),
                encrypted(LOGIN_REUNIQUE_MESSAGE_ID),
            ]
        )
        import game_session as gs

        original_delay = gs.POST_LOGIN_BUSINESS_DELAY_SECONDS
        original_drain = gs.POST_LOGIN_RECOVERY_DRAIN_SECONDS
        gs.POST_LOGIN_BUSINESS_DELAY_SECONDS = 0
        gs.POST_LOGIN_RECOVERY_DRAIN_SECONDS = 0
        try:
            manager = GameSessionManager(
                timeout=1.0,
                socket_factory=lambda _url, _timeout: fake_socket,
                websocket_log=False,
            )
            session = manager.session_for(self.endpoint)
        finally:
            gs.POST_LOGIN_BUSINESS_DELAY_SECONDS = original_delay
            gs.POST_LOGIN_RECOVERY_DRAIN_SECONDS = original_drain

        self.assertFalse(session.recovery_pending)
        self.assertTrue(session.recovery_checked)
        self.assertEqual(fake_socket.text_frames, [])
        self.assertEqual(session.receive_header(0).message_id, 19800)

    def test_knight_arena_running_packets_route_to_battle_recovery(self) -> None:
        snapshot = SessionRecoverySnapshot(
            generation=1,
            game_data_present=True,
            issues=(
                SessionRecoveryIssue(
                    kind="battle",
                    battle_state=1,
                    battle_type=5,
                    message_ids=(
                        BATTLE_INFO_MESSAGE_ID,
                        BATTLE_S2C_START_MESSAGE_ID,
                    ),
                ),
            ),
        )

        self.assertEqual(recovery_content_area(snapshot).id, "knight_arena")

    def test_knight_arena_start_uses_native_pvp_team_slot(self) -> None:
        def team_entry(team_id: int, hero_id: int, x: int, y: int) -> bytes:
            position = encode_int_field(1, x) + encode_int_field(2, y)
            slot = encode_int_field(1, hero_id) + encode_bytes_field(2, position)
            slot_entry = encode_int_field(1, 0) + encode_bytes_field(2, slot)
            team = encode_bytes_field(1, slot_entry)
            return encode_int_field(1, team_id) + encode_bytes_field(2, team)

        teams = b"".join(
            (
                encode_bytes_field(1, team_entry(0, 101, 1, 1)),
                encode_bytes_field(1, team_entry(4, 202, 3, 2)),
            )
        )
        hero = b"".join(
            (
                encode_bytes_field(1, encode_int_field(1, 101)),
                encode_bytes_field(1, encode_int_field(1, 202)),
                encode_bytes_field(4, teams),
                encode_int_field(10, 0),
            )
        )
        context = decode_game_data_battle_context(encode_bytes_field(9, hero))
        self.assertEqual(context.team, (BattleTeamUnit(101, 1, 1),))
        self.assertEqual(context.teams[4], (BattleTeamUnit(202, 3, 2),))
        self.assertTrue(context.team_payloads[4])

        client = KnightArenaClient(
            self.endpoint,
            1.0,
            battle_start_codec="python",
            websocket_log=False,
            business_log=None,
            log=lambda _message: None,
        )
        client._game_data_context = context
        sent: list[tuple[int, bytes]] = []

        def capture(message_id: int, data: bytes = b"", *, encrypted: bool) -> None:
            self.assertTrue(encrypted)
            sent.append((message_id, data))

        client._send_message = capture  # type: ignore[method-assign]
        client.start_battle(
            BattleInfo(
                battle_id=91,
                ret=0,
                battle_type=5,
                location_id=0,
                player_units=1,
                enemy_units=1,
                enemy_team=(BattleTeamUnit(9001, 1, 6),),
                skip_team=False,
                skip_mode=0,
                battle_data=b"",
            )
        )
        client.close()

        self.assertEqual(sent[0][0], 18010)
        player_ids = [
            next(
                int(value)
                for field_number, wire_type, value in ProtoReader(bytes(entry)).fields()
                if field_number == 1 and wire_type == 0
            )
            for field_number, wire_type, entry in ProtoReader(sent[0][1]).fields()
            if field_number == 2 and wire_type == 2
        ]
        self.assertEqual(player_ids, [202])

    def test_knight_arena_syncs_attack_team_before_challenge(self) -> None:
        team = encode_bytes_field(
            1,
            encode_int_field(1, 0)
            + encode_bytes_field(
                2,
                encode_int_field(1, 202)
                + encode_bytes_field(2, encode_int_field(1, 3) + encode_int_field(2, 2)),
            ),
        )
        teams = encode_bytes_field(
            1,
            encode_int_field(1, 4) + encode_bytes_field(2, team),
        )
        hero = (
            encode_bytes_field(1, encode_int_field(1, 202))
            + encode_bytes_field(4, teams)
            + encode_int_field(10, 4)
        )
        context = decode_game_data_battle_context(encode_bytes_field(9, hero))
        client = KnightArenaClient(
            self.endpoint,
            1.0,
            websocket_log=False,
            business_log=None,
            log=lambda _message: None,
        )
        client._game_data_context = context
        sent: list[tuple[int, bytes]] = []
        client._send_message = (
            lambda message_id, data=b"", *, encrypted: sent.append((message_id, data))
        )
        client._queued_headers.extend(
            (
                MessageHeader(15016, 0, encode_int_field(1, 4)),
                MessageHeader(15014, 0, encode_int_field(4, 4)),
            )
        )

        client._sync_attack_team()
        client.close()

        self.assertEqual(sent[0][0], 15016)
        self.assertEqual(
            sent[0][1],
            encode_int_field(1, 4) + encode_bytes_field(2, context.team_payloads[4]),
        )

    def test_knight_arena_daily_runner_stops_after_unsettled_accepted_challenge(
        self,
    ) -> None:
        client = object.__new__(KnightArenaClient)
        client.log = lambda _message: None
        client._last_round_failure = None
        client.get_info = lambda: ArenaInfo(
            challenge_num=3,
            refresh_num=0,
            season_id=1255,
            season_score=223,
            season_open=True,
            season_challenge_num=5,
            opponents=(
                ArenaOpponent(0, 1001, 0, 0, 200, "甲"),
                ArenaOpponent(1, 1002, 0, 0, 180, "乙"),
                ArenaOpponent(2, 1003, 0, 0, 160, "丙"),
            ),
        )
        calls: list[int] = []

        def run_round(index: int) -> ArenaRoundResult:
            calls.append(index)
            if len(calls) == 1:
                return ArenaRoundResult(
                    index,
                    ArenaChallengeResponse(0, 0, 0, index, 4, 6),
                    ArenaChallengeResult(True, 0, index, 230, 7, 230),
                )
            client._last_round_failure = "Battle_S2C_start 返回 ret=7"
            return ArenaRoundResult(
                index,
                ArenaChallengeResponse(0, 0, 0, index, 5, 7),
                None,
            )

        client.run_round = run_round
        result = client.run_daily_free_challenges(daily_free_limit=5)

        self.assertEqual(result.requested_challenges, 2)
        self.assertEqual(result.accepted_challenges, 2)
        self.assertEqual(len(result.results), 1)
        self.assertEqual(result.rejected_challenges, 0)
        self.assertEqual(result.failure, "Battle_S2C_start 返回 ret=7")
        self.assertEqual(len(calls), 2)

    def test_knight_arena_daily_runner_skips_closed_season(self) -> None:
        client = object.__new__(KnightArenaClient)
        client.log = lambda _message: None
        client.get_info = lambda: ArenaInfo(
            challenge_num=0,
            refresh_num=0,
            season_id=1255,
            season_score=223,
            season_open=False,
            season_challenge_num=0,
            opponents=(),
        )
        client.run_round = lambda _index: self.fail("赛季关闭时不得挑战")

        result = client.run_daily_free_challenges(daily_free_limit=5)

        self.assertFalse(result.season_open)
        self.assertEqual(result.requested_challenges, 5)
        self.assertEqual(result.accepted_challenges, 0)
        self.assertEqual(result.results, ())

    def test_sweep_request_uses_native_area_useitem_and_times_fields(self) -> None:
        request = encode_treasure_sweep_request(1001, MAX_SWEEP_TIMES_PER_REQUEST)

        self.assertEqual(request, b"\x08\xe9\x07\x10\x01\x18\x1e")
        with self.assertRaises(ValueError):
            encode_treasure_sweep_request(1001, MAX_SWEEP_TIMES_PER_REQUEST + 1)

    def test_client_queries_status_then_sweeps_with_server_updated_quota(self) -> None:
        session_password = "87654321"
        password_payload = encode_bytes_field(
            1, pack1_encode(session_password.encode("utf-8"), SOCKET_PACK_KEY).encode("utf-8")
        )

        def encrypted(message_id: int, data: bytes = b"") -> tuple[int, bytes]:
            packet = encode_message_header(message_id, data)
            return 0x2, pack1_encode(packet, session_password).encode("utf-8")

        initial = _treasure_status_payload(
            swept_today=2,
            daily_sweep_limit=10,
            area_ids=(1001, 1002),
        )
        updated = _treasure_status_payload(
            swept_today=5,
            daily_sweep_limit=10,
            area_ids=(1001, 1002),
        )
        sweep_response = encode_bytes_field(2, updated)
        fake_socket = FakeSocket(
            [
                (0x2, encode_message_header(PACK_PASSWORD_MESSAGE_ID, password_payload)),
                encrypted(LOGIN_REUNIQUE_MESSAGE_ID),
                encrypted(MAP_TREASURE_INFO_MESSAGE_ID, initial),
                encrypted(MAP_TREASURE_SWEEP_MESSAGE_ID, sweep_response),
            ]
        )
        client = TreasureAreaClient(
            self.endpoint,
            1.0,
            socket_factory=lambda _url, _timeout: fake_socket,
        )

        status = client.get_status()
        response = client.sweep(1001, 3)

        self.assertEqual(status.sweep_remaining, 8)
        self.assertEqual(response.status.swept_today, 5)
        self.assertEqual(response.status.sweep_remaining, 5)
        self.assertEqual(
            decode_message_header(fake_socket.binary_frames[0]).message_id,
            LOGIN_MESSAGE_ID,
        )
        info_packet = decode_message_header(
            pack1_decode(fake_socket.text_frames[0], session_password)
        )
        self.assertEqual(info_packet.message_id, MAP_TREASURE_INFO_MESSAGE_ID)
        self.assertEqual(info_packet.data, b"")
        sweep_packet = decode_message_header(
            pack1_decode(fake_socket.text_frames[1], session_password)
        )
        self.assertEqual(sweep_packet.message_id, MAP_TREASURE_SWEEP_MESSAGE_ID)
        self.assertEqual(sweep_packet.data, encode_treasure_sweep_request(1001, 3))

    def test_client_raises_for_server_rejection(self) -> None:
        session_password = "87654321"
        password_payload = encode_bytes_field(
            1, pack1_encode(session_password.encode("utf-8"), SOCKET_PACK_KEY).encode("utf-8")
        )

        def encrypted(message_id: int, data: bytes = b"") -> tuple[int, bytes]:
            return (
                0x2,
                pack1_encode(
                    encode_message_header(message_id, data), session_password
                ).encode("utf-8"),
            )

        fake_socket = FakeSocket(
            [
                (0x2, encode_message_header(PACK_PASSWORD_MESSAGE_ID, password_payload)),
                encrypted(LOGIN_REUNIQUE_MESSAGE_ID),
                encrypted(MAP_TREASURE_SWEEP_MESSAGE_ID, encode_int_field(1, 4)),
            ]
        )
        client = TreasureAreaClient(
            self.endpoint,
            1.0,
            socket_factory=lambda _url, _timeout: fake_socket,
        )

        with self.assertRaises(TreasureAreaRejected) as context:
            client.sweep(1001, 1)

        self.assertEqual(context.exception.ret, 4)
        self.assertIn("今日扫荡次数不足", str(context.exception))
        self.assertIn(str(SWEEP_RET_TIMES_LACK), str(context.exception))

    def test_status_decodes_sweep_loot_item_names_payload(self) -> None:
        # results: area=1001, one reward package with item id=1 delta=5 total=20
        item_change = (
            encode_int_field(1, 1)
            + encode_int_field(2, 5)
            + encode_int_field(3, 20)
        )
        reward_package = encode_bytes_field(2, item_change)
        results = encode_int_field(1, 1001) + encode_bytes_field(2, reward_package)
        status = decode_treasure_area_status(
            _treasure_status_payload(
                swept_today=3,
                daily_sweep_limit=9,
                area_ids=(1001,),
                results=results,
            )
        )

        self.assertTrue(status.has_pending_results)
        assert status.sweep_loot is not None
        self.assertEqual(status.sweep_loot.area_id, 1001)
        self.assertEqual(
            status.sweep_loot.items,
            (ItemChange(item_id=1, delta=5, total=20),),
        )

        loot = decode_treasure_sweep_loot(results)
        self.assertEqual(loot.area_id, 1001)
        self.assertEqual(loot.items[0].item_id, 1)


if __name__ == "__main__":
    unittest.main()
