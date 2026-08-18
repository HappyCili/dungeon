from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Callable, Mapping

from harvest_fief import (
    HARVEST_NORMAL,
    FiefClient,
    GameEndpoint,
    HarvestError,
)

from treasure_farm import HEARTH_ITEM_ID

from ..config_store import AUTO_TASK_KEYS, AutoTaskSettings
from ..job_manager import JobExecutionError


@dataclass(frozen=True)
class AutoTaskDefinition:
    key: str
    name: str
    description: str


AUTO_TASKS: tuple[AutoTaskDefinition, ...] = (
    AutoTaskDefinition("signin", "活动签到", "领取当前可领取的活动签到"),
    AutoTaskDefinition("fief", "庄园材料", "收取庄园普通产出"),
    AutoTaskDefinition("monthly_vip", "盈月之仪", "领取可用的每日权益奖励"),
    AutoTaskDefinition("furnace", "萃华仪", "收取普通产出"),
    AutoTaskDefinition("dragon_reward_like", "龙痕奖励与点赞", "领取每日奖励并完成可用点赞"),
    AutoTaskDefinition("treasure_sweep", "聚宝之地", "按炉火目标刷取并消耗免费次数"),
    AutoTaskDefinition("knight_arena", "普通竞技场", "按免费次数挑战"),
    AutoTaskDefinition(
        "legion_war",
        "军团日常",
        "处理围攻城堡、每日税收、军官招募与募兵升级",
    ),
    AutoTaskDefinition("dragon_arena", "龙痕竞技场", "挑战至设定的龙痕币目标"),
    AutoTaskDefinition("dungeon_sweep", "地下城扫荡", "扫荡已选地下城并全部抽取宝库奖励"),
    AutoTaskDefinition("monopoly", "宫廷棋", "持续掷骰至骰子耗尽或服务器结束"),
    AutoTaskDefinition("daily_rewards", "日常/周常奖励", "领取已完成任务积分和可用活跃奖励"),
)
AUTO_TASK_BY_KEY = {task.key: task for task in AUTO_TASKS}

SCARARENA_LEADERBOARD_KIND = 6
# Native table: decrypted-data/tables/macros.json / ma_arenascar_like_limit.
SCARARENA_DAILY_LIKE_LIMIT = 10
# 已确认：普通竞技场每日免费挑战次数。
# 它与 Arena_info.refreshnum（刷新次数）及日常任务 109 的挑战目标相互独立。
KNIGHT_ARENA_DAILY_FREE_LIMIT = 5


@dataclass(frozen=True)
class AutoTaskResult:
    key: str
    status: str
    message: str
    details: dict[str, Any]

    def payload(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "status": self.status,
            "message": self.message,
            "details": self.details,
        }


AutoTaskHandler = Callable[
    [GameEndpoint, AutoTaskSettings, Callable[[], bool]], AutoTaskResult
]
AutoTaskEmit = Callable[[str, str, dict[str, Any]], None]
_AUTO_TASK_EMIT: ContextVar[AutoTaskEmit | None] = ContextVar(
    "auto_task_emit", default=None
)


class AutoTaskService:
    """Ordered automation facade for existing gameplay clients.

    Handlers stay injectable so protocol-specific clients can be verified with
    deterministic fixtures without a game-server connection.
    """

    def __init__(
        self,
        *,
        handlers: Mapping[str, AutoTaskHandler] | None = None,
    ) -> None:
        self._handlers = dict(handlers or {})

    @staticmethod
    def snapshot(settings: AutoTaskSettings) -> dict[str, Any]:
        enabled = set(settings.enabled_task_keys)
        return {
            "tasks": [
                {
                    "key": task.key,
                    "name": task.name,
                    "description": task.description,
                    "enabled": task.key in enabled,
                    "result": "等待执行",
                }
                for task in AUTO_TASKS
            ],
            "settings": {
                "enabled_task_keys": list(settings.enabled_task_keys),
                "scheduler_enabled": settings.scheduler_enabled,
                "interval_minutes": settings.interval_minutes,
                "dragon_target_mode": settings.dragon_target_mode,
                "dragon_target_value": settings.dragon_target_value,
                "furnace_target_value": settings.furnace_target_value,
            },
        }

    def run(
        self,
        endpoint: GameEndpoint,
        settings: AutoTaskSettings,
        emit: Callable[[str, str, dict[str, Any]], None],
        stop_requested: Callable[[], bool],
    ) -> dict[str, Any]:
        results: list[AutoTaskResult] = []
        selected = set(settings.enabled_task_keys)
        token = _AUTO_TASK_EMIT.set(emit)
        try:
            for definition in AUTO_TASKS:
                if definition.key not in selected:
                    continue
                if stop_requested():
                    return self._result_payload(results, cancelled=True)
                emit(
                    "info",
                    f"正在执行 {definition.name}",
                    {"auto_task": {"key": definition.key, "status": "running"}},
                )
                result = self._run_one(definition.key, endpoint, settings, stop_requested)
                results.append(result)
                level = {
                    "completed": "success",
                    "skipped": "info",
                    "failed": "error",
                }.get(result.status, "warning")
                emit(
                    level,
                    f"{definition.name}：{result.message}",
                    {"auto_task": result.payload()},
                )
            return self._result_payload(results, cancelled=stop_requested())
        finally:
            _AUTO_TASK_EMIT.reset(token)

    def _run_one(
        self,
        key: str,
        endpoint: GameEndpoint,
        settings: AutoTaskSettings,
        stop_requested: Callable[[], bool],
    ) -> AutoTaskResult:
        handler = self._handlers.get(key)
        if handler is None:
            return AutoTaskResult(
                key,
                "skipped",
                "当前版本未配置该任务的协议处理器",
                {},
            )
        try:
            return handler(endpoint, settings, stop_requested)
        except JobExecutionError as exc:
            return AutoTaskResult(key, "failed", str(exc), {})
        except (HarvestError, OSError, ValueError) as exc:
            return AutoTaskResult(key, "failed", str(exc) or type(exc).__name__, {})
        except Exception as exc:
            return AutoTaskResult(
                key,
                "failed",
                f"{key} 执行异常：{str(exc).strip() or type(exc).__name__}",
                {"error_type": type(exc).__name__},
            )

    @staticmethod
    def _result_payload(
        results: list[AutoTaskResult], *, cancelled: bool
    ) -> dict[str, Any]:
        return {
            "cancelled": cancelled,
            "auto_tasks": [result.payload() for result in results],
            "completed_count": sum(result.status == "completed" for result in results),
            "skipped_count": sum(result.status == "skipped" for result in results),
            "failed_count": sum(result.status == "failed" for result in results),
        }


