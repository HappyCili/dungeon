from __future__ import annotations

from typing import Any, Callable, Protocol

from dragon_arena import (
    MERCY_CHOICE_ID,
    DragonArenaChallengeResult,
    DragonArenaChoiceResult,
    DragonArenaClient,
    DragonArenaInfo,
    DragonArenaRoundResult,
)
from harvest_fief import GameEndpoint, HarvestError
from id_descriptions import item_name

from ..job_manager import JobExecutionError


DRAGON_COIN_ITEM_ID = 440
OUTCOME_CHOICE_IDS = {"mercy": MERCY_CHOICE_ID, "execute": 1}
OUTCOME_LABELS = {"mercy": "仁慈", "execute": "处决"}


class ArenaServiceError(RuntimeError):
    """龙痕竞技场状态不能安全投影到界面时的错误。"""


class ArenaClient(Protocol):
    def __enter__(self) -> "ArenaClient": ...

    def close(self) -> None: ...

    def resume_pending_battle(
        self, *, mercy_choice_id: int
    ) -> DragonArenaRoundResult | None: ...

    def get_info(self) -> DragonArenaInfo: ...

    def match(self) -> Any: ...

    def run_round(
        self, index: int, *, mercy_choice_id: int
    ) -> DragonArenaRoundResult: ...


LiveClientBuilder = Callable[[GameEndpoint, Callable[[str], None]], ArenaClient]


