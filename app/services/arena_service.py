from __future__ import annotations

import random
import time
import uuid
from typing import Any, Callable, Protocol, Sequence

from dragon_arena import (
    LOGIN_KICKOUT_RETRY_DELAY,
    OUTCOME_LABELS,
    DragonArenaChallengeResult,
    DragonArenaChoiceResult,
    DragonArenaClient,
    DragonArenaInfo,
    DragonArenaRoundResult,
    GameLoginKickout,
    resolve_win_choice_id,
)
from game_session import GameSessionManager
from harvest_fief import GameEndpoint, HarvestError
from id_descriptions import (
    arena_stage_name,
    item_name,
    win_choice_name,
)
from logging_store import MANAGED_DESTINATION, LogPersistenceError, write_standard_log

from ..job_manager import JobExecutionError


DRAGON_COIN_ITEM_ID = 440
DRAGON_COIN_NAME = item_name(DRAGON_COIN_ITEM_ID)
EndpointRefresher = Callable[[], GameEndpoint]
# 从可挑战序号列表中选一个（默认随机，贴近客户端列表展示的“乱序”体验）。
OpponentPicker = Callable[[Sequence[int]], int]


def pick_random_opponent(candidates: Sequence[int]) -> int:
    """在未挑战候选中均匀随机选一个 1-based 序号。"""

    if not candidates:
        raise ValueError("没有可挑战对手")
    return random.choice(tuple(candidates))


class ArenaServiceError(RuntimeError):
    """龙痕竞技场状态不能安全投影到界面时的错误。"""


class ArenaClient(Protocol):
    def __enter__(self) -> "ArenaClient": ...

    def close(self) -> None: ...

    def resume_pending_battle(
        self, *, win_choice_id: int | None = None, mercy_choice_id: int | None = None
    ) -> DragonArenaRoundResult | None: ...

    def get_info(self) -> DragonArenaInfo: ...

    def match(self) -> Any: ...

    def run_round(
        self,
        index: int,
        *,
        win_choice_id: int | None = None,
        mercy_choice_id: int | None = None,
    ) -> DragonArenaRoundResult: ...


LiveClientBuilder = Callable[[GameEndpoint, Callable[[str], None]], ArenaClient]


# 客户端 onKickout 的 ret 语义（见 decrypted-js/main.js NOTIFY_EXIT_*）。
# 业务名「登录会话中止」只对应消息 Kickout(10030)，不等于「会话过期」。
# 真正的令牌过期是 ret=51。
KICKOUT_RET_LABELS = {
    1: "关服踢出",
    2: "需重新进入区服",
    50: "校验令牌失败",
    51: "令牌过期",
    70: "登录校验失败",
    100: "关服踢出",
    101: "账号踢出",
    400: "区服踢出",
    410: "区服强制踢出",
}


def describe_login_kickout(exc: GameLoginKickout) -> str:
    """把 Kickout 转成可给操作台显示的说明；不臆测为会话过期。"""

    label = KICKOUT_RET_LABELS.get(exc.ret, "未登记的踢出原因")
    if exc.message:
        return f"游戏服踢出（{label}，ret {exc.ret}）：{exc.message}"
    return f"游戏服踢出（{label}，ret {exc.ret}）"


def _zone_from_endpoint(endpoint: GameEndpoint) -> dict[str, str]:
    """仅使用区服 id/name，不把入口 URL 或令牌写入日志。"""

    return {
        "id": str(endpoint.zone_id or "unknown"),
        "name": str(endpoint.zone_name or endpoint.zone_id or "unknown"),
    }


def _signed(value: int) -> str:
    return f"+{value}" if value >= 0 else str(value)


def _opponent_text(opponents: object) -> str:
    if not isinstance(opponents, dict):
        return "剩余对手 --"
    total = int(opponents.get("total") or 0)
    available = int(opponents.get("available") or 0)
    if total <= 0:
        return "剩余对手 0"
    return f"剩余对手 {available}/{total}"