def ensure_auto_task_keys(keys: list[str]) -> list[str]:
    if len(keys) != len(set(keys)):
        raise ValueError("自动任务不能重复")
    if any(key not in AUTO_TASK_KEYS for key in keys):
        raise ValueError("包含未知自动任务")
    return list(keys)


def build_live_auto_task_handlers(
    *,
    treasure_service: Any,
    arena_service: Any,
    dungeon_service: Any,
    monopoly_service: Any,
    session_manager: Any,
    config_store: Any,
    timeout: float = 15.0,
) -> dict[str, AutoTaskHandler]:
    """Create the concrete handlers that already have local protocol clients."""

    return {
        "signin": lambda endpoint, _settings, stop: _claim_signins(
            endpoint, session_manager, timeout, stop
        ),
        "fief": lambda endpoint, _settings, _stop: _harvest_fief(
            endpoint, session_manager, timeout
        ),
        "monthly_vip": lambda endpoint, _settings, _stop: _claim_monthly_vip(
            endpoint, session_manager, timeout
        ),
        "furnace": lambda endpoint, settings, _stop: _harvest_furnace(
            endpoint, settings, session_manager, timeout
        ),
        "dragon_reward_like": lambda endpoint, _settings, stop: _claim_dragon_reward_and_like(
            endpoint, session_manager, timeout, stop
        ),
        "treasure_sweep": lambda endpoint, settings, stop: _sweep_treasure(
            endpoint, settings, treasure_service, config_store, stop
        ),
        "knight_arena": lambda endpoint, _settings, stop: _run_knight_arena(
            endpoint, session_manager, timeout, stop
        ),
        "legion_war": lambda endpoint, _settings, stop: _run_legion_war(
            endpoint, session_manager, timeout, stop
        ),
        "dragon_arena": lambda endpoint, settings, stop: _run_dragon_arena(
            endpoint, settings, arena_service, stop
        ),
        "dungeon_sweep": lambda endpoint, _settings, stop: _sweep_dungeon(
            endpoint, dungeon_service, config_store, stop
        ),
        "monopoly": lambda endpoint, _settings, stop: _run_monopoly(
            endpoint, monopoly_service, stop
        ),
        "daily_rewards": lambda endpoint, _settings, stop: _claim_daily_rewards(
            endpoint, session_manager, timeout, stop
        ),
    }


def _claim_signins(
    endpoint: GameEndpoint,
    session_manager: Any,
    timeout: float,
    stop_requested: Callable[[], bool],
) -> AutoTaskResult:
    """Claim activities that the ready-session login snapshot marks available.

    ``Activity_signin_sync_all`` (21003) is an inbound handler in the native
    client, but the live trace has no matching reply when it is sent from this
    client.  The native app receives the initial activity collection in
    ``Game_data.signinData`` and then uses ``Activity_do_signin`` (21000) for
    each eligible activity.  Use that same ready-session snapshot here.
    """

    from harvest_fief import decode_int32, encode_int_field

    session = session_manager.session_for(endpoint)
    game_data = session.game_data
    if game_data is None:
        return AutoTaskResult(
            "signin", "skipped", "登录快照未包含活动签到状态", {}
        )

    activities = _signin_activities_from_game_data(game_data)
    if not activities:
        return AutoTaskResult("signin", "skipped", "登录快照没有活动签到数据", {})

    eligible = [
        activity
        for activity in activities
        if not activity.today_signed and activity.ticket_status == 1
    ]
    if not eligible:
        return AutoTaskResult(
            "signin",
            "skipped",
            "活动签到已领取或当前不可领取",
            {
                "activity_ids": [activity.activity_id for activity in activities],
                "eligible_activity_ids": [],
            },
        )

    claimed_ids: list[int] = []
    rejected: list[dict[str, int]] = []
    stopped = False
    for activity in eligible:
        if stop_requested():
            stopped = True
            break
        session.send_message(21000, encode_int_field(1, activity.activity_id))
        response = session.wait_for({21000}, timeout, context="活动签到领取")
        ret = _int_field(response.data, 4, decode_int32)
        if ret == 0:
            claimed_ids.append(activity.activity_id)
        else:
            rejected.append({"activity_id": activity.activity_id, "ret": ret})

    attempted = len(claimed_ids) + len(rejected)
    status = "completed" if claimed_ids else "skipped"
    message = f"已领取 {len(claimed_ids)} 项签到，跳过 {len(rejected)} 项"
    if stopped:
        message += "，已停止"
    return AutoTaskResult(
        "signin",
        status,
        message,
        {
            "activity_ids": [activity.activity_id for activity in activities],
            "eligible_activity_ids": [activity.activity_id for activity in eligible],
            "attempted": attempted,
            "claimed": len(claimed_ids),
            "claimed_activity_ids": claimed_ids,
            "skipped": len(rejected),
            "rejected": rejected,
            "stopped": stopped,
        },
    )


