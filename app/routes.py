from __future__ import annotations

from typing import Any, Callable, Mapping
from urllib.parse import urlparse

from flask import Flask, abort, current_app, jsonify, render_template, request

from .config_store import (
    AUTO_TASK_KEYS,
    MAX_ABYSS_ROUNDS,
    MAX_AUTO_TASK_INTERVAL_MINUTES,
    MAX_TREASURE_FARM_HEARTH,
    MAX_TREASURE_SWEEP_TIMES,
    MIN_AUTO_TASK_INTERVAL_MINUTES,
    UiSettings,
    VALID_DRAGON_TARGET_MODES,
    VALID_OUTCOMES,
)
from .credentials import CredentialStorageError
from .job_manager import JobConflictError, JobExecutionError, JobNotFoundError
from .models import AVAILABLE_TASK_IDS
from .services.account_service import AccountLoginError
from .services.abyss_service import AbyssServiceError
from .services.arena_service import ArenaServiceError
from .services.daily_service import DailyServiceError
from .services.auto_task_service import ensure_auto_task_keys
from .services.twin_spiral_service import TwinSpiralServiceError
from dungeon_sweep import DungeonSweepError
from daily_quest import DailyQuestError
from game_session import GameSessionError, SessionRecoverySnapshot
from harvest_fief import HarvestError
from id_descriptions import treasure_area_name
from session_recovery import recovery_content_area
from treasure_area import TreasureAreaError
from treasure_farm import TreasureFarmError


class ApiError(Exception):
    def __init__(self, message: str, status_code: int = 400) -> None:
        self.message = message
        self.status_code = status_code


def _services() -> Mapping[str, Any]:
    return current_app.extensions["daily_console"]


def _require_json_body() -> dict[str, Any]:
    if not request.is_json:
        raise ApiError("请求必须使用 JSON", 415)
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        raise ApiError("JSON 请求体必须是对象")
    return payload


def _require_string(
    payload: Mapping[str, Any], key: str, *, maximum: int, allow_empty: bool = False
) -> str:
    value = payload.get(key)
    if not isinstance(value, str):
        raise ApiError(f"{key} 必须是字符串")
    value = value.strip()
    if not value and not allow_empty:
        raise ApiError(f"{key} 不能为空")
    if len(value) > maximum:
        raise ApiError(f"{key} 长度不能超过 {maximum}")
    return value


def _require_bool(payload: Mapping[str, Any], key: str) -> bool:
    value = payload.get(key)
    if not isinstance(value, bool):
        raise ApiError(f"{key} 必须是布尔值")
    return value


def _require_rounds(payload: Mapping[str, Any]) -> int:
    value = payload.get("rounds")
    if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= 100:
        raise ApiError("rounds 必须是 1 到 100 之间的整数")
    return value


def _require_treasure_area_id(payload: Mapping[str, Any]) -> int:
    value = payload.get("area_id")
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or not 1 <= value <= 0x7FFFFFFF
    ):
        raise ApiError("area_id 必须是正整数")
    return value


def _require_treasure_times(payload: Mapping[str, Any]) -> int:
    value = payload.get("times")
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or not 1 <= value <= MAX_TREASURE_SWEEP_TIMES
    ):
        raise ApiError(
            f"times 必须是 1 到 {MAX_TREASURE_SWEEP_TIMES} 之间的整数"
        )
    return value


def _require_farm_area_id(payload: Mapping[str, Any]) -> int:
    value = payload.get("farm_area_id", payload.get("area_id"))
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or not 1 <= value <= 0x7FFFFFFF
    ):
        raise ApiError("farm_area_id 必须是正整数")
    return value


def _require_farm_target_hearth(payload: Mapping[str, Any]) -> int:
    value = payload.get("farm_target_hearth", payload.get("target_hearth"))
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or not 1 <= value <= MAX_TREASURE_FARM_HEARTH
    ):
        raise ApiError(
            f"farm_target_hearth 必须是 1 到 {MAX_TREASURE_FARM_HEARTH} 之间的整数"
        )
    return value


def _require_dungeon_id(payload: Mapping[str, Any]) -> int:
    value = payload.get("dungeon_id")
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or not 1 <= value <= 0x7FFFFFFF
    ):
        raise ApiError("dungeon_id 必须是正整数")
    return value


def _require_abyss_max_rounds(payload: Mapping[str, Any]) -> int:
    value = payload.get("max_rounds", 0)
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or not 0 <= value <= MAX_ABYSS_ROUNDS
    ):
        raise ApiError(
            f"max_rounds 必须是 0 到 {MAX_ABYSS_ROUNDS} 之间的整数（0=直到失败）"
        )
    return value


