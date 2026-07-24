"""罪者深渊自动挑战服务：投影状态与可取消作业。"""

from __future__ import annotations

import time
import uuid
from typing import Any, Callable, Protocol

from grave_abyss import (
    AbyssRoundResult,
    AbyssStatus,
    GraveAbyssClient,
)
from harvest_fief import GameEndpoint, HarvestError
from logging_store import MANAGED_DESTINATION, LogPersistenceError, write_standard_log

from ..job_manager import JobExecutionError
from .arena_service import (
    KICKOUT_RET_LABELS,
    describe_login_kickout,
)
from dragon_arena import GameLoginKickout, LOGIN_KICKOUT_RETRY_DELAY

EndpointRefresher = Callable[[], GameEndpoint]
LiveClientBuilder = Callable[[GameEndpoint, Callable[[str], None]], "AbyssClient"]


class AbyssServiceError(RuntimeError):
    """罪者深渊状态不能安全投影到界面时的错误。"""


class AbyssClient(Protocol):
    def __enter__(self) -> "AbyssClient": ...

    def close(self) -> None: ...

    def get_status(self, *, sync: bool = True) -> AbyssStatus: ...

    def run_loop(
        self,
        *,
        stop_requested: Callable[[], bool] | None = None,
        max_rounds: int = 0,
        ensure_buff: bool = True,
        on_round: Callable[[AbyssRoundResult, AbyssStatus], None] | None = None,
    ) -> tuple[AbyssRoundResult, ...]: ...


def _zone_from_endpoint(endpoint: GameEndpoint) -> dict[str, str]:
    return {
        "id": str(endpoint.zone_id or "unknown"),
        "name": str(endpoint.zone_name or endpoint.zone_id or "unknown"),
    }


def format_status_line(stats: dict[str, Any]) -> str:
    wins = int(stats.get("wins") or 0)
    losses = int(stats.get("losses") or 0)
    pass_level = int(stats.get("pass_level") or 0)
    max_level = int(stats.get("max_level") or 0)
    next_level = stats.get("next_level")
    next_part = f"下一层 {next_level}" if next_level else "无下一层"
    return (
        f"通关 {pass_level}/{max_level} · {next_part} · "
        f"本局 {wins} 胜 / {losses} 负"
    )


def format_run_summary(stats: dict[str, Any], *, cancelled: bool = False) -> str:
    wins = int(stats.get("wins") or 0)
    losses = int(stats.get("losses") or 0)
    completed = int(stats.get("completed_rounds") or 0)
    pass_level = int(stats.get("pass_level") or 0)
    max_level = int(stats.get("max_level") or 0)
    header = "已停止" if cancelled else "全部完成"
    stop_reason = str(stats.get("stop_reason") or "")
    reason_part = f" · {stop_reason}" if stop_reason else ""
    return (
        f"{header} · 通关 {pass_level}/{max_level} · "
        f"挑战 {wins} 胜 / {losses} 负 / 共 {completed} 场"
        f"{reason_part}"
    )


def status_to_payload(status: AbyssStatus) -> dict[str, Any]:
    return {
        "season_id": status.season_id,
        "season_name": status.season_name,
        "season_open": status.season_open,
        "left_seconds": status.left_seconds,
        "group_id": status.group_id,
        "pass_id": status.pass_id,
        "pass_level": status.pass_level,
        "next_id": status.next_id,
        "next_level": status.next_level,
        "next_name": status.next_name,
        "max_level": status.max_level,
        "currgrave": status.currgrave,
        "optbuf": status.optbuf,
        "optbuf_desc": status.optbuf_desc,
        "actives": status.actives,
        "total_floors": status.total_floors,
    }


