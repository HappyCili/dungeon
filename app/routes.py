from __future__ import annotations

from typing import Any, Mapping
from urllib.parse import urlparse

from flask import Flask, abort, current_app, jsonify, render_template, request

from .config_store import MAX_TREASURE_SWEEP_TIMES, UiSettings, VALID_OUTCOMES
from .credentials import CredentialStorageError
from .job_manager import JobConflictError, JobNotFoundError
from .models import AVAILABLE_TASK_IDS
from .services.account_service import AccountLoginError
from .services.arena_service import ArenaServiceError
from .services.daily_service import DailyServiceError
from dungeon_sweep import DungeonSweepError
from daily_quest import DailyQuestError
from harvest_fief import HarvestError
from id_descriptions import treasure_area_name
from treasure_area import TreasureAreaError


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


def _require_dungeon_id(payload: Mapping[str, Any]) -> int:
    value = payload.get("dungeon_id")
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or not 1 <= value <= 0x7FFFFFFF
    ):
        raise ApiError("dungeon_id 必须是正整数")
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
        "arena": {
            "rounds": settings.arena.rounds,
            "outcome": settings.arena.outcome,
            "refresh_on_exhaustion": settings.arena.refresh_on_exhaustion,
        },
        "treasure": {
            "area_id": settings.treasure.area_id,
            "area_name": treasure_area_name(settings.treasure.area_id),
            "times": settings.treasure.times,
        },
        "dungeon": {
            "dungeon_id": settings.dungeon.dungeon_id,
            "dungeon_name": services["dungeon"].dungeon_name(
                settings.dungeon.dungeon_id
            ),
        },
        "connection": account.connection_snapshot(),
        "zones": account.zones(),
        "active_job_id": jobs.active_job_id(),
    }


def _daily_snapshot() -> dict[str, Any]:
    services = _services()
    settings = services["config_store"].snapshot()
    return services["daily"].snapshot(settings.daily.enabled_task_ids)


def _use_current_game_server_for_daily(services: Mapping[str, Any]) -> None:
    endpoint = services["account"].resolve_selected_game_endpoint()
    services["daily"].use_game_server(endpoint)


def _treasure_snapshot(endpoint: Any, selected_area_id: int) -> dict[str, Any]:
    return _services()["treasure"].snapshot(endpoint, selected_area_id)


def _validate_treasure_preflight(
    snapshot: Mapping[str, Any], area_id: int, times: int
) -> None:
    areas = snapshot["areas"]
    if not any(area["id"] == area_id for area in areas):
        raise ApiError("所选聚宝之地地图当前不可扫荡")
    remaining = snapshot["sweep"]["available"]
    if remaining <= 0:
        raise ApiError("今日聚宝之地扫荡次数已用完")
    if times > remaining:
        raise ApiError(f"今日聚宝之地仅剩 {remaining} 次可扫荡")


def _dungeon_snapshot(endpoint: Any, selected_dungeon_id: int) -> dict[str, Any]:
    return _services()["dungeon"].snapshot(endpoint, selected_dungeon_id)


