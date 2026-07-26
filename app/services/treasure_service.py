from __future__ import annotations

import uuid
from typing import Any, Callable

from game_session import GameSessionManager
from harvest_fief import GameEndpoint, HarvestError, ItemChange
from id_descriptions import item_name, treasure_area_name
from logging_store import MANAGED_DESTINATION, LogPersistenceError, write_standard_log
from treasure_area import (
    MAX_SWEEP_TIMES_PER_REQUEST,
    TreasureAreaClient,
    TreasureAreaError,
    TreasureAreaRejected,
    TreasureAreaStatus,
    TreasureSweepLoot,
)
from treasure_farm import (
    HEARTH_ITEM_ID,
    FarmProgress,
    TreasureFarmClient,
    TreasureFarmError,
    TreasureFarmKickout,
    TreasureFarmRejected,
    get_treasure_map_entry,
    list_treasure_map_catalog,
    progress_payload,
    run_treasure_farm,
)

from ..job_manager import JobExecutionError


LiveClientBuilder = Callable[[GameEndpoint], TreasureAreaClient]
FarmClientBuilder = Callable[[GameEndpoint], TreasureFarmClient]


def _zone_from_endpoint(endpoint: GameEndpoint) -> dict[str, str]:
    """仅使用区服 id/name，不把入口 URL 或令牌写入日志。"""

    return {
        "id": str(endpoint.zone_id or "unknown"),
        "name": str(endpoint.zone_name or endpoint.zone_id or "unknown"),
    }


def aggregate_loot_entries(
    items: list[dict[str, Any]] | tuple[ItemChange, ...],
) -> list[dict[str, Any]]:
    """按物品 ID 合并重复奖励：delta 累加，total 取最后一次（扫荡后最终持有量）。"""

    order: list[int] = []
    merged: dict[int, dict[str, Any]] = {}
    for entry in items:
        if isinstance(entry, ItemChange):
            item_id = entry.item_id
            delta = entry.delta
            total = entry.total
            name = item_name(item_id)
        elif isinstance(entry, dict):
            raw_id = entry.get("id")
            if not isinstance(raw_id, int) or isinstance(raw_id, bool) or raw_id <= 0:
                continue
            item_id = raw_id
            delta = entry.get("delta")
            if not isinstance(delta, int) or isinstance(delta, bool):
                continue
            total = entry.get("total")
            name = entry.get("name") or item_name(item_id)
        else:
            continue
        if delta == 0:
            continue
        if item_id not in merged:
            order.append(item_id)
            merged[item_id] = {
                "id": item_id,
                "name": name,
                "delta": delta,
                "total": total if isinstance(total, int) and not isinstance(total, bool) else None,
            }
            continue
        current = merged[item_id]
        current["delta"] = int(current["delta"]) + delta
        if isinstance(total, int) and not isinstance(total, bool):
            current["total"] = total
        if name and not current.get("name"):
            current["name"] = name
    return [
        {
            "id": item_id,
            "name": merged[item_id]["name"],
            "delta": merged[item_id]["delta"],
            **(
                {"total": merged[item_id]["total"]}
                if merged[item_id].get("total") is not None
                else {}
            ),
        }
        for item_id in order
        if merged[item_id]["delta"] != 0
    ]


def format_loot_summary(items: list[dict[str, Any]] | tuple[ItemChange, ...]) -> str:
    """把奖励列表格式化为面向人的短摘要（同名物品已合并）。"""

    aggregated = aggregate_loot_entries(items)
    if not aggregated:
        return "无物品明细"
    parts: list[str] = []
    for entry in aggregated:
        name = entry.get("name") or item_name(entry.get("id"))
        delta = int(entry["delta"])
        total = entry.get("total")
        change = f"+{delta}" if delta >= 0 else str(delta)
        current = (
            f"（当前 {total}）"
            if isinstance(total, int) and not isinstance(total, bool)
            else ""
        )
        parts.append(f"{name}：{change}{current}")
    return "、".join(parts)


def format_run_summary(
    *,
    area_name: str,
    times: int,
    remaining: int,
    rewards: list[dict[str, Any]],
    cancelled: bool = False,
) -> str:
    header = "已停止" if cancelled else "扫荡完成"
    reward_part = format_loot_summary(rewards) if rewards else "服务端未返回物品明细"
    return (
        f"{header} · {area_name} × {times} 次 · "
        f"今日剩余 {remaining} 次 · {reward_part}"
    )


