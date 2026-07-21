from __future__ import annotations

import threading
from typing import Any, Callable

from daily_actions import (
    DailyActionResult,
    DailyActionRunner,
    build_live_daily_action_runner,
)
from daily_quest import DailyQuestStatus
from harvest_fief import GameEndpoint
from id_descriptions import activity_reward_name

from app.models import DAILY_REWARD_THRESHOLDS, DAILY_TASKS, DailyTaskDefinition


class DailyServiceError(RuntimeError):
    """尚未建立真实游戏服会话，或真实客户端初始化失败。"""


LiveRunnerBuilder = Callable[[GameEndpoint], DailyActionRunner]


class DailyService:
    """日常页面的状态投影与阶段 B 动作编排入口。"""

    def __init__(
        self,
        *,
        live_runner_builder: LiveRunnerBuilder | None = None,
        game_timeout: float = 15.0,
    ) -> None:
        self._lock = threading.RLock()
        self._runner: DailyActionRunner | None = None
        self._live_runner_builder = live_runner_builder or (
            lambda endpoint: build_live_daily_action_runner(endpoint, game_timeout)
        )
        self._results = {task.task_id: "等待" for task in DAILY_TASKS}

    @property
    def runner(self) -> DailyActionRunner:
        with self._lock:
            if self._runner is None:
                raise DailyServiceError("请先登录、选择区服并刷新日常状态")
            return self._runner

    def clear_game_server(self) -> None:
        """丢弃旧账号或区服对应的日常运行器。"""

        with self._lock:
            self._runner = None
            self._results = {task.task_id: "等待" for task in DAILY_TASKS}

    def use_game_server(self, endpoint: GameEndpoint) -> None:
        """切换到当前登录区服的真实 WebSocket 日常编排器。"""

        self.clear_game_server()
        try:
            runner = self._live_runner_builder(endpoint)
        except Exception as exc:
            raise DailyServiceError("初始化游戏服日常客户端失败") from exc
        with self._lock:
            self._runner = runner
            self._results = {task.task_id: "等待" for task in DAILY_TASKS}

    def snapshot(self, selected_task_ids: list[int]) -> dict[str, Any]:
        runner = self.runner
        status = runner.status()
        with self._lock:
            tasks = [
                self._task_payload(task, selected_task_ids, status, runner)
                for task in DAILY_TASKS
            ]
        completed = sum(
            status.task(task["id"]) is not None and status.task(task["id"]).finished
            for task in tasks
        )
        score = sum(
            task["activity_score"]
            for task in tasks
            if status.task(task["id"]) is not None
            and status.task(task["id"]).finished
        )
        next_reward = next(
            (threshold for threshold in DAILY_REWARD_THRESHOLDS if threshold > score), None
        )
        return {
            "tasks": tasks,
            "summary": {
                "activity_score": score,
                "next_reward": next_reward,
                "completed_count": completed,
                "total_count": len(DAILY_TASKS),
                "refresh_countdown": _format_countdown(status.daily_remaining_seconds),
                "claimed_reward_ids": list(status.daily_reward_ids),
                "claimed_daily_reward_ids": list(status.daily_reward_ids),
                "claimed_weekly_reward_ids": list(status.weekly_reward_ids),
                "claimed_reward_names": [
                    activity_reward_name(reward_id)
                    for reward_id in status.daily_reward_ids
                ],
                "claimed_daily_reward_names": [
                    activity_reward_name(reward_id)
                    for reward_id in status.daily_reward_ids
                ],
                "claimed_weekly_reward_names": [
                    activity_reward_name(reward_id)
                    for reward_id in status.weekly_reward_ids
                ],
                "weekly_refresh_countdown": _format_countdown(
                    status.weekly_remaining_seconds
                ),
            },
        }

    def _task_payload(
        self,
        task: DailyTaskDefinition,
        selected_task_ids: list[int],
        status: DailyQuestStatus,
        runner: DailyActionRunner,
    ) -> dict[str, Any]:
        state = status.task(task.task_id)
        progress = self._progress_for(task, status, runner)
        return {
            "id": task.task_id,
            "name": task.name,
            "target": task.target,
            "progress": progress,
            "activity_score": task.activity_score,
            "available": task.available,
            "implementation_status": task.implementation_status,
            "gap": task.gap,
            "selected": task.task_id in selected_task_ids,
            "result": self._results[task.task_id],
            "finished": bool(state and state.finished),
            "score_claimed": bool(state and state.score_claimed),
        }

    def _progress_for(
        self,
        task: DailyTaskDefinition,
        status: DailyQuestStatus,
        runner: DailyActionRunner,
    ) -> int:
        config = runner.catalog.tasks.get(task.task_id)
        state = status.task(task.task_id)
        if config is None:
            return task.target if state is not None and state.finished else 0
        progress = status.progress_for(config.quest_id)
        if progress is None:
            return task.target if state is not None and state.finished else 0
        return min(max(progress, 0), task.target)

    def run(
        self,
        task_ids: list[int],
        selected_task_ids: list[int],
        emit: Callable[[str, str, dict[str, Any]], None],
        stop_requested: Callable[[], bool],
    ) -> dict[str, Any]:
        runner = self.runner
        completed_in_run = 0
        task_results: list[dict[str, Any]] = []
        for task_id in task_ids:
            if stop_requested():
                return self._cancelled_result(
                    completed_in_run, task_results, selected_task_ids
                )
            task = next(item for item in DAILY_TASKS if item.task_id == task_id)
            with self._lock:
                self._results[task_id] = "运行中"
            emit(
                "info",
                f"正在执行 {task.name}",
                {
                    "phase": "running",
                    "task_id": task_id,
                    "task_name": task.name,
                    "completed_tasks": completed_in_run,
                    "total_tasks": len(task_ids),
                    "daily": self.snapshot(selected_task_ids),
                },
            )

            result = runner.run_task(task_id)
            task_results.append(_result_payload(result))
            with self._lock:
                self._results[task_id] = _display_result(result)
            if result.status == "completed":
                completed_in_run += 1
                level = "success"
                phase = "completed"
            elif result.status == "skipped":
                level = "info"
                phase = "skipped"
            elif result.status == "incomplete":
                level = "warning"
                phase = "incomplete"
            else:
                level = "error"
                phase = "failed"
            emit(
                level,
                f"{task.task_id} {task.name}：{result.message}",
                {
                    "phase": phase,
                    "task_id": task_id,
                    "completed_tasks": completed_in_run,
                    "total_tasks": len(task_ids),
                    "task_result": _result_payload(result),
                    "daily": self.snapshot(selected_task_ids),
                },
            )
            if stop_requested():
                return self._cancelled_result(
                    completed_in_run, task_results, selected_task_ids
                )

        claimed_task_ids: tuple[int, ...] = ()
        claimed_reward_ids: tuple[int, ...] = ()
        claimed_daily_task_ids: tuple[int, ...] = ()
        claimed_weekly_task_ids: tuple[int, ...] = ()
        claimed_daily_reward_ids: tuple[int, ...] = ()
        claimed_weekly_reward_ids: tuple[int, ...] = ()
        claim_error: str | None = None
        try:
            claims = runner.gateway.claim_available(runner.catalog)
            claimed_task_ids = claims.claimed_task_ids
            claimed_reward_ids = claims.claimed_reward_ids
            claimed_daily_task_ids = claims.claimed_daily_task_ids
            claimed_weekly_task_ids = claims.claimed_weekly_task_ids
            claimed_daily_reward_ids = claims.claimed_daily_reward_ids
            claimed_weekly_reward_ids = claims.claimed_weekly_reward_ids
            emit(
                "success",
                "奖励检查完成："
                f"任务积分 {len(claimed_task_ids)}，"
                f"每日奖励 {len(claimed_daily_reward_ids)}，"
                f"周奖励 {len(claimed_weekly_reward_ids)}",
                {
                    "phase": "claimed",
                    "claimed_task_ids": list(claimed_task_ids),
                    "claimed_reward_ids": list(claimed_reward_ids),
                    "claimed_daily_task_ids": list(claimed_daily_task_ids),
                    "claimed_weekly_task_ids": list(claimed_weekly_task_ids),
                    "claimed_daily_reward_ids": list(claimed_daily_reward_ids),
                    "claimed_weekly_reward_ids": list(claimed_weekly_reward_ids),
                    "daily": self.snapshot(selected_task_ids),
                },
            )
        except Exception as exc:
            claim_error = f"领取每日/周奖励失败（{type(exc).__name__}）"
            emit(
                "error",
                claim_error,
                {"phase": "claim_failed", "daily": self.snapshot(selected_task_ids)},
            )

        return {
            "cancelled": False,
            "completed_tasks": completed_in_run,
            "task_results": task_results,
            "claimed_task_ids": list(claimed_task_ids),
            "claimed_reward_ids": list(claimed_reward_ids),
            "claimed_daily_task_ids": list(claimed_daily_task_ids),
            "claimed_weekly_task_ids": list(claimed_weekly_task_ids),
            "claimed_daily_reward_ids": list(claimed_daily_reward_ids),
            "claimed_weekly_reward_ids": list(claimed_weekly_reward_ids),
            "claim_error": claim_error,
            "daily": self.snapshot(selected_task_ids),
        }

    def _cancelled_result(
        self,
        completed_in_run: int,
        task_results: list[dict[str, Any]],
        selected_task_ids: list[int],
    ) -> dict[str, Any]:
        return {
            "cancelled": True,
            "completed_tasks": completed_in_run,
            "task_results": task_results,
            "daily": self.snapshot(selected_task_ids),
        }


def _format_countdown(seconds: int) -> str:
    seconds = max(seconds, 0)
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


def _display_result(result: DailyActionResult) -> str:
    labels = {
        "completed": "完成",
        "skipped": "已完成，跳过",
        "incomplete": "未完成",
        "failed": "失败",
    }
    return f"{labels.get(result.status, result.status)}：{result.message}"


def _result_payload(result: DailyActionResult) -> dict[str, Any]:
    return {
        "task_id": result.task_id,
        "status": result.status,
        "progress_before": result.progress_before,
        "progress_after": result.progress_after,
        "message": result.message,
    }