def format_status_line(stats: dict[str, Any]) -> str:
    """单行状态：积分 / 龙痕币 / 对手 / 进度。"""

    score = int(stats.get("score") or 0)
    score_delta = int(stats.get("score_delta") or 0)
    coin_delta = int(stats.get("dragon_coin_delta") or 0)
    coin_total = stats.get("dragon_coin_total")
    completed = int(stats.get("completed_rounds") or 0)
    requested = int(stats.get("requested_rounds") or 0)

    coin_part = f"{DRAGON_COIN_NAME} 本局 {_signed(coin_delta)}"
    if isinstance(coin_total, int):
        coin_part = f"{DRAGON_COIN_NAME} {coin_total}（本局 {_signed(coin_delta)}）"

    progress = f"进度 {completed}/{requested}" if requested > 0 else f"进度 {completed}"
    return (
        f"积分 {score}（本局 {_signed(score_delta)}） · "
        f"{coin_part} · {_opponent_text(stats.get('opponents'))} · {progress}"
    )


def format_run_summary(stats: dict[str, Any], *, cancelled: bool = False) -> str:
    """全部轮次结束后的汇总。"""

    wins = int(stats.get("wins") or 0)
    losses = int(stats.get("losses") or 0)
    completed = int(stats.get("completed_rounds") or 0)
    requested = int(stats.get("requested_rounds") or 0)
    coin_delta = int(stats.get("dragon_coin_delta") or 0)
    coin_total = stats.get("dragon_coin_total")
    score = int(stats.get("score") or 0)
    score_delta = int(stats.get("score_delta") or 0)
    stop_reason = str(stats.get("stop_reason") or "")

    coin_part = f"{DRAGON_COIN_NAME} 本局 {_signed(coin_delta)}"
    if isinstance(coin_total, int):
        coin_part = f"{DRAGON_COIN_NAME} {coin_total}（本局 {_signed(coin_delta)}）"

    if cancelled:
        header = "已停止"
    elif stop_reason:
        header = "未完成"
    else:
        header = "全部完成"
    reason_part = f" · {stop_reason}" if stop_reason else ""
    return (
        f"{header} · {coin_part} · "
        f"积分 {score}（本局 {_signed(score_delta)}） · "
        f"挑战 {wins} 胜 / {losses} 负 / 共 {completed} 场"
        + (f"（目标 {requested} 轮）" if requested > 0 else "")
        + reason_part
    )