@dataclass(frozen=True)
class SigninActivityState:
    """The subset of ``Game_data.signinData.acts`` used by auto-signin."""

    activity_id: int
    today_signed: bool
    ticket_status: int


def _signin_activities_from_game_data(data: bytes) -> list[SigninActivityState]:
    """Decode ``Game_data`` field 29 without depending on a full game schema.

    Native protobuf mappings: ``Game_data.signinData`` #29, ``acts`` #1,
    activity ``id`` #1 / ``signinData`` #3 / ``ticket`` #5, and nested
    ``todaySigned`` #3 / ``ticket.status`` #2.
    """

    from harvest_fief import ProtoReader, decode_int32

    activities: list[SigninActivityState] = []
    seen_ids: set[int] = set()
    for field_number, wire_type, signin_data in ProtoReader(data).fields():
        if field_number != 29 or wire_type != 2:
            continue
        for act_field, act_wire, act_data in ProtoReader(bytes(signin_data)).fields():
            if act_field != 1 or act_wire != 2:
                continue
            activity_data = bytes(act_data)
            activity_id = _int_field(activity_data, 1, decode_int32)
            if activity_id <= 0 or activity_id in seen_ids:
                continue
            signin_state = _bytes_field(activity_data, 3)
            ticket = _bytes_field(activity_data, 5)
            today_signed = bool(
                _int_field(signin_state, 3, lambda value: int(value))
            )
            ticket_status = _int_field(ticket, 2, decode_int32)
            activities.append(
                SigninActivityState(activity_id, today_signed, ticket_status)
            )
            seen_ids.add(activity_id)
    return activities


def _bytes_field(data: bytes, target: int) -> bytes:
    from harvest_fief import ProtoReader

    for field_number, wire_type, value in ProtoReader(data).fields():
        if field_number == target and wire_type == 2:
            return bytes(value)
    return b""


def _int_field(data: bytes, target: int, decode: Callable[[int], int]) -> int:
    from harvest_fief import ProtoReader

    for field_number, wire_type, value in ProtoReader(data).fields():
        if field_number == target and wire_type == 0:
            return decode(int(value))
    return 0


def _harvest_fief(
    endpoint: GameEndpoint, session_manager: Any, timeout: float
) -> AutoTaskResult:
    session = session_manager.session_for(endpoint)
    result = FiefClient(endpoint, timeout, session=session).harvest(HARVEST_NORMAL)
    if result.response.ret != 0:
        return AutoTaskResult(
            "fief",
            "skipped",
            f"庄园当前不可收取（ret={result.response.ret}）",
            {"ret": result.response.ret},
        )
    return AutoTaskResult(
        "fief",
        "completed",
        "庄园普通产出已收取",
        {"items": len(result.changes), "rewards": len(result.props)},
    )


def _claim_monthly_vip(
    endpoint: GameEndpoint, session_manager: Any, timeout: float
) -> AutoTaskResult:
    from claim_monthly_vip_daily_rewards import (
        RESULT_SUCCESS,
        VIP_BASE_ID,
        VIP_PRO_ID,
        MonthlyVipDailyRewardsClient,
    )

    session = session_manager.session_for(endpoint)
    results = MonthlyVipDailyRewardsClient(endpoint, timeout, session=session).claim(
        (VIP_BASE_ID, VIP_PRO_ID)
    )
    claimed = sum(item.response.result == RESULT_SUCCESS for item in results)
    status = "completed" if claimed else "skipped"
    return AutoTaskResult(
        "monthly_vip",
        status,
        f"盈月之仪领取 {claimed}/{len(results)} 项",
        {"claimed": claimed, "results": len(results)},
    )


def _harvest_furnace(
    endpoint: GameEndpoint,
    settings: AutoTaskSettings,
    session_manager: Any,
    timeout: float,
) -> AutoTaskResult:
    from harvest_furnace import FurnaceClient

    session = session_manager.session_for(endpoint)
    result = FurnaceClient(endpoint, timeout, session=session).harvest()
    if result.response.ret != 0:
        return AutoTaskResult(
            "furnace",
            "skipped",
            f"萃华仪当前不可收取（ret={result.response.ret}）",
            {"ret": result.response.ret},
        )
    return AutoTaskResult(
        "furnace",
        "completed",
        "萃华仪普通产出已收取",
        {
            "items": len(result.changes),
            "target": settings.furnace_target_value,
        },
    )


