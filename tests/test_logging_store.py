from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from logging_store import MANAGED_DESTINATION, LogPersistenceError, write_standard_log


class LoggingStoreTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.reference = datetime(2026, 7, 20, 12, tzinfo=timezone.utc)
        self.details = {
            "claimed_task_ids": [], "claimed_reward_ids": [],
            "claimed_daily_task_ids": [], "claimed_weekly_task_ids": [],
            "claimed_daily_reward_ids": [], "claimed_weekly_reward_ids": [],
            "status": {"daily_remaining_seconds": 0, "daily_reset_seconds": 0,
                       "weekly_remaining_seconds": 0, "weekly_reset_seconds": 0,
                       "claimed_reward_ids": [], "claimed_daily_reward_ids": [],
                       "claimed_weekly_reward_ids": [], "tasks": []},
        }

    def write(self, root: Path, **kwargs):
        details = kwargs.pop("details", self.details)
        return write_standard_log(
            event="daily_quest", operation="status", zone={"id": "1", "name": ""},
            details=details, managed_root=root, clock=lambda: self.reference, **kwargs,
        )

    def test_managed_write_normalizes_zone_and_partitions_by_event_and_date(self) -> None:
        with TemporaryDirectory() as directory:
            result = self.write(Path(directory), timestamp=self.reference)
            self.assertEqual(result.path.name, "2026-07-20.jsonl")
            self.assertEqual(result.record["zone"], {"id": "1", "name": "1"})
            self.assertEqual(json.loads(result.path.read_text()), result.record)

    def test_none_disables_persistence(self) -> None:
        with TemporaryDirectory() as directory:
            self.assertIsNone(self.write(Path(directory), destination=None))
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_rejects_future_and_sensitive_details_before_write(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaisesRegex(LogPersistenceError, "future_timestamp"):
                self.write(root, timestamp=self.reference + timedelta(minutes=5, seconds=1))
            with self.assertRaisesRegex(LogPersistenceError, "sensitive_details"):
                self.write(root, details={"raw_payload": "x"})
            self.assertEqual(list(root.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
