from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from app.services.treasure_service import (
    TreasureService,
    aggregate_loot_entries,
    format_loot_summary,
)
from harvest_fief import GameEndpoint, ItemChange
from treasure_area import TreasureAreaStatus, TreasureSweepLoot, TreasureSweepResponse


class FakeTreasureClient:
    def __init__(self, state: dict[str, object]) -> None:
        self.state = state
        self.closed = False

    def get_status(self) -> TreasureAreaStatus:
        loot = self.state.get("loot")
        cleared_loot = self.state.get("cleared_loot")
        return TreasureAreaStatus(
            open_times=1,
            refresh_seconds=60,
            swept_today=int(self.state["swept_today"]),
            daily_sweep_limit=int(self.state["daily_sweep_limit"]),
            area_ids=tuple(self.state["area_ids"]),  # type: ignore[arg-type]
            sweep_loot=loot if isinstance(loot, TreasureSweepLoot) else None,
            cleared_sweep_loot=(
                cleared_loot if isinstance(cleared_loot, TreasureSweepLoot) else None
            ),
        )

    def sweep(self, area_id: int, times: int) -> TreasureSweepResponse:
        self.state["sweep_calls"] = list(self.state.get("sweep_calls") or []) + [
            (area_id, times)
        ]
        self.state["swept_today"] = int(self.state["swept_today"]) + times
        self.state["loot"] = TreasureSweepLoot(
            area_id=area_id,
            items=(ItemChange(item_id=1, delta=3, total=10),),
        )
        return TreasureSweepResponse(ret=0, status=self.get_status())

    def close(self) -> None:
        self.closed = True


class TreasureServiceTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.endpoint = GameEndpoint("ws://test.invalid", "token", "4101", "真实一区")
        self.state: dict[str, object] = {
            "swept_today": 2,
            "daily_sweep_limit": 8,
            "area_ids": (1001,),
            "sweep_calls": [],
        }
        self.events: list[tuple[str, str, dict]] = []

    def _service(self, destination: object) -> TreasureService:
        return TreasureService(
            live_client_builder=lambda _endpoint: FakeTreasureClient(self.state),
            result_log_destination=destination,
        )

    def test_run_emits_named_rewards_and_writes_standard_log(self) -> None:
        with TemporaryDirectory() as directory:
            log_path = Path(directory) / "treasure.jsonl"
            service = self._service(log_path)
            result = service.run(
                self.endpoint,
                1001,
                2,
                emit=lambda level, message, data: self.events.append(
                    (level, message, data)
                ),
                stop_requested=lambda: False,
            )

            self.assertFalse(result["cancelled"])
            # 1001 在 mapareas 中有正式名时用表名；无表时回退「未知聚宝地图」
            self.assertEqual(result["request"]["area_id"], 1001)
            self.assertTrue(str(result["request"]["area_name"]).strip())
            self.assertEqual(
                result["rewards"],
                [{"id": 1, "name": "金币", "delta": 3, "total": 10}],
            )
            self.assertIn("金币", result["summary"])
            self.assertTrue(any(level == "success" for level, _, _ in self.events))
            success_messages = [
                message for level, message, _ in self.events if level == "success"
            ]
            self.assertTrue(any("金币" in message for message in success_messages))

            record = json.loads(log_path.read_text(encoding="utf-8"))
            self.assertEqual(record["event"], "treasure_area")
            self.assertEqual(record["operation"], "sweep")
            self.assertEqual(record["outcome"], "success")
            self.assertEqual(record["zone"], {"id": "4101", "name": "真实一区"})
            self.assertEqual(record["details"]["area_id"], 1001)
            self.assertEqual(record["details"]["area_name"], result["request"]["area_name"])
            self.assertEqual(record["details"]["rewards"][0]["name"], "金币")
            self.assertNotIn("token", json.dumps(record).lower())
            self.assertNotIn("url", json.dumps(record["details"]).lower())

    def test_format_loot_summary_uses_item_names(self) -> None:
        summary = format_loot_summary(
            [{"id": 1, "name": "金币", "delta": 5, "total": 12}]
        )
        self.assertEqual(summary, "金币：+5（当前 12）")

    def test_snapshot_surfaces_and_persists_acknowledged_sweep_result(self) -> None:
        self.state["cleared_loot"] = TreasureSweepLoot(
            area_id=1001,
            items=(ItemChange(item_id=1, delta=4, total=14),),
        )
        with TemporaryDirectory() as directory:
            log_path = Path(directory) / "treasure.jsonl"
            payload = self._service(log_path).snapshot(self.endpoint, 1001)

            self.assertEqual(
                payload["cleared_result"],
                {
                    "acknowledged": True,
                    "area_id": 1001,
                    "area_name": payload["areas"][0]["name"],
                    "rewards": [
                        {"id": 1, "name": "金币", "delta": 4, "total": 14}
                    ],
                    "summary": "金币：+4（当前 14）",
                },
            )
            record = json.loads(log_path.read_text(encoding="utf-8"))
            self.assertEqual(record["operation"], "clear_result")
            self.assertTrue(record["details"]["acknowledged_while_refreshing_status"])

    def test_format_loot_summary_merges_duplicate_items(self) -> None:
        """同一扫荡结果里多次掉落同物品时，日志只显示合并后的一行。"""

        summary = format_loot_summary(
            [
                {"id": 1, "name": "炉温", "delta": 3, "total": 1085},
                {"id": 2, "name": "污物", "delta": 3, "total": 459},
                {"id": 1, "name": "炉温", "delta": 3, "total": 1088},
                {"id": 1, "name": "炉温", "delta": 5, "total": 1093},
                {"id": 3, "name": "白橡木", "delta": 3, "total": 464},
                {"id": 1, "name": "炉温", "delta": 3, "total": 1108},
            ]
        )
        self.assertEqual(
            summary,
            "炉温：+14（当前 1108）、污物：+3（当前 459）、白橡木：+3（当前 464）",
        )
        # 每种物品只出现一次
        self.assertEqual(summary.count("炉温"), 1)
        self.assertEqual(summary.count("污物"), 1)

    def test_aggregate_loot_entries_sums_delta_and_keeps_last_total(self) -> None:
        merged = aggregate_loot_entries(
            (
                ItemChange(item_id=1, delta=3, total=100),
                ItemChange(item_id=1, delta=5, total=105),
                ItemChange(item_id=2, delta=2, total=20),
            )
        )
        self.assertEqual(len(merged), 2)
        self.assertEqual(merged[0]["id"], 1)
        self.assertEqual(merged[0]["delta"], 8)
        self.assertEqual(merged[0]["total"], 105)
        self.assertEqual(merged[0]["name"], "金币")
        self.assertEqual(merged[1]["id"], 2)
        self.assertEqual(merged[1]["delta"], 2)
        self.assertEqual(merged[1]["total"], 20)


if __name__ == "__main__":
    unittest.main()