def _claim_dragon_reward_and_like(
    endpoint: GameEndpoint,
    session_manager: Any,
    timeout: float,
    stop_requested: Callable[[], bool],
) -> AutoTaskResult:
    """Claim the daily reward and fill available Dragon Arena likes.

    The native app reads ``Scararena_info.likenum``, fetches leaderboard kind 6
    through ``CLeaderboardGetList`` (12910), then sends ``CLeaderboardLike``
    (12912) for each eligible ranking UID.  Keep the full sequence on the
    ready shared game session so the server remains the source of truth.
    """

    from dragon_arena import decode_scararena_info
    emit = _AUTO_TASK_EMIT.get()

    def emit_progress(message: str, **details: Any) -> None:
        if emit is None:
            return
        emit(
            "info",
            f"龙痕奖励与点赞：{message}",
            {
                "auto_task": {
                    "key": "dragon_reward_like",
                    "status": "running",
                },
                "dragon_reward_like": details,
            },
        )

    session = session_manager.session_for(endpoint)
    emit_progress("正在读取龙痕竞技场状态")
    session.send_message(21100, b"")
    info_header = session.wait_for({21100}, timeout, context="龙痕竞技场状态")
    info = decode_scararena_info(info_header.data)
    liked_uids = _scararena_liked_uids(info_header.data)
    initial_liked_count = len(liked_uids)
    claimed = False
    daily_reward_ret: int | None = None
    if info.daily_reward_available and not stop_requested():
        emit_progress("正在领取龙痕每日奖励")
        session.send_message(21110, b"")
        response = session.wait_for({21110}, timeout, context="龙痕竞技场每日奖励")
        daily_reward_ret = _int_field(response.data, 1, lambda value: int(value))
        claimed = daily_reward_ret == 0

    self_uid = _game_data_uid(session.game_data)
    available_likes = max(SCARARENA_DAILY_LIKE_LIMIT - initial_liked_count, 0)
    candidates: list[int] = []
    rejected: list[dict[str, int]] = []
    liked_now: list[int] = []
    ranking_entry_count = 0
    ranking_kind = 0
    ranking_error: str | None = None
    stopped = stop_requested()
    if available_likes > 0 and not stopped:
        if self_uid <= 0:
            ranking_error = "登录快照未包含角色 UID"
            emit_progress("登录快照未包含角色 UID，跳过排行榜点赞")
        else:
            emit_progress(
                f"当前已点赞 {initial_liked_count}/{SCARARENA_DAILY_LIKE_LIMIT}，正在查询排行榜",
                already_liked=initial_liked_count,
                available_likes=available_likes,
            )
            session.send_message(12910, _encode_scararena_leaderboard_query())
            ranking = session.wait_for({12910}, timeout, context="龙痕竞技场排行榜")
            ranking_uids = _leaderboard_uids(ranking.data)
            ranking_entry_count = len(ranking_uids)
            ranking_kind = _leaderboard_rank_kind(ranking.data)
            if ranking_kind != SCARARENA_LEADERBOARD_KIND:
                ranking_error = f"排行榜类型不匹配（rankkind={ranking_kind}）"
                emit_progress(f"{ranking_error}，跳过点赞", ranking_kind=ranking_kind)
            else:
                candidates = [
                    uid
                    for uid in ranking_uids
                    if uid != self_uid and uid not in liked_uids
                ][:available_likes]
                emit_progress(
                    f"排行榜返回 {ranking_entry_count} 人，符合点赞条件 {len(candidates)} 人",
                    ranking_entry_count=ranking_entry_count,
                    ranking_kind=ranking_kind,
                    self_uid=self_uid,
                    candidate_uids=candidates,
                )
                for uid in candidates:
                    if stop_requested():
                        stopped = True
                        break
                    emit_progress(
                        f"正在点赞 UID {uid}",
                        uid=uid,
                        attempted=len(liked_now) + len(rejected) + 1,
                        candidate_count=len(candidates),
                    )
                    session.send_message(12912, _encode_scararena_like(uid))
                    response = session.wait_for({12912}, timeout, context="龙痕竞技场点赞")
                    result = _decode_leaderboard_like_response(response.data)
                    if (
                        result["ret"] == 0
                        and result["kind"] == SCARARENA_LEADERBOARD_KIND
                        and result["uid"] == uid
                    ):
                        liked_now.append(uid)
                        liked_uids.add(uid)
                        emit_progress(
                            f"UID {uid} 点赞成功",
                            uid=uid,
                            liked_count=len(liked_now),
                        )
                    else:
                        rejected.append(
                            {
                                "activity_uid": uid,
                                "ret": result["ret"],
                                "response_uid": result["uid"],
                            }
                        )
                        emit_progress(
                            f"UID {uid} 点赞未生效（ret={result['ret']}）",
                            uid=uid,
                            ret=result["ret"],
                            response_uid=result["uid"],
                            response_kind=result["kind"],
                        )

    if claimed:
        reward_message = "龙痕每日奖励已领取"
    elif info.daily_reward_available:
        reward_message = (
            "龙痕每日奖励待领取"
            if daily_reward_ret is None
            else f"龙痕每日奖励领取失败（ret={daily_reward_ret}）"
        )
    else:
        reward_message = "龙痕每日奖励当前不可领取"
    if ranking_error is not None:
        like_message = f"排行榜点赞未执行（{ranking_error}）"
    elif available_likes > 0:
        like_message = f"排行榜点赞 {len(liked_now)}/{available_likes}"
    else:
        like_message = "排行榜点赞次数已用完"
    if rejected:
        like_message += f"，跳过 {len(rejected)} 项"
    if stopped:
        like_message += "，已停止"
    status = "completed" if claimed or liked_now else "skipped"
    return AutoTaskResult(
        "dragon_reward_like",
        status,
        reward_message + "；" + like_message,
        {
            "daily_reward_claimed": claimed,
            "daily_reward_available": info.daily_reward_available,
            "daily_reward_ret": daily_reward_ret,
            "liked_count": len(liked_now),
            "liked_uids": liked_now,
            "already_liked": initial_liked_count,
            "like_limit": SCARARENA_DAILY_LIKE_LIMIT,
            "ranking_entry_count": ranking_entry_count,
            "ranking_kind": ranking_kind,
            "ranking_error": ranking_error,
            "candidate_count": len(candidates),
            "rejected_likes": rejected,
            "stopped": stopped,
        },
    )