class ArenaService:
    """将龙痕竞技场 WebSocket 客户端投影为 UI 状态和可取消作业。"""

    def __init__(
        self,
        *,
        live_client_builder: LiveClientBuilder | None = None,
        game_timeout: float = 15.0,
    ) -> None:
        self._live_client_builder = live_client_builder or (
            lambda endpoint, log: DragonArenaClient(
                endpoint,
                game_timeout,
                log=log,
                log_server_messages=False,
                websocket_log=None,
                business_log=None,
            )
        )

    def snapshot(self, endpoint: GameEndpoint) -> dict[str, Any]:
        client: ArenaClient | None = None
        try:
            client = self._live_client_builder(endpoint, lambda _message: None)
            self._enter(client)
            return self.payload(client.get_info())
        except (HarvestError, OSError, ValueError) as exc:
            raise ArenaServiceError("读取龙痕竞技场状态失败") from exc
        finally:
            self._close(client)

    def run(
        self,
        endpoint: GameEndpoint,
        rounds: int,
        outcome: str,
        refresh_on_exhaustion: bool,
        emit: Callable[[str, str, dict[str, Any]], None],
        stop_requested: Callable[[], bool],
    ) -> dict[str, Any]:
        choice_id = OUTCOME_CHOICE_IDS.get(outcome)
        if choice_id is None:
            raise JobExecutionError("龙痕竞技场胜利抉择无效")

        stats = self._initial_stats(rounds, outcome, refresh_on_exhaustion)

        def client_log(message: str) -> None:
            emit("info", message, {"arena": dict(stats)})

        client: ArenaClient | None = None
        try:
            client = self._live_client_builder(endpoint, client_log)
            stats["stage"] = "连接游戏服"
            emit("info", "正在连接龙痕竞技场", {"arena": dict(stats)})
            self._enter(client)
            if stop_requested():
                return {"cancelled": True, "arena": stats}

            resumed = client.resume_pending_battle(mercy_choice_id=choice_id)
            if resumed is not None:
                self._apply_round_result(stats, resumed, count_round=False)
                stats["stage"] = "已恢复遗留战斗"
                emit("info", stats["last_result"], {"arena": dict(stats)})

            attempted: set[int] = set()
            refreshed_without_progress = False
            while stats["completed_rounds"] < rounds:
                if stop_requested():
                    return {"cancelled": True, "arena": stats}

                stats["stage"] = "读取竞技场状态"
                emit("info", "正在读取龙痕竞技场状态", {"arena": dict(stats)})
                info = client.get_info()
                self._apply_info(stats, info)
                candidates = [
                    index
                    for index, opponent in enumerate(info.opponents, start=1)
                    if not opponent.challenged and index not in attempted
                ]
                if not candidates:
                    if refresh_on_exhaustion and not refreshed_without_progress:
                        stats["stage"] = "寻找对手"
                        emit("info", "当前对手已耗尽，正在寻找新对手", {"arena": dict(stats)})
                        matched = client.match()
                        if getattr(matched, "ret", 0) == 0:
                            attempted.clear()
                            refreshed_without_progress = True
                            candidate_count = len(getattr(matched, "opponents", ()))
                            emit(
                                "info",
                                f"已收到 {candidate_count} 位竞技场候选对手",
                                {"arena": dict(stats)},
                            )
                            continue
                        stats["stage"] = "寻找对手失败"
                        stats["last_result"] = "服务端未返回可挑战对手"
                        emit("warning", stats["last_result"], {"arena": dict(stats)})
                    else:
                        stats["stage"] = "对手已耗尽"
                        stats["last_result"] = "当前没有可挑战对手"
                        emit("info", stats["last_result"], {"arena": dict(stats)})
                    break

                index = candidates[0]
                stats["stage"] = f"挑战第 {index} 位对手"
                emit("info", stats["stage"], {"arena": dict(stats)})
                result = client.run_round(index, mercy_choice_id=choice_id)
                self._apply_round_result(stats, result)
                attempted.add(index)
                if result.battle is not None and result.battle.win:
                    attempted.clear()
                refreshed_without_progress = False
                level = "success" if result.battle is not None and result.battle.win else "warning"
                emit(level, stats["last_result"], {"arena": dict(stats)})

            if stop_requested():
                return {"cancelled": True, "arena": stats}
            stats["stage"] = "已完成"
            return {"cancelled": False, "arena": stats}
        except JobExecutionError:
            raise
        except (HarvestError, OSError, ValueError) as exc:
            raise JobExecutionError(
                "龙痕竞技场执行未完成，请检查游戏服连接后重试"
            ) from exc
        finally:
            self._close(client)

    @staticmethod
    def payload(info: DragonArenaInfo) -> dict[str, Any]:
        challenged = sum(opponent.challenged for opponent in info.opponents)
        return {
            "level": info.level,
            "score": info.score,
            "stage": {
                "id": info.stage_id,
                "name": f"未知竞技场阶段（ID {info.stage_id}）",
            },
            "opponents": {
                "total": len(info.opponents),
                "available": len(info.opponents) - challenged,
                "challenged": challenged,
            },
            "choice": {
                "pending": bool(info.choice_pending),
                "id": info.choice_id,
            },
            "daily_reward": {
                "received": info.daily_reward_received,
                "count": info.daily_reward_num,
            },
        }

    @staticmethod
    def _enter(client: ArenaClient) -> None:
        client.__enter__()

    @staticmethod
    def _close(client: ArenaClient | None) -> None:
        if client is None:
            return
        try:
            client.close()
        except OSError:
            pass

    @staticmethod
    def _initial_stats(
        rounds: int, outcome: str, refresh_on_exhaustion: bool
    ) -> dict[str, Any]:
        return {
            "requested_rounds": rounds,
            "completed_rounds": 0,
            "wins": 0,
            "losses": 0,
            "score": 0,
            "score_delta": 0,
            "dragon_coin_delta": 0,
            "stage": "准备中",
            "last_result": "等待开始",
            "outcome": outcome,
            "outcome_label": OUTCOME_LABELS[outcome],
            "refresh_on_exhaustion": refresh_on_exhaustion,
            "opponents": {"total": 0, "available": 0, "challenged": 0},
            "daily_reward": {"received": False, "count": 0},
            "rewards": [],
        }

    @classmethod
    def _apply_info(cls, stats: dict[str, Any], info: DragonArenaInfo) -> None:
        payload = cls.payload(info)
        stats["score"] = payload["score"]
        stats["opponents"] = payload["opponents"]
        stats["daily_reward"] = payload["daily_reward"]

    @staticmethod
    def _apply_round_result(
        stats: dict[str, Any],
        result: DragonArenaRoundResult,
        *,
        count_round: bool = True,
    ) -> None:
        if count_round:
            stats["completed_rounds"] += 1
        battle: DragonArenaChallengeResult | None = result.battle
        choice: DragonArenaChoiceResult | None = result.mercy
        if battle is None:
            stats["stage"] = "本轮未完成"
            stats["last_result"] = f"第 {result.index} 场未收到服务端战斗结算"
            return

        stats["score"] = battle.score
        stats["score_delta"] += battle.score_delta
        if battle.win:
            stats["wins"] += 1
            stats["last_result"] = f"第 {result.index} 场胜利"
        else:
            stats["losses"] += 1
            stats["last_result"] = f"第 {result.index} 场失败"
        if choice is not None:
            stats["score"] = choice.score
            stats["score_delta"] += choice.score_delta
            if choice.ret == 0:
                stats["last_result"] += "，胜利抉择已结算"
            else:
                stats["last_result"] += "，胜利抉择未完成"
            for item_change in choice.item_changes:
                if item_change.item_id == DRAGON_COIN_ITEM_ID:
                    stats["dragon_coin_delta"] += item_change.delta
                stats["rewards"].append(
                    {
                        "id": item_change.item_id,
                        "name": item_name(item_change.item_id),
                        "delta": item_change.delta,
                        "total": item_change.total,
                    }
                )
        stats["stage"] = "本轮完成"
