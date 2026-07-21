from __future__ import annotations

import io
import json
import socket
import stat
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from daily_quest import (
    DAILYQUEST_GET_QUEST_REWARD_MESSAGE_ID,
    DAILYQUEST_GET_SCORE_REWARD_MESSAGE_ID,
    DAILYQUEST_INFO_MESSAGE_ID,
    DAILY_GROUP_ID,
    GAME_DATA_MESSAGE_ID,
    WEEKLY_GROUP_ID,
    DailyCatalog,
    DailyClaimResult,
    DailyQuestClient,
    DailyQuestStatus,
    DailyTaskConfig,
    DailyTaskState,
    ActivityRewardConfig,
    append_daily_result_log,
    build_daily_status_payload,
    encode_daily_quest_reward_request,
    encode_daily_score_reward_request,
    load_daily_catalog,
    main,
)
from harvest_fief import (
    LOGIN_MESSAGE_ID,
    LOGIN_REUNIQUE_MESSAGE_ID,
    PACK_PASSWORD_MESSAGE_ID,
    SOCKET_PACK_KEY,
    GameEndpoint,
    decode_message_header,
    encode_bytes_field,
    encode_int_field,
    encode_message_header,
    pack1_decode,
    pack1_encode,
)


SESSION_PASSWORD = "87654321"


class ScriptedSocket:
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


def _task_state_payload(daily_id: int, *, finished: bool, getscore: bool) -> bytes:
    payload = encode_int_field(1, daily_id)
    if finished:
        payload += encode_int_field(2, 1)
    if getscore:
        payload += encode_int_field(3, 1)
    return payload


def _daily_status_payload(
    tasks: list[tuple[int, bool, bool]],
    *,
    reward_ids: tuple[int, ...] = (),
    weekly_reward_ids: tuple[int, ...] = (),
    remaining_seconds: int = 3600,
    reset_seconds: int = 7200,
    weekly_remaining_seconds: int = 3 * 24 * 3600,
    weekly_reset_seconds: int = 7 * 24 * 3600,
) -> bytes:
    payload = (
        encode_int_field(1, remaining_seconds)
        + encode_int_field(2, reset_seconds)
        + encode_int_field(4, weekly_remaining_seconds)
        + encode_int_field(5, weekly_reset_seconds)
    )
    for reward_id in reward_ids:
        payload += encode_int_field(3, reward_id)
    for reward_id in weekly_reward_ids:
        payload += encode_int_field(6, reward_id)
    for daily_id, finished, getscore in tasks:
        payload += encode_bytes_field(
            7,
            _task_state_payload(
                daily_id,
                finished=finished,
                getscore=getscore,
            ),
        )
    return payload


def _encrypted_frame(message_id: int, payload: bytes = b"") -> tuple[int, bytes]:
    packet = encode_message_header(message_id, payload)
    return 0x2, pack1_encode(packet, SESSION_PASSWORD).encode("utf-8")


def _login_frames(initial_status: bytes) -> list[tuple[int, bytes]]:
    password_payload = encode_bytes_field(
        1,
        pack1_encode(SESSION_PASSWORD.encode("utf-8"), SOCKET_PACK_KEY).encode(
            "utf-8"
        ),
    )
    return [
        (0x2, encode_message_header(PACK_PASSWORD_MESSAGE_ID, password_payload)),
        _encrypted_frame(
            GAME_DATA_MESSAGE_ID,
            encode_bytes_field(19, initial_status),
        ),
        _encrypted_frame(LOGIN_REUNIQUE_MESSAGE_ID),
    ]


def _quest_reward_response(
    daily_id: int, group_id: int = DAILY_GROUP_ID
) -> bytes:
    return (
        encode_int_field(1, 0)
        + encode_int_field(2, daily_id)
        + encode_int_field(3, group_id)
        + encode_int_field(4, daily_id)
    )


def _score_reward_response(
    reward_id: int, group_id: int = DAILY_GROUP_ID
) -> bytes:
    return (
        encode_int_field(1, 0)
        + encode_int_field(2, group_id)
        + encode_int_field(3, reward_id)
        + encode_int_field(4, reward_id)
    )


