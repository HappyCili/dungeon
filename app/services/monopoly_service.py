"""宫廷棋自动掷骰的 UI 编排服务。"""

from __future__ import annotations

import uuid
from typing import Any, Callable, Protocol

from game_session import GameSessionManager
from harvest_fief import GameEndpoint, HarvestError
from logging_store import MANAGED_DESTINATION, LogPersistenceError, write_standard_log
from monopoly import (
    MonopolyClient,
    MonopolyDiceSelection,
    MonopolyError,
    MonopolyTurnResult,
    describe_roll_rejection,
)

from ..job_manager import JobExecutionError


LiveClientBuilder = Callable[[GameEndpoint, Callable[[str], None]], "MonopolySession"]


class MonopolySession(Protocol):
    def __enter__(self) -> "MonopolySession": ...

    def close(self) -> None: ...

    def dice_status(self) -> MonopolyDiceSelection: ...

    def roll_once(
        self, *, stop_requested: Callable[[], bool] | None = None
    ) -> MonopolyTurnResult: ...


class MonopolyServiceError(RuntimeError):
    """宫廷棋自动任务无法投影到界面时的错误。"""


def _zone_from_endpoint(endpoint: GameEndpoint) -> dict[str, str]:
    return {
        "id": str(endpoint.zone_id or "unknown"),
        "name": str(endpoint.zone_name or endpoint.zone_id or "unknown"),
    }


def format_run_summary(stats: dict[str, Any], *, cancelled: bool = False) -> str:
    rolls = int(stats.get("rolls") or 0)
    interactions = int(stats.get("interactions") or 0)
    visit_choices = int(stats.get("visit_choices") or 0)
    layout_choices = int(stats.get("layout_choices") or 0)
    confirms = int(stats.get("display_confirms") or 0)
    heading = "已停止" if cancelled else "已结束" if stats.get("stop_reason") else "已完成"
    reason = str(stats.get("stop_reason") or "")
    suffix = f" · {reason}" if reason else ""
    dice_id = stats.get("dice_id")
    dice_label = str(stats.get("dice_label") or "骰子")
    dice_remaining = stats.get("dice_remaining")
    remaining = (
        f" · {dice_label}剩余 {dice_remaining}"
        if dice_id and dice_remaining is not None
        else ""
    )
    return (
        f"{heading} · 掷骰 {rolls} 次 · 交互选择 {interactions} 次"
        f" · 拜访选择 {visit_choices} 次"
        f" · 布局选择 {layout_choices} 次 · 展示确认 {confirms} 次{remaining}{suffix}"
    )


