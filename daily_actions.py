#!/usr/bin/env python3
"""阶段 B 的日常动作适配与服务端状态闭环。

本模块不负责登录、令牌保存或界面展示。它把现有玩法客户端包装为统一动作，
并以 ``Dailyquest_info`` 的前后状态作为任务完成的唯一依据。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Mapping, Protocol, Sequence

from daily_quest import (
    DailyCatalog,
    DailyClaimResult,
    DailyQuestClient,
    DailyQuestStatus,
    load_daily_catalog,
)
from game_session import GameSession, GameSessionManager
from harvest_fief import GameEndpoint, HarvestError


@dataclass(frozen=True)
class DailyActionSpec:
    task_id: int
    target: int
    label: str


DAILY_ACTION_SPECS: tuple[DailyActionSpec, ...] = (
    DailyActionSpec(101, 5, "冒险者公会“换一批”"),
    DailyActionSpec(103, 5, "铁匠铺锻造"),
    DailyActionSpec(104, 1, "秘法塔探索"),
    DailyActionSpec(105, 2, "庄园普通收取"),
    DailyActionSpec(106, 1, "庄园快速收取"),
    DailyActionSpec(109, 3, "骑士比武挑战"),
    DailyActionSpec(112, 1, "龙痕竞技场胜利"),
    DailyActionSpec(119, 1, "古律院铭刻"),
)
DAILY_ACTION_BY_ID = {spec.task_id: spec for spec in DAILY_ACTION_SPECS}
FIEF_NORMAL_HARVESTS_PER_RUN = 1
FIEF_QUICK_HARVESTS_PER_RUN = 1
FIXTURE_DAILY_REMAINING_SECONDS = 5 * 3600 + 42 * 60 + 18
# 独立刷 SS 脚本默认仅在金币单价 < 200 时刷新；日常任务需补足 5 次，
# 允许配置表最高 200 金币单价（比较为 cost >= limit 时停止，故取 201）。
DAILY_GUILD_GOLD_COST_LIMIT = 201


@dataclass(frozen=True)
class ActionExecution:
    """单个玩法客户端返回的脱敏业务摘要。"""

    requested_count: int
    attempted_count: int
    message: str


@dataclass(frozen=True)
class DailyActionResult:
    task_id: int
    status: str
    progress_before: int
    progress_after: int
    message: str


@dataclass(frozen=True)
class DailyActionBatchResult:
    tasks: tuple[DailyActionResult, ...]
    claims: DailyClaimResult | None
    cancelled: bool
    status: DailyQuestStatus


class DailyStatusGateway(Protocol):
    def status(self) -> DailyQuestStatus:
        """读取当前服务端日常状态。"""

    def claim_available(self, catalog: DailyCatalog) -> DailyClaimResult:
        """领取当前服务端明确可领取的日常/周常积分与活跃奖励。"""


class DailyAction(Protocol):
    def run(self, remaining: int) -> ActionExecution:
        """仅执行当前任务的剩余次数。"""


class DailyQuestGateway:
    """日常状态网关。

    注入共享 ``GameSession`` 时复用同一连接；否则每次查询使用短生命周期会话
    （CLI / 测试兼容）。
    """

    def __init__(self, client_factory: Callable[[], DailyQuestClient]) -> None:
        self._client_factory = client_factory

    def status(self) -> DailyQuestStatus:
        client = self._client_factory()
        try:
            return client.get_status()
        finally:
            close = getattr(client, "close", None)
            if callable(close):
                close()

    def claim_available(self, catalog: DailyCatalog) -> DailyClaimResult:
        client = self._client_factory()
        try:
            return client.claim_available(catalog)
        finally:
            close = getattr(client, "close", None)
            if callable(close):
                close()


class CallableDailyAction:
    def __init__(self, callback: Callable[[int], ActionExecution]) -> None:
        self._callback = callback

    def run(self, remaining: int) -> ActionExecution:
        return self._callback(remaining)


class DailyActionRunner:
    """执行动作、确认状态变化，并在末尾统一领取奖励。"""

    def __init__(
        self,
        gateway: DailyStatusGateway,
        catalog: DailyCatalog,
        actions: Mapping[int, DailyAction],
    ) -> None:
        missing = set(actions).difference(DAILY_ACTION_BY_ID)
        if missing:
            raise ValueError(f"不支持的日常动作：{sorted(missing)}")
        catalog_missing = set(actions).difference(catalog.tasks)
        if catalog_missing:
            raise ValueError(f"日常配置缺少动作：{sorted(catalog_missing)}")
        self.gateway = gateway
        self.catalog = catalog
        self.actions = dict(actions)

    def status(self) -> DailyQuestStatus:
        return self.gateway.status()

    def progress_for(self, status: DailyQuestStatus, task_id: int) -> int:
        spec = DAILY_ACTION_BY_ID[task_id]
        task = status.task(task_id)
        config = self.catalog.tasks[task_id]
        progress = status.progress_for(config.quest_id)
        if progress is None:
            return spec.target if task is not None and task.finished else 0
        return min(max(progress, 0), spec.target)

    def run_task(self, task_id: int) -> DailyActionResult:
        """查询前后状态；只有 ``unfinished -> finished`` 才标记完成。"""

        spec = DAILY_ACTION_BY_ID.get(task_id)
        action = self.actions.get(task_id)
        if spec is None or action is None:
            return DailyActionResult(task_id, "failed", 0, 0, "任务动作尚未接入")

        try:
            before = self.gateway.status()
        except Exception as exc:
            return DailyActionResult(
                task_id,
                "failed",
                0,
                0,
                f"{spec.label}执行前状态查询失败（{type(exc).__name__}）",
            )

        before_task = before.task(task_id)
        before_progress = self.progress_for(before, task_id)
        if before_task is None:
            after = self._status_after_action(before)
            return DailyActionResult(
                task_id,
                "failed",
                before_progress,
                self.progress_for(after, task_id),
                f"服务端状态缺少日常任务 {task_id}",
            )

        if before_task.finished:
            after = self._status_after_action(before)
            return DailyActionResult(
                task_id,
                "skipped",
                before_progress,
                self.progress_for(after, task_id),
                "服务端已完成，跳过动作",
            )

        remaining = spec.target - before_progress
        if remaining <= 0:
            after = self._status_after_action(before)
            return DailyActionResult(
                task_id,
                "incomplete",
                before_progress,
                self.progress_for(after, task_id),
                "服务端进度已达目标但尚未标记完成，未重复执行动作",
            )

        try:
            execution = action.run(remaining)
        except Exception as exc:
            after = self._status_after_action(before)
            return DailyActionResult(
                task_id,
                "failed",
                before_progress,
                self.progress_for(after, task_id),
                f"{spec.label}动作异常（{_action_error_detail(exc)}）",
            )

        after = self._status_after_action(before)
        after_progress = self.progress_for(after, task_id)
        after_task = after.task(task_id)
        if after_task is not None and not before_task.finished and after_task.finished:
            return DailyActionResult(
                task_id,
                "completed",
                before_progress,
                after_progress,
                execution.message,
            )
        return DailyActionResult(
            task_id,
            "incomplete",
            before_progress,
            after_progress,
            f"{execution.message}；服务端尚未确认任务完成",
        )

    def run(
        self,
        task_ids: Sequence[int],
        *,
        stop_requested: Callable[[], bool] = lambda: False,
    ) -> DailyActionBatchResult:
        results: list[DailyActionResult] = []
        cancelled = False
        for task_id in task_ids:
            if stop_requested():
                cancelled = True
                break
            results.append(self.run_task(task_id))
            if stop_requested():
                cancelled = True
                break

        claims: DailyClaimResult | None = None
        if not cancelled:
            claims = self.gateway.claim_available(self.catalog)
            final_status = claims.status
        else:
            final_status = self.gateway.status()
        return DailyActionBatchResult(tuple(results), claims, cancelled, final_status)

    def _status_after_action(self, fallback: DailyQuestStatus) -> DailyQuestStatus:
        try:
            return self.gateway.status()
        except Exception:
            return fallback


def _guild_stop_label(stop_reason: str) -> str:
    from adventurer_guild_daily_auto_refresh import RESULT_LABELS, STOP_LABELS

    if stop_reason in STOP_LABELS:
        return STOP_LABELS[stop_reason]
    if stop_reason.startswith("server_ret_"):
        try:
            ret = int(stop_reason.removeprefix("server_ret_"))
        except ValueError:
            return stop_reason
        return RESULT_LABELS.get(ret, f"服务端返回 ret={ret}")
    return stop_reason


def _make_feature_client(
    factory: Callable[..., object],
    endpoint: GameEndpoint,
    timeout: float,
    *,
    session: GameSession | None = None,
    **kwargs: object,
) -> object:
    """Construct a feature client, tolerating fakes without ``session=``."""

    if session is not None:
        try:
            return factory(endpoint, timeout, session=session, **kwargs)
        except TypeError:
            pass
    return factory(endpoint, timeout, **kwargs)


def run_adventurer_guild_action(
    endpoint: GameEndpoint,
    timeout: float,
    remaining: int,
    *,
    client_factory: Callable[..., object] | None = None,
    catalog: object | None = None,
    session: GameSession | None = None,
) -> ActionExecution:
    """按日常剩余次数刷新，发现 SS 时由现有策略立即停止。

    日常模式允许配置表内全部金币价位（最高 200），以便在已用过低价刷新后
    仍能补足任务进度；独立脚本默认的 ``< 200`` 节流策略不在此处使用。
    """

    if remaining <= 0:
        return ActionExecution(0, 0, "无需执行冒险者公会刷新")
    from adventurer_guild_daily_auto_refresh import (
        AdventurerGuildClient,
        DEFAULT_HERO_NAME_TABLE,
        DEFAULT_HERO_TABLE,
        DEFAULT_REFRESH_FEE_TABLE,
        load_tavern_catalog,
    )

    if catalog is None:
        catalog = load_tavern_catalog(
            DEFAULT_HERO_TABLE,
            DEFAULT_HERO_NAME_TABLE,
            DEFAULT_REFRESH_FEE_TABLE,
        )
    factory = client_factory or AdventurerGuildClient
    result = _make_feature_client(
        factory, endpoint, timeout, session=session
    ).run_daily(
        catalog,
        max_refreshes=remaining,
        target_refreshes=remaining,
        gold_cost_limit=DAILY_GUILD_GOLD_COST_LIMIT,
    )
    attempts = len(result.attempts)
    stop_reason = getattr(result, "stop_reason", "") or ""
    stop_label = _guild_stop_label(stop_reason) if stop_reason else ""
    if result.paused:
        message = "检测到 SS，已停止刷新"
    elif attempts == 0:
        message = "没有符合策略的刷新次数"
    elif attempts < remaining:
        message = f"已请求 {attempts}/{remaining} 次冒险者公会刷新"
    else:
        message = f"已请求 {attempts}/{remaining} 次冒险者公会刷新"
    if stop_label and stop_reason != "daily_target_reached":
        message = f"{message}（{stop_label}）"
    return ActionExecution(remaining, attempts, message)


def run_arcane_tower_action(
    endpoint: GameEndpoint,
    timeout: float,
    remaining: int,
    *,
    client_factory: Callable[..., object] | None = None,
    session: GameSession | None = None,
) -> ActionExecution:
    if remaining <= 0:
        return ActionExecution(0, 0, "无需执行秘法塔探索")
    from explore_arcane_tower_daily_free import ArcaneTowerClient, RESULT_SUCCESS

    factory = client_factory or ArcaneTowerClient
    result = _make_feature_client(
        factory, endpoint, timeout, session=session
    ).explore_daily_free(max_attempts=remaining)
    attempts = len(result.attempts)
    succeeded = sum(attempt.response.result == RESULT_SUCCESS for attempt in result.attempts)
    if attempts == 0:
        message = "没有免费秘法塔探索次数，等待服务端状态确认"
    else:
        message = f"秘法塔免费探索成功 {succeeded}/{attempts} 次"
    return ActionExecution(remaining, attempts, message)


def run_smithy_forge_action(
    endpoint: GameEndpoint,
    timeout: float,
    remaining: int,
    *,
    client_factory: Callable[..., object] | None = None,
    session: GameSession | None = None,
) -> ActionExecution:
    """日常 103：铁匠铺普通锻造，按服务端剩余次数补足。"""

    if remaining <= 0:
        return ActionExecution(0, 0, "无需执行铁匠铺锻造")
    if not 0 < remaining <= DAILY_ACTION_BY_ID[103].target:
        raise ValueError("铁匠铺锻造剩余次数必须在 1 到 5 之间")

    from smithy_forge import (
        SmithyForgeClient,
        forged_count,
        stop_reason_label,
    )

    factory = client_factory or SmithyForgeClient
    result = _make_feature_client(
        factory, endpoint, timeout, session=session
    ).forge_for_daily(max_times=remaining)
    forged = forged_count(result)
    stop_reason = getattr(result, "stop_reason", "") or ""
    stop_label = stop_reason_label(stop_reason) if stop_reason else ""
    if forged <= 0:
        message = f"未执行铁匠铺锻造（{stop_label or '无可用次数'}）"
    elif forged < remaining:
        message = f"铁匠铺锻造成功 {forged}/{remaining} 次"
        if stop_label and stop_reason != "completed":
            message = f"{message}（{stop_label}）"
    else:
        message = f"铁匠铺锻造成功 {forged}/{remaining} 次"
    return ActionExecution(remaining, forged, message)


def run_fief_harvest_action(
    endpoint: GameEndpoint,
    timeout: float,
    remaining: int,
    *,
    client_factory: Callable[..., object] | None = None,
    session: GameSession | None = None,
) -> ActionExecution:
    """普通庄园收取只接受日常所需的 0..2 次。"""

    if not 0 <= remaining <= DAILY_ACTION_BY_ID[105].target:
        raise ValueError("庄园普通收取剩余次数必须在 0 到 2 之间")
    if remaining == 0:
        return ActionExecution(0, 0, "无需执行庄园普通收取")
    from harvest_fief import (
        FiefClient,
        FiefHarvestRejected,
        HARVEST_NORMAL,
        describe_fief_harvest_rejection,
    )

    factory = client_factory or FiefClient
    results: list[object] = []
    requested_count = min(remaining, FIEF_NORMAL_HARVESTS_PER_RUN)

    def _make_client() -> object:
        try:
            return factory(endpoint, timeout, session=session)
        except TypeError:
            # Test fakes may not accept session=.
            return factory(endpoint, timeout)

    for attempt_number in range(1, requested_count + 1):
        try:
            results.append(_make_client().harvest(HARVEST_NORMAL))
        except FiefHarvestRejected as exc:
            return ActionExecution(
                requested_count,
                len(results),
                f"第 {attempt_number} 次庄园普通收取未执行：{describe_fief_harvest_rejection(exc.ret)}",
            )
        except HarvestError as exc:
            return ActionExecution(
                requested_count,
                len(results),
                f"第 {attempt_number} 次庄园普通收取异常（{_action_error_detail(exc)}）",
            )
    return ActionExecution(
        requested_count,
        len(results),
        f"已请求 {len(results)}/{requested_count} 次庄园普通收取；需等待新的资源产出后再继续",
    )


def run_fief_quick_harvest_action(
    endpoint: GameEndpoint,
    timeout: float,
    remaining: int,
    *,
    client_factory: Callable[..., object] | None = None,
    allow_pay: bool = False,
    session: GameSession | None = None,
) -> ActionExecution:
    """日常 106：庄园快速收取。

    默认只发送 ``HARVEST_FREE``（免费快速收取），不消耗宝石。``allow_pay``
    为真时才在免费次数不足后尝试 ``HARVEST_PAY``；编排入口默认关闭。
    """

    if not 0 <= remaining <= DAILY_ACTION_BY_ID[106].target:
        raise ValueError("庄园快速收取剩余次数必须在 0 到 1 之间")
    if remaining == 0:
        return ActionExecution(0, 0, "无需执行庄园快速收取")
    from harvest_fief import (
        FiefClient,
        FiefHarvestRejected,
        HARVEST_FREE,
        HARVEST_PAY,
        describe_fief_harvest_rejection,
    )

    factory = client_factory or FiefClient
    requested_count = min(remaining, FIEF_QUICK_HARVESTS_PER_RUN)
    attempted = 0

    def _make_client() -> object:
        try:
            return factory(endpoint, timeout, session=session)
        except TypeError:
            return factory(endpoint, timeout)

    for attempt_number in range(1, requested_count + 1):
        try:
            _make_client().harvest(HARVEST_FREE)
            attempted += 1
        except FiefHarvestRejected as free_exc:
            if free_exc.ret != 1 or not allow_pay:
                return ActionExecution(
                    requested_count,
                    attempted,
                    f"第 {attempt_number} 次庄园快速收取未执行："
                    f"{describe_fief_harvest_rejection(free_exc.ret)}",
                )
            try:
                _make_client().harvest(HARVEST_PAY)
                attempted += 1
            except FiefHarvestRejected as pay_exc:
                return ActionExecution(
                    requested_count,
                    attempted,
                    f"第 {attempt_number} 次庄园付费快速收取未执行："
                    f"{describe_fief_harvest_rejection(pay_exc.ret)}",
                )
            except HarvestError as exc:
                return ActionExecution(
                    requested_count,
                    attempted,
                    f"第 {attempt_number} 次庄园付费快速收取异常（{_action_error_detail(exc)}）",
                )
        except HarvestError as exc:
            return ActionExecution(
                requested_count,
                attempted,
                f"第 {attempt_number} 次庄园快速收取异常（{_action_error_detail(exc)}）",
            )
    return ActionExecution(
        requested_count,
        attempted,
        f"已请求 {attempted}/{requested_count} 次庄园快速收取，等待服务端状态确认",
    )


def run_knight_arena_action(
    endpoint: GameEndpoint,
    timeout: float,
    remaining: int,
    *,
    client_factory: Callable[..., object] | None = None,
    refresh_on_exhaustion: bool = True,
    session: GameSession | None = None,
) -> ActionExecution:
    """执行日常任务 109：在骑士比武完成 ``remaining`` 次挑战。

    只计挑战次数，不要求胜利；角色与对手均可任意选取。
    """

    if remaining <= 0:
        return ActionExecution(0, 0, "无需执行骑士比武挑战")
    from knight_arena import KnightArenaClient

    factory = client_factory or KnightArenaClient
    client_kwargs: dict = {
        "log": lambda _message: None,
        "log_server_messages": False,
        "business_log": None,
        "task": "knight_arena",
    }
    if session is not None:
        client_kwargs["session"] = session
    try:
        client = factory(endpoint, timeout, **client_kwargs)
    except TypeError:
        client_kwargs.pop("session", None)
        client = factory(endpoint, timeout, **client_kwargs)
    try:
        enter = getattr(client, "__enter__", None)
        if callable(enter):
            enter()
        results = client.run_loop(
            rounds=remaining,
            refresh_on_exhaustion=refresh_on_exhaustion,
        )
    finally:
        close = getattr(client, "close", None)
        if callable(close):
            close()
    completed = sum(1 for result in results if result.battle is not None)
    wins = sum(
        1 for result in results if result.battle is not None and result.battle.win
    )
    losses = completed - wins
    return ActionExecution(
        remaining,
        completed,
        (
            f"骑士比武完成 {completed}/{remaining} 场挑战"
            f"（{wins} 胜 / {losses} 负），等待服务端状态确认"
        ),
    )


def run_dragon_arena_action(
    endpoint: GameEndpoint,
    timeout: float,
    remaining: int,
    *,
    client_factory: Callable[..., object] | None = None,
    win_choice_id: int | None = None,
    mercy_choice_id: int | None = None,
    outcome: str = "mercy",
    refresh_on_exhaustion: bool = True,
    session: GameSession | None = None,
) -> ActionExecution:
    """执行日常任务 112：在龙痕竞技场取得 ``remaining`` 场胜利。

    ``remaining`` 是服务端尚未完成的胜利次数，不是挑战次数；失败后会继续
    挑战直至胜利数达标或候选对手耗尽。
    """

    if remaining <= 0:
        return ActionExecution(0, 0, "无需执行龙痕竞技场挑战")
    from dragon_arena import DragonArenaClient, resolve_win_choice_id

    factory = client_factory or DragonArenaClient
    choice_id = resolve_win_choice_id(
        win_choice_id=win_choice_id,
        mercy_choice_id=mercy_choice_id,
        outcome=None if (win_choice_id is not None or mercy_choice_id is not None) else outcome,
    )
    # 不向页面回显服务端报文；原始 WebSocket 帧默认写入 logs/websocket_raw/。
    client_kwargs: dict = {
        "log": lambda _message: None,
        "log_server_messages": False,
        "business_log": None,
        "task": "dragon_arena",
    }
    if session is not None:
        client_kwargs["session"] = session
    try:
        client = factory(endpoint, timeout, **client_kwargs)
    except TypeError:
        client_kwargs.pop("session", None)
        client = factory(endpoint, timeout, **client_kwargs)
    try:
        enter = getattr(client, "__enter__", None)
        if callable(enter):
            enter()
        resume = getattr(client, "resume_pending_battle", None)
        if callable(resume):
            resume(win_choice_id=choice_id)
        results = client.run_loop(
            rounds=remaining,
            win_choice_id=choice_id,
            refresh_on_exhaustion=refresh_on_exhaustion,
            require_wins=True,
        )
    finally:
        close = getattr(client, "close", None)
        if callable(close):
            close()
    wins = sum(
        result.battle is not None and result.battle.win for result in results
    )
    return ActionExecution(
        remaining,
        len(results),
        f"龙痕竞技场胜利 {wins}/{remaining} 场（挑战 {len(results)} 次），等待服务端状态确认",
    )


def run_ancient_law_court_action(
    endpoint: GameEndpoint,
    timeout: float,
    remaining: int,
    *,
    client_factory: Callable[..., object] | None = None,
    session: GameSession | None = None,
) -> ActionExecution:
    if remaining <= 0:
        return ActionExecution(0, 0, "无需执行古律院铭刻")
    from engrave_ancient_law_court_daily_free import (
        AncientLawCourtClient,
        RESULT_SUCCESS,
        collect_highlight_runes,
        format_highlight_rune_summary,
        load_name_catalogs,
        DEFAULT_ITEM_MAP,
        DEFAULT_RUNE_TABLE,
    )

    factory = client_factory or AncientLawCourtClient
    result = _make_feature_client(
        factory, endpoint, timeout, session=session
    ).engrave_daily_free(max_attempts=remaining)
    attempts = len(result.attempts)
    verified = sum(
        attempt.response.result == RESULT_SUCCESS and attempt.safety_verified
        for attempt in result.attempts
    )
    if attempts == 0:
        message = "没有免费古律院铭刻次数，等待服务端状态确认"
    else:
        message = f"古律院免费铭刻校验成功 {verified}/{attempts} 次"
    try:
        catalogs = load_name_catalogs(DEFAULT_ITEM_MAP, DEFAULT_RUNE_TABLE)
    except Exception:
        catalogs = None
    highlights = collect_highlight_runes(result, catalogs)
    if highlights:
        message = (
            f"{message}；高品质掉落：{format_highlight_rune_summary(highlights)}"
        )
    return ActionExecution(remaining, attempts, message)


def build_live_daily_action_runner(
    endpoint: GameEndpoint,
    timeout: float,
    *,
    catalog: DailyCatalog | None = None,
    status_client_factory: Callable[[], DailyQuestClient] | None = None,
    session: GameSession | None = None,
    session_manager: GameSessionManager | None = None,
) -> DailyActionRunner:
    """构造真实客户端编排器；调用者显式提供已解析的游戏服入口。

    传入 ``session`` 或 ``session_manager`` 时，状态查询与已支持 session 的
    日常动作复用同一条游戏服 WebSocket（对齐原生 SocketManager）。
    """

    daily_catalog = catalog or load_daily_catalog()
    shared: GameSession | None = session
    if shared is None and session_manager is not None:
        shared = session_manager.session_for(endpoint)

    status_factory = status_client_factory or (
        lambda: DailyQuestClient(endpoint, timeout, session=shared)
    )
    actions: dict[int, DailyAction] = {
        101: CallableDailyAction(
            lambda remaining: run_adventurer_guild_action(
                endpoint, timeout, remaining, session=shared
            )
        ),
        103: CallableDailyAction(
            lambda remaining: run_smithy_forge_action(
                endpoint, timeout, remaining, session=shared
            )
        ),
        104: CallableDailyAction(
            lambda remaining: run_arcane_tower_action(
                endpoint, timeout, remaining, session=shared
            )
        ),
        105: CallableDailyAction(
            lambda remaining: run_fief_harvest_action(
                endpoint, timeout, remaining, session=shared
            )
        ),
        106: CallableDailyAction(
            lambda remaining: run_fief_quick_harvest_action(
                endpoint, timeout, remaining, session=shared
            )
        ),
        109: CallableDailyAction(
            lambda remaining: run_knight_arena_action(
                endpoint, timeout, remaining, session=shared
            )
        ),
        112: CallableDailyAction(
            lambda remaining: run_dragon_arena_action(
                endpoint, timeout, remaining, session=shared
            )
        ),
        119: CallableDailyAction(
            lambda remaining: run_ancient_law_court_action(
                endpoint, timeout, remaining, session=shared
            )
        ),
    }
    return DailyActionRunner(DailyQuestGateway(status_factory), daily_catalog, actions)


def _action_error_detail(exc: Exception) -> str:
    if not isinstance(exc, HarvestError):
        return type(exc).__name__
    detail = " ".join(str(exc).split())
    if not detail:
        return type(exc).__name__
    return f"{type(exc).__name__}：{detail[:160]}"
