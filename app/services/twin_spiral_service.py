"""秽肉之塔自动挑战的 UI 服务层。"""

from __future__ import annotations

from typing import Any, Callable, Protocol

from dragon_arena import GameLoginKickout
from game_session import GameSessionManager
from harvest_fief import GameEndpoint, HarvestError
from twin_spiral import (
    TwinSpiralClient,
    TwinSpiralRoundResult,
    TwinSpiralStatus,
)

from ..job_manager import JobExecutionError
from .arena_service import describe_login_kickout


EndpointRefresher = Callable[[], GameEndpoint]
LiveClientBuilder = Callable[[GameEndpoint, Callable[[str], None]], "TwinSpiralGameClient"]


class TwinSpiralServiceError(RuntimeError):
    """双生螺旋状态或作业无法投影到 UI。"""


class TwinSpiralGameClient(Protocol):
    def __enter__(self) -> "TwinSpiralGameClient": ...

    def close(self) -> None: ...

    def get_status(self) -> TwinSpiralStatus: ...

    def run_after_first(
        self,
        node_id: int,
        *,
        stop_requested: Callable[[], bool] | None = None,
        on_round: Callable[[TwinSpiralRoundResult], None] | None = None,
    ) -> tuple[TwinSpiralRoundResult, ...]: ...


def status_to_payload(status: TwinSpiralStatus) -> dict[str, Any]:
    return {
        "tower_name": status.tower_name,
        "active": status.active,
        "current_area_id": status.current_area_id,
        "current_node_id": status.current_node_id,
        "available_node_ids": list(status.available_node_ids),
        "node_states": dict(status.node_states),
        "battle_count": status.battle_count,
        "steps": status.steps,
        "step_limit": status.step_limit,
    }


def format_run_summary(stats: dict[str, Any], *, cancelled: bool = False) -> str:
    prefix = "已停止" if cancelled else "挑战结束"
    return (
        f"{prefix} · {int(stats.get('wins') or 0)} 胜 / "
        f"{int(stats.get('losses') or 0)} 负 / "
        f"共 {int(stats.get('completed_rounds') or 0)} 场"
        f" · {stats.get('stop_reason') or '循环结束'}"
    )