def _game_data_uid(data: bytes | None) -> int:
    """Return the current player's UID from ``Game_data.uid`` (field 2)."""

    if data is None:
        return 0
    return _int_field(data, 2, lambda value: int(value))


def _scararena_liked_uids(data: bytes) -> set[int]:
    """Decode ``Scararena_info.likenum`` map keys (field 5)."""

    from harvest_fief import ProtoReader

    liked: set[int] = set()
    for field_number, wire_type, value in ProtoReader(data).fields():
        if field_number != 5 or wire_type != 2:
            continue
        uid = _int_field(bytes(value), 1, lambda item: int(item))
        if uid > 0:
            liked.add(uid)
    return liked


def _leaderboard_uids(data: bytes) -> list[int]:
    """Decode ranking entries from ``CLeaderboardGetList`` response field 4."""

    from harvest_fief import ProtoReader

    uids: list[int] = []
    seen: set[int] = set()
    for field_number, wire_type, value in ProtoReader(data).fields():
        if field_number != 4 or wire_type != 2:
            continue
        uid = _int_field(bytes(value), 3, lambda item: int(item))
        if uid > 0 and uid not in seen:
            uids.append(uid)
            seen.add(uid)
    return uids


def _leaderboard_rank_kind(data: bytes) -> int:
    """Return ``CLeaderboardGetList.rankkind`` (field 9) for diagnostics."""

    from harvest_fief import decode_int32

    return _int_field(data, 9, decode_int32)


def _encode_scararena_leaderboard_query() -> bytes:
    """Encode native RankModule's first-page query for Dragon Arena.

    ``CLeaderboardGetList`` maps ``page`` to #1, ``grp`` to #3, ``kind`` to
    #4 and ``innergrp`` to #5.  RankModule sends zero for the first page and
    both group values, so protobuf omits those defaults and retains only kind.
    """

    from harvest_fief import encode_int_field

    return encode_int_field(4, SCARARENA_LEADERBOARD_KIND)


def _encode_scararena_like(uid: int) -> bytes:
    from harvest_fief import encode_int_field

    if uid <= 0:
        raise ValueError("龙痕竞技场点赞目标 UID 无效")
    return (
        encode_int_field(1, SCARARENA_LEADERBOARD_KIND)
        + encode_int_field(3, uid)
    )


def _decode_leaderboard_like_response(data: bytes) -> dict[str, int]:
    from harvest_fief import decode_int32

    return {
        "kind": _int_field(data, 1, decode_int32),
        "uid": _int_field(data, 3, lambda value: int(value)),
        "ret": _int_field(data, 5, decode_int32),
    }


