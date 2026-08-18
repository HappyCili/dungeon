"""Shared post-login recovery coordinator.

The game server owns the actual state after a local process exits.  This module
turns the residual packets retained by :mod:`game_session` into a small
dispatcher: a matching feature handler gets an exclusive recovery scope, then
the coordinator re-reads the shared snapshot before allowing ordinary work.
"""

from __future__ import annotations

from dataclasses import dataclass
import time
from typing import Protocol, Sequence

from game_session import (
    BATTLE_INFO_MESSAGE_ID,
    BATTLE_OFFLINE_MESSAGE_ID,
    BATTLE_S2C_FRAME_BROADCAST_MESSAGE_ID,
    BATTLE_S2C_FRAME_HASH_VERIFY_MESSAGE_ID,
    BATTLE_S2C_START_MESSAGE_ID,
    BATTLE_UNIT_INFO_MESSAGE_ID,
    GameSession,
    GameSessionError,
    GameSessionKickout,
    SessionRecoverySnapshot,
)
from harvest_fief import GameEndpoint


MAP_BATTLE_TYPES = frozenset({2, 7, 8})
DRAGON_ARENA_MESSAGE_IDS = frozenset({21104, 21106, 21108})
GRAVE_ABYSS_MESSAGE_IDS = frozenset({19400, 19402, 19410, 19414})
KNIGHT_ARENA_BATTLE_TYPE = 5
# ``Battle_info`` opens both the native BattlePrepare screen and an actual
# battle handshake.  These packets are emitted only after the player has
# started the battle (or while reconnecting to one), so they are the reliable
# signal for a running Knight Arena battle.
KNIGHT_ARENA_RUNNING_MESSAGE_IDS = frozenset(
    {
        BATTLE_UNIT_INFO_MESSAGE_ID,
        BATTLE_OFFLINE_MESSAGE_ID,
        BATTLE_S2C_START_MESSAGE_ID,
        BATTLE_S2C_FRAME_BROADCAST_MESSAGE_ID,
        BATTLE_S2C_FRAME_HASH_VERIFY_MESSAGE_ID,
    }
)


def _battle_issue(snapshot: SessionRecoverySnapshot):
    return next((issue for issue in snapshot.issues if issue.kind == "battle"), None)


def knight_arena_battle_is_running(snapshot: SessionRecoverySnapshot) -> bool:
    """Return whether the Knight Arena residue has crossed BattlePrepare.

    The native client receives ``Battle_info`` before displaying the team
    preparation screen.  Treating that packet as proof of a running battle
    sends an unsolicited ``Battle_C2S_start`` during recovery, which the
    server rejects for a preparation-only state.
    """

    battle_issue = _battle_issue(snapshot)
    return bool(
        battle_issue
        and battle_issue.battle_type == KNIGHT_ARENA_BATTLE_TYPE
        and KNIGHT_ARENA_RUNNING_MESSAGE_IDS.intersection(
            battle_issue.message_ids
        )
    )


@dataclass(frozen=True)
class RecoveryResult:
    """One recovery handler attempt, suitable for UI/log projection."""

    handler: str
    message: str
    handled: bool = True


@dataclass(frozen=True)
class RecoveryContentArea:
    """UI-facing owner for a login-time residual state.

    The coordinator still owns the actual dispatch.  This compact projection is
    used before recovery starts so every task can tell the UI which feature area
    is about to consume the server-side residue.
    """

    id: str
    label: str

    def to_payload(self) -> dict[str, str]:
        return {"id": self.id, "label": self.label}