class TwinSpiralService:
    """将 Rogue 挑战流封装为可轮询、可取消的控制台作业。"""

    def __init__(
        self,
        *,
        live_client_builder: LiveClientBuilder | None = None,
        game_timeout: float = 15.0,
        battle_timeout: float = 180.0,
        session_manager: GameSessionManager | None = None,
    ) -> None:
        self._session_manager = session_manager
        self._live_client_builder = live_client_builder or (
            lambda endpoint, log: TwinSpiralClient(
                endpoint,
                game_timeout,
                battle_timeout=battle_timeout,
                log=log,
                log_server_messages=False,
                business_log=None,
                task="twin_spiral",
                session=(
                    self._session_manager.session_for(endpoint)
                    if self._session_manager is not None
                    else None
                ),
            )
        )

    def snapshot(
        self,
        endpoint: GameEndpoint,
        *,
        refresh_endpoint: EndpointRefresher | None = None,
    ) -> dict[str, Any]:
        client: TwinSpiralGameClient | None = None
        try:
            client = self._open_client(endpoint, lambda _message: None)
            return {"twin_spiral": status_to_payload(client.get_status())}
        except GameLoginKickout as exc:
            raise TwinSpiralServiceError(describe_login_kickout(exc)) from exc
        except (HarvestError, OSError, ValueError) as exc:
            raise TwinSpiralServiceError("读取双生螺旋状态失败") from exc
        finally:
            self._close(client)

    def run(
        self,
        endpoint: GameEndpoint,
        node_id: int,
        emit: Callable[[str, str, dict[str, Any]], None],
        stop_requested: Callable[[], bool],
        *,
        refresh_endpoint: EndpointRefresher | None = None,
    ) -> dict[str, Any]:
        stats = self._initial_stats(node_id)

        def progress(level: str, message: str) -> None:
            emit(level, message, {"twin_spiral": dict(stats)})

        client: TwinSpiralGameClient | None = None
        try:
            stats["stage"] = "连接中"
            stats["last_result"] = "正在连接双生螺旋"
            progress("info", "连接双生螺旋·秽肉之塔…")
            client = self._open_client(endpoint, lambda _message: None)
            status = client.get_status()
            self._apply_status(stats, status)
            selected_node_id = node_id or status.current_node_id
            if selected_node_id <= 0:
                raise TwinSpiralServiceError(
                    "未识别当前挑战节点，请先进入秽肉之塔并定位挑战节点"
                )
            stats["node_id"] = selected_node_id
            if stop_requested():
                return self._finish(stats, cancelled=True, progress=progress)

            def on_round(result: TwinSpiralRoundResult) -> None:
                stats["completed_rounds"] = int(stats["completed_rounds"]) + 1
                battle = result.battle
                label = "自动连战" if result.automatic else "首战"
                if battle is None:
                    stats["auto_enabled"] = result.auto_enabled
                    stats["stage"] = "开始失败"
                    stats["stop_reason"] = f"{label}开始失败 ret={result.start.ret}"
                    stats["last_result"] = stats["stop_reason"]
                    progress("error", stats["last_result"])
                    return
                if battle.win:
                    stats["wins"] = int(stats["wins"]) + 1
                    stats["auto_enabled"] = result.auto_enabled
                    if not result.automatic:
                        stats["stage"] = "自动连战中"
                        stats["last_result"] = "首战胜利，自动连战已开启"
                        progress("success", stats["last_result"])
                    else:
                        stats["stage"] = "自动连战中"
                        stats["last_result"] = "自动连战胜利，继续下一场"
                        progress("success", stats["last_result"])
                    return
                stats["losses"] = int(stats["losses"]) + 1
                stats["auto_enabled"] = False
                stats["stage"] = "战斗失败"
                stats["stop_reason"] = "战斗失败"
                stats["last_result"] = f"{label}失败，停止自动连战"
                progress("warning", stats["last_result"])

            stats["stage"] = "首战中"
            stats["last_result"] = "正在执行首战"
            progress("info", "开始首战；首战胜利后开启自动连战…")
            results = client.run_after_first(
                selected_node_id,
                stop_requested=stop_requested,
                on_round=on_round,
            )
            if stop_requested():
                return self._finish(stats, cancelled=True, progress=progress)
            if not stats["stop_reason"]:
                if results and results[-1].battle is None:
                    stats["stop_reason"] = "挑战未进入战斗"
                elif results and results[-1].battle and not results[-1].battle.win:
                    stats["stop_reason"] = "战斗失败"
                else:
                    stats["stop_reason"] = "循环结束"
            return self._finish(stats, cancelled=False, progress=progress)
        except TwinSpiralServiceError as exc:
            stats["stage"] = "准备失败"
            stats["last_result"] = str(exc)
            progress("error", stats["last_result"])
            raise JobExecutionError(str(exc)) from exc
        except GameLoginKickout as exc:
            message = describe_login_kickout(exc)
            stats["stage"] = "登录被拒绝"
            stats["last_result"] = message
            progress("error", message)
            raise JobExecutionError(message) from exc
        except (HarvestError, OSError, ValueError) as exc:
            stats["stage"] = "执行失败"
            stats["last_result"] = "双生螺旋挑战未完成，请检查游戏服连接后重试"
            progress("error", stats["last_result"])
            raise JobExecutionError(stats["last_result"]) from exc
        finally:
            self._close(client)

    @staticmethod
    def _initial_stats(node_id: int) -> dict[str, Any]:
        return {
            "tower_name": "秽肉之塔",
            "node_id": node_id,
            "current_area_id": 0,
            "current_node_id": 0,
            "battle_count": 0,
            "completed_rounds": 0,
            "wins": 0,
            "losses": 0,
            "auto_enabled": False,
            "stage": "空闲",
            "last_result": "等待开始",
            "stop_reason": "",
        }

    @staticmethod
    def _apply_status(stats: dict[str, Any], status: TwinSpiralStatus) -> None:
        stats["tower_name"] = status.tower_name
        stats["current_area_id"] = status.current_area_id
        stats["current_node_id"] = status.current_node_id
        stats["battle_count"] = status.battle_count
        stats["available_node_ids"] = list(status.available_node_ids)

    @staticmethod
    def _finish(
        stats: dict[str, Any],
        *,
        cancelled: bool,
        progress: Callable[[str, str], None],
    ) -> dict[str, Any]:
        if cancelled:
            stats["auto_enabled"] = False
            stats["stage"] = "已停止"
            if not stats["stop_reason"]:
                stats["stop_reason"] = "用户停止"
        elif stats["stage"] not in {"战斗失败", "开始失败"}:
            stats["stage"] = "已完成"
        stats["last_result"] = format_run_summary(stats, cancelled=cancelled)
        progress("warning" if cancelled else "success", stats["last_result"])
        return {"cancelled": cancelled, "twin_spiral": dict(stats)}

    def _open_client(
        self, endpoint: GameEndpoint, log: Callable[[str], None]
    ) -> TwinSpiralGameClient:
        return self._live_client_builder(endpoint, log).__enter__()

    @staticmethod
    def _close(client: TwinSpiralGameClient | None) -> None:
        if client is not None:
            client.close()