def _sweep_treasure(
    endpoint: GameEndpoint,
    settings: AutoTaskSettings,
    treasure_service: Any,
    config_store: Any,
    stop_requested: Callable[[], bool],
) -> AutoTaskResult:
    emit = _AUTO_TASK_EMIT.get()

    def emit_progress(level: str, message: str, data: dict[str, Any]) -> None:
        if emit is None:
            return
        emit(
            level,
            f"聚宝之地：{message}",
            {
                "auto_task": {"key": "treasure_sweep", "status": "running"},
                **data,
            },
        )

    live_settings = config_store.snapshot()
    area_id = live_settings.treasure.area_id
    if area_id <= 0:
        return AutoTaskResult("treasure_sweep", "skipped", "尚未选择聚宝地图", {})
    emit_progress("info", "正在读取免费扫荡次数", {})
    snapshot = treasure_service.snapshot(endpoint, area_id)
    available = int(snapshot["sweep"]["available"])
    target_hearth = int(settings.furnace_target_value or 4000)
    emit_progress(
        "info",
        f"当前剩余 {available} 次免费扫荡，炉火目标 {target_hearth}",
        {"treasure": snapshot},
    )
    swept = 0
    hearth_total: int | None = None
    while available > 0 and not stop_requested():
        times = min(available, 30)
        result = treasure_service.run(
            endpoint,
            area_id,
            times,
            emit_progress,
            stop_requested,
        )
        swept += int(result["request"]["times"])
        available = int(result["treasure"]["sweep"]["available"])
        for reward in result.get("rewards", []):
            if not isinstance(reward, dict) or reward.get("id") != HEARTH_ITEM_ID:
                continue
            total = reward.get("total")
            if isinstance(total, int) and not isinstance(total, bool):
                hearth_total = total

    if stop_requested():
        return AutoTaskResult(
            "treasure_sweep",
            "skipped",
            f"已停止，已扫荡 {swept} 次",
            {"area_id": area_id, "swept": swept, "remaining": available},
        )

    emit_progress("info", "正在读取扫荡后的当前炉火", {})
    hearth_total = treasure_service.farm_hearth_total(endpoint)
    if hearth_total >= target_hearth:
        return AutoTaskResult(
            "treasure_sweep",
            "completed" if swept else "skipped",
            f"已按免费次数扫荡 {swept} 次，炉火已达到目标 {target_hearth}",
            {
                "area_id": area_id,
                "swept": swept,
                "remaining": available,
                "hearth_total": hearth_total,
                "target_hearth": target_hearth,
            },
        )

    farm_target = target_hearth - hearth_total
    # Reuse the existing treasure-farm state machine after free sweeps.
    emit_progress(
        "info",
        (
            f"免费扫荡完成 {swept} 次，炉火当前 "
            f"{hearth_total if hearth_total is not None else '未返回'}，"
            f"开始刷宝箱补足 {farm_target}"
        ),
        {
            "treasure": {
                "swept": swept,
                "remaining": available,
                "hearth_total": hearth_total,
                "target_hearth": target_hearth,
            }
        },
    )
    farm_result = treasure_service.run_farm(
        endpoint,
        area_id,
        farm_target,
        emit_progress,
        stop_requested,
    )
    farm = farm_result.get("farm", {}) if isinstance(farm_result, dict) else {}
    return AutoTaskResult(
        "treasure_sweep",
        "completed" if swept or farm_result.get("completed", False) else "skipped",
        (
            f"已按免费次数扫荡 {swept} 次，炉火未达标，"
            f"已转入刷宝箱补足 {farm_target}（总目标 {target_hearth}）"
        ),
        {
            "area_id": area_id,
            "swept": swept,
            "remaining": available,
            "target_hearth": target_hearth,
            "hearth_before_farm": hearth_total,
            **farm,
        },
    )


def _sweep_dungeon(
    endpoint: GameEndpoint,
    dungeon_service: Any,
    config_store: Any,
    stop_requested: Callable[[], bool],
) -> AutoTaskResult:
    """Run the configured dungeon sweep and settle its treasure draw."""

    live_settings = config_store.snapshot()
    dungeon_id = live_settings.dungeon.dungeon_id
    if dungeon_id <= 0:
        return AutoTaskResult(
            "dungeon_sweep", "skipped", "尚未选择地下城", {}
        )

    name_getter = getattr(dungeon_service, "dungeon_name", None)
    dungeon_name = (
        name_getter(dungeon_id)
        if callable(name_getter)
        else f"地下城 {dungeon_id}"
    )
    if stop_requested():
        return AutoTaskResult(
            "dungeon_sweep",
            "skipped",
            "地下城扫荡已停止",
            {"dungeon_id": dungeon_id, "dungeon_name": dungeon_name},
        )

    emit = _AUTO_TASK_EMIT.get()

    def emit_progress(level: str, message: str, data: dict[str, Any]) -> None:
        if emit is None:
            return
        emit(
            level,
            f"地下城扫荡：{message}",
            {
                "auto_task": {"key": "dungeon_sweep", "status": "running"},
                **data,
            },
        )

    emit_progress("info", f"正在执行 {dungeon_name}", {})
    from .dungeon_service import DungeonSweepUnavailable

    try:
        daily_runner = getattr(dungeon_service, "run_daily", None)
        if callable(daily_runner):
            result = daily_runner(
                endpoint,
                dungeon_id,
                emit_progress,
                stop_requested,
            )
        else:
            # Keep older injected services usable while the live service adopts
            # the daily multi-sweep contract.
            result = dungeon_service.run(
                endpoint,
                dungeon_id,
                emit_progress,
                stop_requested,
            )
    except DungeonSweepUnavailable as exc:
        return AutoTaskResult(
            "dungeon_sweep",
            "skipped",
            str(exc),
            {
                "dungeon_id": dungeon_id,
                "dungeon_name": dungeon_name,
                "sweep_completed": False,
                "draw": None,
                "rewards": [],
                "rejection": {"ret": exc.ret, "reason": exc.reason},
            },
        )
    rewards = result.get("rewards", [])
    reward_count = len(rewards) if isinstance(rewards, list) else 0
    lamp_claim = result.get("lamp_claim")
    if not isinstance(lamp_claim, dict):
        lamp_claim = {
            "item_id": 901,
            "item_name": "永焰之灯",
            "claimed": False,
            "quantity": 0,
            "ret": 0,
        }
    sweeps_completed = int(result.get("sweeps_completed") or (1 if result.get("sweep_completed") else 0))
    sweeps_requested = int(result.get("sweeps_requested") or sweeps_completed)
    details = {
        "dungeon_id": dungeon_id,
        "dungeon_name": dungeon_name,
        "sweep_completed": bool(result.get("sweep_completed")),
        "sweeps_completed": sweeps_completed,
        "sweeps_requested": sweeps_requested,
        "sweep_limit": result.get("sweep_limit"),
        "lamp_claim": lamp_claim,
        "draw": result.get("draw"),
        "rewards": rewards,
    }
    if result.get("cancelled") or stop_requested():
        return AutoTaskResult(
            "dungeon_sweep",
            "skipped",
            f"{dungeon_name} 扫荡已停止",
            details,
        )
    if not details["sweep_completed"]:
        return AutoTaskResult(
            "dungeon_sweep",
            "skipped",
            f"{dungeon_name} 当前未完成扫荡",
            details,
        )
    return AutoTaskResult(
        "dungeon_sweep",
        "completed",
        f"{dungeon_name} 已完成 {sweeps_completed} 次扫荡并全部抽取 {reward_count} 项奖励",
        details,
    )