def recovery_content_area(snapshot: SessionRecoverySnapshot) -> RecoveryContentArea:
    """Classify a residual snapshot using the default coordinator's routing order."""

    if not snapshot.pending:
        return RecoveryContentArea("none", "无遗留状态")

    battle_issue = _battle_issue(snapshot)
    if any(issue.kind == "event" for issue in snapshot.issues) or (
        battle_issue is not None and battle_issue.battle_type in MAP_BATTLE_TYPES
    ):
        return RecoveryContentArea("map_activity", "地图探索")
    if battle_issue is not None and (
        battle_issue.battle_type == 10
        or bool(DRAGON_ARENA_MESSAGE_IDS.intersection(snapshot.observed_message_ids))
    ):
        return RecoveryContentArea("dragon_arena", "龙痕竞技场")
    if battle_issue is not None and (
        battle_issue.battle_type == 3
        or bool(GRAVE_ABYSS_MESSAGE_IDS.intersection(snapshot.observed_message_ids))
    ):
        return RecoveryContentArea("grave_abyss", "罪者深渊")
    if knight_arena_battle_is_running(snapshot):
        return RecoveryContentArea("knight_arena", "普通竞技场")
    if (
        battle_issue is not None
        and battle_issue.battle_type == KNIGHT_ARENA_BATTLE_TYPE
    ):
        return RecoveryContentArea("knight_arena_preparation", "骑士比武")
    if battle_issue is not None:
        return RecoveryContentArea("generic_battle", "通用战斗")
    return RecoveryContentArea("unknown", "未分类服务状态")


@dataclass(frozen=True)
class SessionRecoveryReport:
    """Outcome of resolving one login epoch before a new task starts."""

    generation: int
    attempts: tuple[RecoveryResult, ...]
    initial: SessionRecoverySnapshot
    final: SessionRecoverySnapshot

    @property
    def recovered(self) -> bool:
        return not self.final.pending


class SessionRecoveryBlocked(GameSessionError):
    """No registered handler can safely settle the current server state."""

    def __init__(
        self,
        snapshot: SessionRecoverySnapshot,
        *,
        detail: str = "",
    ) -> None:
        self.snapshot = snapshot
        self.detail = detail
        suffix = f"：{detail}" if detail else ""
        super().__init__(
            "游戏服恢复状态未完成（"
            f"{snapshot.describe()}）{suffix}；已阻止开始新任务"
        )


class RecoveryHandler(Protocol):
    """Feature-specific continuation for a server-side residual state."""

    name: str

    def can_handle(self, snapshot: SessionRecoverySnapshot) -> bool:
        """Return whether this handler owns the currently pending state."""

    def recover(
        self,
        session: GameSession,
        endpoint: GameEndpoint,
        snapshot: SessionRecoverySnapshot,
    ) -> RecoveryResult:
        """Advance state while the coordinator holds ``session.recovery_scope``."""


class SessionRecoveryCoordinator:
    """Run registered recovery handlers until the login epoch is idle.

    A handler must either make the snapshot change or raise
    :class:`SessionRecoveryBlocked`.  This rule avoids a retry loop that would
    repeatedly send a command while the server remains in the same state.
    """

    def __init__(
        self,
        handlers: Sequence[RecoveryHandler] = (),
        *,
        max_passes: int = 8,
    ) -> None:
        if max_passes <= 0:
            raise ValueError("max_passes 必须为正整数")
        self._handlers = list(handlers)
        self._max_passes = max_passes

    @property
    def handlers(self) -> tuple[RecoveryHandler, ...]:
        return tuple(self._handlers)

    def register(self, handler: RecoveryHandler) -> None:
        self._handlers.append(handler)

    def recover(
        self,
        session: GameSession,
        endpoint: GameEndpoint,
    ) -> SessionRecoveryReport:
        attempts: list[RecoveryResult] = []
        with session.recovery_scope():
            initial = session.collect_recovery_messages()
            current = initial
            for _ in range(self._max_passes):
                if not current.pending:
                    session.mark_recovery_checked()
                    return SessionRecoveryReport(
                        generation=current.generation,
                        attempts=tuple(attempts),
                        initial=initial,
                        final=current,
                    )

                handler = next(
                    (
                        candidate
                        for candidate in self._handlers
                        if candidate.can_handle(current)
                    ),
                    None,
                )
                if handler is None:
                    raise SessionRecoveryBlocked(current)

                result = handler.recover(session, endpoint, current)
                attempts.append(result)
                if not result.handled:
                    raise SessionRecoveryBlocked(current, detail=result.message)

                next_snapshot = session.collect_recovery_messages()
                # Observed message ids are diagnostic-only.  Treating a newly
                # observed unrelated push as progress would let a handler retry
                # the same unfinished battle/dialog and potentially duplicate
                # a continuation command.
                if next_snapshot.issues == current.issues:
                    raise SessionRecoveryBlocked(
                        next_snapshot,
                        detail=f"处理器 {handler.name} 未推进服务端状态：{result.message}",
                    )
                current = next_snapshot

        raise SessionRecoveryBlocked(
            current,
            detail=f"恢复链超过 {self._max_passes} 个阶段",
        )


