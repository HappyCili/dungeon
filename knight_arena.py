#!/usr/bin/env python3
"""骑士比武（普通竞技场 Arena_*）WebSocket 客户端。

日常任务 109：在骑士比武中完成 3 次挑战。只计挑战次数，不要求胜利。
对手与角色均可任意选取；默认随机挑未战胜的候选，用当前编队开战。

协议来源：``decrypted-js/main.js`` 中 ``Arena_info`` / ``Arena_challenge`` /
``Arena_challenge_result`` 与战斗握手（与龙痕竞技场共用 Battle_*）。

用法：
    .venv/bin/python knight_arena.py info
    .venv/bin/python knight_arena.py loop --rounds 3
    .venv/bin/python knight_arena.py --self-test
"""

from __future__ import annotations

import argparse
import random
import socket
import sys
import time
from dataclasses import dataclass
from typing import Callable, Sequence

from dragon_arena import (
    BATTLE_C2S_START_MESSAGE_ID,
    BATTLE_INFO_MESSAGE_ID,
    BATTLE_S2C_END_MESSAGE_ID,
    BATTLE_S2C_FRAME_BROADCAST_MESSAGE_ID,
    BATTLE_S2C_FRAME_HASH_VERIFY_MESSAGE_ID,
    BATTLE_S2C_START_MESSAGE_ID,
    GAME_DATA_MESSAGE_ID,
    DragonArenaClient,
    GameLoginKickout,
    GameMessageTimeout,
    GameSessionClosed,
    decode_battle_info,
    decode_battle_start_response,
    _first_int_field,
)
from dragon_arena_business_map import (
    ARENA_CHALLENGE_MESSAGE_ID,
    ARENA_CHALLENGE_RESULT_MESSAGE_ID,
    ARENA_INFO_MESSAGE_ID,
    ARENA_REFRESH_OPPONENTS_MESSAGE_ID,
    LOGIN_OK_MESSAGE_ID,
    LOGIN_REUNIQUE_MESSAGE_ID,
)
from harvest_fief import (
    PACK_PASSWORD_MESSAGE_ID,
    SOCKET_PACK_KEY,
    GameEndpoint,
    HarvestError,
    ProtoReader,
    decode_int32,
    decode_message_header,
    encode_bytes_field,
    encode_int_field,
    load_tokens,
    pack1_decode,
    pack1_encode,
    resolve_game_endpoint,
)
from harvest_fief import build_parser as build_base_parser

# 匹配列表下标（Arena_challenge.opponenttype=0）；1 为历史复仇。
OPPONENT_TYPE_MATCH = 0
OPPONENT_TYPE_REVENGE = 1


@dataclass(frozen=True)
class ArenaOpponent:
    """骑士比武候选对手（``Arena_info.mdatas`` 一项）。"""

    index: int
    opponent_id: int
    opponent_type: int
    win: int
    score: int
    nick: str


@dataclass(frozen=True)
class ArenaInfo:
    """``Arena_info`` / ``Game_data.arenainfo`` 的可用字段。"""

    challenge_num: int
    refresh_num: int
    season_id: int
    season_score: int
    season_open: bool
    season_challenge_num: int
    opponents: tuple[ArenaOpponent, ...]


@dataclass(frozen=True)
class ArenaChallengeResponse:
    ret: int
    mode: int
    opponent_type: int
    opponent_id: int
    challenge_num: int
    season_challenge_num: int


@dataclass(frozen=True)
class ArenaChallengeResult:
    win: bool
    opponent_type: int
    opponent_id: int
    season_score: int
    score_delta: int
    best_score: int


@dataclass(frozen=True)
class ArenaRoundResult:
    opponent_index: int
    challenge: ArenaChallengeResponse
    battle: ArenaChallengeResult | None


def _decode_string_field(data: bytes) -> str:
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return ""