def format_farm_summary(progress: FarmProgress, *, cancelled: bool = False) -> str:
    hearth_name = item_name(HEARTH_ITEM_ID)
    if cancelled:
        header = "已停止"
    elif progress.completed:
        header = "刷取完成"
    else:
        header = "刷取结束"
    return (
        f"{header} · {progress.area_name} · "
        f"{hearth_name} +{progress.hearth_gained}/{progress.target_hearth}"
        f"（当前 {progress.hearth_total}）· "
        f"击杀 {progress.monsters_killed} · "
        f"普通宝箱 {progress.small_chests_opened} · "
        f"大宝箱 {progress.big_chests_opened} · "
        f"{progress.key_item_name} {progress.keys_total} · "
        f"已结算怪物 {progress.settled_monsters}（无钥匙 {progress.no_key_monsters}）· "
        f"缺炉温宝箱 {progress.missing_hearth_chests} · "
        f"阶段 {progress.phase_label if hasattr(progress, 'phase_label') else progress.phase}"
    )


class TreasureService:
    """聚宝之地状态投影、扫荡/刷取编排与托管结果日志。"""

    def __init__(
        self,
        *,
        live_client_builder: LiveClientBuilder | None = None,
        farm_client_builder: FarmClientBuilder | None = None,
        game_timeout: float = 15.0,
        result_log_destination: object = MANAGED_DESTINATION,
        session_manager: GameSessionManager | None = None,
    ) -> None:
        self._session_manager = session_manager
        self._live_client_builder = live_client_builder or (
            lambda endpoint: TreasureAreaClient(
                endpoint,
                game_timeout,
                session=(
                    self._session_manager.session_for(endpoint)
                    if self._session_manager is not None
                    else None
                ),
            )
        )
        self._farm_client_builder = farm_client_builder or (
            lambda endpoint: TreasureFarmClient(
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

    @staticmethod
    def farm_catalog(selected_area_id: int = 0) -> dict[str, Any]:
        """返回全部可刷取聚宝地图列表（不连游戏服）。"""

        return {
            "farm_areas": [
                {
                    "id": entry.area_id,
                    "name": entry.name,
                    "mapgroup": entry.mapgroup,
                    "key_item_id": entry.key_item_id,
                    "key_item_name": entry.key_item_name,
                    "selected": entry.area_id == selected_area_id,
                }
                for entry in list_treasure_map_catalog()
            ],
            "hearth_item_id": HEARTH_ITEM_ID,
            "hearth_item_name": item_name(HEARTH_ITEM_ID),
        }

    def run_farm(
        self,
        endpoint: GameEndpoint,
        area_id: int,
        target_hearth: int,
        emit: Callable[[str, str, dict[str, Any]], None],
        stop_requested: Callable[[], bool],
    ) -> dict[str, Any]:
        """进图刷怪开箱，直到炉温增量达到目标。"""

        try:
            entry = get_treasure_map_entry(area_id)
        except TreasureFarmError as exc:
            raise JobExecutionError(str(exc)) from exc

        area_label = entry.name
        run_id = uuid.uuid4().hex
        zone = _zone_from_endpoint(endpoint)
        client = self._farm_client_builder(endpoint)
        result_payload: dict[str, Any] | None = None
        failure_message: str | None = None
        try:
            try:
                emit(
                    "info",
                    (
                        "即将登录游戏服开始刷取：请保持本机仅有一个会话，"
                        "刷取期间不要打开手机/模拟器游戏客户端（否则会顶号 Kickout）"
                    ),
                    {},
                )
                progress = run_treasure_farm(
                    client,
                    area_id,
                    target_hearth,
                    emit=emit,
                    stop_requested=stop_requested,
                )
            except TreasureFarmKickout as exc:
                failure_message = str(exc)
                raise JobExecutionError(failure_message) from exc
            except TreasureFarmRejected as exc:
                failure_message = str(exc)
                raise JobExecutionError(failure_message) from exc
            except (TreasureFarmError, HarvestError, OSError, ValueError) as exc:
                failure_message = str(exc).strip() or (
                    f"{area_label} 刷取未完成，请检查网络与区服状态后重试"
                )
                raise JobExecutionError(failure_message) from exc

            cancelled = stop_requested() and not progress.completed
            farm = progress_payload(progress)
            summary = format_farm_summary(progress, cancelled=cancelled)
            result_payload = {
                "cancelled": cancelled,
                "farm": farm,
                "request": {
                    "area_id": progress.area_id,
                    "area_name": progress.area_name,
                    "target_hearth": target_hearth,
                },
                "summary": summary,
            }
            return result_payload
        finally:
            client.close()
            if result_payload is not None:
                self._persist_farm_result(
                    zone=zone,
                    run_id=run_id,
                    result=result_payload,
                )
            elif failure_message is not None:
                self._persist_farm_result(
                    zone=zone,
                    run_id=run_id,
                    result={
                        "cancelled": False,
                        "failed": True,
                        "farm": {
                            "area_id": area_id,
                            "area_name": area_label,
                            "target_hearth": target_hearth,
                            "hearth_gained": 0,
                            "hearth_item_name": item_name(HEARTH_ITEM_ID),
                        },
                        "request": {
                            "area_id": area_id,
                            "area_name": area_label,
                            "target_hearth": target_hearth,
                        },
                        "summary": failure_message,
                    },
                )

    def _persist_farm_result(
        self,
        *,
        zone: dict[str, str],
        run_id: str,
        result: dict[str, Any],
    ) -> None:
        if self._result_log_destination is None:
            return
        farm = result.get("farm") if isinstance(result.get("farm"), dict) else {}
        request = result.get("request") if isinstance(result.get("request"), dict) else {}
        cancelled = bool(result.get("cancelled"))
        failed = bool(result.get("failed"))
        if cancelled:
            log_outcome, level = "skipped", "warning"
        elif failed:
            log_outcome, level = "failure", "error"
        else:
            log_outcome, level = "success", "info"

        details = {
            "area_id": request.get("area_id") or farm.get("area_id"),
            "area_name": request.get("area_name")
            or farm.get("area_name")
            or treasure_area_name(request.get("area_id") or farm.get("area_id")),
            "target_hearth": request.get("target_hearth") or farm.get("target_hearth"),
            "hearth_gained": farm.get("hearth_gained"),
            "hearth_total": farm.get("hearth_total"),
            "hearth_item_name": farm.get("hearth_item_name") or item_name(HEARTH_ITEM_ID),
            "key_item_name": farm.get("key_item_name"),
            "keys_total": farm.get("keys_total"),
            "monsters_killed": farm.get("monsters_killed"),
            "settled_monsters": farm.get("settled_monsters"),
            "no_key_monsters": farm.get("no_key_monsters"),
            "missing_hearth_chests": farm.get("missing_hearth_chests"),
            "small_chests_opened": farm.get("small_chests_opened"),
            "big_chests_opened": farm.get("big_chests_opened"),
            "phase": farm.get("phase"),
            "phase_label": farm.get("phase_label"),
            "current_node_id": farm.get("current_node_id"),
            "current_node_name": farm.get("current_node_name"),
            "current_node_kind": farm.get("current_node_kind"),
            "last_reward_item_name": farm.get("last_reward_item_name"),
            "last_reward_delta": farm.get("last_reward_delta"),
            "last_transition": farm.get("last_transition"),
            "last_reset_reason": farm.get("last_reset_reason"),
            "completed": farm.get("completed"),
            "cancelled": cancelled,
            "failed": failed,
            "summary": result.get("summary"),
        }
        try:
            write_standard_log(
                event="treasure_area",
                operation="farm",
                zone=zone,
                details=details,
                destination=self._result_log_destination,
                run_id=run_id,
                outcome=log_outcome,
                level=level,
            )
        except LogPersistenceError:
            pass

    def snapshot(self, endpoint: GameEndpoint, selected_area_id: int) -> dict[str, Any]:
        client = self._live_client_builder(endpoint)
        try:
            status = client.get_status()
        finally:
            client.close()
        payload = self.payload(status, selected_area_id)
        if status.cleared_sweep_loot is not None:
            self._persist_cleared_sweep_result(
                zone=_zone_from_endpoint(endpoint),
                loot=status.cleared_sweep_loot,
            )
        return payload

    def _persist_cleared_sweep_result(
        self,
        *,
        zone: dict[str, str],
        loot: TreasureSweepLoot,
    ) -> None:
        """Record an automatic sweep-result acknowledgement without credentials."""

        if self._result_log_destination is None:
            return
        rewards = self.reward_payload(loot)
        area_id = loot.area_id if loot.area_id > 0 else None
        details = {
            "area_id": area_id,
            "area_name": treasure_area_name(area_id) if area_id else "",
            "rewards": rewards,
            "summary": format_loot_summary(rewards),
            "acknowledged_while_refreshing_status": True,
        }
        try:
            write_standard_log(
                event="treasure_area",
                operation="clear_result",
                zone=zone,
                details=details,
                destination=self._result_log_destination,
                outcome="success",
                level="info",
            )
        except LogPersistenceError:
            pass

    def run(
        self,
        endpoint: GameEndpoint,
        area_id: int,
        times: int,
        emit: Callable[[str, str, dict[str, Any]], None],
        stop_requested: Callable[[], bool],
    ) -> dict[str, Any]:
        area_label = treasure_area_name(area_id)
        run_id = uuid.uuid4().hex
        zone = _zone_from_endpoint(endpoint)
        client = self._live_client_builder(endpoint)
        result_payload: dict[str, Any] | None = None
        failure_message: str | None = None
        try:
            try:
                before = client.get_status()
            except (TreasureAreaError, HarvestError, OSError) as exc:
                failure_message = "读取聚宝之地状态失败，请检查网络和区服状态后重试"
                raise JobExecutionError(failure_message) from exc

            try:
                self._validate_sweep(before, area_id, times)
            except TreasureAreaError as exc:
                failure_message = str(exc)
                raise JobExecutionError(failure_message) from exc

            before_payload = self.payload(before, area_id)
            emit(
                "info",
                (
                    f"已读取聚宝之地状态：可扫荡地图 {len(before.area_ids)} 个，"
                    f"今日剩余 {before.sweep_remaining} 次"
                    f"（将扫荡 {area_label} × {times} 次）"
                ),
                {"treasure": before_payload},
            )
            if stop_requested():
                result_payload = {
                    "cancelled": True,
                    "treasure": before_payload,
                    "request": {
                        "area_id": area_id,
                        "area_name": area_label,
                        "times": times,
                    },
                    "rewards": [],
                    "summary": format_run_summary(
                        area_name=area_label,
                        times=0,
                        remaining=before.sweep_remaining,
                        rewards=[],
                        cancelled=True,
                    ),
                }
                return result_payload

            try:
                response = client.sweep(area_id, times)
            except TreasureAreaRejected as exc:
                failure_message = str(exc)
                raise JobExecutionError(failure_message) from exc
            except (TreasureAreaError, HarvestError, OSError) as exc:
                failure_message = f"{area_label} 扫荡未完成，请检查游戏服连接后重试"
                raise JobExecutionError(failure_message) from exc

            after = response.status
            assert after is not None
            rewards = self.reward_payload(after.sweep_loot)
            payload = self.payload(after, area_id)
            summary = format_run_summary(
                area_name=area_label,
                times=times,
                remaining=after.sweep_remaining,
                rewards=rewards,
            )
            if rewards:
                success_message = (
                    f"{area_label} 已扫荡 {times} 次，"
                    f"今日剩余 {after.sweep_remaining} 次 · {format_loot_summary(rewards)}"
                )
            else:
                success_message = (
                    f"{area_label} 已扫荡 {times} 次，"
                    f"今日剩余 {after.sweep_remaining} 次"
                    "（服务端未返回物品明细）"
                )
            emit(
                "success",
                success_message,
                {"treasure": payload, "rewards": rewards, "summary": summary},
            )
            result_payload = {
                "cancelled": False,
                "treasure": payload,
                "request": {
                    "area_id": area_id,
                    "area_name": area_label,
                    "times": times,
                },
                "rewards": rewards,
                "summary": summary,
            }
            return result_payload
        finally:
            client.close()
            if result_payload is not None:
                self._persist_run_result(
                    zone=zone,
                    run_id=run_id,
                    result=result_payload,
                )
            elif failure_message is not None:
                self._persist_run_result(
                    zone=zone,
                    run_id=run_id,
                    result={
                        "cancelled": False,
                        "failed": True,
                        "treasure": {
                            "areas": [],
                            "sweep": {},
                        },
                        "request": {
                            "area_id": area_id,
                            "area_name": area_label,
                            "times": times,
                        },
                        "rewards": [],
                        "summary": failure_message,
                    },
                )

    def _persist_run_result(
        self,
        *,
        zone: dict[str, str],
        run_id: str,
        result: dict[str, Any],
    ) -> None:
        """将脱敏后的聚宝之地作业摘要写入托管 JSONL（project-logging 标准）。"""

        if self._result_log_destination is None:
            return
        treasure = result.get("treasure")
        if not isinstance(treasure, dict):
            return
        cancelled = bool(result.get("cancelled"))
        failed = bool(result.get("failed"))
        if cancelled:
            log_outcome, level = "skipped", "warning"
        elif failed:
            log_outcome, level = "failure", "error"
        else:
            log_outcome, level = "success", "info"

        request = result.get("request") if isinstance(result.get("request"), dict) else {}
        rewards = result.get("rewards") if isinstance(result.get("rewards"), list) else []
        redacted_rewards: list[dict[str, Any]] = []
        for reward in rewards:
            if not isinstance(reward, dict):
                continue
            item_id = reward.get("id")
            redacted_rewards.append(
                {
                    "id": item_id,
                    "name": reward.get("name") or item_name(item_id),
                    "delta": reward.get("delta"),
                    "total": reward.get("total"),
                }
            )

        sweep = treasure.get("sweep") if isinstance(treasure.get("sweep"), dict) else {}
        details = {
            "area_id": request.get("area_id"),
            "area_name": request.get("area_name")
            or treasure_area_name(request.get("area_id")),
            "times": request.get("times"),
            "sweep": {
                "used": sweep.get("used"),
                "limit": sweep.get("limit"),
                "available": sweep.get("available"),
            },
            "rewards": redacted_rewards,
            "cancelled": cancelled,
            "failed": failed,
            "summary": result.get("summary")
            or format_run_summary(
                area_name=str(request.get("area_name") or "聚宝之地"),
                times=int(request.get("times") or 0),
                remaining=int(sweep.get("available") or 0),
                rewards=redacted_rewards,
                cancelled=cancelled,
            ),
        }
        try:
            write_standard_log(
                event="treasure_area",
                operation="sweep",
                zone=zone,
                details=details,
                destination=self._result_log_destination,
                run_id=run_id,
                outcome=log_outcome,
                level=level,
            )
        except LogPersistenceError:
            # 作业结果已返回界面；日志失败不阻断主流程。
            pass

    @staticmethod
    def reward_payload(loot: TreasureSweepLoot | None) -> list[dict[str, Any]]:
        if loot is None:
            return []
        return aggregate_loot_entries(loot.items)

    @staticmethod
    def payload(status: TreasureAreaStatus, selected_area_id: int) -> dict[str, Any]:
        remaining = status.sweep_remaining
        cleared_loot = status.cleared_sweep_loot
        cleared_area_id = (
            cleared_loot.area_id
            if cleared_loot is not None and cleared_loot.area_id > 0
            else None
        )
        cleared_rewards = (
            TreasureService.reward_payload(cleared_loot)
            if cleared_loot is not None
            else []
        )
        return {
            "areas": [
                {
                    "id": area_id,
                    "name": treasure_area_name(area_id),
                    "selected": area_id == selected_area_id,
                }
                for area_id in status.area_ids
            ],
            "sweep": {
                "used": status.swept_today,
                "limit": status.daily_sweep_limit,
                "available": remaining,
                "request_limit": min(remaining, MAX_SWEEP_TIMES_PER_REQUEST),
            },
            "cleared_result": {
                "acknowledged": cleared_loot is not None,
                "area_id": cleared_area_id,
                "area_name": (
                    treasure_area_name(cleared_area_id)
                    if cleared_area_id is not None
                    else ""
                ),
                "rewards": cleared_rewards,
                "summary": (
                    format_loot_summary(cleared_rewards)
                    if cleared_loot is not None
                    else ""
                ),
            },
        }

    @staticmethod
    def _validate_sweep(
        status: TreasureAreaStatus, area_id: int, times: int
    ) -> None:
        if not 1 <= times <= MAX_SWEEP_TIMES_PER_REQUEST:
            raise TreasureAreaError(
                f"聚宝之地单次扫荡次数必须是 1 到 {MAX_SWEEP_TIMES_PER_REQUEST} 之间的整数"
            )
        if not status.can_sweep(area_id):
            raise TreasureAreaError(
                f"所选地图 {treasure_area_name(area_id)} 当前不可扫荡"
            )
        if status.sweep_remaining <= 0:
            raise TreasureAreaError("今日聚宝之地扫荡次数已用完")
        if times > status.sweep_remaining:
            raise TreasureAreaError(
                f"今日聚宝之地仅剩 {status.sweep_remaining} 次可扫荡"
            )