class AbyssService:
    """将罪者深渊 WebSocket 客户端投影为 UI 状态和可取消作业。"""

    def __init__(
        self,
        *,
        live_client_builder: LiveClientBuilder | None = None,
        game_timeout: float = 15.0,
        battle_timeout: float = 180.0,
        kickout_retry_delay: float = LOGIN_KICKOUT_RETRY_DELAY,
        result_log_destination: object = MANAGED_DESTINATION,
    ) -> None:
        self._live_client_builder = live_client_builder or (
            lambda endpoint, log: GraveAbyssClient(
                endpoint,
                game_timeout,
                battle_timeout=battle_timeout,
                log=log,
                log_server_messages=False,
                business_log=None,
                task="grave_abyss",
            )
        )
        self._kickout_retry_delay = kickout_retry_delay
        self._result_log_destination = result_log_destination

    def snapshot(
        self,
        endpoint: GameEndpoint,
        *,
        refresh_endpoint: EndpointRefresher | None = None,
    ) -> dict[str, Any]:
        client: AbyssClient | None = None
        try:
            client = self._open_client(
                endpoint,
                lambda _message: None,
                refresh_endpoint=refresh_endpoint,
            )
            status = client.get_status(sync=True)
            return {"abyss": status_to_payload(status)}
        except GameLoginKickout as exc:
            raise AbyssServiceError(describe_login_kickout(exc)) from exc
        except (HarvestError, OSError, ValueError) as exc:
            raise AbyssServiceError("读取罪者深渊状态失败") from exc
        finally:
            self._close(client)

    def run(
        self,
        endpoint: GameEndpoint,
        emit: Callable[[str, str, dict[str, Any]], None],
        stop_requested: Callable[[], bool],
        *,
        max_rounds: int = 0,
        auto_buff: bool = True,
        refresh_endpoint: EndpointRefresher | None = None,
    ) -> dict[str, Any]:
        stats = self._initial_stats(max_rounds=max_rounds, auto_buff=auto_buff)
        run_id = uuid.uuid4().hex
        zone = _zone_from_endpoint(endpoint)

        def progress(level: str, message: str) -> None:
            emit(level, message, {"abyss": dict(stats)})

        quiet_log = lambda _message: None
        client: AbyssClient | None = None
        result_payload: dict[str, Any] | None = None
        try:
            stats["stage"] = "连接中"
            stats["last_result"] = "正在连接罪者深渊"
            progress("info", "连接罪者深渊…")
            client = self._open_client(
                endpoint,
                quiet_log,
                refresh_endpoint=refresh_endpoint,
                on_kickout_retry=lambda: progress(
                    "warning",
                    (
                        "游戏服要求重新进入区服（Kickout ret 2），"
                        f"正在刷新入口并重试（等待 {self._kickout_retry_delay:g} 秒）"
                    ),
                ),
            )
            if stop_requested():
                result_payload = self._finish(stats, cancelled=True, progress=progress)
                return result_payload

            status = client.get_status(sync=True)
            self._apply_status(stats, status)
            progress(
                "info",
                (
                    f"已连接 · {status.season_name or '未知赛季'} · "
                    f"通关 {status.pass_level}/{status.max_level} · "
                    f"下一层 {status.next_level or '无'}"
                ),
            )
            if status.next_id <= 0:
                stats["stop_reason"] = "已全部通关"
                stats["stage"] = "已完成"
                stats["last_result"] = "当前赛季已全部通关，无需挑战"
                progress("success", stats["last_result"])
                result_payload = {
                    "cancelled": False,
                    "abyss": dict(stats),
                }
                return result_payload

            def on_round(result: AbyssRoundResult, status_after: AbyssStatus) -> None:
                self._apply_status(stats, status_after)
                stats["completed_rounds"] = int(stats["completed_rounds"]) + 1
                battle = result.battle
                title = f"第 {result.level} 层"
                if battle is None:
                    if result.start.ret != 0:
                        stats["stop_reason"] = f"开始失败 ret={result.start.ret}"
                        stats["last_result"] = f"{title} · 开始失败"
                        progress("error", stats["last_result"])
                    else:
                        stats["stop_reason"] = "未收到战斗结算"
                        stats["last_result"] = f"{title} · 未收到战斗结算"
                        progress("warning", stats["last_result"])
                    progress("info", format_status_line(stats))
                    return
                if battle.win:
                    stats["wins"] = int(stats["wins"]) + 1
                    stats["last_result"] = f"{title} · 胜利"
                    stats["stage"] = f"已通关 {status_after.pass_level}"
                    progress("success", f"{title} · 战斗完成：胜利")
                else:
                    stats["losses"] = int(stats["losses"]) + 1
                    stats["stop_reason"] = "战斗失败"
                    stats["last_result"] = f"{title} · 失败，停止挑战"
                    stats["stage"] = "战斗失败"
                    progress("warning", f"{title} · 战斗完成：失败，停止")
                progress("info", format_status_line(stats))

            stats["stage"] = "自动挑战中"
            progress("info", "开始自动挑战，直到战斗失败停止…")
            results = client.run_loop(
                stop_requested=stop_requested,
                max_rounds=max_rounds,
                ensure_buff=auto_buff,
                on_round=on_round,
            )
            if stop_requested():
                if not stats.get("stop_reason"):
                    stats["stop_reason"] = "用户停止"
                result_payload = self._finish(stats, cancelled=True, progress=progress)
                return result_payload

            if not stats.get("stop_reason"):
                if results and results[-1].battle and not results[-1].battle.win:
                    stats["stop_reason"] = "战斗失败"
                elif status_to_payload(
                    client.get_status(sync=False)
                ).get("next_id", 0) in (0, None):
                    stats["stop_reason"] = "已全部通关"
                elif max_rounds > 0 and len(results) >= max_rounds:
                    stats["stop_reason"] = f"达到轮数上限 {max_rounds}"
                else:
                    stats["stop_reason"] = "循环结束"

            result_payload = self._finish(stats, cancelled=False, progress=progress)
            return result_payload
        except JobExecutionError:
            raise
        except GameLoginKickout as exc:
            message = describe_login_kickout(exc)
            stats["stage"] = "登录被拒绝"
            stats["last_result"] = message
            progress("error", message)
            result_payload = {"cancelled": False, "abyss": stats, "failed": True}
            raise JobExecutionError(message) from exc
        except (HarvestError, OSError, ValueError) as exc:
            stats["stage"] = "执行失败"
            stats["last_result"] = "罪者深渊执行未完成，请检查游戏服连接后重试"
            progress("error", stats["last_result"])
            result_payload = {"cancelled": False, "abyss": stats, "failed": True}
            raise JobExecutionError(stats["last_result"]) from exc
        finally:
            self._close(client)
            if result_payload is not None:
                self._persist_run_result(
                    zone=zone,
                    run_id=run_id,
                    result=result_payload,
                )

    def _initial_stats(
        self, *, max_rounds: int, auto_buff: bool
    ) -> dict[str, Any]:
        return {
            "max_rounds": max_rounds,
            "auto_buff": auto_buff,
            "completed_rounds": 0,
            "wins": 0,
            "losses": 0,
            "pass_id": 0,
            "pass_level": 0,
            "next_id": 0,
            "next_level": 0,
            "next_name": "",
            "max_level": 0,
            "season_id": 0,
            "season_name": "",
            "season_open": False,
            "optbuf": 0,
            "optbuf_desc": "",
            "stage": "空闲",
            "last_result": "等待开始",
            "stop_reason": "",
        }

    def _apply_status(self, stats: dict[str, Any], status: AbyssStatus) -> None:
        stats["pass_id"] = status.pass_id
        stats["pass_level"] = status.pass_level
        stats["next_id"] = status.next_id
        stats["next_level"] = status.next_level
        stats["next_name"] = status.next_name
        stats["max_level"] = status.max_level
        stats["season_id"] = status.season_id
        stats["season_name"] = status.season_name
        stats["season_open"] = status.season_open
        stats["optbuf"] = status.optbuf
        stats["optbuf_desc"] = status.optbuf_desc
        stats["actives"] = status.actives
        stats["left_seconds"] = status.left_seconds

    def _finish(
        self,
        stats: dict[str, Any],
        *,
        cancelled: bool,
        progress: Callable[[str, str], None],
    ) -> dict[str, Any]:
        summary = format_run_summary(stats, cancelled=cancelled)
        stats["stage"] = "已停止" if cancelled else "已完成"
        stats["last_result"] = summary
        progress("warning" if cancelled else "success", summary)
        return {"cancelled": cancelled, "abyss": stats}

    def _persist_run_result(
        self,
        *,
        zone: dict[str, str],
        run_id: str,
        result: dict[str, Any],
    ) -> None:
        if self._result_log_destination is None:
            return
        abyss = result.get("abyss")
        if not isinstance(abyss, dict):
            return
        cancelled = bool(result.get("cancelled"))
        failed = bool(result.get("failed"))
        if cancelled:
            log_outcome, level = "skipped", "warning"
        elif failed:
            log_outcome, level = "failure", "error"
        else:
            log_outcome, level = "success", "info"
        details = {
            "completed_rounds": abyss.get("completed_rounds"),
            "wins": abyss.get("wins"),
            "losses": abyss.get("losses"),
            "pass_level": abyss.get("pass_level"),
            "max_level": abyss.get("max_level"),
            "next_level": abyss.get("next_level"),
            "season_id": abyss.get("season_id"),
            "season_name": abyss.get("season_name"),
            "stop_reason": abyss.get("stop_reason"),
            "stage": abyss.get("stage"),
            "last_result": abyss.get("last_result"),
            "cancelled": cancelled,
            "summary": format_run_summary(abyss, cancelled=cancelled),
        }
        try:
            write_standard_log(
                event="grave_abyss",
                operation="run",
                zone=zone,
                details=details,
                destination=self._result_log_destination,
                run_id=run_id,
                outcome=log_outcome,
                level=level,
            )
        except LogPersistenceError:
            pass

    def _open_client(
        self,
        endpoint: GameEndpoint,
        log: Callable[[str], None],
        *,
        refresh_endpoint: EndpointRefresher | None = None,
        on_kickout_retry: Callable[[], None] | None = None,
    ) -> AbyssClient:
        current = endpoint
        last_kickout: GameLoginKickout | None = None
        for attempt in range(2):
            client = self._live_client_builder(current, log)
            try:
                return client.__enter__()
            except GameLoginKickout as exc:
                self._close(client)
                last_kickout = exc
                # ret=2：需重新进入区服
                if (
                    exc.ret == 2
                    and attempt == 0
                    and refresh_endpoint is not None
                ):
                    if on_kickout_retry is not None:
                        on_kickout_retry()
                    time.sleep(self._kickout_retry_delay)
                    current = refresh_endpoint()
                    continue
                label = KICKOUT_RET_LABELS.get(exc.ret, "未登记的踢出原因")
                raise GameLoginKickout(exc.ret, exc.message or label) from exc
            except Exception:
                self._close(client)
                raise
        assert last_kickout is not None
        raise last_kickout

    @staticmethod
    def _close(client: AbyssClient | None) -> None:
        if client is None:
            return
        try:
            client.close()
        except Exception:
            pass