def decode_arena_opponent(data: bytes, *, index: int) -> ArenaOpponent:
    values: dict[str, object] = {
        "opponent_id": 0,
        "opponent_type": 0,
        "win": 0,
        "score": 0,
        "nick": "",
    }
    for field_number, wire_type, value in ProtoReader(data).fields():
        if field_number == 1 and wire_type == 0:
            values["opponent_id"] = int(value)
        elif field_number == 2 and wire_type == 0:
            values["opponent_type"] = decode_int32(int(value))
        elif field_number == 3 and wire_type == 0:
            values["win"] = decode_int32(int(value))
        elif field_number == 4 and wire_type == 0:
            values["score"] = decode_int32(int(value))
        elif field_number == 6 and wire_type == 2:
            values["nick"] = _decode_string_field(bytes(value))
    return ArenaOpponent(
        index=index,
        opponent_id=int(values["opponent_id"]),
        opponent_type=int(values["opponent_type"]),
        win=int(values["win"]),
        score=int(values["score"]),
        nick=str(values["nick"]),
    )


def decode_arena_info(data: bytes) -> ArenaInfo:
    """Decode ``ArenaInfo``（proto ``Mf``）。"""

    challenge_num = 0
    refresh_num = 0
    season_id = 0
    season_score = 0
    season_open = False
    season_challenge_num = 0
    opponents: list[ArenaOpponent] = []
    for field_number, wire_type, value in ProtoReader(data).fields():
        if field_number == 1 and wire_type == 0:
            challenge_num = decode_int32(int(value))
        elif field_number == 2 and wire_type == 0:
            refresh_num = decode_int32(int(value))
        elif field_number == 3 and wire_type == 0:
            season_id = decode_int32(int(value))
        elif field_number == 4 and wire_type == 0:
            season_score = decode_int32(int(value))
        elif field_number == 10 and wire_type == 2:
            opponents.append(
                decode_arena_opponent(bytes(value), index=len(opponents))
            )
        elif field_number == 14 and wire_type == 0:
            season_open = bool(value)
        elif field_number == 17 and wire_type == 0:
            season_challenge_num = decode_int32(int(value))
    return ArenaInfo(
        challenge_num=challenge_num,
        refresh_num=refresh_num,
        season_id=season_id,
        season_score=season_score,
        season_open=season_open,
        season_challenge_num=season_challenge_num,
        opponents=tuple(opponents),
    )


def encode_arena_challenge(
    *,
    opponent_id: int,
    opponent_type: int = OPPONENT_TYPE_MATCH,
    mode: int = 0,
    get_team: int = 0,
) -> bytes:
    """Encode ``Arena_challenge`` request（proto ``Ff``）。

    ``opponent_id`` 在匹配列表中为 ``mdatas`` 下标（可为 0；0 时字段省略，
    服务端按默认 0 处理）。
    """

    parts: list[bytes] = []
    if mode:
        parts.append(encode_int_field(1, mode))
    if opponent_type:
        parts.append(encode_int_field(2, opponent_type))
    if opponent_id:
        parts.append(encode_int_field(3, opponent_id))
    if get_team:
        parts.append(encode_int_field(4, get_team))
    return b"".join(parts)


def decode_arena_challenge_response(data: bytes) -> ArenaChallengeResponse:
    values = {
        "ret": 0,
        "mode": 0,
        "opponent_type": 0,
        "opponent_id": 0,
        "challenge_num": 0,
        "season_challenge_num": 0,
    }
    for field_number, wire_type, value in ProtoReader(data).fields():
        if field_number == 1 and wire_type == 0:
            values["ret"] = decode_int32(int(value))
        elif field_number == 2 and wire_type == 0:
            values["mode"] = decode_int32(int(value))
        elif field_number == 3 and wire_type == 0:
            values["opponent_type"] = decode_int32(int(value))
        elif field_number == 4 and wire_type == 0:
            values["opponent_id"] = decode_int32(int(value))
        elif field_number == 5 and wire_type == 0:
            values["challenge_num"] = decode_int32(int(value))
        elif field_number == 8 and wire_type == 0:
            values["season_challenge_num"] = decode_int32(int(value))
    return ArenaChallengeResponse(**values)