def _claim_catalog() -> DailyCatalog:
    return DailyCatalog(
        tasks={
            daily_id: DailyTaskConfig(daily_id, 50000 + daily_id, DAILY_GROUP_ID, 10)
            for daily_id in (101, 102, 103, 104, 105)
        },
        activity_rewards=(
            ActivityRewardConfig(201, DAILY_GROUP_ID, 20),
            ActivityRewardConfig(202, DAILY_GROUP_ID, 40),
        ),
    )


def _daily_and_weekly_claim_catalog() -> DailyCatalog:
    return DailyCatalog(
        tasks={
            101: DailyTaskConfig(101, 50101, DAILY_GROUP_ID, 20),
        },
        activity_rewards=(
            ActivityRewardConfig(101, DAILY_GROUP_ID, 20),
        ),
        weekly_tasks={
            201: DailyTaskConfig(201, 60101, WEEKLY_GROUP_ID, 20),
            202: DailyTaskConfig(202, 60201, WEEKLY_GROUP_ID, 20),
        },
        weekly_activity_rewards=(
            ActivityRewardConfig(201, WEEKLY_GROUP_ID, 20),
            ActivityRewardConfig(202, WEEKLY_GROUP_ID, 40),
        ),
    )


def _sent_message_ids(socket_state: ScriptedSocket) -> list[int]:
    return [packet.message_id for packet in _sent_packets(socket_state)]


def _sent_packets(socket_state: ScriptedSocket):
    return [
        decode_message_header(pack1_decode(frame, SESSION_PASSWORD))
        for frame in socket_state.text_frames
    ]


class DailyQuestClientSessionTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.endpoint = GameEndpoint(
            "ws://test.invalid",
            "game-token",
            "1",
            "测试区服",
        )

    def test_claim_covers_reward_thresholds_and_is_idempotent(self) -> None:
        catalog = _claim_catalog()
        initial = _daily_status_payload(
            [
                (101, False, False),
                (102, True, False),
                (103, True, False),
                (104, True, True),
                (105, True, True),
            ]
        )
        after_task_claims = _daily_status_payload(
            [
                (101, False, False),
                (102, True, True),
                (103, True, True),
                (104, True, True),
                (105, True, True),
            ]
        )
        final = _daily_status_payload(
            [
                (101, False, False),
                (102, True, True),
                (103, True, True),
                (104, True, True),
                (105, True, True),
            ],
            reward_ids=(201, 202),
        )
        socket_state = ScriptedSocket(
            _login_frames(initial)
            + [
                _encrypted_frame(DAILYQUEST_INFO_MESSAGE_ID, initial),
                _encrypted_frame(
                    DAILYQUEST_GET_QUEST_REWARD_MESSAGE_ID,
                    _quest_reward_response(102),
                ),
                _encrypted_frame(
                    DAILYQUEST_GET_QUEST_REWARD_MESSAGE_ID,
                    _quest_reward_response(103),
                ),
                _encrypted_frame(DAILYQUEST_INFO_MESSAGE_ID, after_task_claims),
                _encrypted_frame(
                    DAILYQUEST_GET_SCORE_REWARD_MESSAGE_ID,
                    _score_reward_response(201),
                ),
                _encrypted_frame(
                    DAILYQUEST_GET_SCORE_REWARD_MESSAGE_ID,
                    _score_reward_response(202),
                ),
                _encrypted_frame(DAILYQUEST_INFO_MESSAGE_ID, final),
                _encrypted_frame(DAILYQUEST_INFO_MESSAGE_ID, final),
                _encrypted_frame(DAILYQUEST_INFO_MESSAGE_ID, final),
                _encrypted_frame(DAILYQUEST_INFO_MESSAGE_ID, final),
            ]
        )
        client = DailyQuestClient(
            self.endpoint,
            1.0,
            socket_factory=lambda _url, _timeout: socket_state,
        )

        try:
            first = client.claim_available(catalog)
            second = client.claim_available(catalog)
        finally:
            client.close()

        self.assertEqual(first.claimed_task_ids, (102, 103))
        self.assertEqual(first.claimed_reward_ids, (201, 202))
        self.assertEqual(second.claimed_task_ids, ())
        self.assertEqual(second.claimed_reward_ids, ())
        self.assertTrue(first.status.task(102).score_claimed)
        self.assertEqual(first.status.daily_reward_ids, (201, 202))
        sent_packets = _sent_packets(socket_state)
        self.assertEqual(
            [packet.message_id for packet in sent_packets],
            [
                DAILYQUEST_INFO_MESSAGE_ID,
                DAILYQUEST_GET_QUEST_REWARD_MESSAGE_ID,
                DAILYQUEST_GET_QUEST_REWARD_MESSAGE_ID,
                DAILYQUEST_INFO_MESSAGE_ID,
                DAILYQUEST_GET_SCORE_REWARD_MESSAGE_ID,
                DAILYQUEST_GET_SCORE_REWARD_MESSAGE_ID,
                DAILYQUEST_INFO_MESSAGE_ID,
                DAILYQUEST_INFO_MESSAGE_ID,
                DAILYQUEST_INFO_MESSAGE_ID,
                DAILYQUEST_INFO_MESSAGE_ID,
            ],
        )
        self.assertEqual(
            [
                packet.data
                for packet in sent_packets
                if packet.message_id == DAILYQUEST_GET_QUEST_REWARD_MESSAGE_ID
            ],
            [
                encode_daily_quest_reward_request(102),
                encode_daily_quest_reward_request(103),
            ],
        )
        self.assertEqual(
            [
                packet.data
                for packet in sent_packets
                if packet.message_id == DAILYQUEST_GET_SCORE_REWARD_MESSAGE_ID
            ],
            [
                encode_daily_score_reward_request(201),
                encode_daily_score_reward_request(202),
            ],
        )
        self.assertEqual(
            decode_message_header(socket_state.binary_frames[0]).message_id,
            LOGIN_MESSAGE_ID,
        )
        self.assertTrue(socket_state.closed)

    def test_claim_available_checks_daily_and_weekly_groups(self) -> None:
        catalog = _daily_and_weekly_claim_catalog()
        initial = _daily_status_payload(
            [
                (101, True, False),
                (201, True, False),
                (202, True, True),
            ]
        )
        after_task_claims = _daily_status_payload(
            [
                (101, True, True),
                (201, True, True),
                (202, True, True),
            ]
        )
        final = _daily_status_payload(
            [
                (101, True, True),
                (201, True, True),
                (202, True, True),
            ],
            reward_ids=(101,),
            weekly_reward_ids=(201, 202),
        )
        socket_state = ScriptedSocket(
            _login_frames(initial)
            + [
                _encrypted_frame(DAILYQUEST_INFO_MESSAGE_ID, initial),
                _encrypted_frame(
                    DAILYQUEST_GET_QUEST_REWARD_MESSAGE_ID,
                    _quest_reward_response(101),
                ),
                _encrypted_frame(
                    DAILYQUEST_GET_QUEST_REWARD_MESSAGE_ID,
                    _quest_reward_response(201, WEEKLY_GROUP_ID),
                ),
                _encrypted_frame(DAILYQUEST_INFO_MESSAGE_ID, after_task_claims),
                _encrypted_frame(
                    DAILYQUEST_GET_SCORE_REWARD_MESSAGE_ID,
                    _score_reward_response(101),
                ),
                _encrypted_frame(
                    DAILYQUEST_GET_SCORE_REWARD_MESSAGE_ID,
                    _score_reward_response(201, WEEKLY_GROUP_ID),
                ),
                _encrypted_frame(
                    DAILYQUEST_GET_SCORE_REWARD_MESSAGE_ID,
                    _score_reward_response(202, WEEKLY_GROUP_ID),
                ),
                _encrypted_frame(DAILYQUEST_INFO_MESSAGE_ID, final),
            ]
        )
        client = DailyQuestClient(
            self.endpoint,
            1.0,
            socket_factory=lambda _url, _timeout: socket_state,
        )

        try:
            result = client.claim_available(catalog)
        finally:
            client.close()

        self.assertEqual(result.claimed_task_ids, (101, 201))
        self.assertEqual(result.claimed_daily_task_ids, (101,))
        self.assertEqual(result.claimed_weekly_task_ids, (201,))
        self.assertEqual(result.claimed_reward_ids, (101, 201, 202))
        self.assertEqual(result.claimed_daily_reward_ids, (101,))
        self.assertEqual(result.claimed_weekly_reward_ids, (201, 202))
        self.assertEqual(result.status.daily_reward_ids, (101,))
        self.assertEqual(result.status.weekly_reward_ids, (201, 202))

        sent_packets = _sent_packets(socket_state)
        self.assertEqual(
            [
                packet.data
                for packet in sent_packets
                if packet.message_id == DAILYQUEST_GET_QUEST_REWARD_MESSAGE_ID
            ],
            [
                encode_daily_quest_reward_request(101, DAILY_GROUP_ID),
                encode_daily_quest_reward_request(201, WEEKLY_GROUP_ID),
            ],
        )
        self.assertEqual(
            [
                packet.data
                for packet in sent_packets
                if packet.message_id == DAILYQUEST_GET_SCORE_REWARD_MESSAGE_ID
            ],
            [
                encode_daily_score_reward_request(101, DAILY_GROUP_ID),
                encode_daily_score_reward_request(201, WEEKLY_GROUP_ID),
                encode_daily_score_reward_request(202, WEEKLY_GROUP_ID),
            ],
        )

    def test_status_refresh_observes_cross_day_reset(self) -> None:
        before_reset = _daily_status_payload(
            [(101, True, True)],
            reward_ids=(201,),
            remaining_seconds=30,
            reset_seconds=60,
        )
        after_reset = _daily_status_payload(
            [(101, False, False)],
            remaining_seconds=24 * 3600,
            reset_seconds=48 * 3600,
        )
        socket_state = ScriptedSocket(
            _login_frames(before_reset)
            + [
                _encrypted_frame(DAILYQUEST_INFO_MESSAGE_ID, before_reset),
                _encrypted_frame(DAILYQUEST_INFO_MESSAGE_ID, after_reset),
            ]
        )
        client = DailyQuestClient(
            self.endpoint,
            1.0,
            socket_factory=lambda _url, _timeout: socket_state,
        )

        try:
            first = client.get_status()
            second = client.get_status()
        finally:
            client.close()

        self.assertTrue(first.task(101).finished)
        self.assertTrue(first.task(101).score_claimed)
        self.assertEqual(first.daily_reward_ids, (201,))
        self.assertFalse(second.task(101).finished)
        self.assertFalse(second.task(101).score_claimed)
        self.assertEqual(second.daily_reward_ids, ())
        self.assertEqual(second.daily_remaining_seconds, 24 * 3600)
        self.assertEqual(second.daily_reset_seconds, 48 * 3600)
        self.assertEqual(
            _sent_message_ids(socket_state),
            [DAILYQUEST_INFO_MESSAGE_ID, DAILYQUEST_INFO_MESSAGE_ID],
        )


class DailyQuestOutputTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.catalog = load_daily_catalog()
        self.endpoint = GameEndpoint(
            "wss://sensitive.example.invalid/session",
            "game-token-must-not-be-logged",
            "4101",
            "测试一区",
        )
        self.status = DailyQuestStatus(
            daily_remaining_seconds=3600,
            daily_reset_seconds=7200,
            daily_reward_ids=(101,),
            tasks={
                101: DailyTaskState(101, False, False),
                104: DailyTaskState(104, True, True),
            },
            quest_progress={50001: 3, 50301: 1},
            weekly_remaining_seconds=3 * 24 * 3600,
            weekly_reset_seconds=7 * 24 * 3600,
            weekly_reward_ids=(201,),
        )

    def test_status_payload_lists_all_tasks_and_log_is_redacted(self) -> None:
        payload = build_daily_status_payload(self.status, self.catalog)

        self.assertEqual(len(self.catalog.tasks), 20)
        self.assertEqual(len(self.catalog.weekly_tasks), 11)
        self.assertEqual(len(self.catalog.activity_rewards), 5)
        self.assertEqual(len(self.catalog.weekly_activity_rewards), 5)
        self.assertEqual(len(payload["tasks"]), 20)
        self.assertEqual(payload["tasks"][0]["daily_id"], 101)
        self.assertEqual(payload["tasks"][0]["progress"], 3)
        self.assertFalse(payload["tasks"][0]["finished"])
        self.assertTrue(payload["tasks"][3]["getscore"])
        self.assertTrue(payload["tasks"][3]["reported_by_server"])
        self.assertEqual(payload["claimed_daily_reward_ids"], [101])
        self.assertEqual(payload["claimed_weekly_reward_ids"], [201])
        self.assertEqual(payload["weekly_remaining_seconds"], 3 * 24 * 3600)

        with TemporaryDirectory() as directory:
            log_path = Path(directory) / "records" / "daily.jsonl"
            record = append_daily_result_log(
                log_path,
                self.endpoint,
                "status",
                self.status,
                self.catalog,
                timestamp="2026-07-20T12:00:00+08:00",
            )
            text = log_path.read_text(encoding="utf-8")

            self.assertEqual(json.loads(text), record)
            self.assertEqual(record["zone"], {"id": "4101", "name": "测试一区"})
            self.assertNotIn("game-token", text)
            self.assertNotIn("sensitive.example", text)
            self.assertNotIn("session", text)
            self.assertEqual(stat.S_IMODE(log_path.stat().st_mode), 0o600)

    def test_status_and_claim_cli_write_jsonl_records(self) -> None:
        log_directory = TemporaryDirectory()
        self.addCleanup(log_directory.cleanup)
        log_path = Path(log_directory.name) / "daily.jsonl"

        class FakeClient:
            instances: list["FakeClient"] = []

            def __init__(self, endpoint: GameEndpoint, _timeout: float) -> None:
                self.endpoint = endpoint
                self.closed = False
                self.instances.append(self)

            def __enter__(self) -> "FakeClient":
                return self

            def __exit__(self, _exc_type, _exc, _traceback) -> None:
                self.closed = True

            def get_status(self) -> DailyQuestStatus:
                return self_status

            def claim_available(self, _catalog: DailyCatalog) -> DailyClaimResult:
                return DailyClaimResult(
                    claimed_task_ids=(104, 201),
                    claimed_reward_ids=(101, 201),
                    status=self_status,
                    claimed_daily_task_ids=(104,),
                    claimed_weekly_task_ids=(201,),
                    claimed_daily_reward_ids=(101,),
                    claimed_weekly_reward_ids=(201,),
                )

        self_status = self.status
        stdout = io.StringIO()
        stderr = io.StringIO()
        with (
            patch("daily_quest.load_tokens", return_value={"userid": "private-user"}),
            patch("daily_quest.resolve_game_endpoint", return_value=self.endpoint),
            patch("daily_quest.DailyQuestClient", FakeClient),
            redirect_stdout(stdout),
            redirect_stderr(stderr),
        ):
            self.assertEqual(main(["status", "--result-log", str(log_path)]), 0)
            self.assertEqual(main(["claim", "--result-log", str(log_path)]), 0)

        records = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]
        self.assertEqual([record["operation"] for record in records], ["status", "claim"])
        self.assertEqual(records[1]["details"]["claimed_task_ids"], [104, 201])
        self.assertEqual(records[1]["details"]["claimed_reward_ids"], [101, 201])
        self.assertEqual(records[1]["details"]["claimed_daily_task_ids"], [104])
        self.assertEqual(records[1]["details"]["claimed_weekly_task_ids"], [201])
        self.assertEqual(records[1]["details"]["claimed_daily_reward_ids"], [101])
        self.assertEqual(records[1]["details"]["claimed_weekly_reward_ids"], [201])
        self.assertTrue(all(instance.closed for instance in FakeClient.instances))
        self.assertNotIn("private-user", stdout.getvalue())
        self.assertNotIn("game-token", stdout.getvalue())
        self.assertIn(str(log_path.resolve()), stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