def _run_monopoly(
    endpoint: GameEndpoint,
    monopoly_service: Any,
    stop_requested: Callable[[], bool],
) -> AutoTaskResult:
    """Run the existing court-chess service through the auto-task contract."""

    if stop_requested():
        return AutoTaskResult("monopoly", "skipped", "宫廷棋已停止", {})

    emit = _AUTO_TASK_EMIT.get()

    def emit_progress(level: str, message: str, data: dict[str, Any]) -> None:
        if emit is None:
            return
        payload = dict(data)
        payload["auto_task"] = {"key": "monopoly", "status": "running"}
        emit(level, f"宫廷棋：{message}", payload)

    result = monopoly_service.run(endpoint, emit_progress, stop_requested)
    stats = result.get("monopoly") if isinstance(result, dict) else None
    if not isinstance(stats, dict):
        raise JobExecutionError("宫廷棋自动掷骰未返回执行状态")

    details = dict(stats)
    message = str(details.get("last_result") or "宫廷棋自动掷骰已结束")
    if result.get("cancelled") or stop_requested():
        return AutoTaskResult("monopoly", "skipped", message, details)

    rolls = int(details.get("rolls") or 0)
    return AutoTaskResult(
        "monopoly",
        "completed" if rolls else "skipped",
        message,
        details,
    )


def _run_legion_war(
    endpoint: GameEndpoint,
    session_manager: Any,
    timeout: float,
    stop_requested: Callable[[], bool],
    *,
    client_factory: Callable[..., Any] | None = None,
) -> AutoTaskResult:
    """Run the complete legion daily sequence on the shared game session."""

    if stop_requested():
        return AutoTaskResult("legion_war", "skipped", "军团日常已停止", {"stopped": True})

    from legion_war import LegionWarCancelled, LegionWarClient

    emit = _AUTO_TASK_EMIT.get()
    if emit is not None:
        emit(
            "info",
            "军团日常：正在处理围攻、税收、招募和募兵升级",
            {"auto_task": {"key": "legion_war", "status": "running"}},
        )

    factory = client_factory or LegionWarClient
    session = session_manager.session_for(endpoint)
    client: Any | None = None
    try:
        # Keep compatibility with deterministic test doubles and older feature
        # factories that do not yet declare the optional stop callback.
        try:
            client = factory(
                endpoint,
                timeout,
                session=session,
                stop_requested=stop_requested,
            )
        except TypeError:
            try:
                client = factory(endpoint, timeout, session=session)
            except TypeError:
                client = factory(endpoint, timeout)
        result = client.run_daily()
    except LegionWarCancelled as exc:
        return AutoTaskResult(
            "legion_war",
            "skipped",
            str(exc) or "军团日常已停止",
            {"stopped": True},
        )
    finally:
        close = getattr(client, "close", None)
        if callable(close):
            close()

    details = {
        "siege_wins": int(result.siege_wins),
        "tax_collected": bool(result.tax_collected),
        "officer_pull_times": int(result.officer_pull_times),
        "troops_upgraded": bool(result.troops_upgraded),
        "stopped": False,
    }
    actions_taken = (
        details["siege_wins"] > 0
        or details["tax_collected"]
        or details["officer_pull_times"] > 0
        or details["troops_upgraded"]
    )
    return AutoTaskResult(
        "legion_war",
        "completed" if actions_taken else "skipped",
        result.summary(),
        details,
    )


def _claim_daily_rewards(
    endpoint: GameEndpoint,
    session_manager: Any,
    timeout: float,
    stop_requested: Callable[[], bool],
    *,
    client_factory: Callable[..., Any] | None = None,
    catalog_factory: Callable[[], Any] | None = None,
) -> AutoTaskResult:
    """Claim only rewards the server currently marks as available."""

    if stop_requested():
        return AutoTaskResult("daily_rewards", "skipped", "奖励领取已停止", {})

    from daily_quest import DailyQuestClient, load_daily_catalog

    factory = client_factory or DailyQuestClient
    catalog = (catalog_factory or load_daily_catalog)()
    client = factory(
        endpoint,
        timeout,
        session=session_manager.session_for(endpoint),
    )
    try:
        claims = client.claim_available(catalog)
    finally:
        close = getattr(client, "close", None)
        if callable(close):
            close()

    details = {
        "claimed_task_ids": list(claims.claimed_task_ids),
        "claimed_reward_ids": list(claims.claimed_reward_ids),
        "claimed_daily_task_ids": list(claims.claimed_daily_task_ids),
        "claimed_weekly_task_ids": list(claims.claimed_weekly_task_ids),
        "claimed_daily_reward_ids": list(claims.claimed_daily_reward_ids),
        "claimed_weekly_reward_ids": list(claims.claimed_weekly_reward_ids),
        "daily_remaining_seconds": claims.status.daily_remaining_seconds,
        "weekly_remaining_seconds": claims.status.weekly_remaining_seconds,
    }
    claimed_count = len(claims.claimed_task_ids) + len(claims.claimed_reward_ids)
    if claimed_count == 0:
        return AutoTaskResult(
            "daily_rewards", "skipped", "当前没有可领取的日常/周常奖励", details
        )

    message = (
        f"已领取任务积分 {len(claims.claimed_task_ids)} 项，"
        f"日常活跃奖励 {len(claims.claimed_daily_reward_ids)} 项，"
        f"周常活跃奖励 {len(claims.claimed_weekly_reward_ids)} 项"
    )
    emit = _AUTO_TASK_EMIT.get()
    if emit is not None:
        emit(
            "success",
            f"日常/周常奖励：{message}",
            {
                "auto_task": {"key": "daily_rewards", "status": "running"},
                "daily_rewards": details,
            },
        )
    return AutoTaskResult("daily_rewards", "completed", message, details)


