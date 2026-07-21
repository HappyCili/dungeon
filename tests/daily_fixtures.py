from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Mapping

from daily_actions import (
    DAILY_ACTION_SPECS,
    ActionExecution,
    DailyActionSpec,
    DailyActionRunner,
)
from daily_quest import (
    DAILY_GROUP_ID,
    WEEKLY_GROUP_ID,
    DailyCatalog,
    DailyClaimResult,
    DailyQuestStatus,
    DailyTaskState,
    load_daily_catalog,
)


FIXTURE_DAILY_REMAINING_SECONDS = 5 * 3600 + 42 * 60 + 18


@dataclass
class FixtureTask:
    progress: int
    target: int
    finished: bool = False
    score_claimed: bool = False


class InMemoryDailyGateway:
    def __init__(
        self,
        catalog: DailyCatalog,
        *,
        task_targets: Mapping[int, int] | None = None,
    ) -> None:
        targets = task_targets or {
            spec.task_id: spec.target for spec in DAILY_ACTION_SPECS
        }
        self._catalog = catalog
        self._task_configs = {
            **catalog.tasks,
            **catalog.weekly_tasks,
        }
        self._tasks = {
            task_id: FixtureTask(0, targets.get(task_id, 1))
            for task_id in self._task_configs
        }
        self._reward_ids_by_group: dict[int, set[int]] = {
            DAILY_GROUP_ID: set(),
            WEEKLY_GROUP_ID: set(),
        }
        self.status_calls = 0
        self.claim_calls = 0

    def set_progress(self, task_id: int, progress: int) -> None:
        task = self._tasks[task_id]
        task.progress = min(max(progress, 0), task.target)
        task.finished = task.progress >= task.target

    def advance(self, task_id: int, count: int) -> None:
        if count < 0:
            raise ValueError("夹具进度不能倒退")
        task = self._tasks[task_id]
        self.set_progress(task_id, task.progress + count)

    def status(self) -> DailyQuestStatus:
        self.status_calls += 1
        tasks = {
            task_id: DailyTaskState(task_id, task.finished, task.score_claimed)
            for task_id, task in self._tasks.items()
        }
        progress = {
            self._task_configs[task_id].quest_id: task.progress
            for task_id, task in self._tasks.items()
        }
        return DailyQuestStatus(
            daily_remaining_seconds=FIXTURE_DAILY_REMAINING_SECONDS,
            daily_reset_seconds=7200,
            daily_reward_ids=tuple(
                sorted(self._reward_ids_by_group[DAILY_GROUP_ID])
            ),
            tasks=tasks,
            quest_progress=progress,
            weekly_remaining_seconds=3 * 24 * 3600,
            weekly_reset_seconds=7 * 24 * 3600,
            weekly_reward_ids=tuple(
                sorted(self._reward_ids_by_group[WEEKLY_GROUP_ID])
            ),
        )

    def claim_available(self, catalog: DailyCatalog) -> DailyClaimResult:
        self.claim_calls += 1
        group_ids = (DAILY_GROUP_ID, WEEKLY_GROUP_ID)
        claimed_tasks_by_group: dict[int, list[int]] = {}
        claimed_rewards_by_group: dict[int, list[int]] = {}
        for group_id in group_ids:
            group_tasks = catalog.tasks_for_group(group_id)
            claimed_tasks: list[int] = []
            for task_id in group_tasks:
                task = self._tasks[task_id]
                if task.finished and not task.score_claimed:
                    task.score_claimed = True
                    claimed_tasks.append(task_id)
            claimed_tasks_by_group[group_id] = claimed_tasks

            activity_score = sum(
                config.activity_score
                for task_id, config in group_tasks.items()
                if self._tasks[task_id].score_claimed
            )
            claimed_rewards: list[int] = []
            reward_ids = self._reward_ids_by_group[group_id]
            for reward in catalog.rewards_for_group(group_id):
                if reward.score <= activity_score and reward.reward_id not in reward_ids:
                    reward_ids.add(reward.reward_id)
                    claimed_rewards.append(reward.reward_id)
            claimed_rewards_by_group[group_id] = claimed_rewards

        claimed_tasks = tuple(
            task_id
            for group_id in group_ids
            for task_id in claimed_tasks_by_group[group_id]
        )
        claimed_rewards = tuple(
            reward_id
            for group_id in group_ids
            for reward_id in claimed_rewards_by_group[group_id]
        )
        return DailyClaimResult(
            claimed_task_ids=claimed_tasks,
            claimed_reward_ids=claimed_rewards,
            status=self.status(),
            claimed_daily_task_ids=tuple(
                claimed_tasks_by_group[DAILY_GROUP_ID]
            ),
            claimed_weekly_task_ids=tuple(
                claimed_tasks_by_group[WEEKLY_GROUP_ID]
            ),
            claimed_daily_reward_ids=tuple(
                claimed_rewards_by_group[DAILY_GROUP_ID]
            ),
            claimed_weekly_reward_ids=tuple(
                claimed_rewards_by_group[WEEKLY_GROUP_ID]
            ),
        )


class FixtureAction:
    def __init__(
        self,
        gateway: InMemoryDailyGateway,
        spec: DailyActionSpec,
        *,
        mode: str = "complete",
        delay: float = 0.0,
    ) -> None:
        if mode not in {"complete", "ss_detected", "no_free", "no_resources", "lost"}:
            raise ValueError(f"未知夹具动作模式：{mode}")
        self.gateway = gateway
        self.spec = spec
        self.mode = mode
        self.delay = delay
        self.calls: list[int] = []

    def run(self, remaining: int) -> ActionExecution:
        self.calls.append(remaining)
        if self.delay:
            time.sleep(self.delay)
        if self.mode == "complete":
            self.gateway.advance(self.spec.task_id, remaining)
            return ActionExecution(remaining, remaining, f"已执行 {remaining} 次{self.spec.label}")
        messages = {
            "ss_detected": "检测到 SS，已停止刷新",
            "no_free": "没有免费次数",
            "no_resources": "没有可收取资源",
            "lost": "竞技场挑战失败",
        }
        return ActionExecution(remaining, 0, messages[self.mode])


@dataclass(frozen=True)
class TestDailyActionBundle:
    runner: DailyActionRunner
    gateway: InMemoryDailyGateway
    actions: Mapping[int, FixtureAction]


def build_test_daily_action_runner(
    simulation_delay: float = 0.0,
    *,
    modes: Mapping[int, str] | None = None,
) -> TestDailyActionBundle:
    catalog = load_daily_catalog()
    gateway = InMemoryDailyGateway(catalog)
    requested_modes = modes or {}
    actions = {
        spec.task_id: FixtureAction(
            gateway,
            spec,
            mode=requested_modes.get(spec.task_id, "complete"),
            delay=simulation_delay,
        )
        for spec in DAILY_ACTION_SPECS
    }
    return TestDailyActionBundle(
        DailyActionRunner(gateway, catalog, actions), gateway, actions
    )