class MonopolyService:
    """持续掷骰，直到所选骰子耗尽、服务器拒绝或任务被停止。"""

    def __init__(
        self,
        *,
        live_client_builder: LiveClientBuilder | None = None,
        game_timeout: float = 15.0,
        result_log_destination: object = MANAGED_DESTINATION,
        session_manager: GameSessionManager | None = None,
    ) -> None:
        self._session_manager = session_manager
        self._live_client_builder = live_client_builder or (
            lambda endpoint, _log: MonopolyClient(
                endpoint,
                game_timeout,
                session=(
                    self._session_manager.session_for(endpoint)
                    if self._session_manager is not None
                    else None
                ),
            )
        )
        self._result_log_destination = result_log_destination

    def run(
        self,
        endpoint: GameEndpoint,
        emit: Callable[[str, str, dict[str, Any]], None],
        stop_requested: Callable[[], bool],
    ) -> dict[str, Any]:
        stats = self._initial_stats()
        run_id = uuid.uuid4().hex
        zone = _zone_from_endpoint(endpoint)
        client: MonopolySession | None = None
        result_payload: dict[str, Any] | None = None

        def progress(level: str, message: str) -> None:
            emit(level, message, {"monopoly": dict(stats)})

        try:
            stats["stage"] = "连接中"
            stats["last_result"] = "正在连接宫廷棋"
            progress("info", "连接宫廷棋…")
            client = self._live_client_builder(endpoint, lambda _message: None).__enter__()
            self._sync_dice_status(stats, client)
            stats["stage"] = "自动掷骰"
            dice_text = self._dice_text(stats)
            selection_policy = "事件默认选最后一项 · 拜访/布局默认选第二项"
            stats["last_result"] = f"{selection_policy} · {dice_text}"
            progress("info", f"已连接 · {selection_policy} · {dice_text}")
            if stats["dice_remaining"] == 0:
                stats["stop_reason"] = f"{stats['dice_label']}已耗尽"
                stats["stage"] = "骰子耗尽"
                result_payload = self._finish(stats, cancelled=False, progress=progress)
                return result_payload

            while True:
                if stop_requested():
                    result_payload = self._finish(stats, cancelled=True, progress=progress)
                    return result_payload

                stats["stage"] = "掷骰中"
                progress("info", "正在请求服务器掷骰…")
                turn = client.roll_once(stop_requested=stop_requested)
                if turn.cancelled or stop_requested():
                    result_payload = self._finish(stats, cancelled=True, progress=progress)
                    return result_payload
                if turn.roll is None:
                    if turn.visit_choices:
                        count = len(turn.visit_choices)
                        stats["interactions"] = int(stats["interactions"]) + count
                        stats["visit_choices"] = int(stats["visit_choices"]) + count
                    if turn.interaction_error:
                        stats["stop_reason"] = turn.interaction_error
                        stats["stage"] = "交互失败"
                        result_payload = self._finish(stats, cancelled=False, progress=progress)
                        return result_payload
                    if turn.pending_interaction:
                        stats["stop_reason"] = "宫廷棋事件未完成，已停止后续掷骰"
                        stats["stage"] = "等待事件"
                        result_payload = self._finish(stats, cancelled=False, progress=progress)
                        return result_payload
                    if turn.layout_choice is not None:
                        self._apply_layout_choice(stats, turn)
                        self._sync_dice_status(
                            stats,
                            client,
                            include_remaining=turn.dice_remaining is None,
                        )
                    if turn.dice_depleted:
                        if turn.dice_remaining is not None:
                            stats["dice_remaining"] = turn.dice_remaining
                        stats["stop_reason"] = f"{stats['dice_label']}已耗尽"
                        stats["stage"] = "骰子耗尽"
                        result_payload = self._finish(stats, cancelled=False, progress=progress)
                        return result_payload
                    if turn.layout_choice is not None:
                        continue
                    raise MonopolyServiceError("服务端未返回宫廷棋掷骰结果")

                roll = turn.roll
                self._apply_turn(stats, turn)
                self._sync_dice_status(
                    stats,
                    client,
                    include_remaining=turn.dice_remaining is None,
                )
                if roll.ret != 0:
                    stats["stop_reason"] = describe_roll_rejection(roll.ret)
                    stats["stage"] = "掷骰结束"
                    result_payload = self._finish(stats, cancelled=False, progress=progress)
                    return result_payload
                if turn.interaction_error:
                    stats["stop_reason"] = turn.interaction_error
                    stats["stage"] = "交互失败"
                    result_payload = self._finish(stats, cancelled=False, progress=progress)
                    return result_payload
                if turn.pending_interaction:
                    stats["stop_reason"] = "宫廷棋事件未完成，已停止后续掷骰"
                    stats["stage"] = "等待事件"
                    result_payload = self._finish(stats, cancelled=False, progress=progress)
                    return result_payload

                details = (
                    f"第 {stats['rolls']} 次 · 骰子 {roll.dice_id or '--'}"
                    f" · 点数 {roll.point} · 格位 {roll.cell_no}"
                    f" · {self._dice_text(stats)}"
                )
                if turn.choice is not None:
                    details += (
                        f" · 已选第 {turn.choice.button_number} 个按钮"
                        f"（{turn.choice.title or '未命名选项'}）"
                    )
                if turn.visit_choices:
                    details += " · 拜访默认选" + "、".join(
                        f"第 {item.button_number} 项" for item in turn.visit_choices
                    )
                if turn.layout_choice is not None:
                    details += (
                        f" · 棋盘布局已选第 {turn.layout_choice.button_number} 项"
                        f"（{turn.layout_choice.layout_id}）"
                    )
                stats["stage"] = "自动掷骰"
                stats["last_result"] = details
                progress("success", details)
                if turn.dice_depleted:
                    stats["stop_reason"] = f"{stats['dice_label']}已耗尽"
                    stats["stage"] = "骰子耗尽"
                    result_payload = self._finish(stats, cancelled=False, progress=progress)
                    return result_payload
        except JobExecutionError:
            raise
        except MonopolyServiceError as exc:
            stats["stage"] = "执行失败"
            stats["last_result"] = str(exc)
            progress("error", stats["last_result"])
            result_payload = {"cancelled": False, "monopoly": dict(stats), "failed": True}
            raise JobExecutionError(stats["last_result"]) from exc
        except (MonopolyError, HarvestError, OSError, ValueError) as exc:
            stats["stage"] = "执行失败"
            stats["last_result"] = "宫廷棋自动掷骰未完成，请检查游戏服连接后重试"
            progress("error", stats["last_result"])
            result_payload = {"cancelled": False, "monopoly": dict(stats), "failed": True}
            raise JobExecutionError(stats["last_result"]) from exc
        finally:
            self._close(client)
            if result_payload is not None:
                self._persist_run_result(zone=zone, run_id=run_id, result=result_payload)

    @staticmethod
    def _initial_stats() -> dict[str, Any]:
        return {
            "rolls": 0,
            "interactions": 0,
            "visit_choices": 0,
            "layout_choices": 0,
            "display_confirms": 0,
            "dice_id": None,
            "dice_label": "骰子",
            "dice_remaining": None,
            "last_dice_id": None,
            "last_point": None,
            "cell_no": None,
            "current_turn": None,
            "total_turn": None,
            "stage": "空闲",
            "last_result": "等待开始",
            "stop_reason": "",
        }

    @staticmethod
    def _apply_turn(stats: dict[str, Any], turn: MonopolyTurnResult) -> None:
        assert turn.roll is not None
        roll = turn.roll
        if roll.ret == 0:
            stats["rolls"] = int(stats["rolls"]) + 1
        if turn.choice is not None:
            stats["interactions"] = int(stats["interactions"]) + 1
        if turn.visit_choices:
            count = len(turn.visit_choices)
            stats["interactions"] = int(stats["interactions"]) + count
            stats["visit_choices"] = int(stats["visit_choices"]) + count
        if turn.layout_choice is not None:
            stats["interactions"] = int(stats["interactions"]) + 1
            stats["layout_choices"] = int(stats["layout_choices"]) + 1
        stats["display_confirms"] = int(stats["display_confirms"]) + turn.display_confirms
        stats["last_dice_id"] = roll.dice_id or None
        if roll.dice_id:
            stats["dice_id"] = roll.dice_id
        if turn.dice_remaining is not None:
            stats["dice_remaining"] = turn.dice_remaining
        stats["last_point"] = roll.point
        stats["cell_no"] = roll.cell_no
        stats["current_turn"] = roll.current_turn
        stats["total_turn"] = roll.total_turn

    @staticmethod
    def _apply_layout_choice(stats: dict[str, Any], turn: MonopolyTurnResult) -> None:
        if turn.layout_choice is None:
            return
        stats["interactions"] = int(stats["interactions"]) + 1
        stats["layout_choices"] = int(stats["layout_choices"]) + 1

    @staticmethod
    def _sync_dice_status(
        stats: dict[str, Any], client: MonopolySession, *, include_remaining: bool = True
    ) -> None:
        get_status = getattr(client, "dice_status", None)
        if not callable(get_status):
            return
        status = get_status()
        dice_id = int(getattr(status, "dice_id", 0) or 0)
        if dice_id:
            stats["dice_id"] = dice_id
        label = str(getattr(status, "label", "") or "")
        if label:
            stats["dice_label"] = label
        available = getattr(status, "available", None)
        if include_remaining and available is not None:
            stats["dice_remaining"] = max(0, int(available))

    @staticmethod
    def _dice_text(stats: dict[str, Any]) -> str:
        label = str(stats.get("dice_label") or "骰子")
        remaining = stats.get("dice_remaining")
        return f"{label}剩余 {remaining}" if remaining is not None else f"{label}余量待同步"

    @staticmethod
    def _finish(
        stats: dict[str, Any],
        *,
        cancelled: bool,
        progress: Callable[[str, str], None],
    ) -> dict[str, Any]:
        stats["stage"] = "已停止" if cancelled else "已完成"
        stats["last_result"] = format_run_summary(stats, cancelled=cancelled)
        progress("warning" if cancelled else "success", stats["last_result"])
        return {"cancelled": cancelled, "monopoly": dict(stats)}

    def _persist_run_result(
        self,
        *,
        zone: dict[str, str],
        run_id: str,
        result: dict[str, Any],
    ) -> None:
        if self._result_log_destination is None:
            return
        monopoly = result.get("monopoly")
        if not isinstance(monopoly, dict):
            return
        cancelled = bool(result.get("cancelled"))
        failed = bool(result.get("failed"))
        outcome = "skipped" if cancelled else "failure" if failed else "success"
        level = "warning" if cancelled else "error" if failed else "info"
        details = {
            "rolls": monopoly.get("rolls"),
            "interactions": monopoly.get("interactions"),
            "visit_choices": monopoly.get("visit_choices"),
            "layout_choices": monopoly.get("layout_choices"),
            "display_confirms": monopoly.get("display_confirms"),
            "dice_id": monopoly.get("dice_id"),
            "dice_label": monopoly.get("dice_label"),
            "dice_remaining": monopoly.get("dice_remaining"),
            "last_dice_id": monopoly.get("last_dice_id"),
            "last_point": monopoly.get("last_point"),
            "cell_no": monopoly.get("cell_no"),
            "current_turn": monopoly.get("current_turn"),
            "total_turn": monopoly.get("total_turn"),
            "stage": monopoly.get("stage"),
            "stop_reason": monopoly.get("stop_reason"),
            "cancelled": cancelled,
            "summary": format_run_summary(monopoly, cancelled=cancelled),
        }
        try:
            write_standard_log(
                event="monopoly",
                operation="run",
                zone=zone,
                details=details,
                destination=self._result_log_destination,
                run_id=run_id,
                outcome=outcome,
                level=level,
            )
        except LogPersistenceError:
            pass

    @staticmethod
    def _close(client: MonopolySession | None) -> None:
        if client is not None:
            client.close()
