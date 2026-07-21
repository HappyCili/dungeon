from __future__ import annotations

import logging
import threading
import uuid
from collections import deque
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

from .models import JobEvent, JobStatus, TERMINAL_JOB_STATUSES


Runner = Callable[[Callable[[str, str, dict[str, Any]], None], Callable[[], bool]], dict[str, Any]]
LOGGER = logging.getLogger(__name__)


class JobConflictError(RuntimeError):
    pass


class JobNotFoundError(RuntimeError):
    pass


class JobExecutionError(RuntimeError):
    """A runner failure whose message is safe to render in the UI."""


@dataclass
class ManagedJob:
    job_id: str
    feature: str
    status: JobStatus = JobStatus.QUEUED
    created_at: str = field(default_factory=lambda: _timestamp())
    started_at: str | None = None
    completed_at: str | None = None
    result: dict[str, Any] | None = None
    error_message: str | None = None
    cancel_requested: threading.Event = field(default_factory=threading.Event)
    events: deque[JobEvent] = field(default_factory=lambda: deque(maxlen=400))
    next_sequence: int = 1
    progress: dict[str, Any] = field(default_factory=dict)
    future: Future[None] | None = None


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class JobManager:
    def __init__(self) -> None:
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="daily-console")
        self._jobs: dict[str, ManagedJob] = {}
        self._active_job_id: str | None = None
        self._lock = threading.RLock()

    def start(self, feature: str, runner: Runner) -> dict[str, Any]:
        with self._lock:
            if self._active_job_id is not None:
                active = self._jobs[self._active_job_id]
                if active.status not in TERMINAL_JOB_STATUSES:
                    raise JobConflictError("已有任务正在运行")
            job = ManagedJob(job_id=uuid.uuid4().hex, feature=feature)
            self._jobs[job.job_id] = job
            self._active_job_id = job.job_id
            self._append_event_locked(job, "info", "任务已进入队列", {})
            job.future = self._executor.submit(self._run, job.job_id, runner)
            return self._snapshot_locked(job, after_sequence=0)

    def _run(self, job_id: str, runner: Runner) -> None:
        with self._lock:
            job = self._jobs[job_id]
            job.status = JobStatus.RUNNING
            job.started_at = _timestamp()
            self._append_event_locked(job, "info", "任务开始运行", {})

        def emit(level: str, message: str, data: dict[str, Any]) -> None:
            with self._lock:
                current_job = self._jobs[job_id]
                current_job.progress = data
                self._append_event_locked(current_job, level, message, data)

        try:
            result = runner(emit, job.cancel_requested.is_set)
        except JobExecutionError as exc:
            failure_message = str(exc).strip() or "任务执行失败"
            with self._lock:
                job = self._jobs[job_id]
                job.status = JobStatus.FAILED
                job.error_message = failure_message
                job.completed_at = _timestamp()
                self._append_event_locked(job, "error", job.error_message, {})
            return
        except Exception:
            LOGGER.exception("Unhandled %s job failure", job.feature)
            with self._lock:
                job = self._jobs[job_id]
                job.status = JobStatus.FAILED
                job.error_message = "任务执行失败"
                job.completed_at = _timestamp()
                self._append_event_locked(job, "error", job.error_message, {})
            return

        with self._lock:
            job = self._jobs[job_id]
            job.result = result
            job.completed_at = _timestamp()
            if job.cancel_requested.is_set() or result.get("cancelled"):
                job.status = JobStatus.CANCELLED
                self._append_event_locked(job, "warning", "任务已停止", result)
            else:
                job.status = JobStatus.SUCCEEDED
                self._append_event_locked(job, "success", "任务已完成", result)

    def _append_event_locked(
        self, job: ManagedJob, level: str, message: str, data: dict[str, Any]
    ) -> None:
        job.events.append(
            JobEvent(
                sequence=job.next_sequence,
                timestamp=_timestamp(),
                level=level,
                feature=job.feature,
                message=message,
                data=data,
            )
        )
        job.next_sequence += 1

    def request_cancel(self, job_id: str) -> dict[str, Any]:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                raise JobNotFoundError("作业不存在")
            if job.status not in TERMINAL_JOB_STATUSES:
                job.cancel_requested.set()
                job.status = JobStatus.STOPPING
                self._append_event_locked(job, "warning", "已请求在当前操作结束后停止", {})
            return self._snapshot_locked(job, after_sequence=0)

    def snapshot(self, job_id: str, after_sequence: int = 0) -> dict[str, Any]:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                raise JobNotFoundError("作业不存在")
            return self._snapshot_locked(job, after_sequence)

    def _snapshot_locked(self, job: ManagedJob, after_sequence: int) -> dict[str, Any]:
        return {
            "id": job.job_id,
            "feature": job.feature,
            "status": job.status.value,
            "created_at": job.created_at,
            "started_at": job.started_at,
            "completed_at": job.completed_at,
            "progress": job.progress,
            "result": job.result,
            "error_message": job.error_message,
            "last_sequence": job.next_sequence - 1,
            "events": [
                event.to_dict()
                for event in job.events
                if event.sequence > after_sequence
            ],
        }

    def active_job_id(self) -> str | None:
        with self._lock:
            if self._active_job_id is None:
                return None
            job = self._jobs[self._active_job_id]
            return job.job_id if job.status not in TERMINAL_JOB_STATUSES else None

    def shutdown(self) -> None:
        self._executor.shutdown(wait=True, cancel_futures=False)