def decode_arena_challenge_result(data: bytes) -> ArenaChallengeResult:
    """Decode ``Arena_challenge_result``（proto ``qf``）。"""

    values = {
        "win": False,
        "opponent_type": 0,
        "opponent_id": 0,
        "season_score": 0,
        "score_delta": 0,
        "best_score": 0,
    }
    for field_number, wire_type, value in ProtoReader(data).fields():
        if field_number == 1 and wire_type == 0:
            values["win"] = bool(value)
        elif field_number == 2 and wire_type == 0:
            values["opponent_type"] = decode_int32(int(value))
        elif field_number == 3 and wire_type == 0:
            values["opponent_id"] = decode_int32(int(value))
        elif field_number == 4 and wire_type == 0:
            values["season_score"] = decode_int32(int(value))
        elif field_number == 5 and wire_type == 0:
            values["score_delta"] = decode_int32(int(value))
        elif field_number == 6 and wire_type == 0:
            values["best_score"] = decode_int32(int(value))
    return ArenaChallengeResult(**values)


def decode_arena_refresh_response(data: bytes) -> tuple[int, tuple[ArenaOpponent, ...]]:
    """Decode ``Arena_refresh_opponents`` response（proto ``Gf``）。"""

    ret = 0
    opponents: list[ArenaOpponent] = []
    for field_number, wire_type, value in ProtoReader(data).fields():
        if field_number == 1 and wire_type == 0:
            ret = decode_int32(int(value))
        elif field_number == 4 and wire_type == 2:
            opponents.append(
                decode_arena_opponent(bytes(value), index=len(opponents))
            )
    return ret, tuple(opponents)


def pick_challenge_candidate(
    opponents: Sequence[ArenaOpponent],
    *,
    attempted: set[int] | None = None,
    prefer_unbeaten: bool = True,
) -> int | None:
    """从候选中选一个 ``mdatas`` 下标；优先未战胜且本轮未挑战过的。"""

    attempted = attempted or set()
    pool = [opp for opp in opponents if opp.index not in attempted]
    if not pool:
        return None
    if prefer_unbeaten:
        unbeaten = [opp for opp in pool if not opp.win]
        if unbeaten:
            pool = unbeaten
    return random.choice(pool).index