def _validate_dungeon_preflight(
    snapshot: Mapping[str, Any], dungeon_id: int
) -> None:
    dungeons = snapshot["dungeons"]
    if not any(dungeon["id"] == dungeon_id for dungeon in dungeons):
        raise ApiError("所选地下城当前不可扫荡")


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
        password = _require_string(payload, "password", maximum=256)
        remember_password = _require_bool(payload, "remember_password")
        services = _services()
        services["daily"].clear_game_server()
        account = services["account"]
        try:
            result = account.login(username, password, remember_password)
        except CredentialStorageError as exc:
            raise ApiError(str(exc), 503) from exc
        except AccountLoginError as exc:
            raise ApiError(str(exc), 502) from exc
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
            _use_current_game_server_for_daily(services)
        except AccountLoginError as exc:
            raise ApiError(str(exc)) from exc
        except DailyServiceError as exc:
            raise ApiError(str(exc), 502) from exc
        try:
            return jsonify(_daily_snapshot())
        except (DailyQuestError, HarvestError, OSError, ValueError) as exc:
            raise ApiError(
                "游戏服日常状态查询失败，请检查网络和区服状态", 502
            ) from exc

    @app.put("/api/daily-tasks/selection")
    def update_daily_selection():
        task_ids = _require_task_ids(_require_json_body())
        _services()["config_store"].set_daily_selection(task_ids)
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
            snapshot = services["arena"].snapshot(endpoint)
        except AccountLoginError as exc:
            raise ApiError(str(exc)) from exc
        except (ArenaServiceError, HarvestError, OSError, ValueError) as exc:
            raise ApiError(
                "龙痕竞技场状态查询失败，请检查网络和区服状态", 502
            ) from exc
        return jsonify({"config": _public_config(), **snapshot})

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
            _use_current_game_server_for_daily(services)
        except AccountLoginError as exc:
            raise ApiError(str(exc)) from exc
        except DailyServiceError as exc:
            raise ApiError(str(exc), 502) from exc
        try:
            daily = _daily_snapshot()
        except (DailyQuestError, HarvestError, OSError, ValueError) as exc:
            raise ApiError(
                "游戏服日常状态查询失败，请检查网络和区服状态", 502
            ) from exc
        try:
            job = services["jobs"].start(
                "daily",
                lambda emit, stop_requested: services["daily"].run(
                    task_ids,
                    task_ids,
                    emit,
                    stop_requested,
                ),
            )
        except JobConflictError as exc:
            raise ApiError(str(exc), 409) from exc
        return jsonify({"job": job, "daily": daily})

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
        try:
            job = services["jobs"].start(
                "arena",
                lambda emit, stop_requested: services["arena"].run(
                    endpoint,
                    rounds,
                    outcome,
                    refresh_on_exhaustion,
                    emit,
                    stop_requested,
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
            snapshot = _treasure_snapshot(endpoint, area_id)
            _validate_treasure_preflight(snapshot, area_id, times)
        except AccountLoginError as exc:
            raise ApiError(str(exc)) from exc
        except ApiError:
            raise
        except (TreasureAreaError, OSError, ValueError) as exc:
            raise ApiError(
                "聚宝之地状态查询失败，请检查网络和区服状态", 502
            ) from exc
        services["config_store"].set_treasure(area_id, times)
        try:
            job = services["jobs"].start(
                "treasure",
                lambda emit, stop_requested: services["treasure"].run(
                    endpoint,
                    area_id,
                    times,
                    emit,
                    stop_requested,
                ),
            )
        except JobConflictError as exc:
            raise ApiError(str(exc), 409) from exc
        return jsonify({"job": job, "config": _public_config(), "treasure": snapshot})

    @app.post("/api/jobs/dungeon")
    def start_dungeon_job():
        dungeon_id = _require_dungeon_id(_require_json_body())
        services = _services()
        if services["jobs"].active_job_id() is not None:
            raise ApiError("已有任务正在运行", 409)
        try:
            endpoint = services["account"].resolve_selected_game_endpoint()
            snapshot = _dungeon_snapshot(endpoint, dungeon_id)
            _validate_dungeon_preflight(snapshot, dungeon_id)
        except AccountLoginError as exc:
            raise ApiError(str(exc)) from exc
        except ApiError:
            raise
        except (DungeonSweepError, HarvestError, OSError, ValueError) as exc:
            raise ApiError(
                "地下城状态查询失败，请检查网络和区服状态", 502
            ) from exc
        services["config_store"].set_dungeon(dungeon_id)
        try:
            job = services["jobs"].start(
                "dungeon",
                lambda emit, stop_requested: services["dungeon"].run(
                    endpoint,
                    dungeon_id,
                    emit,
                    stop_requested,
                ),
            )
        except JobConflictError as exc:
            raise ApiError(str(exc), 409) from exc
        return jsonify({"job": job, "config": _public_config(), "dungeon": snapshot})

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