class MapActivityRecoveryHandler:
    """Resume map battle/event residues through the existing map state machine."""

    name = "map_activity"
    _MAP_BATTLE_TYPES = MAP_BATTLE_TYPES

    @staticmethod
    def _map_area_id(snapshot: SessionRecoverySnapshot, session: GameSession) -> int:
        game_data = session.game_data
        if not game_data:
            return 0
        try:
            from treasure_farm import decode_game_data_map_snapshot

            return int(decode_game_data_map_snapshot(game_data).curarea)
        except Exception:
            return 0

    def can_handle(self, snapshot: SessionRecoverySnapshot) -> bool:
        battle_issue = next(
            (issue for issue in snapshot.issues if issue.kind == "battle"), None
        )
        if battle_issue is not None:
            return battle_issue.battle_type in self._MAP_BATTLE_TYPES
        # The handler verifies the map area before it sends any continuation.
        # Routing here gives a useful explicit blocked state for non-map events
        # instead of letting an unrelated task consume the dialog packet.
        return any(issue.kind == "event" for issue in snapshot.issues)

    def recover(
        self,
        session: GameSession,
        endpoint: GameEndpoint,
        snapshot: SessionRecoverySnapshot,
    ) -> RecoveryResult:
        from treasure_farm import (
            PHASE_BATTLE_PREPARE,
            PHASE_BATTLE_RUNNING,
            TreasureFarmClient,
        )

        # A map event can arrive without a non-zero field35 marker.  Recheck
        # here because can_handle intentionally has no session argument.
        area_id = self._map_area_id(snapshot, session)
        if any(issue.kind == "event" for issue in snapshot.issues) and area_id <= 0:
            return RecoveryResult(
                self.name,
                "地图事件未携带当前区域，保留等待以避免误选对话",
                handled=False,
            )

        client = TreasureFarmClient(endpoint, session=session)
        try:
            try:
                progressed = client.resume_login_recovery()
            except GameSessionKickout:
                raise
            except Exception as exc:
                return RecoveryResult(
                    self.name,
                    f"地图恢复失败：{type(exc).__name__}",
                    handled=False,
                )
            if client.has_pending_login_event():
                return RecoveryResult(
                    self.name,
                    "地图事件仍需明确选项，未自动选择",
                    handled=False,
                )

            battle_issue = next(
                (issue for issue in snapshot.issues if issue.kind == "battle"), None
            )
            if (
                battle_issue is not None
                and progressed
                and client.battle_subphase()
                not in {PHASE_BATTLE_PREPARE, PHASE_BATTLE_RUNNING}
            ):
                # Some map recovery paths finish through Map_return_start rather
                # than a fresh Game_data snapshot.  The map handler has verified
                # that no Battle_info/frame remains before acknowledging it.
                session.resolve_recovery_issue("battle")
            return RecoveryResult(
                self.name,
                "已执行地图登录恢复" if progressed else "地图恢复未收到可推进状态",
                handled=progressed,
            )
        finally:
            client.close()


