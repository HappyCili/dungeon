from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


@dataclass(frozen=True)
class DailyTaskDefinition:
    task_id: int
    name: str
    target: int
    activity_score: int
    available: bool
    implementation_status: str
    gap: str


DAILY_TASKS: tuple[DailyTaskDefinition, ...] = (
    DailyTaskDefinition(101, "冒险者公会“换一批”", 5, 15, True, "可执行", "登录并选择区服后通过游戏服会话执行"),
    DailyTaskDefinition(102, "任意商店购买", 1, 20, False, "尚未接入", "缺少商店选品、预算限制和购买适配器"),
    DailyTaskDefinition(103, "铁匠铺锻造", 5, 20, True, "可执行", "登录并选择区服后通过游戏服会话执行；消耗铁矿"),
    DailyTaskDefinition(104, "秘法塔探索", 1, 10, True, "可执行", "登录并选择区服后通过游戏服会话执行"),
    DailyTaskDefinition(105, "庄园普通收取", 2, 10, True, "可执行", "每次作业至多收取 1 次；资源产出后可再次执行"),
    DailyTaskDefinition(106, "庄园快速收取", 1, 10, True, "可执行", "默认仅免费快速收取，不消耗宝石"),
    DailyTaskDefinition(107, "地下城挑战", 1, 15, False, "尚未接入", "缺少进入、战斗和结算适配器"),
    DailyTaskDefinition(108, "公会委托派遣", 3, 15, False, "尚未接入", "缺少委托选择与派遣适配器"),
    DailyTaskDefinition(109, "骑士比武挑战", 3, 15, True, "可执行", "登录并选择区服后通过游戏服会话执行；只计挑战次数，不要求胜利"),
    DailyTaskDefinition(110, "聚宝之地大宝箱", 1, 15, False, "尚未接入", "缺少进入、开箱和资源检查适配器"),
    DailyTaskDefinition(111, "联盟捐献", 2, 10, False, "尚未接入", "缺少公会捐献适配器"),
    DailyTaskDefinition(112, "龙痕竞技场胜利", 1, 10, True, "可执行", "登录并选择区服后通过游戏服会话执行"),
    DailyTaskDefinition(113, "裂境角逐挑战", 1, 10, False, "尚未接入", "缺少挑战和结算适配器"),
    DailyTaskDefinition(114, "审判庭人物送礼", 1, 10, False, "尚未接入", "缺少礼物选择与库存检查适配器"),
    DailyTaskDefinition(115, "铁匠铺精炼", 1, 10, False, "尚未接入", "缺少精炼适配器"),
    DailyTaskDefinition(116, "军团税收领取", 1, 10, False, "尚未接入", "缺少税收状态与领取适配器"),
    DailyTaskDefinition(117, "进入阴魂大厅", 1, 10, False, "尚未接入", "缺少大厅进入适配器"),
    DailyTaskDefinition(118, "无序迷境挑战", 1, 10, False, "尚未接入", "缺少队伍、战斗和结算适配器"),
    DailyTaskDefinition(119, "古律院铭刻", 1, 10, True, "可执行", "登录并选择区服后通过游戏服会话执行"),
    DailyTaskDefinition(120, "铁匠铺高温锻造", 1, 10, False, "尚未接入", "缺少高温锻造适配器"),
)

AVAILABLE_TASK_IDS = frozenset(task.task_id for task in DAILY_TASKS if task.available)
DAILY_TASK_BY_ID = {task.task_id: task for task in DAILY_TASKS}
DAILY_REWARD_THRESHOLDS = (20, 40, 60, 80, 100)


class JobStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    STOPPING = "stopping"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


TERMINAL_JOB_STATUSES = frozenset(
    {JobStatus.SUCCEEDED, JobStatus.FAILED, JobStatus.CANCELLED}
)


@dataclass(frozen=True)
class TaskRunResult:
    task_id: int
    status: str
    progress_before: int
    progress_after: int
    message: str
    rewards: tuple[str, ...] = ()


@dataclass(frozen=True)
class ArenaRunResult:
    requested_rounds: int
    completed_rounds: int
    wins: int
    losses: int
    score_delta: int
    rewards: tuple[str, ...] = ()


@dataclass(frozen=True)
class JobEvent:
    sequence: int
    timestamp: str
    level: str
    feature: str
    message: str
    data: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "sequence": self.sequence,
            "timestamp": self.timestamp,
            "level": self.level,
            "feature": self.feature,
            "message": self.message,
            "data": self.data,
        }
