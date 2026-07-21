from __future__ import annotations

import unittest

from app.services.arena_service import ArenaService
from dragon_arena import (
    DragonArenaChallengeResponse,
    DragonArenaChallengeResult,
    DragonArenaChoiceResult,
    DragonArenaInfo,
    DragonArenaMatchResponse,
    DragonArenaOpponent,
    DragonArenaRoundResult,
)
from harvest_fief import GameEndpoint, ItemChange


class InMemoryArenaClient:
    def __init__(self) -> None:
        self.entered = False
        self.closed = False
        self.matched = False
        self.match_calls = 0
        self.choice_ids: list[int] = []
        self.challenged: list[bool] = []
        self.rounds = 0

    def __enter__(self) -> "InMemoryArenaClient":
        self.entered = True
        return self

    def close(self) -> None:
        self.closed = True

    def resume_pending_battle(self, *, mercy_choice_id: int) -> None:
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
        self, index: int, *, mercy_choice_id: int
    ) -> DragonArenaRoundResult:
        self.rounds += 1
        self.challenged[index - 1] = True
        if self.rounds == 1:
            self.choice_ids.append(mercy_choice_id)
            return DragonArenaRoundResult(
                index=index,
                challenge=DragonArenaChallengeResponse(0, index, 0, True, 0),
                battle=DragonArenaChallengeResult(True, index, False, 100, 10, 1, 0, 3),
                mercy=DragonArenaChoiceResult(
                    0,
                    mercy_choice_id,
                    97,
                    -3,
                    False,
                    0,
                    0,
                    4,
                    (ItemChange(440, 4, 8848),),
                ),
            )
        return DragonArenaRoundResult(
            index=index,
            challenge=DragonArenaChallengeResponse(0, index, 0, True, 0),
            battle=DragonArenaChallengeResult(False, index, False, 94, -3, 0, 0, 4),
            mercy=None,
        )


class ArenaServiceTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.clients: list[InMemoryArenaClient] = []
        self.endpoint = GameEndpoint("ws://test.invalid", "token", "4101", "真实一区")
        self.service = ArenaService(live_client_builder=self._build_client)

    def _build_client(self, _endpoint: GameEndpoint, _log):
        client = InMemoryArenaClient()
        self.clients.append(client)
        return client

    def test_snapshot_projects_live_server_state(self) -> None:
        snapshot = self.service.snapshot(self.endpoint)

        self.assertEqual(snapshot["level"], 32)
        self.assertEqual(snapshot["score"], 90)
        self.assertEqual(snapshot["stage"], {"id": 33, "name": "未知竞技场阶段（ID 33）"})
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
        self.assertTrue(any(message == "当前对手已耗尽，正在寻找新对手" for _, message, _ in events))

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


if __name__ == "__main__":
    unittest.main()
