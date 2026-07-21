from __future__ import annotations

from typing import Any, Callable

from harvest_fief import GameEndpoint
from id_descriptions import treasure_area_name
from treasure_area import (
    MAX_SWEEP_TIMES_PER_REQUEST,
    TreasureAreaClient,
    TreasureAreaError,
    TreasureAreaStatus,
)


LiveClientBuilder = Callable[[GameEndpoint], TreasureAreaClient]


class TreasureService:
    """聚宝之地状态投影与单次扫荡编排。"""

    def __init__(
        self,
        *,
        live_client_builder: LiveClientBuilder | None = None,
        game_timeout: float = 15.0,
    ) -> None:
        self._live_client_builder = live_client_builder or (
            lambda endpoint: TreasureAreaClient(endpoint, game_timeout)
        )

    def snapshot(self, endpoint: GameEndpoint, selected_area_id: int) -> dict[str, Any]:
        client = self._live_client_builder(endpoint)
        try:
            status = client.get_status()
        finally:
            client.close()
        return self.payload(status, selected_area_id)

    def run(
        self,
        endpoint: GameEndpoint,
        area_id: int,
        times: int,
        emit: Callable[[str, str, dict[str, Any]], None],
        stop_requested: Callable[[], bool],
    ) -> dict[str, Any]:
        client = self._live_client_builder(endpoint)
        try:
            before = client.get_status()
            self._validate_sweep(before, area_id, times)
            emit(
                "info",
                f"已读取聚宝之地状态：今日可扫荡 {before.sweep_remaining} 次",
                {"treasure": self.payload(before, area_id)},
            )
            if stop_requested():
                return {"cancelled": True, "treasure": self.payload(before, area_id)}

            response = client.sweep(area_id, times)
            after = response.status
            assert after is not None
            payload = self.payload(after, area_id)
            emit(
                "success",
                f"聚宝之地 {treasure_area_name(area_id)} 已扫荡 {times} 次",
                {"treasure": payload},
            )
            return {
                "cancelled": False,
                "treasure": payload,
                "request": {
                    "area_id": area_id,
                    "area_name": treasure_area_name(area_id),
                    "times": times,
                },
            }
        finally:
            client.close()

    @staticmethod
    def payload(status: TreasureAreaStatus, selected_area_id: int) -> dict[str, Any]:
        remaining = status.sweep_remaining
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
            raise TreasureAreaError("所选聚宝之地地图当前不可扫荡")
        if status.sweep_remaining <= 0:
            raise TreasureAreaError("今日聚宝之地扫荡次数已用完")
        if times > status.sweep_remaining:
            raise TreasureAreaError(
                f"今日聚宝之地仅剩 {status.sweep_remaining} 次可扫荡"
            )
