from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable, Mapping

from dungeon_sweep import (
    DungeonDrawRejected,
    DungeonDrawResponse,
    DungeonLampClaimRejected,
    DungeonLampClaimResponse,
    DungeonStatus,
    DungeonSweepClient,
    DungeonSweepError,
    DungeonSweepRejected,
    DUNGEON_LAMP_ITEM_ID,
)
from game_session import GameSessionManager
from harvest_fief import GameEndpoint, HarvestError
from id_descriptions import drop_name, dungeon_name, reward_name
from project_paths import NATIVE_APP_ROOT, UI_APP_ROOT

from ..job_manager import JobExecutionError


DEFAULT_DUNGEON_DATA_PATH = NATIVE_APP_ROOT / "decrypted-data" / "dungeon.json"
DEFAULT_ITEM_DATA_PATH = NATIVE_APP_ROOT / "decrypted-data" / "item.json"
DEFAULT_ITEM_ID_MAP_PATH = UI_APP_ROOT / "item_id_map.json"
DEFAULT_REWARD_BOX_PATH = NATIVE_APP_ROOT / "decrypted-data" / "rewardbox.json"

PROP_KIND_LABELS = {
    1: "物品",
    2: "奖励箱",
    3: "装备",
    4: "秘宝",
    5: "英雄",
    6: "律文",
    7: "活动装备",
}
PROP_KIND_ITEM = 1
PROP_KIND_REWARD_BOX = 2

LiveClientBuilder = Callable[[GameEndpoint], DungeonSweepClient]


class DungeonSweepUnavailable(JobExecutionError):
    """A known server-side sweep condition that an automatic run may skip."""

    def __init__(self, dungeon_name: str, ret: int, reason: str) -> None:
        self.dungeon_name = dungeon_name
        self.ret = ret
        self.reason = reason
        super().__init__(
            f"{dungeon_name} 扫荡被服务端拒绝：{reason}（ret={ret}）"
        )


def _load_dungeon_names(path: Path) -> dict[int, str]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}
    if not isinstance(payload, list):
        return {}
    names: dict[int, str] = {}
    for row in payload:
        if not isinstance(row, Mapping):
            continue
        dungeon_id = row.get("id")
        name = row.get("name")
        if (
            isinstance(dungeon_id, int)
            and not isinstance(dungeon_id, bool)
            and dungeon_id > 0
            and isinstance(name, str)
            and name.strip()
        ):
            names[dungeon_id] = name.strip()
    return names


def _load_dungeon_limits(path: Path) -> dict[int, int]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}
    if not isinstance(payload, list):
        return {}
    limits: dict[int, int] = {}
    for row in payload:
        if not isinstance(row, Mapping):
            continue
        dungeon_id = row.get("id")
        limit = row.get("limit")
        if (
            isinstance(dungeon_id, int)
            and not isinstance(dungeon_id, bool)
            and dungeon_id > 0
            and isinstance(limit, int)
            and not isinstance(limit, bool)
            and limit >= 0
        ):
            limits[dungeon_id] = limit
    return limits


def _load_item_details(path: Path) -> dict[int, dict[str, str]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}
    if isinstance(payload, Mapping):
        rows = payload.items()
    elif isinstance(payload, list):
        rows = (
            (row.get("id"), row)
            for row in payload
            if isinstance(row, Mapping)
        )
    else:
        return {}
    details: dict[int, dict[str, str]] = {}
    for raw_id, row in rows:
        try:
            item_id = int(raw_id)
        except (TypeError, ValueError):
            continue
        if item_id <= 0 or not isinstance(row, Mapping):
            continue
        name = row.get("name")
        if not isinstance(name, str) or not name.strip():
            continue
        entry = {"name": name.strip()}
        category = row.get("type_name")
        if isinstance(category, str) and category.strip():
            entry["category"] = category.strip()
        description = row.get("text")
        if isinstance(description, str) and description.strip():
            entry["description"] = description.strip()
        details[item_id] = entry
    return details


