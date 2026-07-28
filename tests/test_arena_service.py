from __future__ import annotations

import unittest

from app.job_manager import JobExecutionError
from app.services.arena_service import (
    ArenaService,
    describe_login_kickout,
    format_run_summary,
    format_status_line,
)
from dragon_arena import (
    DragonArenaChallengeResponse,
    DragonArenaChallengeResult,
    DragonArenaChoiceResult,
    DragonArenaInfo,
    DragonArenaMatchResponse,
    DragonArenaOpponent,
    DragonArenaRoundResult,
    GameLoginKickout,
)
from harvest_fief import GameEndpoint, ItemChange


class InMemoryArenaClient:
    def __init__(self, *, fail_enter: Exception | None = None) -> None:
        self.entered = False
        self.closed = False
        self.matched = False
        self.match_calls = 0
        self.choice_ids: list[int] = []
        self.challenged: list[bool] = []
        self.rounds = 0
        self._fail_enter = fail_enter

    def __enter__(self) -> "InMemoryArenaClient":
        if self._fail_enter is not None:
            raise self._fail_enter
        self.entered = True
        return self

    def close(self) -> None:
        self.closed = True

    def resume_pending_battle(
        self, *, win_choice_id: int | None = None, mercy_choice_id: int | None = None
    ) -> None:
        return None

    def get_info(self) -> DragonArenaInfo:
        opponents = (
            tuple(
                DragonArenaOpponent(1000 + index, challenged)
                for index, challenged in enumerate(self.challenged, start=1)
            )
            if self.matched
            else ()
        )
        return DragonArenaInfo(
            level=32,
            clearance_time=0,
            opponents=opponents,
            rewards=(),
            score=97 if self.rounds else 90,
            choice_pending=0,
            choice_id=0,
            buff_choice_id=0,
            buff_choice_index=0,
            stage_id=33,
            daily_reward_num=4,
            daily_reward_received=True,
        )

    def match(self) -> DragonArenaMatchResponse:
        self.match_calls += 1
        self.matched = True
        self.challenged = [False, False]
        return DragonArenaMatchResponse(
            ret=0,
            opponents=tuple(
                DragonArenaOpponent(1000 + index, False) for index in range(1, 3)
            ),
        )

    def run_round(
        self,
        index: int,
        *,
        win_choice_id: int | None = None,
        mercy_choice_id: int | None = None,
    ) -> DragonArenaRoundResult:
        self.rounds += 1
        self.challenged[index - 1] = True
        choice_id = (
            win_choice_id if win_choice_id is not None else (mercy_choice_id or 0)
        )
        if self.rounds == 1:
            self.choice_ids.append(choice_id)
            return DragonArenaRoundResult(
                index=index,
                challenge=DragonArenaChallengeResponse(0, index, 0, True, 0),
                battle=DragonArenaChallengeResult(True, index, False, 100, 10, 1, 0, 3),
                mercy=DragonArenaChoiceResult(
                    0,
                    choice_id,
                    97,
                    -3,
                    False,
                    0,
                    0,
                    4,
                    (ItemChange(440, 4, 8848),),
                    ((440, 4),),
                ),
            )
        return DragonArenaRoundResult(
            index=index,
            challenge=DragonArenaChallengeResponse(0, index, 0, True, 0),
            battle=DragonArenaChallengeResult(False, index, False, 94, -3, 0, 0, 4),
            mercy=None,
        )


class UnsettledArenaClient(InMemoryArenaClient):
    def run_round(
        self,
        index: int,
        *,
        win_choice_id: int | None = None,
        mercy_choice_id: int | None = None,
    ) -> DragonArenaRoundResult:
        self.rounds += 1
        self.challenged[index - 1] = True
        return DragonArenaRoundResult(
            index=index,
            challenge=DragonArenaChallengeResponse(0, index, 0, True, 0),
            battle=None,
            mercy=None,
        )


class ArenaServiceTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.clients: list[InMemoryArenaClient] = []
        self.endpoint = GameEndpoint("ws://test.invalid", "token", "4101", "真实一区")
        self.enter_errors: list[Exception | None] = []
        self.service = ArenaService(
            live_client_builder=self._build_client,
            kickout_retry_delay=0,
            result_log_destination=None,
        )

    def _build_client(self, endpoint: GameEndpoint, _log):
        fail_enter = self.enter_errors.pop(0) if self.enter_errors else None
        client = InMemoryArenaClient(fail_enter=fail_enter)
        client.endpoint = endpoint  # type: ignore[attr-defined]
        self.clients.append(client)
        return client

    def test_snapshot_projects_live_server_state(self) -> None:
        snapshot = self.service.snapshot(self.endpoint)

        self.assertEqual(snapshot["level"], 32)
        self.assertEqual(snapshot["score"], 90)
        self.assertEqual(snapshot["stage"], {"id": 33, "name": "未知竞技场阶段（ID 33）"})
        self.assertEqual(snapshot["choice"]["name"], "无")
        self.assertEqual(
            snapshot["opponents"], {"total": 0, "available": 0, "challenged": 0}
        )
        self.assertEqual(snapshot["daily_reward"], {"received": True, "count": 4})
        self.assertTrue(self.clients[0].entered)
        self.assertTrue(self.clients[0].closed)

    def test_run_matches_then_uses_server_round_results(self) -> None:
        events: list[tuple[str, str, dict[str, object]]] = []
        result = self.service.run(
            self.endpoint,
            2,
            "mercy",
            True,
            lambda level, message, data: events.append((level, message, data)),
            lambda: False,
        )

        client = self.clients[0]
        stats = result["arena"]
        self.assertFalse(result["cancelled"])
        self.assertEqual(client.match_calls, 1)
        self.assertEqual(client.choice_ids, [2])
        self.assertTrue(client.closed)
        self.assertEqual(stats["completed_rounds"], 2)
        self.assertEqual(stats["wins"], 1)
        self.assertEqual(stats["losses"], 1)
        self.assertEqual(stats["score_delta"], 4)
        self.assertEqual(stats["dragon_coin_delta"], 4)
        self.assertEqual(
            stats["rewards"],
            [{"id": 440, "name": "龙痕币", "delta": 4, "total": 8848}],
        )
        self.assertEqual(stats["dragon_coin_total"], 8848)
        messages = [message for _, message, _ in events]
        self.assertTrue(any("开始战斗" in message for message in messages))
        self.assertTrue(any("战斗进行中" in message for message in messages))
        self.assertTrue(any("战斗完成：胜利" in message for message in messages))
        self.assertTrue(any(message == "胜利抉择：仁慈" for message in messages))
        self.assertTrue(any("剩余对手" in message and "进度" in message for message in messages))
        self.assertTrue(any("第 1 轮已完成" in message for message in messages))
        self.assertTrue(any("对手已耗尽，正在寻找新对手" in message for message in messages))
        self.assertTrue(any(message.startswith("全部完成") for message in messages))
        self.assertIn("1 胜", stats["last_result"])
        self.assertIn("1 负", stats["last_result"])
        self.assertIn("共 2 场", stats["last_result"])

    def test_execute_uses_the_execute_choice_slot(self) -> None:
        self.service.run(
            self.endpoint,
            1,
            "execute",
            True,
            lambda _level, _message, _data: None,
            lambda: False,
        )

        self.assertEqual(self.clients[0].choice_ids, [1])

    def test_unsettled_round_is_not_counted_or_reported_complete(self) -> None:
        client = UnsettledArenaClient()
        service = ArenaService(
            live_client_builder=lambda _endpoint, _log: client,
            kickout_retry_delay=0,
            result_log_destination=None,
        )
        events: list[tuple[str, str, dict[str, object]]] = []

        result = service.run(
            self.endpoint,
            2,
            "mercy",
            True,
            lambda level, message, data: events.append((level, message, data)),
            lambda: False,
        )

        stats = result["arena"]
        self.assertEqual(stats["completed_rounds"], 0)
        self.assertEqual(stats["wins"], 0)
        self.assertEqual(stats["losses"], 0)
        self.assertEqual(stats["stop_reason"], "未收到服务端战斗结算")
        self.assertEqual(stats["stage"], "未完成")
        messages = [message for _, message, _ in events]
        self.assertTrue(any("未收到战斗结算" in message for message in messages))
        self.assertTrue(any(message.startswith("状态未更新") for message in messages))
        self.assertTrue(any("进度 0/2" in message for message in messages))
        self.assertFalse(any("第 1 轮已完成" in message for message in messages))
        self.assertTrue(any(message.startswith("未完成") for message in messages))

    def test_kickout_ret2_refreshes_endpoint_and_retries_login(self) -> None:
        refreshed = GameEndpoint("ws://retry.invalid", "token-2", "4101", "真实一区")
        self.enter_errors = [GameLoginKickout(2, "会话已切换"), None]
        events: list[str] = []

        result = self.service.run(
            self.endpoint,
            1,
            "mercy",
            False,
            lambda _level, message, _data: events.append(message),
            lambda: False,
            refresh_endpoint=lambda: refreshed,
        )

        self.assertFalse(result["cancelled"])
        self.assertEqual(len(self.clients), 2)
        self.assertTrue(self.clients[0].closed)
        self.assertEqual(self.clients[1].endpoint.url, "ws://retry.invalid")
        self.assertTrue(
            any("重新进入区服" in message and "重试" in message for message in events)
        )

    def test_opponent_picker_is_used_instead_of_list_order(self) -> None:
        picked: list[int] = []

        def always_last(candidates):
            index = candidates[-1]
            picked.append(index)
            return index

        service = ArenaService(
            live_client_builder=self._build_client,
            kickout_retry_delay=0,
            result_log_destination=None,
            opponent_picker=always_last,
        )
        service.run(
            self.endpoint,
            1,
            "mercy",
            True,
            lambda _level, _message, _data: None,
            lambda: False,
        )
        # 刷新后有两名未挑战对手；应挑战列表末位而不是首位。
        self.assertEqual(picked, [2])

    def test_status_and_summary_lines_are_human_readable(self) -> None:
        stats = {
            "score": 97,
            "score_delta": 7,
            "dragon_coin_delta": 4,
            "dragon_coin_total": 8848,
            "completed_rounds": 1,
            "requested_rounds": 10,
            "opponents": {"total": 15, "available": 10, "challenged": 5},
            "wins": 1,
            "losses": 0,
        }
        status = format_status_line(stats)
        self.assertIn("积分 97", status)
        self.assertIn("龙痕币 8848", status)
        self.assertIn("剩余对手 10/15", status)
        self.assertIn("进度 1/10", status)
        summary = format_run_summary(stats)
        self.assertTrue(summary.startswith("全部完成"))
        self.assertIn("1 胜", summary)
        self.assertIn("共 1 场", summary)

    def test_kickout_without_recovery_surfaces_specific_message(self) -> None:
        self.enter_errors = [GameLoginKickout(2, "会话已切换")]

        with self.assertRaises(JobExecutionError) as raised:
            self.service.run(
                self.endpoint,
                1,
                "mercy",
                False,
                lambda _level, _message, _data: None,
                lambda: False,
            )

        self.assertIn("ret 2", str(raised.exception))
        self.assertIn("需重新进入区服", str(raised.exception))
        self.assertNotIn("过期", describe_login_kickout(GameLoginKickout(2)))
        self.assertIn("令牌过期", describe_login_kickout(GameLoginKickout(51)))


if __name__ == "__main__":
    unittest.main()