class DragonArenaRecoveryHandler:
    """Resume a leftover Scararena battle using the saved default outcome."""

    name = "dragon_arena"
    _SCARARENA_MESSAGE_IDS = DRAGON_ARENA_MESSAGE_IDS

    def __init__(self, *, outcome: str = "mercy") -> None:
        self._outcome = outcome

    def can_handle(self, snapshot: SessionRecoverySnapshot) -> bool:
        battle_issue = next(
            (issue for issue in snapshot.issues if issue.kind == "battle"), None
        )
        return bool(
            battle_issue
            and (
                battle_issue.battle_type == 10
                or bool(self._SCARARENA_MESSAGE_IDS.intersection(snapshot.observed_message_ids))
            )
        )

    def recover(
        self,
        session: GameSession,
        endpoint: GameEndpoint,
        _snapshot: SessionRecoverySnapshot,
    ) -> RecoveryResult:
        from dragon_arena import DragonArenaClient

        client = DragonArenaClient(
            endpoint,
            session.timeout,
            session=session,
            log=lambda _message: None,
            log_server_messages=False,
            business_log=None,
            task="session_recovery",
        )
        try:
            try:
                client.login()
                resumed = client.resume_pending_battle(outcome=self._outcome)
            except GameSessionKickout:
                raise
            except Exception as exc:
                return RecoveryResult(
                    self.name,
                    f"龙痕竞技场恢复失败：{type(exc).__name__}",
                    handled=False,
                )
            return RecoveryResult(
                self.name,
                "已续接龙痕竞技场遗留战斗" if resumed else "未发现可续接的龙痕战斗",
                handled=resumed is not None,
            )
        finally:
            client.close()


class GraveAbyssRecoveryHandler:
    """Resume a leftover Grave/abyss battle before another task starts."""

    name = "grave_abyss"
    _GRAVE_MESSAGE_IDS = GRAVE_ABYSS_MESSAGE_IDS

    def can_handle(self, snapshot: SessionRecoverySnapshot) -> bool:
        battle_issue = next(
            (issue for issue in snapshot.issues if issue.kind == "battle"), None
        )
        return bool(
            battle_issue
            and (
                battle_issue.battle_type == 3
                or bool(self._GRAVE_MESSAGE_IDS.intersection(snapshot.observed_message_ids))
            )
        )

    def recover(
        self,
        session: GameSession,
        endpoint: GameEndpoint,
        _snapshot: SessionRecoverySnapshot,
    ) -> RecoveryResult:
        from grave_abyss import GraveAbyssClient

        client = GraveAbyssClient(
            endpoint,
            session.timeout,
            session=session,
            log=lambda _message: None,
            log_server_messages=False,
            business_log=None,
            task="session_recovery",
        )
        try:
            try:
                client.login()
                resumed = client.resume_pending_battle()
            except GameSessionKickout:
                raise
            except Exception as exc:
                return RecoveryResult(
                    self.name,
                    f"罪者深渊恢复失败：{type(exc).__name__}",
                    handled=False,
                )
            return RecoveryResult(
                self.name,
                "已续接罪者深渊遗留战斗" if resumed else "未发现可续接的深渊战斗",
                handled=resumed is not None,
            )
        finally:
            client.close()


class KnightArenaPreparationRecoveryHandler:
    """Release a Knight Arena BattlePrepare screen without starting a battle.

    ``battleType=5`` is the ordinary Arena PVP flow.  The native client keeps
    ``battleState=1`` while its team preparation screen is open and replays
    ``Battle_info`` when reconnecting.  That packet carries the prospective
    teams, not evidence that ``Battle_C2S_start`` was sent.  Recovery releases
    the local barrier and lets the following task issue its own Arena action.
    """

    name = "knight_arena_preparation"
    _KNIGHT_ARENA_BATTLE_TYPE = KNIGHT_ARENA_BATTLE_TYPE

    def can_handle(self, snapshot: SessionRecoverySnapshot) -> bool:
        battle_issue = _battle_issue(snapshot)
        return bool(
            battle_issue
            and battle_issue.battle_type == self._KNIGHT_ARENA_BATTLE_TYPE
            and not knight_arena_battle_is_running(snapshot)
        )

    def recover(
        self,
        session: GameSession,
        _endpoint: GameEndpoint,
        _snapshot: SessionRecoverySnapshot,
    ) -> RecoveryResult:
        deferred = []
        while True:
            try:
                header = session.receive_header(0)
            except GameSessionError as exc:
                if "超时" not in str(exc):
                    raise
                break
            if header.message_id != BATTLE_INFO_MESSAGE_ID:
                deferred.append(header)
        if deferred:
            session.push_headers(deferred)

        # The server has only restored BattlePrepare.  Drop its prospective
        # Battle_info and clear the local login barrier; a later explicit Arena
        # action owns the decision to send Battle_C2S_start.
        session.resolve_recovery_issue("battle")
        return RecoveryResult(
            self.name,
            "骑士比武停在备战界面，未自动开始战斗，已解除本地等待",
        )