class KnightArenaClient(DragonArenaClient):
    """骑士比武客户端：复用龙痕竞技场的登录与战斗握手，走 Arena_* 协议。"""

    def get_info(self) -> ArenaInfo:
        self._ensure_arena_idle()
        self._send_message(ARENA_INFO_MESSAGE_ID, encrypted=True)
        for header in self._wait_for({ARENA_INFO_MESSAGE_ID}, self.timeout):
            if header.message_id == ARENA_INFO_MESSAGE_ID:
                return decode_arena_info(header.data)
            self._log_background_message(header)
        raise AssertionError("_wait_for 未返回骑士比武信息")

    def refresh_opponents(self) -> tuple[int, tuple[ArenaOpponent, ...]]:
        self._ensure_arena_idle()
        self._send_message(ARENA_REFRESH_OPPONENTS_MESSAGE_ID, encrypted=True)
        for header in self._wait_for({ARENA_REFRESH_OPPONENTS_MESSAGE_ID}, self.timeout):
            if header.message_id == ARENA_REFRESH_OPPONENTS_MESSAGE_ID:
                return decode_arena_refresh_response(header.data)
            self._log_background_message(header)
        raise AssertionError("_wait_for 未返回刷新对手响应")

    def challenge(
        self,
        opponent_id: int,
        *,
        opponent_type: int = OPPONENT_TYPE_MATCH,
        mode: int = 0,
    ) -> ArenaChallengeResponse:
        self._ensure_arena_idle()
        payload = encode_arena_challenge(
            opponent_id=opponent_id,
            opponent_type=opponent_type,
            mode=mode,
        )
        self._send_message(ARENA_CHALLENGE_MESSAGE_ID, payload, encrypted=True)
        for header in self._wait_for({ARENA_CHALLENGE_MESSAGE_ID}, self.timeout):
            if header.message_id == ARENA_CHALLENGE_MESSAGE_ID:
                return decode_arena_challenge_response(header.data)
            if header.message_id == BATTLE_INFO_MESSAGE_ID:
                battle = decode_battle_info(header.data)
                if battle.ret != 0:
                    raise HarvestError(f"Battle_info 返回 ret={battle.ret}")
                self._queued_headers.appendleft(header)
                self.log(
                    "[挑战] Battle_info 早于挑战响应到达，已转入普通战斗握手。"
                )
                return ArenaChallengeResponse(
                    ret=0,
                    mode=mode,
                    opponent_type=opponent_type,
                    opponent_id=opponent_id,
                    challenge_num=0,
                    season_challenge_num=0,
                )
            self._log_background_message(header)
        raise AssertionError("_wait_for 未返回挑战响应")

    def await_challenge_result(self) -> ArenaChallengeResult:
        """等待骑士比武结算；中途完成战斗握手与加速/自动技能。"""

        configured = False
        battle_start_sent = False
        frame_count = 0
        hash_verify_count = 0
        state = "等待 Battle_info"
        started_at = time.monotonic()
        deadline = time.monotonic() + self.battle_timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise HarvestError("等待骑士比武战斗结算超时")
            try:
                header = self._next_header(min(remaining, 5.0))
            except GameMessageTimeout:
                elapsed = int(time.monotonic() - started_at)
                self.log(
                    f"[战斗] 仍在等待：阶段={state}，已等待 {elapsed} 秒，"
                    f"战斗帧={frame_count}，哈希校验={hash_verify_count}。"
                )
                continue
            except GameSessionClosed:
                self.log(f"[战斗] 服务端在阶段={state} 时关闭 WebSocket。")
                raise
            if header.message_id == BATTLE_INFO_MESSAGE_ID:
                info = decode_battle_info(header.data)
                self.log(
                    "[战斗] Battle_info："
                    f"ret={info.ret}，玩家单位={info.player_units}，"
                    f"敌方单位={info.enemy_units}。"
                )
                if info.ret != 0:
                    raise HarvestError(f"Battle_info 返回 ret={info.ret}")
                if not battle_start_sent:
                    self.start_battle(info)
                    battle_start_sent = True
                    state = "已发送 Battle_C2S_start，等待 Battle_S2C_start"
                continue
            if header.message_id == ARENA_CHALLENGE_MESSAGE_ID:
                delayed = decode_arena_challenge_response(header.data)
                if delayed.ret != 0:
                    raise HarvestError(f"挑战响应延迟返回 ret={delayed.ret}")
                self.log(
                    "[挑战] 收到延迟挑战响应："
                    f"opponentid={delayed.opponent_id}，"
                    f"challengenum={delayed.challenge_num}。"
                )
                continue
            if header.message_id == BATTLE_S2C_START_MESSAGE_ID:
                start = decode_battle_start_response(header.data)
                if start.ret != 0:
                    raise HarvestError(f"Battle_S2C_start 返回 ret={start.ret}")
                state = "战斗中"
                self.log("[战斗] 服务端开始战斗。")
                if not configured:
                    self.configure_battle()
                    configured = True
                continue
            if header.message_id == BATTLE_S2C_END_MESSAGE_ID:
                state = "等待骑士比武结算"
                win = _first_int_field(header.data, 2)
                self.log(
                    "[战斗] 服务端结束战斗："
                    f"胜利={win if win is not None else '未提供'}，"
                    f"载荷={len(header.data)} 字节。"
                )
                continue
            if header.message_id == BATTLE_S2C_FRAME_BROADCAST_MESSAGE_ID:
                frame_count += 1
                continue
            if header.message_id == BATTLE_S2C_FRAME_HASH_VERIFY_MESSAGE_ID:
                hash_verify_count += 1
                continue
            if header.message_id == ARENA_CHALLENGE_RESULT_MESSAGE_ID:
                result = decode_arena_challenge_result(header.data)
                self.log(
                    "[战斗] 骑士比武完成："
                    f"opponentid={result.opponent_id}，"
                    f"胜利={'是' if result.win else '否'}，"
                    f"积分={result.season_score}（{result.score_delta:+d}）。"
                )
                return result
            if battle_start_sent and header.message_id == BATTLE_C2S_START_MESSAGE_ID:
                self.log("[战斗] 收到 Battle_C2S_start 回显。")
            self._log_background_message(header)

    def run_round(self, opponent_index: int) -> ArenaRoundResult:
        """挑战指定 mdatas 下标；不论输赢都算完成一次挑战。"""

        self._begin_arena_settlement()
        challenge = self.challenge(opponent_index)
        if challenge.ret != 0:
            self._arena_settlement_changes.clear()
            self.log(
                f"[挑战] 下标={opponent_index}，服务端 ret={challenge.ret}，跳过本轮。"
            )
            return ArenaRoundResult(opponent_index, challenge, None)

        self.log(
            "[挑战] "
            f"下标={opponent_index}，"
            f"opponentid={challenge.opponent_id}，"
            f"今日已挑战={challenge.challenge_num}。"
        )
        try:
            battle = self.await_challenge_result()
        except GameSessionClosed:
            self.log(f"[战斗] 下标={opponent_index}，游戏服连接已关闭。")
            raise
        except HarvestError as exc:
            self.log(f"[战斗] 下标={opponent_index}，本轮未完成：{exc}。")
            return ArenaRoundResult(opponent_index, challenge, None)

        self.log(
            f"[挑战] 本场{'胜利' if battle.win else '失败'}（日常只计挑战次数）。"
        )
        self._collect_post_settlement_item_changes()
        return ArenaRoundResult(opponent_index, challenge, battle)

    def run_loop(
        self,
        *,
        rounds: int,
        refresh_on_exhaustion: bool = True,
    ) -> tuple[ArenaRoundResult, ...]:
        """完成 ``rounds`` 次挑战；不要求胜利。

        ``rounds=0`` 表示打光当前候选列表（可刷新一次）。
        """

        if rounds < 0:
            raise HarvestError("循环次数不能为负数")
        results: list[ArenaRoundResult] = []
        attempted: set[int] = set()
        unlimited = rounds == 0
        target = rounds if not unlimited else sys.maxsize

        info = self.get_info()
        if not info.season_open:
            self.log("[骑士比武] 赛季未开放，停止。")
            return tuple(results)
        opponents = list(info.opponents)
        self.log(
            f"[骑士比武] 赛季={info.season_id}，积分={info.season_score}，"
            f"候选={len(opponents)}，今日已挑战={info.challenge_num}。"
        )

        while len(results) < target:
            index = pick_challenge_candidate(opponents, attempted=attempted)
            if index is None:
                if not refresh_on_exhaustion:
                    self.log("[骑士比武] 候选已耗尽，停止。")
                    break
                self.log("[骑士比武] 候选已耗尽，刷新对手。")
                ret, refreshed = self.refresh_opponents()
                if ret != 0 or not refreshed:
                    self.log(f"[骑士比武] 刷新失败 ret={ret}，停止。")
                    break
                opponents = list(refreshed)
                attempted.clear()
                index = pick_challenge_candidate(opponents, attempted=attempted)
                if index is None:
                    self.log("[骑士比武] 刷新后仍无候选，停止。")
                    break

            attempted.add(index)
            result = self.run_round(index)
            if result.battle is not None:
                results.append(result)
                # 本地标记已战胜，避免重复优先选同一人。
                if result.battle.win:
                    opponents = [
                        (
                            ArenaOpponent(
                                index=opp.index,
                                opponent_id=opp.opponent_id,
                                opponent_type=opp.opponent_type,
                                win=1,
                                score=opp.score,
                                nick=opp.nick,
                            )
                            if opp.index == index
                            else opp
                        )
                        for opp in opponents
                    ]
            elif result.challenge.ret != 0:
                # 明确失败的序号不再重试；其它错误保留序号以便换人。
                continue
            else:
                # 战斗中断：记一次尝试但未完成，换下一位。
                continue

            if unlimited and all(opp.index in attempted for opp in opponents):
                if not refresh_on_exhaustion:
                    break

        wins = sum(1 for item in results if item.battle and item.battle.win)
        losses = len(results) - wins
        self.log(
            f"[骑士比武] 循环结束：完成 {len(results)} 场"
            f"（{wins} 胜 / {losses} 负），目标 {rounds if not unlimited else '耗尽候选'}。"
        )
        return tuple(results)