def _load_reward_box_details(path: Path) -> dict[int, dict[str, str]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}
    if not isinstance(payload, list):
        return {}
    details: dict[int, dict[str, str]] = {}
    for row in payload:
        if not isinstance(row, Mapping):
            continue
        reward_id = row.get("id")
        name = row.get("name")
        if (
            not isinstance(reward_id, int)
            or isinstance(reward_id, bool)
            or reward_id <= 0
            or not isinstance(name, str)
            or not name.strip()
        ):
            continue
        entry = {"name": name.strip()}
        description = row.get("text")
        if isinstance(description, str) and description.strip():
            entry["description"] = description.strip()
        details[reward_id] = entry
    return details


class DungeonService:
    """地下城状态投影、扫荡与宝库全部抽取编排。"""

    def __init__(
        self,
        *,
        live_client_builder: LiveClientBuilder | None = None,
        dungeon_names: Mapping[int, str] | None = None,
        reward_names: Mapping[int, str] | None = None,
        game_timeout: float = 15.0,
        session_manager: GameSessionManager | None = None,
    ) -> None:
        self._session_manager = session_manager
        self._live_client_builder = live_client_builder or (
            lambda endpoint: DungeonSweepClient(
                endpoint,
                game_timeout,
                session=(
                    self._session_manager.session_for(endpoint)
                    if self._session_manager is not None
                    else None
                ),
            )
        )
        self._dungeon_names = dict(dungeon_names or _load_dungeon_names(DEFAULT_DUNGEON_DATA_PATH))
        self._dungeon_limits = _load_dungeon_limits(DEFAULT_DUNGEON_DATA_PATH)
        self._reward_names = dict(reward_names or {})
        self._item_details = _load_item_details(DEFAULT_ITEM_DATA_PATH)
        self._item_details.update(_load_item_details(DEFAULT_ITEM_ID_MAP_PATH))
        self._reward_box_details = _load_reward_box_details(DEFAULT_REWARD_BOX_PATH)

    def snapshot(
        self, endpoint: GameEndpoint, selected_dungeon_id: int
    ) -> dict[str, Any]:
        client = self._live_client_builder(endpoint)
        try:
            status = client.get_status()
        finally:
            client.close()
        return self.payload(status, selected_dungeon_id)

    def claim_daily_lamp(self, endpoint: GameEndpoint) -> dict[str, Any]:
        """Claim the direct-shop dungeon lamp without starting a sweep."""

        client = self._live_client_builder(endpoint)
        try:
            return self._claim_daily_lamp(client)
        finally:
            client.close()

    def run(
        self,
        endpoint: GameEndpoint,
        dungeon_id: int,
        emit: Callable[[str, str, dict[str, Any]], None],
        stop_requested: Callable[[], bool],
    ) -> dict[str, Any]:
        client = self._live_client_builder(endpoint)
        try:
            try:
                before = client.get_status()
            except (DungeonSweepError, HarvestError, OSError) as exc:
                raise JobExecutionError("读取地下城状态失败，请刷新后重试") from exc
            try:
                self._validate_sweep(before, dungeon_id)
            except DungeonSweepError as exc:
                raise JobExecutionError(str(exc)) from exc
            dungeon = self.payload(before, dungeon_id)
            emit(
                "info",
                f"已读取 {self.dungeon_name(dungeon_id)}，历史最高分 {before.best_score_for(dungeon_id)}",
                {"dungeon": dungeon},
            )
            if stop_requested():
                return {"cancelled": True, "dungeon": dungeon, "rewards": []}

            try:
                client.sweep(dungeon_id)
            except DungeonSweepRejected as exc:
                if exc.reason is not None:
                    raise DungeonSweepUnavailable(
                        self.dungeon_name(dungeon_id), exc.ret, exc.reason
                    ) from exc
                raise JobExecutionError(
                    f"{self.dungeon_name(dungeon_id)} 扫荡被服务端拒绝（ret={exc.ret}）"
                ) from exc
            except (DungeonSweepError, HarvestError, OSError) as exc:
                raise JobExecutionError(
                    f"{self.dungeon_name(dungeon_id)} 扫荡未完成，请检查游戏服连接后重试"
                ) from exc
            emit(
                "info",
                f"{self.dungeon_name(dungeon_id)} 扫荡完成，开始全部抽取",
                {"dungeon": dungeon, "phase": "drawing"},
            )
            if stop_requested():
                return {
                    "cancelled": True,
                    "dungeon": dungeon,
                    "rewards": [],
                    "sweep_completed": True,
                }

            try:
                draw = client.draw_all(dungeon_id)
            except DungeonDrawRejected as exc:
                raise JobExecutionError(
                    f"{self.dungeon_name(dungeon_id)} 全部抽取被服务端拒绝（ret={exc.ret}）"
                ) from exc
            except (DungeonSweepError, HarvestError, OSError) as exc:
                raise JobExecutionError(
                    f"{self.dungeon_name(dungeon_id)} 全部抽取未完成，请检查游戏服连接后重试"
                ) from exc
            rewards = self.reward_payload(draw)
            dungeon = self.payload(
                replace(
                    before,
                    draw_times=draw.draw_times,
                    total_draw_times=draw.total_draw_times,
                ),
                dungeon_id,
            )
            draw_payload = {
                "all_drawn": draw.all_drawn,
                "draw_times": draw.draw_times,
                "total_draw_times": draw.total_draw_times,
                "count": len(rewards),
                "reward_notice_received": draw.reward_notice_received,
            }
            if rewards:
                reward_label = (
                    "服务端结算奖励"
                    if rewards[0]["source"] == "item_change"
                    else "服务端抽取结果"
                )
                completion_message = (
                    f"{self.dungeon_name(dungeon_id)} 全部抽取完成，"
                    f"获得 {len(rewards)} 项{reward_label}："
                    f"{self.reward_summary(rewards)}"
                )
            else:
                completion_message = (
                    f"{self.dungeon_name(dungeon_id)} 全部抽取完成，"
                    "服务端未返回可展示的奖励明细"
                )
            emit(
                "success",
                completion_message,
                {
                    "dungeon": dungeon,
                    "rewards": rewards,
                    "draw": draw_payload,
                    "phase": "completed",
                },
            )
            return {
                "cancelled": False,
                "dungeon": dungeon,
                "rewards": rewards,
                "draw": draw_payload,
                "sweep_completed": True,
            }
        finally:
            client.close()

    def run_daily(
        self,
        endpoint: GameEndpoint,
        dungeon_id: int,
        emit: Callable[[str, str, dict[str, Any]], None],
        stop_requested: Callable[[], bool],
    ) -> dict[str, Any]:
        """Claim the daily lamp, sweep every remaining attempt, then draw once."""

        client = self._live_client_builder(endpoint)
        try:
            if stop_requested():
                return {
                    "cancelled": True,
                    "dungeon": {"id": dungeon_id, "name": self.dungeon_name(dungeon_id)},
                    "rewards": [],
                    "lamp_claim": self._lamp_payload(None),
                    "sweeps_completed": 0,
                    "sweeps_requested": 0,
                    "sweep_limit": self._dungeon_limits.get(dungeon_id, 3),
                }

            lamp_claim = self._claim_daily_lamp(client)
            try:
                before = client.get_status()
            except (DungeonSweepError, HarvestError, OSError) as exc:
                raise JobExecutionError("读取地下城状态失败，请刷新后重试") from exc
            try:
                self._validate_sweep(before, dungeon_id)
            except DungeonSweepError as exc:
                # A daily lamp can still be claimed when all sweep attempts are used.
                if not before.can_sweep(dungeon_id) or self._has_sweep_attempt_remaining(
                    before, dungeon_id
                ):
                    raise JobExecutionError(str(exc)) from exc
                return {
                    "cancelled": False,
                    "dungeon": self.payload(before, dungeon_id),
                    "rewards": [],
                    "lamp_claim": lamp_claim,
                    "sweeps_completed": 0,
                    "sweeps_requested": 0,
                    "sweep_limit": self._dungeon_limits.get(dungeon_id, 3),
                    "sweep_completed": False,
                }

            dungeon = self.payload(before, dungeon_id)
            sweep_limit = self._dungeon_limits.get(dungeon_id, 3)
            used_before = before.challenge_times_for(dungeon_id)
            requested = max(sweep_limit - used_before, 0)
            emit(
                "info",
                f"已读取 {self.dungeon_name(dungeon_id)}，今日已扫荡 {used_before}/{sweep_limit} 次",
                {"dungeon": dungeon, "lamp_claim": lamp_claim},
            )
            if requested <= 0:
                return {
                    "cancelled": False,
                    "dungeon": dungeon,
                    "rewards": [],
                    "lamp_claim": lamp_claim,
                    "sweeps_completed": 0,
                    "sweeps_requested": 0,
                    "sweep_limit": sweep_limit,
                    "sweep_completed": False,
                }

            sweeps_completed = 0
            for _ in range(requested):
                if stop_requested():
                    break
                try:
                    client.sweep(dungeon_id)
                except DungeonSweepRejected as exc:
                    if sweeps_completed > 0 and exc.ret in {3, 5}:
                        emit(
                            "warning",
                            f"服务端已将 {self.dungeon_name(dungeon_id)} 次数判定为已用尽",
                            {"dungeon": dungeon, "ret": exc.ret},
                        )
                        break
                    if exc.reason is not None:
                        raise DungeonSweepUnavailable(
                            self.dungeon_name(dungeon_id), exc.ret, exc.reason
                        ) from exc
                    raise JobExecutionError(
                        f"{self.dungeon_name(dungeon_id)} 扫荡被服务端拒绝（ret={exc.ret}）"
                    ) from exc
                except (DungeonSweepError, HarvestError, OSError) as exc:
                    raise JobExecutionError(
                        f"{self.dungeon_name(dungeon_id)} 扫荡未完成，请检查游戏服连接后重试"
                    ) from exc
                sweeps_completed += 1
                emit(
                    "info",
                    f"{self.dungeon_name(dungeon_id)} 扫荡完成 {sweeps_completed}/{requested}",
                    {
                        "dungeon": dungeon,
                        "phase": "sweeping",
                        "sweeps_completed": sweeps_completed,
                        "sweeps_requested": requested,
                    },
                )

            if stop_requested() or sweeps_completed == 0:
                return {
                    "cancelled": stop_requested(),
                    "dungeon": dungeon,
                    "rewards": [],
                    "lamp_claim": lamp_claim,
                    "sweeps_completed": sweeps_completed,
                    "sweeps_requested": requested,
                    "sweep_limit": sweep_limit,
                    "sweep_completed": sweeps_completed > 0,
                }

            emit(
                "info",
                f"{self.dungeon_name(dungeon_id)} 已完成 {sweeps_completed} 次扫荡，开始全部抽取",
                {
                    "dungeon": dungeon,
                    "phase": "drawing",
                    "sweeps_completed": sweeps_completed,
                },
            )
            try:
                draw = client.draw_all(dungeon_id)
            except DungeonDrawRejected as exc:
                raise JobExecutionError(
                    f"{self.dungeon_name(dungeon_id)} 全部抽取被服务端拒绝（ret={exc.ret}）"
                ) from exc
            except (DungeonSweepError, HarvestError, OSError) as exc:
                raise JobExecutionError(
                    f"{self.dungeon_name(dungeon_id)} 全部抽取未完成，请检查游戏服连接后重试"
                ) from exc

            rewards = self.reward_payload(draw)
            challenge_times = dict(before.challenge_times)
            challenge_times[dungeon_id] = used_before + sweeps_completed
            dungeon = self.payload(
                replace(
                    before,
                    challenge_times=challenge_times,
                    draw_times=draw.draw_times,
                    total_draw_times=draw.total_draw_times,
                ),
                dungeon_id,
            )
            draw_payload = {
                "all_drawn": draw.all_drawn,
                "draw_times": draw.draw_times,
                "total_draw_times": draw.total_draw_times,
                "count": len(rewards),
                "reward_notice_received": draw.reward_notice_received,
            }
            if rewards:
                reward_label = (
                    "服务端结算奖励"
                    if rewards[0]["source"] == "item_change"
                    else "服务端抽取结果"
                )
                completion_message = (
                    f"{self.dungeon_name(dungeon_id)} 全部抽取完成，"
                    f"获得 {len(rewards)} 项{reward_label}："
                    f"{self.reward_summary(rewards)}"
                )
            else:
                completion_message = (
                    f"{self.dungeon_name(dungeon_id)} 全部抽取完成，"
                    "服务端未返回可展示的奖励明细"
                )
            emit(
                "success",
                completion_message,
                {
                    "dungeon": dungeon,
                    "rewards": rewards,
                    "draw": draw_payload,
                    "phase": "completed",
                    "lamp_claim": lamp_claim,
                    "sweeps_completed": sweeps_completed,
                },
            )
            return {
                "cancelled": False,
                "dungeon": dungeon,
                "rewards": rewards,
                "draw": draw_payload,
                "sweep_completed": True,
                "lamp_claim": lamp_claim,
                "sweeps_completed": sweeps_completed,
                "sweeps_requested": requested,
                "sweep_limit": sweep_limit,
            }
        finally:
            client.close()

    def _claim_daily_lamp(self, client: DungeonSweepClient) -> dict[str, Any]:
        claim = getattr(client, "claim_daily_lamp", None)
        if not callable(claim):
            return self._lamp_payload(None)
        try:
            response = claim()
        except DungeonLampClaimRejected as exc:
            raise JobExecutionError(str(exc)) from exc
        except (DungeonSweepError, HarvestError, OSError) as exc:
            raise JobExecutionError("领取地下城每日永焰之灯失败，请检查游戏服连接后重试") from exc
        return self._lamp_payload(response)

    @staticmethod
    def _lamp_payload(response: Any) -> dict[str, Any]:
        if response is None:
            quantity = 0
            ret = 0
        elif isinstance(response, Mapping):
            quantity = int(response.get("claimed_qty", response.get("quantity", 0)) or 0)
            ret = int(response.get("ret", 0) or 0)
        else:
            quantity = int(getattr(response, "claimed_qty", 0) or 0)
            ret = int(getattr(response, "ret", 0) or 0)
        return {
            "item_id": DUNGEON_LAMP_ITEM_ID,
            "item_name": "永焰之灯",
            "claimed": quantity > 0,
            "quantity": quantity,
            "ret": ret,
        }

    def payload(
        self, status: DungeonStatus, selected_dungeon_id: int
    ) -> dict[str, Any]:
        sweepable_ids = tuple(
            dungeon_id
            for dungeon_id in status.sweepable_ids
            if self._has_sweep_attempt_remaining(status, dungeon_id)
        )
        return {
            "dungeons": [
                {
                    "id": dungeon_id,
                    "name": self.dungeon_name(dungeon_id),
                    "highest_score": status.best_score_for(dungeon_id),
                    "selected": dungeon_id == selected_dungeon_id,
                }
                for dungeon_id in sweepable_ids
            ],
            "current_dungeon_id": status.current_dungeon_id,
            "current_dungeon_name": self.dungeon_name(status.current_dungeon_id),
            "draw": {
                "used": status.draw_times,
                "total": status.total_draw_times,
                "available": status.pending_draws,
            },
        }

    def dungeon_name(self, dungeon_id: int) -> str:
        return self._dungeon_names.get(dungeon_id, dungeon_name(dungeon_id))

    def _has_sweep_attempt_remaining(
        self, status: DungeonStatus, dungeon_id: int
    ) -> bool:
        limit = self._dungeon_limits.get(dungeon_id)
        return limit is None or status.challenge_times_for(dungeon_id) < limit

    def reward_payload(self, draw: DungeonDrawResponse) -> list[dict[str, Any]]:
        if draw.reward_notice_received:
            rewards = self._settled_reward_payload(draw)
            if rewards:
                return rewards

        rewards: list[dict[str, Any]] = []
        for index, reward_id in enumerate(draw.reward_ids):
            payload = self._drop_reward_payload(reward_id)
            if index < len(draw.probabilities):
                payload["probability_code"] = draw.probabilities[index]
            rewards.append(payload)
        return rewards

    @staticmethod
    def reward_summary(rewards: list[dict[str, Any]]) -> str:
        return "、".join(
            f"{reward['name']} × {reward['quantity']}" for reward in rewards
        )

    def _settled_reward_payload(
        self, draw: DungeonDrawResponse
    ) -> list[dict[str, Any]]:
        rewards: list[dict[str, Any]] = []
        item_quantities_from_props: dict[int, int] = {}
        for prop in draw.reward_props:
            if prop.item_id <= 0 or prop.amount <= 0:
                continue
            if prop.kind == PROP_KIND_ITEM:
                item_quantities_from_props[prop.item_id] = (
                    item_quantities_from_props.get(prop.item_id, 0) + prop.amount
                )
            rewards.append(
                self._reward_entry(
                    prop.kind,
                    prop.item_id,
                    prop.amount,
                    source="item_change",
                )
            )

        for item_change in draw.item_changes:
            if item_change.item_id <= 0 or item_change.delta <= 0:
                continue
            covered_quantity = min(
                item_change.delta,
                item_quantities_from_props.get(item_change.item_id, 0),
            )
            item_quantities_from_props[item_change.item_id] = (
                item_quantities_from_props.get(item_change.item_id, 0)
                - covered_quantity
            )
            quantity = item_change.delta - covered_quantity
            if quantity <= 0:
                continue
            rewards.append(
                self._reward_entry(
                    PROP_KIND_ITEM,
                    item_change.item_id,
                    quantity,
                    source="item_change",
                )
            )
        return rewards

    def _drop_reward_payload(self, reward_id: int) -> dict[str, Any]:
        detail = self._item_details.get(reward_id) or self._reward_box_details.get(
            reward_id, {}
        )
        payload: dict[str, Any] = {
            "id": reward_id,
            "name": self._reward_names.get(reward_id)
            or detail.get("name")
            or drop_name(reward_id),
            "kind_label": "抽取掉落",
            "quantity": 1,
            "source": "draw_response",
        }
        for key in ("category", "description"):
            if key in detail:
                payload[key] = detail[key]
        return payload

    def _reward_entry(
        self, kind: int, reward_id: int, quantity: int, *, source: str
    ) -> dict[str, Any]:
        detail = self._details_for(kind, reward_id)
        kind_label = PROP_KIND_LABELS.get(kind, f"类型 {kind}")
        payload: dict[str, Any] = {
            "id": reward_id,
            "name": self._reward_names.get(reward_id)
            or detail.get("name")
            or reward_name(kind, reward_id),
            "kind": kind,
            "kind_label": kind_label,
            "quantity": quantity,
            "source": source,
        }
        for key in ("category", "description"):
            if key in detail:
                payload[key] = detail[key]
        return payload

    def _details_for(self, kind: int, reward_id: int) -> Mapping[str, str]:
        if kind == PROP_KIND_ITEM:
            return self._item_details.get(reward_id, {})
        if kind == PROP_KIND_REWARD_BOX:
            return self._reward_box_details.get(reward_id) or self._item_details.get(
                reward_id, {}
            )
        return self._item_details.get(reward_id) or self._reward_box_details.get(
            reward_id, {}
        )

    def _validate_sweep(self, status: DungeonStatus, dungeon_id: int) -> None:
        if not status.can_sweep(dungeon_id):
            raise DungeonSweepError("所选地下城当前不可扫荡")
        if not self._has_sweep_attempt_remaining(status, dungeon_id):
            raise DungeonSweepError("所选地下城今日扫荡次数已用尽")