class KnightArenaRecoveryHandler:
    """Finish an ordinary Arena battle with runtime continuation packets."""

    name = "knight_arena"
    _KNIGHT_ARENA_BATTLE_TYPE = KNIGHT_ARENA_BATTLE_TYPE

    def can_handle(self, snapshot: SessionRecoverySnapshot) -> bool:
        return knight_arena_battle_is_running(snapshot)

    def recover(
        self,
        session: GameSession,
        endpoint: GameEndpoint,
        _snapshot: SessionRecoverySnapshot,
    ) -> RecoveryResult:
        from knight_arena import KnightArenaClient

        client = KnightArenaClient(
            endpoint,
            session.timeout,
            session=session,
            log=lambda _message: None,
            log_server_messages=False,
            business_log=None,
            task="session_recovery",
        )
        try:
            try:
                client.login()
                result = client.await_challenge_result()
                session.resolve_recovery_issue("battle")
            except GameSessionKickout:
                raise
            except Exception as exc:
                return RecoveryResult(
                    self.name,
                    f"普通竞技场恢复失败：{type(exc).__name__}",
                    handled=False,
                )
            return RecoveryResult(
                self.name,
                "已完成普通竞技场遗留战斗"
                + ("（胜利）" if result.win else "（失败）"),
            )
        finally:
            client.close()


