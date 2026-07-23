"""罪者深渊服务层单元测试（不连游戏服）。"""

from __future__ import annotations

import unittest
from typing import Any, Callable

from app.services.abyss_service import AbyssService, format_run_summary, status_to_payload
from grave_abyss import (
    AbyssBattleResult,
    AbyssRoundResult,
    AbyssStatus,
    GraveChallengeResponse,
)
from harvest_fief import GameEndpoint


class FakeAbyssClient:
    def __init__(self, status: AbyssStatus, rounds: list[AbyssRoundResult]) -> None:
        self._status = status
        self._rounds = rounds
        self.closed = False

    def __enter__(self) -> "FakeAbyssClient":
        return self

    def close(self) -> None:
        self.closed = True

    def get_status(self, *, sync: bool = True) -> AbyssStatus:
        return self._status

    def run_loop(
        self,
        *,
        stop_requested: Callable[[], bool] | None = None,
        max_rounds: int = 0,
        ensure_buff: bool = True,
        on_round: Callable[[AbyssRoundResult, AbyssStatus], None] | None = None,
    ) -> tuple[AbyssRoundResult, ...]:
        results: list[AbyssRoundResult] = []
        for result in self._rounds:
            if stop_requested and stop_requested():
                break
            results.append(result)
            if on_round is not None:
                # 模拟胜利后 pass 推进
                if result.battle and result.battle.win:
                    self._status = AbyssStatus(
                        season_id=self._status.season_id,
                        season_name=self._status.season_name,
                        season_open=True,
                        left_seconds=1000,
                        group_id=3,
                        pass_id=result.challenge_id,
                        pass_level=result.level,
                        next_id=result.challenge_id + 1,
                        next_level=result.level + 1,
                        next_name=f"罪者深渊-{result.level + 1}",
                        max_level=900,
                        currgrave=0,
                        optbuf=201,
                        optbuf_desc="测试增益",
                        actives=0,
                        total_floors=900,
                    )
                on_round(result, self._status)
            if result.battle is None or not result.battle.win:
                break
            if max_rounds > 0 and len(results) >= max_rounds:
                break
        return tuple(results)


class AbyssServiceTests(unittest.TestCase):
    def test_status_payload(self) -> None:
        status = AbyssStatus(
            season_id=1024,
            season_name="第24赛季",
            season_open=True,
            left_seconds=3600,
            group_id=3,
            pass_id=30010,
            pass_level=10,
            next_id=30011,
            next_level=11,
            next_name="罪者深渊-11",
            max_level=900,
            currgrave=0,
            optbuf=201,
            optbuf_desc="测试",
            actives=1,
            total_floors=900,
        )
        payload = status_to_payload(status)
        self.assertEqual(payload["pass_level"], 10)
        self.assertEqual(payload["next_id"], 30011)

    def test_run_until_loss(self) -> None:
        status = AbyssStatus(
            season_id=1024,
            season_name="第24赛季",
            season_open=True,
            left_seconds=3600,
            group_id=3,
            pass_id=30001,
            pass_level=1,
            next_id=30002,
            next_level=2,
            next_name="罪者深渊-2",
            max_level=900,
            currgrave=0,
            optbuf=201,
            optbuf_desc="测试",
            actives=0,
            total_floors=900,
        )
        rounds = [
            AbyssRoundResult(
                30002,
                2,
                "罪者深渊-2",
                GraveChallengeResponse(30002, 0, 1),
                AbyssBattleResult(30002, 2, "罪者深渊-2", True, 2, 10),
            ),
            AbyssRoundResult(
                30003,
                3,
                "罪者深渊-3",
                GraveChallengeResponse(30003, 0, 1),
                AbyssBattleResult(30003, 3, "罪者深渊-3", False, 1, 8),
            ),
        ]
        fake = FakeAbyssClient(status, rounds)
        service = AbyssService(
            live_client_builder=lambda _endpoint, _log: fake,
            result_log_destination=None,
        )
        events: list[tuple[str, str]] = []
        endpoint = GameEndpoint("ws://test.invalid", "token", "1", "测试区")

        def emit(level: str, message: str, _data: dict[str, Any]) -> None:
            events.append((level, message))

        result = service.run(
            endpoint,
            emit,
            lambda: False,
            max_rounds=0,
            auto_buff=True,
        )
        self.assertFalse(result["cancelled"])
        abyss = result["abyss"]
        self.assertEqual(abyss["wins"], 1)
        self.assertEqual(abyss["losses"], 1)
        self.assertEqual(abyss["completed_rounds"], 2)
        self.assertEqual(abyss["stop_reason"], "战斗失败")
        self.assertTrue(fake.closed)
        summary = format_run_summary(abyss)
        self.assertIn("1 胜", summary)
        self.assertIn("1 负", summary)


if __name__ == "__main__":
    unittest.main()
