from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta
from typing import Any, Callable
from zoneinfo import ZoneInfo

from .config_store import ConfigStore
from .job_manager import JobManager


BEIJING_TIMEZONE = ZoneInfo("Asia/Shanghai")


class AutoTaskScheduler:
    """Run a callback at configured intervals while the local app is alive."""

    def __init__(
        self,
        config_store: ConfigStore,
        jobs: JobManager,
        launch: Callable[[], str | None],
    ) -> None:
        self._config_store = config_store
        self._jobs = jobs
        self._launch = launch
        self._lock = threading.RLock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._last_checked_at: datetime | None = None
        self._next_check_at: datetime | None = None
        self._last_status = "等待启用"
        self._last_job_id: str | None = None
        self._thread = threading.Thread(
            target=self._run,
            name="auto-task-scheduler",
            daemon=True,
        )

    def start(self) -> None:
        if not self._thread.is_alive():
            self._thread.start()

    def shutdown(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread.is_alive():
            self._thread.join(timeout=2.0)

    def wake(self) -> None:
        self._wake.set()

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "last_checked_at": _timestamp(self._last_checked_at),
                "next_check_at": _timestamp(self._next_check_at),
                "last_status": self._last_status,
                "last_job_id": self._last_job_id,
            }

    def _run(self) -> None:
        due_at: datetime | None = None
        while not self._stop.is_set():
            settings = self._config_store.snapshot().auto_tasks
            if not settings.scheduler_enabled:
                due_at = None
                self._set_state(next_check_at=None, status="定时执行未启用")
                self._wait(None)
                continue

            now = datetime.now(BEIJING_TIMEZONE)
            if due_at is None:
                due_at = now + timedelta(minutes=settings.interval_minutes)
            if now < due_at:
                self._set_state(next_check_at=due_at, status="等待下一次检查")
                if self._wait((due_at - now).total_seconds()):
                    due_at = None
                continue

            self._set_state(last_checked_at=now, next_check_at=None)
            if self._jobs.active_job_id() is not None:
                self._set_state(status="当前有作业运行，已跳过本轮")
            else:
                try:
                    job_id = self._launch()
                except Exception:
                    self._set_state(status="启动定时作业失败")
                else:
                    if job_id is None:
                        self._set_state(status="当前条件不满足，未启动任务")
                    else:
                        self._set_state(status="已启动定时作业", last_job_id=job_id)
            due_at = now + timedelta(minutes=settings.interval_minutes)

    def _wait(self, timeout: float | None) -> bool:
        self._wake.clear()
        self._wake.wait(timeout)
        if self._stop.is_set():
            return False
        return self._wake.is_set()

    def _set_state(
        self,
        *,
        last_checked_at: datetime | None = None,
        next_check_at: datetime | None = None,
        status: str | None = None,
        last_job_id: str | None = None,
    ) -> None:
        with self._lock:
            if last_checked_at is not None:
                self._last_checked_at = last_checked_at
            self._next_check_at = next_check_at
            if status is not None:
                self._last_status = status
            if last_job_id is not None:
                self._last_job_id = last_job_id


def _timestamp(value: datetime | None) -> str | None:
    return value.isoformat(timespec="seconds") if value is not None else None