class ArenaService:
    """将龙痕竞技场 WebSocket 客户端投影为 UI 状态和可取消作业。"""

    def __init__(
        self,
        *,
        live_client_builder: LiveClientBuilder | None = None,
        game_timeout: float = 15.0,
        kickout_retry_delay: float = LOGIN_KICKOUT_RETRY_DELAY,
        result_log_destination: object = MANAGED_DESTINATION,
        opponent_picker: OpponentPicker | None = None,
        session_manager: GameSessionManager | None = None,
    ) -> None:
        self._session_manager = session_manager
        self._live_client_builder = live_client_builder or (
            lambda endpoint, log: DragonArenaClient(
                endpoint,
                game_timeout,
                log=log,
                log_server_messages=False,
                business_log=None,
                task="dragon_arena",
                session=(
                    self._session_manager.session_for(endpoint)
                    if self._session_manager is not None
                    else None
                ),
            )
        )
        self._kickout_retry_delay = kickout_retry_delay
        self._result_log_destination = result_log_destination
        self._opponent_picker = opponent_picker or pick_random_opponent

    def snapshot(
        self,
        endpoint: GameEndpoint,
        *,
        refresh_endpoint: EndpointRefresher | None = None,
    ) -> dict[str, Any]:
        client: ArenaClient | None = None
        try:
            client = self._open_client(
                endpoint,
                lambda _message: None,
                refresh_endpoint=refresh_endpoint,
            )
            return self.payload(client.get_info())
        except GameLoginKickout as exc:
            raise ArenaServiceError(describe_login_kickout(exc)) from exc
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
        *,
        refresh_endpoint: EndpointRefresher | None = None,
    ) -> dict[str, Any]:
        try:
            choice_id = resolve_win_choice_id(outcome=outcome)
        except HarvestError as exc:
            raise JobExecutionError("龙痕竞技场胜利抉择无效") from exc

        stats = self._initial_stats(rounds, outcome, refresh_on_exhaustion)
        run_id = uuid.uuid4().hex
        zone = _zone_from_endpoint(endpoint)
        outcome_label = OUTCOME_LABELS[outcome]

        def progress(level: str, message: str) -> None:
            emit(level, message, {"arena": dict(stats)})

        # 底层协议细节不刷屏；界面只展示精简进度。
        quiet_log = lambda _message: None

        client: ArenaClient | None = None
        result_payload: dict[str, Any] | None = None
        try:
            stats["stage"] = "连接中"
            stats["last_result"] = "正在连接龙痕竞技场"
            progress("info", "连接龙痕竞技场…")
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
            progress("info", f"已连接 · 胜利抉择：{outcome_label} · 目标 {rounds} 轮")
            if stop_requested():
                result_payload = self._finish(stats, cancelled=True, progress=progress)
                return result_payload

            resumed = client.resume_pending_battle(win_choice_id=choice_id)
            if resumed is not None:
                self._emit_round(
                    stats,
                    resumed,
                    progress,
                    count_round=False,
                    title="恢复遗留战斗",
                )

            attempted: set[int] = set()
            refreshed_without_progress = False
            while stats["completed_rounds"] < rounds:
                if stop_requested():
                    result_payload = self._finish(stats, cancelled=True, progress=progress)
                    return result_payload

                stats["stage"] = "读取状态"
                info = client.get_info()
                self._apply_info(stats, info)
                candidates = [
                    index
                    for index, opponent in enumerate(info.opponents, start=1)
                    if not opponent.challenged and index not in attempted
                ]
                if not candidates:
                    if refresh_on_exhaustion and not refreshed_without_progress:
                        stats["stage"] = "刷新对手"
                        progress("info", "对手已耗尽，正在寻找新对手…")
                        matched = client.match()
                        if getattr(matched, "ret", 0) == 0:
                            attempted.clear()
                            refreshed_without_progress = True
                            candidate_count = len(getattr(matched, "opponents", ()))
                            # match 后立刻读一次状态，保证对手计数准确
                            info = client.get_info()
                            self._apply_info(stats, info)
                            progress(
                                "info",
                                f"已刷新对手 {candidate_count} 人 · {format_status_line(stats)}",
                            )
                            continue
                        stats["stage"] = "寻找对手失败"
                        stats["last_result"] = "服务端未返回可挑战对手"
                        stats["stop_reason"] = stats["last_result"]
                        progress("warning", stats["last_result"])
                    else:
                        stats["stage"] = "对手已耗尽"
                        stats["last_result"] = "当前没有可挑战对手"
                        stats["stop_reason"] = stats["last_result"]
                        progress("info", f"{stats['last_result']} · {format_status_line(stats)}")
                    break

                index = self._opponent_picker(candidates)
                round_no = int(stats["completed_rounds"]) + 1
                stats["stage"] = f"第 {round_no}/{rounds} 轮"
                progress(
                    "info",
                    (
                        f"第 {round_no}/{rounds} 轮 · "
                        f"随机挑战第 {index} 位对手"
                        f"（候选 {len(candidates)} 人）· 开始战斗"
                    ),
                )
                progress("info", "战斗进行中…")
                result = client.run_round(index, win_choice_id=choice_id)
                self._emit_round(
                    stats,
                    result,
                    progress,
                    count_round=True,
                    title=f"第 {round_no}/{rounds} 轮",
                )
                if result.battle is None:
                    # 未收到结算时不能继续发送下一次竞技场请求，避免把未完成
                    # 的战斗误计入轮数并污染后续状态。
                    stats["stop_reason"] = "未收到服务端战斗结算"
                    progress(
                        "warning",
                        (
                            f"停止竞技场循环：{stats['stop_reason']} · "
                            f"已完成 {stats['completed_rounds']}/{rounds} 轮"
                        ),
                    )
                    break
                attempted.add(index)
                if result.battle is not None and result.battle.win:
                    attempted.clear()
                refreshed_without_progress = False

            if stop_requested():
                result_payload = self._finish(stats, cancelled=True, progress=progress)
                return result_payload
            result_payload = self._finish(stats, cancelled=False, progress=progress)
            return result_payload
        except JobExecutionError:
            raise
        except GameLoginKickout as exc:
            message = describe_login_kickout(exc)
            stats["stage"] = "登录被拒绝"
            stats["last_result"] = message
            progress("error", message)
            result_payload = {"cancelled": False, "arena": stats, "failed": True}
            raise JobExecutionError(message) from exc
        except (HarvestError, OSError, ValueError) as exc:
            stats["stage"] = "执行失败"
            stats["last_result"] = "龙痕竞技场执行未完成，请检查游戏服连接后重试"
            progress("error", stats["last_result"])
            result_payload = {"cancelled": False, "arena": stats, "failed": True}
            raise JobExecutionError(stats["last_result"]) from exc
        finally:
            self._close(client)
            if result_payload is not None:
                self._persist_run_result(
                    zone=zone,
                    run_id=run_id,
                    outcome_mode=outcome,
                    result=result_payload,
                )

    def _emit_round(
        self,
        stats: dict[str, Any],
        result: DragonArenaRoundResult,
        progress: Callable[[str, str], None],
        *,
        count_round: bool,
        title: str,
    ) -> bool:
        self._apply_round_result(stats, result, count_round=count_round)
        battle = result.battle
        choice = result.mercy

        if battle is None:
            stats["stop_reason"] = "未收到服务端战斗结算"
            progress("warning", f"{title} · 未收到战斗结算")
            progress("info", f"状态未更新 · {format_status_line(stats)}")
            return False

        if battle.win:
            progress("success", f"{title} · 战斗完成：胜利")
            level = "success"
        else:
            progress("warning", f"{title} · 战斗完成：失败")
            level = "warning"

        if choice is not None:
            choice_label = win_choice_name(choice.choice_id)
            if choice.ret == 0:
                progress("info", f"胜利抉择：{choice_label}")
            else:
                progress("warning", f"胜利抉择未完成：{choice_label}")
            coin_bits = [
                f"{item_name(change.item_id)} {_signed(change.delta)}"
                for change in choice.item_changes
                if change.delta != 0
            ]
            if coin_bits:
                progress("info", "奖励：" + "、".join(coin_bits))

        progress(level, format_status_line(stats))
        if count_round:
            progress(
                "info",
                f"第 {stats['completed_rounds']} 轮已完成",
            )
        return True

    def _finish(
        self,
        stats: dict[str, Any],
        *,
        cancelled: bool,
        progress: Callable[[str, str], None],
    ) -> dict[str, Any]:
        summary = format_run_summary(stats, cancelled=cancelled)
        incomplete = bool(stats.get("stop_reason"))
        stats["stage"] = (
            "已停止" if cancelled else "未完成" if incomplete else "已完成"
        )
        stats["last_result"] = summary
        progress("warning" if cancelled or incomplete else "success", summary)
        return {"cancelled": cancelled, "arena": stats}

    def _persist_run_result(
        self,
        *,
        zone: dict[str, str],
        run_id: str,
        outcome_mode: str,
        result: dict[str, Any],
    ) -> None:
        """将脱敏后的竞技场作业摘要写入托管 JSONL（project-logging 标准）。"""

        if self._result_log_destination is None:
            return
        arena = result.get("arena")
        if not isinstance(arena, dict):
            return
        cancelled = bool(result.get("cancelled"))
        failed = bool(result.get("failed"))
        if cancelled:
            log_outcome, level = "skipped", "warning"
        elif failed:
            log_outcome, level = "failure", "error"
        else:
            log_outcome, level = "success", "info"

        rewards = arena.get("rewards") if isinstance(arena.get("rewards"), list) else []
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

        details = {
            "requested_rounds": arena.get("requested_rounds"),
            "completed_rounds": arena.get("completed_rounds"),
            "wins": arena.get("wins"),
            "losses": arena.get("losses"),
            "score": arena.get("score"),
            "score_delta": arena.get("score_delta"),
            "dragon_coin_delta": arena.get("dragon_coin_delta"),
            "dragon_coin_total": arena.get("dragon_coin_total"),
            "dragon_coin_name": DRAGON_COIN_NAME,
            "outcome": outcome_mode,
            "outcome_label": arena.get("outcome_label")
            or OUTCOME_LABELS.get(outcome_mode, win_choice_name(0)),
            "refresh_on_exhaustion": arena.get("refresh_on_exhaustion"),
            "stage": arena.get("stage"),
            "last_result": arena.get("last_result"),
            "stop_reason": arena.get("stop_reason"),
            "opponents": arena.get("opponents"),
            "daily_reward": arena.get("daily_reward"),
            "rewards": redacted_rewards,
            "cancelled": cancelled,
            "summary": format_run_summary(arena, cancelled=cancelled),
        }
        try:
            write_standard_log(
                event="dragon_arena",
                operation="run",
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

    def _open_client(
        self,
        endpoint: GameEndpoint,
        log: Callable[[str], None],
        *,
        refresh_endpoint: EndpointRefresher | None = None,
        on_kickout_retry: Callable[[], None] | None = None,
    ) -> ArenaClient:
        """登录游戏服；Kickout ret=2 时刷新入口并重试一次（与 CLI 行为对齐）。"""

        current = endpoint
        last_kickout: GameLoginKickout | None = None
        for attempt in range(2):
            client = self._live_client_builder(current, log)
            try:
                self._enter(client)
                return client
            except GameLoginKickout as exc:
                self._close(client)
                last_kickout = exc
                if exc.ret != 2 or attempt > 0 or refresh_endpoint is None:
                    raise
                if on_kickout_retry is not None:
                    on_kickout_retry()
                if self._kickout_retry_delay > 0:
                    time.sleep(self._kickout_retry_delay)
                try:
                    current = refresh_endpoint()
                except Exception as refresh_exc:
                    raise GameLoginKickout(
                        exc.ret,
                        exc.message or "重新获取游戏服入口失败",
                    ) from refresh_exc
            except Exception:
                self._close(client)
                raise
        assert last_kickout is not None
        raise last_kickout

    @staticmethod
    def payload(info: DragonArenaInfo) -> dict[str, Any]:
        challenged = sum(opponent.challenged for opponent in info.opponents)
        return {
            "level": info.level,
            "score": info.score,
            "stage": {
                "id": info.stage_id,
                "name": arena_stage_name(info.stage_id),
            },
            "opponents": {
                "total": len(info.opponents),
                "available": len(info.opponents) - challenged,
                "challenged": challenged,
            },
            "choice": {
                "pending": bool(info.choice_pending),
                "id": info.choice_id,
                "name": win_choice_name(info.choice_id),
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
            "dragon_coin_total": None,
            "stage": "准备中",
            "last_result": "等待开始",
            "stop_reason": "",
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
        stats["arena_stage"] = payload["stage"]
        stats["choice"] = payload["choice"]

    @staticmethod
    def _apply_round_result(
        stats: dict[str, Any],
        result: DragonArenaRoundResult,
        *,
        count_round: bool = True,
    ) -> None:
        battle: DragonArenaChallengeResult | None = result.battle
        choice: DragonArenaChoiceResult | None = result.mercy
        if battle is None:
            stats["stage"] = "本轮未完成"
            stats["last_result"] = "未收到服务端战斗结算"
            return

        if count_round:
            stats["completed_rounds"] += 1

        round_score_delta = battle.score_delta
        stats["score"] = battle.score
        stats["score_delta"] += battle.score_delta
        if battle.win:
            stats["wins"] += 1
            stats["last_result"] = "战斗完成：胜利"
        else:
            stats["losses"] += 1
            stats["last_result"] = "战斗完成：失败"
        if choice is not None:
            stats["score"] = choice.score
            stats["score_delta"] += choice.score_delta
            round_score_delta += choice.score_delta
            choice_label = win_choice_name(choice.choice_id)
            if choice.ret == 0:
                stats["last_result"] = f"战斗完成：胜利 · 抉择 {choice_label}"
            else:
                stats["last_result"] = f"战斗完成：胜利 · 抉择 {choice_label} 未完成"
            for item_change in choice.item_changes:
                if item_change.item_id == DRAGON_COIN_ITEM_ID:
                    stats["dragon_coin_delta"] += item_change.delta
                    if item_change.total > 0:
                        stats["dragon_coin_total"] = item_change.total
                stats["rewards"].append(
                    {
                        "id": item_change.item_id,
                        "name": item_name(item_change.item_id),
                        "delta": item_change.delta,
                        "total": item_change.total,
                    }
                )
        stats["last_round_score_delta"] = round_score_delta
        stats["stage"] = "本轮完成"