def _build_parser() -> argparse.ArgumentParser:
    parser = build_base_parser()
    parser.prog = "knight_arena.py"
    parser.description = __doc__
    parser.add_argument(
        "command",
        nargs="?",
        choices=("info", "loop"),
        default="info",
        help="info=查询；loop=循环挑战（默认 info）",
    )
    parser.add_argument(
        "--rounds",
        type=int,
        default=3,
        help="loop 的挑战次数；0 表示打光当前候选（可刷新）",
    )
    parser.add_argument(
        "--no-refresh",
        action="store_true",
        help="候选耗尽时不刷新对手",
    )
    parser.add_argument(
        "--battle-timeout",
        type=float,
        default=180.0,
        help="单场战斗等待结算的最长秒数",
    )
    return parser


def _self_test() -> None:
    """离线校验编码/解码、选人与最小挑战回放，不连游戏服。"""

    mdata0 = (
        encode_int_field(1, 1001)
        + encode_int_field(2, 1)
        + encode_int_field(3, 1)
        + encode_int_field(4, 200)
    )
    mdata1 = encode_int_field(1, 1002) + encode_int_field(2, 1) + encode_int_field(4, 300)
    mdata2 = encode_int_field(1, 1003) + encode_int_field(2, 1) + encode_int_field(4, 400)
    info_payload = b"".join(
        (
            encode_int_field(1, 2),
            encode_int_field(3, 1254),
            encode_int_field(4, 316),
            encode_bytes_field(10, mdata0),
            encode_bytes_field(10, mdata1),
            encode_bytes_field(10, mdata2),
            encode_int_field(14, 1),
            encode_int_field(17, 10),
        )
    )
    info = decode_arena_info(info_payload)
    assert info.challenge_num == 2
    assert info.season_id == 1254
    assert info.season_score == 316
    assert info.season_open is True
    assert info.season_challenge_num == 10
    assert len(info.opponents) == 3
    assert info.opponents[0].win == 1
    assert info.opponents[1].win == 0
    assert info.opponents[1].opponent_id == 1002

    assert encode_arena_challenge(opponent_id=0) == b""
    assert encode_arena_challenge(opponent_id=2) == encode_int_field(3, 2)

    challenge_payload = encode_int_field(4, 1) + encode_int_field(5, 3)
    challenge = decode_arena_challenge_response(challenge_payload)
    assert challenge.ret == 0
    assert challenge.opponent_id == 1
    assert challenge.challenge_num == 3

    result_payload = b"".join(
        (
            encode_int_field(1, 1),
            encode_int_field(3, 1),
            encode_int_field(4, 350),
            encode_int_field(5, -10),
            encode_int_field(6, 600),
        )
    )
    result = decode_arena_challenge_result(result_payload)
    assert result.win is True
    assert result.opponent_id == 1
    assert result.season_score == 350
    assert result.score_delta == -10
    assert result.best_score == 600

    picks = {
        pick_challenge_candidate(info.opponents, attempted=set()) for _ in range(40)
    }
    assert picks <= {1, 2}
    # 未战胜候选耗尽时回退到已战胜对手，只要还有未尝试下标。
    assert pick_challenge_candidate(info.opponents, attempted={1, 2}) == 0
    assert pick_challenge_candidate(info.opponents, attempted={0, 1, 2}) is None

    session_password = "87654321"
    password_payload = encode_bytes_field(
        1,
        pack1_encode(session_password.encode("utf-8"), SOCKET_PACK_KEY).encode("utf-8"),
    )

    def encrypted(message_id: int, data: bytes = b"") -> bytes:
        return pack1_encode(
            encode_message_header(message_id, data), session_password
        ).encode("utf-8")

    position = encode_int_field(1, 1) + encode_int_field(2, 1)
    slot = encode_int_field(1, 10101) + encode_bytes_field(2, position)
    slot_entry = encode_int_field(1, 0) + encode_bytes_field(2, slot)
    team = encode_bytes_field(1, slot_entry)
    team_entry = encode_int_field(1, 0) + encode_bytes_field(2, team)
    teams = encode_bytes_field(1, team_entry)
    hero = (
        encode_bytes_field(1, encode_int_field(1, 10101))
        + encode_bytes_field(4, teams)
        + encode_int_field(10, 0)
    )
    game_data = encode_int_field(5, 1) + encode_bytes_field(9, hero)

    challenge_ok = encode_int_field(4, 1) + encode_int_field(5, 1)
    result_ok = (
        encode_int_field(1, 0)
        + encode_int_field(3, 1)
        + encode_int_field(4, 300)
        + encode_int_field(5, 0)
    )

    class TestSocket:
        def __init__(self, frames: list[tuple[int, bytes]]) -> None:
            self.frames = frames
            self.binary_frames: list[bytes] = []
            self.text_frames: list[str] = []
            self.closed = False

        def send_binary(self, payload: bytes) -> None:
            self.binary_frames.append(payload)

        def send_text(self, payload: str) -> None:
            self.text_frames.append(payload)

        def recv_message(self, _timeout: float) -> tuple[int, bytes]:
            if not self.frames:
                raise socket.timeout()
            return self.frames.pop(0)

        def close(self) -> None:
            self.closed = True

    from harvest_fief import encode_message_header

    frames = [
        (0x2, encode_message_header(LOGIN_OK_MESSAGE_ID)),
        (0x2, encode_message_header(PACK_PASSWORD_MESSAGE_ID, password_payload)),
        (0x2, encrypted(GAME_DATA_MESSAGE_ID, game_data)),
        (0x2, encrypted(LOGIN_REUNIQUE_MESSAGE_ID)),
        (0x2, encrypted(ARENA_CHALLENGE_MESSAGE_ID, challenge_ok)),
        (0x2, encrypted(ARENA_CHALLENGE_RESULT_MESSAGE_ID, result_ok)),
    ]
    endpoint = GameEndpoint("ws://test.invalid", "game-token", "1", "test")
    logs: list[str] = []
    fake = TestSocket(frames)
    with KnightArenaClient(
        endpoint,
        1.0,
        battle_timeout=2.0,
        state_probe_timeout=0.0,
        socket_factory=lambda _url, _timeout: fake,
        log=logs.append,
        log_server_messages=False,
        websocket_log=None,
        business_log=None,
    ) as client:
        round_result = client.run_round(1)

    assert round_result.challenge.ret == 0
    assert round_result.battle is not None
    assert round_result.battle.win is False
    assert round_result.battle.opponent_id == 1
    sent_ids = [
        decode_message_header(pack1_decode(packet, session_password)).message_id
        for packet in fake.text_frames
    ]
    assert ARENA_CHALLENGE_MESSAGE_ID in sent_ids
    assert fake.closed
    assert any("骑士比武完成" in message for message in logs)
    print("knight_arena self-test passed")


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.self_test:
        _self_test()
        return 0

    tokens = load_tokens(args.token_file)
    endpoint = resolve_game_endpoint(tokens, args)
    log: Callable[[str], None] = print
    with KnightArenaClient(
        endpoint,
        args.timeout,
        battle_timeout=args.battle_timeout,
        log=log,
    ) as client:
        if args.command == "info":
            info = client.get_info()
            print(
                f"赛季={info.season_id} open={info.season_open} "
                f"score={info.season_score} challengenum={info.challenge_num} "
                f"opponents={len(info.opponents)}"
            )
            for opp in info.opponents:
                print(
                    f"  [{opp.index}] id={opp.opponent_id} win={opp.win} "
                    f"score={opp.score} nick={opp.nick or '-'}"
                )
            return 0

        results = client.run_loop(
            rounds=args.rounds,
            refresh_on_exhaustion=not args.no_refresh,
        )
        print(f"完成 {len(results)} 场挑战")
        for item in results:
            battle = item.battle
            if battle is None:
                print(f"  [{item.opponent_index}] 未结算 ret={item.challenge.ret}")
            else:
                print(
                    f"  [{item.opponent_index}] "
                    f"{'胜' if battle.win else '负'} "
                    f"score={battle.season_score}({battle.score_delta:+d})"
                )
        return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (HarvestError, GameLoginKickout, GameSessionClosed) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        raise SystemExit(1) from exc