def _require_twin_spiral_node_id(payload: Mapping[str, Any]) -> int:
    value = payload.get("node_id", 0)
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or not 0 <= value <= 0x7FFFFFFF
    ):
        raise ApiError("node_id 必须是非负整数（0=当前节点）")
    return value


def _require_task_ids(payload: Mapping[str, Any]) -> list[int]:
    values = payload.get("task_ids")
    if not isinstance(values, list):
        raise ApiError("task_ids 必须是数组")
    if len(values) > len(AVAILABLE_TASK_IDS):
        raise ApiError("任务数量超出范围")
    if any(not isinstance(value, int) or isinstance(value, bool) for value in values):
        raise ApiError("task_ids 只能包含整数")
    if len(set(values)) != len(values):
        raise ApiError("task_ids 不能重复")
    if any(value not in AVAILABLE_TASK_IDS for value in values):
        raise ApiError("包含尚未接入的任务")
    return values


def _require_auto_task_keys(payload: Mapping[str, Any]) -> list[str]:
    values = payload.get("enabled_task_keys")
    if not isinstance(values, list) or any(not isinstance(value, str) for value in values):
        raise ApiError("enabled_task_keys 必须是字符串数组")
    if len(values) > len(AUTO_TASK_KEYS):
        raise ApiError("自动任务数量超出范围")
    try:
        return ensure_auto_task_keys(values)
    except ValueError as exc:
        raise ApiError(str(exc)) from exc


def _require_auto_task_interval(payload: Mapping[str, Any]) -> int:
    value = payload.get("interval_minutes")
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or not MIN_AUTO_TASK_INTERVAL_MINUTES
        <= value
        <= MAX_AUTO_TASK_INTERVAL_MINUTES
    ):
        raise ApiError(
            "interval_minutes 必须是 "
            f"{MIN_AUTO_TASK_INTERVAL_MINUTES} 到 {MAX_AUTO_TASK_INTERVAL_MINUTES} 之间的整数"
        )
    return value


def _require_auto_task_target(payload: Mapping[str, Any]) -> tuple[str, int]:
    mode = _require_string(payload, "dragon_target_mode", maximum=16)
    if mode not in VALID_DRAGON_TARGET_MODES:
        raise ApiError("dragon_target_mode 必须是 daily 或 inventory")
    value = payload.get("dragon_target_value")
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or not 0 <= value <= 1_000_000_000
    ):
        raise ApiError("dragon_target_value 必须是 0 到 1000000000 之间的整数")
    return mode, value


def _require_furnace_target(payload: Mapping[str, Any]) -> int:
    value = payload.get("furnace_target_value")
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or not 0 <= value <= 1_000_000_000
    ):
        raise ApiError("furnace_target_value 必须是 0 到 1000000000 之间的整数")
    return value


def _require_same_origin() -> None:
    origin = request.headers.get("Origin")
    if not origin:
        return
    parsed = urlparse(origin)
    if parsed.scheme != request.scheme or parsed.netloc != request.host:
        raise ApiError("请求来源不匹配", 403)


def _public_config() -> dict[str, Any]:
    services = _services()
    settings: UiSettings = services["config_store"].snapshot()
    account = services["account"]
    jobs = services["jobs"]
    return {
        "version": settings.version,
        "account": {
            "username": settings.account.username,
            "remember_password": settings.account.remember_password,
            "password_configured": account.password_configured(),
        },
        "zone": {"id": settings.zone.id, "name": settings.zone.name},
        "daily": {"enabled_task_ids": settings.daily.enabled_task_ids},
        "auto_tasks": {
            "enabled_task_keys": settings.auto_tasks.enabled_task_keys,
            "scheduler_enabled": settings.auto_tasks.scheduler_enabled,
            "interval_minutes": settings.auto_tasks.interval_minutes,
            "dragon_target_mode": settings.auto_tasks.dragon_target_mode,
            "dragon_target_value": settings.auto_tasks.dragon_target_value,
            "furnace_target_value": settings.auto_tasks.furnace_target_value,
        },
        "arena": {
            "rounds": settings.arena.rounds,
            "outcome": settings.arena.outcome,
            "refresh_on_exhaustion": settings.arena.refresh_on_exhaustion,
        },
        "treasure": {
            "area_id": settings.treasure.area_id,
            "area_name": treasure_area_name(settings.treasure.area_id),
            "times": settings.treasure.times,
            "farm_area_id": settings.treasure.farm_area_id,
            "farm_area_name": treasure_area_name(settings.treasure.farm_area_id),
            "farm_target_hearth": settings.treasure.farm_target_hearth,
        },
        "dungeon": {
            "dungeon_id": settings.dungeon.dungeon_id,
            "dungeon_name": services["dungeon"].dungeon_name(
                settings.dungeon.dungeon_id
            ),
        },
        "abyss": {
            "max_rounds": settings.abyss.max_rounds,
            "auto_buff": settings.abyss.auto_buff,
        },
        "twin_spiral": {"node_id": settings.twin_spiral.node_id},
        "connection": account.connection_snapshot(),
        "zones": account.zones(),
        "active_job_id": jobs.active_job_id(),
    }