class GenericBattleRecoveryHandler:
    """Continue a standard Battle_info handshake without feature-local state.

    This is intentionally last in the registry.  Map, dragon-arena, and grave
    handlers own their respective settlement semantics; this handler only
    drives the common battle protocol through ``Battle_S2C_end``.
    """

    name = "generic_battle"

    def __init__(self, *, timeout: float = 180.0) -> None:
        self._timeout = timeout

    def can_handle(self, snapshot: SessionRecoverySnapshot) -> bool:
        return any(issue.kind == "battle" for issue in snapshot.issues)

    def recover(
        self,
        session: GameSession,
        _endpoint: GameEndpoint,
        _snapshot: SessionRecoverySnapshot,
    ) -> RecoveryResult:
        from dragon_arena import (
            BATTLE_TIMESCALE_X3,
            decode_battle_info,
            decode_game_data_battle_context,
            encode_battle_auto,
            encode_battle_c2s_start,
            encode_battle_timescale,
        )
        from dragon_arena_business_map import (
            BATTLE_C2S_AUTO_ARTIFACT_SKILL_MESSAGE_ID,
            BATTLE_C2S_AUTO_UNIQUE_SKILL_MESSAGE_ID,
            BATTLE_C2S_SET_TIMESCALE_MESSAGE_ID,
            BATTLE_C2S_START_MESSAGE_ID,
            BATTLE_INFO_MESSAGE_ID,
            BATTLE_S2C_END_MESSAGE_ID,
            BATTLE_S2C_FRAME_BROADCAST_MESSAGE_ID,
            BATTLE_S2C_FRAME_HASH_VERIFY_MESSAGE_ID,
            BATTLE_S2C_START_MESSAGE_ID,
            GAME_DATA_MESSAGE_ID,
        )

        context = None
        if session.game_data:
            try:
                context = decode_game_data_battle_context(session.game_data)
            except Exception:
                context = None

        deferred = []
        started = False
        configured = False
        saw_running = False
        started_at = time.monotonic()
        deadline = started_at + self._timeout
        handshake_deadline = started_at + min(8.0, max(2.0, session.timeout))
        try:
            while time.monotonic() < deadline:
                if not (started or saw_running) and time.monotonic() >= handshake_deadline:
                    return RecoveryResult(
                        self.name,
                        "未收到可续接的 Battle_info，保留等待状态",
                        handled=False,
                    )
                remaining = deadline - time.monotonic()
                try:
                    header = session.receive_header(min(remaining, session.timeout))
                except GameSessionError as exc:
                    if "超时" in str(exc):
                        continue
                    return RecoveryResult(self.name, str(exc), handled=False)

                if header.message_id == GAME_DATA_MESSAGE_ID:
                    if session.game_data:
                        try:
                            context = decode_game_data_battle_context(session.game_data)
                        except Exception:
                            context = None
                    continue
                if header.message_id == BATTLE_INFO_MESSAGE_ID:
                    try:
                        battle = decode_battle_info(header.data)
                    except Exception as exc:
                        return RecoveryResult(
                            self.name,
                            f"无法解析 Battle_info：{type(exc).__name__}",
                            handled=False,
                        )
                    if battle.ret != 0:
                        return RecoveryResult(
                            self.name,
                            f"Battle_info 返回 ret={battle.ret}",
                            handled=False,
                        )
                    if context is None:
                        return RecoveryResult(
                            self.name,
                            "登录快照缺少当前编队，无法安全续接战斗",
                            handled=False,
                        )
                    if not started:
                        try:
                            payload = encode_battle_c2s_start(battle, context.team)
                        except Exception as exc:
                            return RecoveryResult(
                                self.name,
                                f"无法构造 Battle_C2S_start：{type(exc).__name__}",
                                handled=False,
                            )
                        session.send_message(
                            BATTLE_C2S_START_MESSAGE_ID, payload, encrypted=True
                        )
                        started = True
                    continue
                if header.message_id == BATTLE_S2C_START_MESSAGE_ID:
                    saw_running = True
                    if not configured:
                        session.send_message(
                            BATTLE_C2S_SET_TIMESCALE_MESSAGE_ID,
                            encode_battle_timescale(BATTLE_TIMESCALE_X3),
                            encrypted=True,
                        )
                        session.send_message(
                            BATTLE_C2S_AUTO_UNIQUE_SKILL_MESSAGE_ID,
                            encode_battle_auto(True),
                            encrypted=True,
                        )
                        session.send_message(
                            BATTLE_C2S_AUTO_ARTIFACT_SKILL_MESSAGE_ID,
                            encode_battle_auto(True),
                            encrypted=True,
                        )
                        configured = True
                    continue
                if header.message_id in {
                    BATTLE_S2C_FRAME_BROADCAST_MESSAGE_ID,
                    BATTLE_S2C_FRAME_HASH_VERIFY_MESSAGE_ID,
                }:
                    saw_running = True
                    continue
                if header.message_id == BATTLE_S2C_END_MESSAGE_ID:
                    return RecoveryResult(self.name, "已完成通用战斗结算")
                deferred.append(header)
        finally:
            if deferred:
                session.push_headers(deferred)

        phase = "战斗中" if saw_running else "等待 Battle_info"
        return RecoveryResult(
            self.name,
            f"{phase}恢复超时，保留服务端状态",
            handled=False,
        )


def build_default_recovery_coordinator() -> SessionRecoveryCoordinator:
    """Build the process-wide handlers without importing feature modules eagerly."""

    return SessionRecoveryCoordinator(
        (
            MapActivityRecoveryHandler(),
            DragonArenaRecoveryHandler(),
            GraveAbyssRecoveryHandler(),
            KnightArenaRecoveryHandler(),
            KnightArenaPreparationRecoveryHandler(),
            GenericBattleRecoveryHandler(),
        )
    )