def _run_knight_arena(
    endpoint: GameEndpoint,
    session_manager: Any,
    timeout: float,
    stop_requested: Callable[[], bool],
    *,
    client_factory: Callable[..., Any] | None = None,
) -> AutoTaskResult:
    from knight_arena import KnightArenaClient

    if stop_requested():
        return AutoTaskResult("knight_arena", "skipped", "普通竞技场已停止", {})

    factory = client_factory or KnightArenaClient
    session = session_manager.session_for(endpoint)
    with factory(
        endpoint, timeout, log=lambda _message: None, session=session
    ) as client:
        run = client.run_daily_free_challenges(
            daily_free_limit=KNIGHT_ARENA_DAILY_FREE_LIMIT,
            stop_requested=stop_requested,
        )

    completed = len(run.results)
    details = {
        "daily_free_limit": run.daily_free_limit,
        "used_before": run.initial_challenge_num,
        "requested": run.requested_challenges,
        "accepted": run.accepted_challenges,
        "completed": completed,
        "rejected": run.rejected_challenges,
        "cancelled": run.cancelled,
        "stop_reason": run.stop_reason,
    }
    if not run.season_open:
        return AutoTaskResult("knight_arena", "skipped", "普通竞技场赛季未开放", details)
    if run.requested_challenges == 0:
        return AutoTaskResult("knight_arena", "skipped", "普通竞技场免费次数已用完", details)
    if run.cancelled:
        return AutoTaskResult(
            "knight_arena",
            "skipped",
            f"普通竞技场已停止，已完成 {completed}/{run.requested_challenges} 次免费挑战",
            details,
        )
    if run.failure:
        raise HarvestError(
            "普通竞技场挑战已接受 "
            f"{run.accepted_challenges}/{run.requested_challenges} 次，"
            f"但未完成战斗结算：{run.failure}"
        )
    if completed == run.requested_challenges:
        return AutoTaskResult(
            "knight_arena",
            "completed",
            f"普通竞技场完成 {completed}/{run.requested_challenges} 次免费挑战",
            details,
        )
    if completed:
        return AutoTaskResult(
            "knight_arena",
            "completed",
            (
                f"普通竞技场完成 {completed}/{run.requested_challenges} 次免费挑战，"
                f"{run.stop_reason or '未继续挑战'}"
            ),
            details,
        )
    reason = run.stop_reason or f"服务端拒绝 {run.rejected_challenges} 次挑战"
    return AutoTaskResult(
        "knight_arena",
        "skipped",
        f"普通竞技场未完成免费挑战：{reason}",
        details,
    )


def _run_dragon_arena(
    endpoint: GameEndpoint,
    settings: AutoTaskSettings,
    arena_service: Any,
    stop_requested: Callable[[], bool],
) -> AutoTaskResult:
    target = settings.dragon_target_value
    if target <= 0:
        return AutoTaskResult("dragon_arena", "skipped", "未设置龙痕币目标", {})

    key = "daily_reward.count" if settings.dragon_target_mode == "daily" else "dragon_coin_total"
    rounds = 0
    latest: dict[str, Any] = {}
    while not stop_requested() and rounds < 100:
        snapshot = arena_service.snapshot(endpoint)
        value = (
            int(snapshot["daily_reward"]["count"])
            if settings.dragon_target_mode == "daily"
            else int(snapshot.get("dragon_coin_total") or 0)
        )
        latest = {"current": value, "target": target, "mode": settings.dragon_target_mode}
        if value >= target:
            return AutoTaskResult("dragon_arena", "completed" if rounds else "skipped", "龙痕币目标已达到", latest)
        result = arena_service.run(
            endpoint,
            1,
            "mercy",
            True,
            lambda _level, _message, _data: None,
            stop_requested,
        )
        rounds += int(result["arena"]["completed_rounds"])
        if int(result["arena"].get("dragon_coin_delta") or 0) <= 0:
            break
    return AutoTaskResult(
        "dragon_arena",
        "completed" if rounds else "skipped",
        f"龙痕竞技场已挑战 {rounds} 场，当前未达到目标",
        latest | {"rounds": rounds, "metric": key},
    )