def _recovery_payload(
    snapshot: SessionRecoverySnapshot,
    stage: str,
    *,
    detected_snapshot: SessionRecoverySnapshot | None = None,
) -> dict[str, Any]:
    source = detected_snapshot or snapshot
    return {
        "stage": stage,
        "pending": snapshot.pending,
        "description": snapshot.describe(),
        "content_area": recovery_content_area(source).to_payload(),
        "issues": [
            {
                "kind": issue.kind,
                "label": issue.label,
                "battle_state": issue.battle_state,
                "battle_type": issue.battle_type,
            }
            for issue in snapshot.issues
        ],
    }


def _settle_session_before_task(
    services: Mapping[str, Any],
    endpoint: Any,
    emit: Any,
    stop_requested: Any,
) -> tuple[SessionRecoverySnapshot, SessionRecoverySnapshot, bool]:
    """Inspect login residue, route it to its owner, then release the task barrier."""

    manager = services["game_session"]
    emit("info", "正在读取服务端遗留状态", {"recovery": {"stage": "读取状态"}})
    session = manager.session_for_snapshot(endpoint)
    initial = session.recovery_snapshot
    if stop_requested():
        return initial, initial, True

    initial_payload = _recovery_payload(initial, "结算中")
    if initial.pending:
        area = initial_payload["content_area"]["label"]
        emit(
            "info",
            f"检测到{initial.describe()}，正在交由{area}内容区处理",
            {"recovery": initial_payload},
        )
    else:
        emit(
            "info",
            "服务端当前没有需要处理的遗留战斗或事件",
            {"recovery": _recovery_payload(initial, "无需处理")},
        )

    # session_for runs the coordinator and is the only path that releases the
    # shared-session barrier for feature traffic.
    session = manager.session_for(endpoint)
    settled = session.recovery_snapshot
    if settled.pending:
        raise JobExecutionError(f"遗留状态尚未结算：{settled.describe()}")
    return initial, settled, stop_requested()


def _run_task_with_session_recovery(
    services: Mapping[str, Any],
    endpoint: Any,
    task_label: str,
    emit: Any,
    stop_requested: Any,
    operation: Callable[[], dict[str, Any]],
) -> dict[str, Any]:
    """Run every feature job behind the same observable recovery barrier."""

    try:
        initial, settled, cancelled = _settle_session_before_task(
            services, endpoint, emit, stop_requested
        )
    except JobExecutionError:
        raise
    except GameSessionError as exc:
        raise JobExecutionError("处理服务端遗留状态失败，请刷新后重试") from exc

    recovery = _recovery_payload(
        settled,
        "已停止" if cancelled else "已完成",
        detected_snapshot=initial,
    )
    if cancelled:
        return {"cancelled": True, "recovery": recovery}

    emit(
        "info",
        f"服务端遗留状态已确认，正在启动{task_label}",
        {"recovery": _recovery_payload(settled, "启动任务", detected_snapshot=initial)},
    )
    result = operation()
    result["recovery"] = recovery
    return result


def _run_session_recovery(
    services: Mapping[str, Any],
    endpoint: Any,
    selected_task_ids: list[int],
    emit: Any,
    stop_requested: Any,
) -> dict[str, Any]:
    """Settle the current login residue, then return a fresh daily snapshot."""

    try:
        initial, settled, cancelled = _settle_session_before_task(
            services, endpoint, emit, stop_requested
        )
        if cancelled:
            return {
                "cancelled": True,
                "recovery": _recovery_payload(
                    settled, "已停止", detected_snapshot=initial
                ),
            }

        emit(
            "info",
            "遗留状态已结算，正在刷新日常任务",
            {
                "recovery": _recovery_payload(
                    settled, "刷新任务状态", detected_snapshot=initial
                )
            },
        )
        daily = services["daily"].refresh(endpoint, selected_task_ids)
        emit(
            "success",
            "遗留状态已处理，日常任务可继续执行",
            {
                "daily": daily,
                "recovery": _recovery_payload(
                    settled, "已完成", detected_snapshot=initial
                ),
            },
        )
        return {
            "cancelled": False,
            "daily": daily,
            "recovery": _recovery_payload(
                settled, "已完成", detected_snapshot=initial
            ),
        }
    except JobExecutionError:
        raise
    except GameSessionError as exc:
        raise JobExecutionError("处理服务端遗留状态失败，请刷新后重试") from exc
    except (DailyServiceError, DailyQuestError, HarvestError, OSError, ValueError) as exc:
        raise JobExecutionError("刷新结算后的日常任务状态失败") from exc


def _treasure_snapshot(endpoint: Any, selected_area_id: int) -> dict[str, Any]:
    return _services()["treasure"].snapshot(endpoint, selected_area_id)


def _dungeon_snapshot(endpoint: Any, selected_dungeon_id: int) -> dict[str, Any]:
    return _services()["dungeon"].snapshot(endpoint, selected_dungeon_id)


def register_routes(app: Flask) -> None:
    @app.before_request
    def validate_api_write_request() -> None:
        if request.path.startswith("/api/") and request.method in {"POST", "PUT", "PATCH", "DELETE"}:
            _require_same_origin()
            _require_json_body()

    @app.errorhandler(ApiError)
    def handle_api_error(error: ApiError):
        return jsonify({"error": error.message}), error.status_code

    @app.errorhandler(JobNotFoundError)
    def handle_missing_job(error: JobNotFoundError):
        return jsonify({"error": str(error)}), 404

    @app.get("/")
    def index():
        return render_template(
            "index.html",
            initial_state={"config": _public_config(), "daily": None},
        )

    @app.get("/api/config")
    def get_config():
        return jsonify(_public_config())

    @app.post("/api/account/login")
    def login():
        payload = _require_json_body()
        username = _require_string(payload, "username", maximum=64)
        # 允许空密码：勾选「记住密码」且系统凭据库已有密码时复用。
        password = _require_string(payload, "password", maximum=256, allow_empty=True)
        remember_password = _require_bool(payload, "remember_password")
        services = _services()
        services["daily"].clear_game_server()
        account = services["account"]
        try:
            result = account.login(username, password, remember_password)
        except CredentialStorageError as exc:
            raise ApiError(str(exc), 503) from exc
        except AccountLoginError as exc:
            # 缺密码属于客户端可修正的输入问题，其余上游失败仍返回 502。
            status = 400 if "请输入密码" in str(exc) else 502
            raise ApiError(str(exc), status) from exc
        return jsonify({"config": _public_config(), **result})

    @app.put("/api/config/zone")
    def update_zone():
        payload = _require_json_body()
        zone_id = _require_string(payload, "id", maximum=64)
        zone_name = _require_string(payload, "name", maximum=128)
        try:
            _services()["account"].select_zone(zone_id, zone_name)
        except ValueError as exc:
            raise ApiError(str(exc)) from exc
        _services()["daily"].clear_game_server()
        return jsonify({"config": _public_config()})

    @app.get("/api/daily-tasks")
    def get_daily_tasks():
        services = _services()
        if services["jobs"].active_job_id() is not None:
            raise ApiError("已有任务正在运行", 409)
        try:
            endpoint = services["account"].resolve_selected_game_endpoint()
            settings = services["config_store"].snapshot()
            return jsonify(
                services["daily"].refresh(
                    endpoint,
                    settings.daily.enabled_task_ids,
                )
            )
        except AccountLoginError as exc:
            raise ApiError(str(exc)) from exc
        except DailyServiceError as exc:
            raise ApiError(str(exc), 502) from exc
        except (DailyQuestError, HarvestError, OSError, ValueError) as exc:
            raise ApiError(
                "游戏服日常状态查询失败，请检查网络和区服状态", 502
            ) from exc

    @app.put("/api/daily-tasks/selection")
    def update_daily_selection():
        task_ids = _require_task_ids(_require_json_body())
        _services()["config_store"].set_daily_selection(task_ids)
        return jsonify({"config": _public_config()})

    @app.get("/api/auto-tasks")
    def get_auto_tasks():
        services = _services()
        settings = services["config_store"].snapshot().auto_tasks
        return jsonify(
            {
                "config": _public_config(),
                **services["auto_tasks"].snapshot(settings),
                "scheduler": services["auto_task_scheduler"].snapshot(),
            }
        )

    @app.put("/api/config/auto-tasks")
    def update_auto_tasks_config():
        payload = _require_json_body()
        task_keys = _require_auto_task_keys(payload)
        scheduler_enabled = _require_bool(payload, "scheduler_enabled")
        interval_minutes = _require_auto_task_interval(payload)
        target_mode, target_value = _require_auto_task_target(payload)
        furnace_target_value = _require_furnace_target(payload)
        _services()["config_store"].set_auto_tasks(
            task_keys,
            scheduler_enabled,
            interval_minutes,
            target_mode,
            target_value,
            furnace_target_value,
        )
        _services()["auto_task_scheduler"].wake()
        return jsonify({"config": _public_config()})

    @app.put("/api/config/arena")
    def update_arena_config():
        payload = _require_json_body()
        rounds = _require_rounds(payload)
        outcome = _require_string(payload, "outcome", maximum=16)
        if outcome not in VALID_OUTCOMES:
            raise ApiError("outcome 必须是 mercy 或 execute")
        refresh_on_exhaustion = _require_bool(payload, "refresh_on_exhaustion")
        _services()["config_store"].set_arena(
            rounds, outcome, refresh_on_exhaustion
        )
        return jsonify({"config": _public_config()})

    @app.get("/api/arena")
    def get_arena_status():
        services = _services()
        if services["jobs"].active_job_id() is not None:
            raise ApiError("已有任务正在运行", 409)
        try:
            endpoint = services["account"].resolve_selected_game_endpoint()
            snapshot = services["arena"].snapshot(
                endpoint,
                refresh_endpoint=services["account"].resolve_selected_game_endpoint,
            )
        except AccountLoginError as exc:
            raise ApiError(str(exc)) from exc
        except ArenaServiceError as exc:
            raise ApiError(str(exc), 502) from exc
        except (HarvestError, OSError, ValueError) as exc:
            raise ApiError(
                "龙痕竞技场状态查询失败，请检查网络和区服状态", 502
            ) from exc
        return jsonify({"config": _public_config(), **snapshot})

    @app.get("/api/treasure/farm-catalog")
    def get_treasure_farm_catalog():
        services = _services()
        selected = services["config_store"].snapshot().treasure.farm_area_id
        return jsonify(
            {
                "config": _public_config(),
                "farm": services["treasure"].farm_catalog(selected),
            }
        )

    @app.get("/api/treasure")
    def get_treasure_status():
        services = _services()
        if services["jobs"].active_job_id() is not None:
            raise ApiError("已有任务正在运行", 409)
        try:
            endpoint = services["account"].resolve_selected_game_endpoint()
            selected_area_id = services["config_store"].snapshot().treasure.area_id
            snapshot = _treasure_snapshot(endpoint, selected_area_id)
        except AccountLoginError as exc:
            raise ApiError(str(exc)) from exc
        except (TreasureAreaError, OSError, ValueError) as exc:
            raise ApiError(
                "聚宝之地状态查询失败，请检查网络和区服状态", 502
            ) from exc
        return jsonify({"config": _public_config(), **snapshot})

    @app.put("/api/config/treasure")
    def update_treasure_config():
        payload = _require_json_body()
        area_id = _require_treasure_area_id(payload)
        times = _require_treasure_times(payload)
        _services()["config_store"].set_treasure(area_id, times)
        return jsonify({"config": _public_config()})

    @app.put("/api/config/treasure-farm")
    def update_treasure_farm_config():
        payload = _require_json_body()
        farm_area_id = _require_farm_area_id(payload)
        farm_target_hearth = _require_farm_target_hearth(payload)
        try:
            # 校验地图在配置表中存在
            from treasure_farm import get_treasure_map_entry

            get_treasure_map_entry(farm_area_id)
        except TreasureFarmError as exc:
            raise ApiError(str(exc)) from exc
        _services()["config_store"].set_treasure_farm(
            farm_area_id, farm_target_hearth
        )
        return jsonify({"config": _public_config()})

    @app.get("/api/dungeon")
    def get_dungeon_status():
        services = _services()
        if services["jobs"].active_job_id() is not None:
            raise ApiError("已有任务正在运行", 409)
        try:
            endpoint = services["account"].resolve_selected_game_endpoint()
            selected_dungeon_id = (
                services["config_store"].snapshot().dungeon.dungeon_id
            )
            snapshot = _dungeon_snapshot(endpoint, selected_dungeon_id)
        except AccountLoginError as exc:
            raise ApiError(str(exc)) from exc
        except (DungeonSweepError, HarvestError, OSError, ValueError) as exc:
            raise ApiError(
                "地下城状态查询失败，请检查网络和区服状态", 502
            ) from exc
        return jsonify({"config": _public_config(), **snapshot})

    @app.put("/api/config/dungeon")
    def update_dungeon_config():
        dungeon_id = _require_dungeon_id(_require_json_body())
        _services()["config_store"].set_dungeon(dungeon_id)
        return jsonify({"config": _public_config()})

    @app.get("/api/abyss")
    def get_abyss_status():
        services = _services()
        if services["jobs"].active_job_id() is not None:
            raise ApiError("已有任务正在运行", 409)
        try:
            endpoint = services["account"].resolve_selected_game_endpoint()
            snapshot = services["abyss"].snapshot(
                endpoint,
                refresh_endpoint=services["account"].resolve_selected_game_endpoint,
            )
        except AccountLoginError as exc:
            raise ApiError(str(exc)) from exc
        except AbyssServiceError as exc:
            raise ApiError(str(exc), 502) from exc
        except (HarvestError, OSError, ValueError) as exc:
            raise ApiError(
                "罪者深渊状态查询失败，请检查网络和区服状态", 502
            ) from exc
        return jsonify({"config": _public_config(), **snapshot})

    @app.put("/api/config/abyss")
    def update_abyss_config():
        payload = _require_json_body()
        max_rounds = _require_abyss_max_rounds(payload)
        auto_buff = _require_bool(payload, "auto_buff")
        _services()["config_store"].set_abyss(max_rounds, auto_buff)
        return jsonify({"config": _public_config()})

    @app.get("/api/twin-spiral")
    def get_twin_spiral_status():
        services = _services()
        if services["jobs"].active_job_id() is not None:
            raise ApiError("已有任务正在运行", 409)
        try:
            endpoint = services["account"].resolve_selected_game_endpoint()
            snapshot = services["twin_spiral"].snapshot(
                endpoint,
                refresh_endpoint=services["account"].resolve_selected_game_endpoint,
            )
        except AccountLoginError as exc:
            raise ApiError(str(exc)) from exc
        except TwinSpiralServiceError as exc:
            raise ApiError(str(exc), 502) from exc
        except (HarvestError, OSError, ValueError) as exc:
            raise ApiError(
                "双生螺旋状态查询失败，请检查网络和区服状态", 502
            ) from exc
        return jsonify({"config": _public_config(), **snapshot})

    @app.put("/api/config/twin-spiral")
    def update_twin_spiral_config():
        node_id = _require_twin_spiral_node_id(_require_json_body())
        _services()["config_store"].set_twin_spiral(node_id)
        return jsonify({"config": _public_config()})

    @app.post("/api/jobs/daily")
    def start_daily_job():
        _require_json_body()
        services = _services()
        settings = services["config_store"].snapshot()
        task_ids = [
            task_id
            for task_id in settings.daily.enabled_task_ids
            if task_id in AVAILABLE_TASK_IDS
        ]
        if not task_ids:
            raise ApiError("请先选择至少一项可执行任务")
        if services["jobs"].active_job_id() is not None:
            raise ApiError("已有任务正在运行", 409)
        try:
            endpoint = services["account"].resolve_selected_game_endpoint()
        except AccountLoginError as exc:
            raise ApiError(str(exc)) from exc

        def run_daily(emit: Any, stop_requested: Any) -> dict[str, Any]:
            try:
                services["daily"].use_game_server(endpoint)
                return services["daily"].run(
                    task_ids,
                    task_ids,
                    emit,
                    stop_requested,
                )
            except DailyServiceError as exc:
                raise JobExecutionError(str(exc)) from exc
            except (DailyQuestError, HarvestError, OSError, ValueError) as exc:
                raise JobExecutionError("游戏服日常任务执行失败，请刷新后重试") from exc

        try:
            job = services["jobs"].start(
                "daily",
                lambda emit, stop_requested: _run_task_with_session_recovery(
                    services,
                    endpoint,
                    "日常任务",
                    emit,
                    stop_requested,
                    lambda: run_daily(emit, stop_requested),
                ),
            )
        except JobConflictError as exc:
            raise ApiError(str(exc), 409) from exc
        return jsonify({"job": job})

    @app.post("/api/jobs/auto-tasks")
    def start_auto_tasks_job():
        _require_json_body()
        services = _services()
        settings = services["config_store"].snapshot().auto_tasks
        if not settings.enabled_task_keys:
            raise ApiError("请先选择至少一项自动任务")
        if services["jobs"].active_job_id() is not None:
            raise ApiError("已有任务正在运行", 409)
        try:
            endpoint = services["account"].resolve_selected_game_endpoint()
        except AccountLoginError as exc:
            raise ApiError(str(exc)) from exc
        try:
            job = services["jobs"].start(
                "auto_tasks",
                lambda emit, stop_requested: _run_task_with_session_recovery(
                    services,
                    endpoint,
                    "自动任务",
                    emit,
                    stop_requested,
                    lambda: services["auto_tasks"].run(
                        endpoint,
                        settings,
                        emit,
                        stop_requested,
                    ),
                ),
            )
        except JobConflictError as exc:
            raise ApiError(str(exc), 409) from exc
        return jsonify({"job": job})

    @app.post("/api/jobs/recovery")
    def start_recovery_job():
        _require_json_body()
        services = _services()
        if services["jobs"].active_job_id() is not None:
            raise ApiError("已有任务正在运行", 409)
        try:
            endpoint = services["account"].resolve_selected_game_endpoint()
        except AccountLoginError as exc:
            raise ApiError(str(exc)) from exc
        selected_task_ids = services["config_store"].snapshot().daily.enabled_task_ids
        try:
            job = services["jobs"].start(
                "recovery",
                lambda emit, stop_requested: _run_session_recovery(
                    services,
                    endpoint,
                    selected_task_ids,
                    emit,
                    stop_requested,
                ),
            )
        except JobConflictError as exc:
            raise ApiError(str(exc), 409) from exc
        return jsonify({"job": job})

    @app.post("/api/jobs/arena")
    def start_arena_job():
        payload = _require_json_body()
        rounds = _require_rounds(payload)
        outcome = _require_string(payload, "outcome", maximum=16)
        if outcome not in VALID_OUTCOMES:
            raise ApiError("outcome 必须是 mercy 或 execute")
        refresh_on_exhaustion = _require_bool(payload, "refresh_on_exhaustion")
        services = _services()
        if services["jobs"].active_job_id() is not None:
            raise ApiError("已有任务正在运行", 409)
        try:
            endpoint = services["account"].resolve_selected_game_endpoint()
        except AccountLoginError as exc:
            raise ApiError(str(exc)) from exc
        services["config_store"].set_arena(
            rounds, outcome, refresh_on_exhaustion
        )
        refresh_endpoint = services["account"].resolve_selected_game_endpoint
        try:
            job = services["jobs"].start(
                "arena",
                lambda emit, stop_requested: _run_task_with_session_recovery(
                    services,
                    endpoint,
                    "龙痕竞技场",
                    emit,
                    stop_requested,
                    lambda: services["arena"].run(
                        endpoint,
                        rounds,
                        outcome,
                        refresh_on_exhaustion,
                        emit,
                        stop_requested,
                        refresh_endpoint=refresh_endpoint,
                    ),
                ),
            )
        except JobConflictError as exc:
            raise ApiError(str(exc), 409) from exc
        return jsonify({"job": job})

    @app.post("/api/jobs/treasure")
    def start_treasure_job():
        payload = _require_json_body()
        area_id = _require_treasure_area_id(payload)
        times = _require_treasure_times(payload)
        services = _services()
        if services["jobs"].active_job_id() is not None:
            raise ApiError("已有任务正在运行", 409)
        try:
            endpoint = services["account"].resolve_selected_game_endpoint()
        except AccountLoginError as exc:
            raise ApiError(str(exc)) from exc
        services["config_store"].set_treasure(area_id, times)
        try:
            job = services["jobs"].start(
                "treasure",
                lambda emit, stop_requested: _run_task_with_session_recovery(
                    services,
                    endpoint,
                    "聚宝之地扫荡",
                    emit,
                    stop_requested,
                    lambda: services["treasure"].run(
                        endpoint,
                        area_id,
                        times,
                        emit,
                        stop_requested,
                    ),
                ),
            )
        except JobConflictError as exc:
            raise ApiError(str(exc), 409) from exc
        return jsonify({"job": job, "config": _public_config()})

    @app.post("/api/jobs/treasure-farm")
    def start_treasure_farm_job():
        payload = _require_json_body()
        farm_area_id = _require_farm_area_id(payload)
        farm_target_hearth = _require_farm_target_hearth(payload)
        services = _services()
        if services["jobs"].active_job_id() is not None:
            raise ApiError("已有任务正在运行", 409)
        try:
            from treasure_farm import get_treasure_map_entry

            get_treasure_map_entry(farm_area_id)
            endpoint = services["account"].resolve_selected_game_endpoint()
        except AccountLoginError as exc:
            raise ApiError(str(exc)) from exc
        except TreasureFarmError as exc:
            raise ApiError(str(exc)) from exc
        services["config_store"].set_treasure_farm(
            farm_area_id, farm_target_hearth
        )
        try:
            job = services["jobs"].start(
                "treasure_farm",
                lambda emit, stop_requested: _run_task_with_session_recovery(
                    services,
                    endpoint,
                    "聚宝之地刷取",
                    emit,
                    stop_requested,
                    lambda: services["treasure"].run_farm(
                        endpoint,
                        farm_area_id,
                        farm_target_hearth,
                        emit,
                        stop_requested,
                    ),
                ),
            )
        except JobConflictError as exc:
            raise ApiError(str(exc), 409) from exc
        return jsonify({"job": job, "config": _public_config()})

    @app.post("/api/jobs/dungeon")
    def start_dungeon_job():
        dungeon_id = _require_dungeon_id(_require_json_body())
        services = _services()
        if services["jobs"].active_job_id() is not None:
            raise ApiError("已有任务正在运行", 409)
        try:
            endpoint = services["account"].resolve_selected_game_endpoint()
        except AccountLoginError as exc:
            raise ApiError(str(exc)) from exc
        services["config_store"].set_dungeon(dungeon_id)
        try:
            job = services["jobs"].start(
                "dungeon",
                lambda emit, stop_requested: _run_task_with_session_recovery(
                    services,
                    endpoint,
                    "地下城扫荡",
                    emit,
                    stop_requested,
                    lambda: services["dungeon"].run(
                        endpoint,
                        dungeon_id,
                        emit,
                        stop_requested,
                    ),
                ),
            )
        except JobConflictError as exc:
            raise ApiError(str(exc), 409) from exc
        return jsonify({"job": job, "config": _public_config()})

    @app.post("/api/jobs/abyss")
    def start_abyss_job():
        payload = _require_json_body()
        max_rounds = _require_abyss_max_rounds(payload)
        auto_buff = _require_bool(payload, "auto_buff")
        services = _services()
        if services["jobs"].active_job_id() is not None:
            raise ApiError("已有任务正在运行", 409)
        try:
            endpoint = services["account"].resolve_selected_game_endpoint()
        except AccountLoginError as exc:
            raise ApiError(str(exc)) from exc
        services["config_store"].set_abyss(max_rounds, auto_buff)
        refresh_endpoint = services["account"].resolve_selected_game_endpoint
        try:
            job = services["jobs"].start(
                "abyss",
                lambda emit, stop_requested: _run_task_with_session_recovery(
                    services,
                    endpoint,
                    "罪者深渊",
                    emit,
                    stop_requested,
                    lambda: services["abyss"].run(
                        endpoint,
                        emit,
                        stop_requested,
                        max_rounds=max_rounds,
                        auto_buff=auto_buff,
                        refresh_endpoint=refresh_endpoint,
                    ),
                ),
            )
        except JobConflictError as exc:
            raise ApiError(str(exc), 409) from exc
        return jsonify({"job": job, "config": _public_config()})

    @app.post("/api/jobs/twin-spiral")
    def start_twin_spiral_job():
        node_id = _require_twin_spiral_node_id(_require_json_body())
        services = _services()
        if services["jobs"].active_job_id() is not None:
            raise ApiError("已有任务正在运行", 409)
        try:
            endpoint = services["account"].resolve_selected_game_endpoint()
        except AccountLoginError as exc:
            raise ApiError(str(exc)) from exc
        services["config_store"].set_twin_spiral(node_id)
        try:
            job = services["jobs"].start(
                "twin_spiral",
                lambda emit, stop_requested: _run_task_with_session_recovery(
                    services,
                    endpoint,
                    "双生螺旋",
                    emit,
                    stop_requested,
                    lambda: services["twin_spiral"].run(
                        endpoint,
                        node_id,
                        emit,
                        stop_requested,
                        refresh_endpoint=services["account"].resolve_selected_game_endpoint,
                    ),
                ),
            )
        except JobConflictError as exc:
            raise ApiError(str(exc), 409) from exc
        return jsonify({"job": job, "config": _public_config()})

    @app.post("/api/jobs/monopoly")
    def start_monopoly_job():
        _require_json_body()
        services = _services()
        if services["jobs"].active_job_id() is not None:
            raise ApiError("已有任务正在运行", 409)
        try:
            endpoint = services["account"].resolve_selected_game_endpoint()
        except AccountLoginError as exc:
            raise ApiError(str(exc)) from exc
        try:
            job = services["jobs"].start(
                "monopoly",
                lambda emit, stop_requested: _run_task_with_session_recovery(
                    services,
                    endpoint,
                    "宫廷棋",
                    emit,
                    stop_requested,
                    lambda: services["monopoly"].run(
                        endpoint,
                        emit,
                        stop_requested,
                    ),
                ),
            )
        except JobConflictError as exc:
            raise ApiError(str(exc), 409) from exc
        return jsonify({"job": job})

    @app.get("/api/jobs/<job_id>")
    def get_job(job_id: str):
        after_raw = request.args.get("after", "0")
        try:
            after_sequence = int(after_raw)
        except ValueError as exc:
            raise ApiError("after 必须是非负整数") from exc
        if after_sequence < 0:
            raise ApiError("after 必须是非负整数")
        return jsonify(_services()["jobs"].snapshot(job_id, after_sequence))

    @app.post("/api/jobs/<job_id>/cancel")
    def cancel_job(job_id: str):
        _require_json_body()
        return jsonify({"job": _services()["jobs"].request_cancel(job_id)})

    @app.get("/healthz")
    def healthz():
        return {"status": "ok"}

    @app.get("/favicon.ico")
    def favicon():
        abort(404)
