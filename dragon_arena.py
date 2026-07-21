#!/usr/bin/env python3
"""龙痕竞技场 WebSocket 客户端。

静态协议来源：当前安装包的 ``decrypted-js/main.js`` 与动态战斗脚本
``decrypted-js/chunks/battle.mjs``。本文件复用
``harvest_fief.py`` 已验证的登录、WebSocket 和 Pack1 封装，覆盖龙痕竞技场
的查询、寻找对手、挑战、战斗控制以及胜利后的抉择结算流程。

用法：
    .venv/bin/python dragon_arena.py info
    .venv/bin/python dragon_arena.py match
    .venv/bin/python dragon_arena.py challenge --index 1
    .venv/bin/python dragon_arena.py resume
    .venv/bin/python dragon_arena.py loop --rounds 10
    .venv/bin/python dragon_arena.py --self-test

普通战斗会先下发 ``Battle_info``，客户端随后必须以当前编队回送
``Battle_C2S_start`` 才会收到战斗开始消息。本文件从登录期 ``Game_data``
提取该编队，并发送三倍速和自动技能控制消息。所有战斗和奖励结果均以服务端消息
为准。

所有 WebSocket 收发消息默认追加写入 ``dragon_arena_websocket.jsonl``，
包括线上原文、解密后的完整消息和业务载荷，便于后续逐帧分析。已识别消息的
业务名称另行追加写入 ``dragon_arena_business.log``。
"""

from __future__ import annotations

import argparse
import base64
import json
import socket
import subprocess
import sys
import tempfile
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Deque, Iterable, Iterator, Sequence

from dragon_arena_business_map import (
    BATTLE_C2S_AUTO_ARTIFACT_SKILL_MESSAGE_ID,
    BATTLE_C2S_AUTO_UNIQUE_SKILL_MESSAGE_ID,
    BATTLE_C2S_FRAME_HASH_VERIFY_RET_MESSAGE_ID,
    BATTLE_C2S_SET_TIMESCALE_MESSAGE_ID,
    BATTLE_C2S_START_MESSAGE_ID,
    BATTLE_INFO_MESSAGE_ID,
    BATTLE_OFFLINE_MESSAGE_ID,
    BATTLE_S2C_AUTO_ARTIFACT_SKILL_MESSAGE_ID,
    BATTLE_S2C_AUTO_UNIQUE_SKILL_MESSAGE_ID,
    BATTLE_S2C_END_MESSAGE_ID,
    BATTLE_S2C_FRAME_BROADCAST_MESSAGE_ID,
    BATTLE_S2C_FRAME_HASH_VERIFY_MESSAGE_ID,
    BATTLE_S2C_SET_TIMESCALE_MESSAGE_ID,
    BATTLE_S2C_START_MESSAGE_ID,
    BATTLE_UNIT_INFO_MESSAGE_ID,
    GAME_DATA_MESSAGE_ID,
    HEARTBEAT_MESSAGE_ID,
    HEARTBEAT_RET_MESSAGE_ID,
    KICKOUT_MESSAGE_ID,
    LOGIN_FAIL_MESSAGE_ID,
    LOGIN_MESSAGE_ID,
    LOGIN_OK_MESSAGE_ID,
    LOGIN_REUNIQUE_MESSAGE_ID,
    MESSAGE_NAMES,
    PACK_PASSWORD_MESSAGE_ID,
    SCARARENA_CHALLENGE_MESSAGE_ID,
    SCARARENA_CHALLENGE_RESULT_MESSAGE_ID,
    SCARARENA_GET_DAILY_REWARD_MESSAGE_ID,
    SCARARENA_INFO_MESSAGE_ID,
    SCARARENA_MATCH_MESSAGE_ID,
    SCARARENA_WIN_CHOICE_MESSAGE_ID,
    STORAGE_ITEM_CHANGE_MESSAGE_ID,
)
from dragon_arena_websocket import (
    DEFAULT_BUSINESS_LOG,
    DEFAULT_SOCKET_FACTORY,
    DEFAULT_WEBSOCKET_LOG,
    DragonArenaWebSocket,
    SocketFactory,
    WebSocketBusinessLogger,
    WebSocketTransport,
)

from harvest_fief import (
    SOCKET_PACK_KEY,
    GameEndpoint,
    HarvestError,
    ItemChange,
    ItemChangeNotify,
    MessageHeader,
    ProtoReader,
    decode_int32,
    decode_item_change_notify,
    decode_message_header,
    decode_pack_password,
    encode_bytes_field,
    encode_int_field,
    encode_login_payload,
    encode_message_header,
    load_tokens,
    pack1_decode,
    pack1_encode,
    resolve_game_endpoint,
)
from harvest_fief import build_parser as build_base_parser
from id_descriptions import business_name, hero_name, item_change_text, item_name


class GameMessageTimeout(HarvestError):
    """A receive timeout that callers may treat as an expected wait state."""


class GameSessionClosed(HarvestError):
    """The game server closed this WebSocket session; retrying it is invalid."""


class GameLoginKickout(HarvestError):
    """The game server rejected the login with a structured Kickout response."""

    def __init__(self, ret: int, message: str = "") -> None:
        self.ret = ret
        self.message = message
        detail = f"，消息={message}" if message else ""
        super().__init__(f"游戏服在登录阶段踢出会话：ret={ret}{detail}")


# ItemChangeNotify.source values from enum ItemChangeSource.
SCARARENA_REWARD_SOURCE = 235
SCARARENA_CHOICE_REWARD_SOURCE = 237
SCARARENA_DAILY_REWARD_SOURCE = 238
SCARARENA_REWARD_SOURCES = frozenset(
    {
        SCARARENA_REWARD_SOURCE,
        SCARARENA_CHOICE_REWARD_SOURCE,
        SCARARENA_DAILY_REWARD_SOURCE,
    }
)

BATTLE_TIMESCALE_X3 = 3
MERCY_CHOICE_ID = 2
LOGIN_KICKOUT_RETRY_DELAY = 3.0
PROJECT_ROOT = Path(__file__).resolve().parent
DEFAULT_JS_CODEC_BRIDGE = PROJECT_ROOT / "dragon_arena_js_codec_bridge.mjs"

BATTLE_ACTIVE_MESSAGE_IDS = frozenset(
    {
        BATTLE_INFO_MESSAGE_ID,
        BATTLE_UNIT_INFO_MESSAGE_ID,
        BATTLE_OFFLINE_MESSAGE_ID,
        BATTLE_S2C_START_MESSAGE_ID,
        BATTLE_S2C_SET_TIMESCALE_MESSAGE_ID,
        BATTLE_S2C_AUTO_UNIQUE_SKILL_MESSAGE_ID,
        BATTLE_S2C_AUTO_ARTIFACT_SKILL_MESSAGE_ID,
        BATTLE_S2C_FRAME_BROADCAST_MESSAGE_ID,
        BATTLE_S2C_FRAME_HASH_VERIFY_MESSAGE_ID,
    }
)
BATTLE_SETTLEMENT_MESSAGE_IDS = frozenset(
    {
        BATTLE_S2C_END_MESSAGE_ID,
        SCARARENA_CHALLENGE_RESULT_MESSAGE_ID,
    }
)


@dataclass(frozen=True)
class DragonArenaOpponent:
    """一位服务端下发的龙痕竞技场候选对手。"""

    robot_id: int
    challenged: bool


@dataclass(frozen=True)
class KickoutResponse:
    ret: int
    message: str


@dataclass(frozen=True)
class DragonArenaInfo:
    """Scararena_info / Game_data.scararenainfo 的可用字段。"""

    level: int
    clearance_time: int
    opponents: tuple[DragonArenaOpponent, ...]
    rewards: tuple[int, ...]
    score: int
    choice_pending: int
    choice_id: int
    buff_choice_id: int
    buff_choice_index: int
    stage_id: int
    daily_reward_num: int
    daily_reward_received: bool


@dataclass(frozen=True)
class DragonArenaMatchResponse:
    ret: int
    opponents: tuple[DragonArenaOpponent, ...]


@dataclass(frozen=True)
class DragonArenaChallengeResponse:
    ret: int
    index: int
    evaluation: int
    quick: bool
    opponent_bytes: int


@dataclass(frozen=True)
class BattleInfo:
    """Compact summary of the Battle_info (18002) handshake packet."""

    battle_id: int
    ret: int
    battle_type: int
    location_id: int
    player_units: int
    enemy_units: int
    enemy_team: tuple["BattleTeamUnit", ...]
    skip_team: bool
    skip_mode: int
    battle_data: bytes
    raw_payload: bytes = b""


@dataclass(frozen=True)
class BattleTeamUnit:
    """``Game_data.hero.teams`` 中一位已验证的当前编队角色。"""

    hero_id: int
    x: int
    y: int


@dataclass(frozen=True)
class BattleSessionState:
    """Login-time state before issuing any new Dragon Arena request."""

    phase: str
    battle_id: int
    message_ids: tuple[int, ...]


class JsCodecBridge:
    """Call the locally extracted protobuf codecs without starting the game."""

    def __init__(
        self,
        script_path: Path,
        *,
        node_binary: str = "node",
        timeout: float = 10.0,
    ) -> None:
        self.script_path = script_path
        self.node_binary = node_binary
        self.timeout = timeout

    def encode_battle_start(
        self,
        battle: BattleInfo,
        team: Sequence[BattleTeamUnit],
    ) -> bytes:
        if not self.script_path.is_file():
            raise HarvestError(f"JS codec bridge 不存在：{self.script_path}")
        if not battle.raw_payload:
            raise HarvestError("Battle_info 原始载荷缺失，无法调用 JS codec")

        request = {
            "id": 1,
            "op": "battle-start-from-info",
            "battleInfo": base64.b64encode(battle.raw_payload).decode("ascii"),
            "team": [
                {"heroId": unit.hero_id, "x": unit.x, "y": unit.y}
                for unit in team
            ],
        }
        try:
            completed = subprocess.run(
                [self.node_binary, str(self.script_path)],
                input=json.dumps(request, separators=(",", ":")) + "\n",
                text=True,
                capture_output=True,
                timeout=self.timeout,
                check=False,
            )
        except FileNotFoundError as exc:
            raise HarvestError(f"找不到 Node.js 可执行文件：{self.node_binary}") from exc
        except subprocess.TimeoutExpired as exc:
            raise HarvestError("JS codec bridge 编码 Battle_C2S_start 超时") from exc
        except OSError as exc:
            raise HarvestError(f"启动 JS codec bridge 失败：{exc}") from exc

        output_lines = [line for line in completed.stdout.splitlines() if line.strip()]
        if completed.returncode != 0 or not output_lines:
            detail = " ".join(completed.stderr.split())[:240]
            suffix = f"：{detail}" if detail else ""
            raise HarvestError(f"JS codec bridge 执行失败{suffix}")
        try:
            response = json.loads(output_lines[-1])
        except json.JSONDecodeError as exc:
            raise HarvestError("JS codec bridge 返回了无效 JSON") from exc
        if response.get("ok") is not True:
            detail = str(response.get("error", "未知错误"))[:240]
            raise HarvestError(f"JS codec bridge 编码失败：{detail}")
        encoded = response.get("result", {}).get("data")
        if not isinstance(encoded, str):
            raise HarvestError("JS codec bridge 未返回 Battle_C2S_start 数据")
        try:
            payload = base64.b64decode(encoded, validate=True)
        except (ValueError, TypeError) as exc:
            raise HarvestError("JS codec bridge 返回了无效 Base64 数据") from exc
        if not payload:
            raise HarvestError("JS codec bridge 返回了空 Battle_C2S_start 载荷")
        return payload


@dataclass(frozen=True)
class GameDataBattleContext:
    """构造 ``Battle_C2S_start`` 所需的登录期本地状态。"""

    server_time_ms: int
    received_monotonic: float
    current_team_id: int
    team: tuple[BattleTeamUnit, ...]


@dataclass(frozen=True)
class BattleStartResponse:
    """``Battle_S2C_start`` 的紧凑握手摘要。"""

    battle_id: int
    ret: int
    server_time_ms: int
    start_param_bytes: int


@dataclass(frozen=True)
class DragonArenaChallengeResult:
    win: bool
    index: int
    clearance: bool
    score: int
    score_delta: int
    choice_pending: int
    choice_id: int
    daily_reward_num: int


@dataclass(frozen=True)
class DragonArenaChoiceResult:
    ret: int
    choice_id: int
    score: int
    score_delta: int
    clearance: bool
    buff_choice_id: int
    buff_choice_index: int
    daily_reward_num: int
    item_changes: tuple[ItemChange, ...]


@dataclass(frozen=True)
class DragonArenaRoundResult:
    index: int
    challenge: DragonArenaChallengeResponse
    battle: DragonArenaChallengeResult | None
    mercy: DragonArenaChoiceResult | None


def _decode_packed_int32(data: bytes) -> tuple[int, ...]:
    reader = ProtoReader(data)
    values: list[int] = []
    while reader.position < len(reader.data):
        values.append(decode_int32(reader.read_varint()))
    return tuple(values)


def _decode_opponent(data: bytes) -> DragonArenaOpponent:
    robot_id = 0
    challenged = False
    for field_number, wire_type, value in ProtoReader(data).fields():
        if field_number == 1 and wire_type == 0:
            robot_id = decode_int32(int(value))
        elif field_number == 2 and wire_type == 0:
            challenged = bool(value)
    return DragonArenaOpponent(robot_id=robot_id, challenged=challenged)


def _first_int_field(data: bytes, field_number: int) -> int | None:
    for number, wire_type, value in ProtoReader(data).fields():
        if number == field_number and wire_type == 0:
            return int(value)
    return None


def _first_bytes_field(data: bytes, field_number: int) -> bytes | None:
    for number, wire_type, value in ProtoReader(data).fields():
        if number == field_number and wire_type == 2:
            return bytes(value)
    return None


def decode_game_data_item_totals(data: bytes) -> dict[int, int]:
    """Decode ``Game_data.storage.items.list`` into current item totals."""

    storage = _first_bytes_field(data, 8)
    if storage is None:
        return {}
    items = _first_bytes_field(storage, 1)
    if items is None:
        return {}

    totals: dict[int, int] = {}
    for field_number, wire_type, value in ProtoReader(items).fields():
        if field_number != 3 or wire_type != 2:
            continue
        item = bytes(value)
        item_id = _first_int_field(item, 1)
        total = _first_int_field(item, 2)
        if item_id is not None and item_id > 0 and total is not None:
            totals[item_id] = total
    return totals


def decode_kickout(data: bytes) -> KickoutResponse:
    ret = 0
    message = ""
    for field_number, wire_type, value in ProtoReader(data).fields():
        if field_number == 1 and wire_type == 0:
            ret = decode_int32(int(value))
        elif field_number == 2 and wire_type == 2:
            try:
                message = bytes(value).decode("utf-8")
            except UnicodeDecodeError as exc:
                raise HarvestError("Kickout.msg 不是 UTF-8 文本") from exc
    return KickoutResponse(ret, message)


def _decode_battle_position(data: bytes) -> tuple[int, int]:
    x = 0
    y = 0
    for field_number, wire_type, value in ProtoReader(data).fields():
        if field_number == 1 and wire_type == 0:
            x = decode_int32(int(value))
        elif field_number == 2 and wire_type == 0:
            y = decode_int32(int(value))
    return x, y


def _decode_battle_unit_position(data: bytes) -> BattleTeamUnit | None:
    """Extract the battle entity id and grid coordinates used by ``eteam``."""

    unit_id = 0
    x = 0
    y = 0
    for field_number, wire_type, value in ProtoReader(data).fields():
        if wire_type != 0:
            continue
        if field_number == 5:
            x = decode_int32(int(value))
        elif field_number == 6:
            y = decode_int32(int(value))
        elif field_number == 10:
            unit_id = decode_int32(int(value))
    if unit_id <= 0:
        return None
    return BattleTeamUnit(hero_id=unit_id, x=x, y=y)


def decode_game_data_battle_context(
    data: bytes,
    *,
    received_monotonic: float | None = None,
) -> GameDataBattleContext:
    """Extract the current normal team from ``Game_data``.

    The layout follows ``St.hero`` -> ``Xo.teams`` -> ``vs.teams`` in the
    generated protocol code.  Dragon Arena is not assigned a dedicated team
    slot by ``TeamModule.battleTypeToTeamType``, so the native client falls
    back to ``hero.currTeam``.
    """

    server_time_ms = 0
    hero_data: bytes | None = None
    for field_number, wire_type, value in ProtoReader(data).fields():
        if field_number == 5 and wire_type == 0:
            server_time_ms = int(value)
        elif field_number == 9 and wire_type == 2:
            hero_data = bytes(value)

    if hero_data is None:
        raise HarvestError("Game_data 缺少 hero 编队数据")

    current_team_id = 0
    teams_data: bytes | None = None
    owned_hero_ids: set[int] = set()
    for field_number, wire_type, value in ProtoReader(hero_data).fields():
        if field_number == 1 and wire_type == 2:
            hero_id = _first_int_field(bytes(value), 1)
            if hero_id is not None:
                owned_hero_ids.add(hero_id)
        elif field_number == 4 and wire_type == 2:
            teams_data = bytes(value)
        elif field_number == 10 and wire_type == 0:
            current_team_id = decode_int32(int(value))

    if teams_data is None:
        raise HarvestError("Game_data.hero 缺少 teams")

    hero_teams: dict[int, bytes] = {}
    for field_number, wire_type, value in ProtoReader(teams_data).fields():
        if field_number != 1 or wire_type != 2:
            continue
        entry = bytes(value)
        team_id = _first_int_field(entry, 1)
        team = _first_bytes_field(entry, 2)
        if team is not None:
            hero_teams[decode_int32(team_id or 0)] = team

    selected_team = hero_teams.get(current_team_id)
    if selected_team is None:
        raise HarvestError(f"Game_data 中不存在当前编队 {current_team_id}")

    slots: dict[int, bytes] = {}
    for field_number, wire_type, value in ProtoReader(selected_team).fields():
        if field_number != 1 or wire_type != 2:
            continue
        entry = bytes(value)
        slot_index = _first_int_field(entry, 1)
        slot = _first_bytes_field(entry, 2)
        if slot is not None:
            slots[decode_int32(slot_index or 0)] = slot

    team: list[BattleTeamUnit] = []
    seen_heroes: set[int] = set()
    for slot_index in range(6):
        slot = slots.get(slot_index)
        if slot is None:
            continue
        hero_id = _first_int_field(slot, 1)
        position = _first_bytes_field(slot, 2)
        if hero_id is None or hero_id <= 0 or position is None:
            continue
        if owned_hero_ids and hero_id not in owned_hero_ids:
            continue
        if hero_id in seen_heroes:
            continue
        x, y = _decode_battle_position(position)
        team.append(BattleTeamUnit(hero_id=hero_id, x=x, y=y))
        seen_heroes.add(hero_id)

    if not team:
        raise HarvestError(f"当前编队 {current_team_id} 没有可用于战斗的角色")

    return GameDataBattleContext(
        server_time_ms=server_time_ms,
        received_monotonic=(
            time.monotonic() if received_monotonic is None else received_monotonic
        ),
        current_team_id=current_team_id,
        team=tuple(team),
    )


def summarize_protobuf_fields(data: bytes, *, limit: int = 6) -> str:
    """Return a bounded field summary without rendering payload bytes."""

    try:
        parts: list[str] = []
        for index, (field_number, wire_type, value) in enumerate(
            ProtoReader(data).fields(), start=1
        ):
            if index > limit:
                parts.append("...")
                break
            if wire_type == 0:
                text = str(int(value))
                parts.append(
                    f"f{field_number}=v:{text if len(text) <= 12 else 'large'}"
                )
            else:
                parts.append(f"f{field_number}=bytes:{len(bytes(value))}")
        return ", ".join(parts) if parts else "空"
    except HarvestError as exc:
        return f"解析失败:{exc}"


def decode_scararena_info(data: bytes) -> DragonArenaInfo:
    """Decode the ``ScararenaInfo`` fields used by the main panel."""

    values = {
        "level": 0,
        "clearance_time": 0,
        "opponents": [],
        "rewards": [],
        "score": 0,
        "choice_pending": 0,
        "choice_id": 0,
        "buff_choice_id": 0,
        "buff_choice_index": 0,
        "stage_id": 0,
        "daily_reward_num": 0,
        "daily_reward_received": False,
    }
    for field_number, wire_type, value in ProtoReader(data).fields():
        if field_number == 1 and wire_type == 0:
            values["level"] = decode_int32(int(value))
        elif field_number == 2 and wire_type == 0:
            values["clearance_time"] = int(value)
        elif field_number == 3 and wire_type == 2:
            values["opponents"].append(_decode_opponent(bytes(value)))
        elif field_number == 4 and wire_type == 0:
            values["rewards"].append(decode_int32(int(value)))
        elif field_number == 4 and wire_type == 2:
            values["rewards"].extend(_decode_packed_int32(bytes(value)))
        elif field_number == 9 and wire_type == 0:
            values["score"] = decode_int32(int(value))
        elif field_number == 10 and wire_type == 0:
            values["choice_pending"] = decode_int32(int(value))
        elif field_number == 11 and wire_type == 0:
            values["choice_id"] = decode_int32(int(value))
        elif field_number == 12 and wire_type == 0:
            values["buff_choice_id"] = decode_int32(int(value))
        elif field_number == 13 and wire_type == 0:
            values["buff_choice_index"] = decode_int32(int(value))
        elif field_number == 15 and wire_type == 0:
            values["stage_id"] = decode_int32(int(value))
        elif field_number == 16 and wire_type == 0:
            values["daily_reward_num"] = decode_int32(int(value))
        elif field_number == 17 and wire_type == 0:
            values["daily_reward_received"] = bool(value)
    return DragonArenaInfo(
        level=values["level"],
        clearance_time=values["clearance_time"],
        opponents=tuple(values["opponents"]),
        rewards=tuple(values["rewards"]),
        score=values["score"],
        choice_pending=values["choice_pending"],
        choice_id=values["choice_id"],
        buff_choice_id=values["buff_choice_id"],
        buff_choice_index=values["buff_choice_index"],
        stage_id=values["stage_id"],
        daily_reward_num=values["daily_reward_num"],
        daily_reward_received=values["daily_reward_received"],
    )


def decode_scararena_match_response(data: bytes) -> DragonArenaMatchResponse:
    ret = 0
    opponents: list[DragonArenaOpponent] = []
    for field_number, wire_type, value in ProtoReader(data).fields():
        if field_number == 1 and wire_type == 0:
            ret = decode_int32(int(value))
        elif field_number == 3 and wire_type == 2:
            opponents.append(_decode_opponent(bytes(value)))
    return DragonArenaMatchResponse(ret=ret, opponents=tuple(opponents))


def encode_scararena_challenge(index: int) -> bytes:
    """Encode ``Scararena_challenge``: candidate index is field 1."""

    if index <= 0:
        raise HarvestError("龙痕竞技场对手序号必须从 1 开始")
    return encode_int_field(1, index)


def decode_scararena_challenge_response(data: bytes) -> DragonArenaChallengeResponse:
    ret = 0
    index = 0
    evaluation = 0
    quick = False
    opponent_bytes = 0
    for field_number, wire_type, value in ProtoReader(data).fields():
        if field_number == 1 and wire_type == 0:
            ret = decode_int32(int(value))
        elif field_number == 2 and wire_type == 0:
            index = decode_int32(int(value))
        elif field_number == 3 and wire_type == 2:
            opponent_bytes = len(bytes(value))
        elif field_number == 4 and wire_type == 0:
            evaluation = decode_int32(int(value))
        elif field_number == 5 and wire_type == 0:
            quick = bool(value)
    return DragonArenaChallengeResponse(
        ret=ret,
        index=index,
        evaluation=evaluation,
        quick=quick,
        opponent_bytes=opponent_bytes,
    )


def decode_battle_info(data: bytes) -> BattleInfo:
    """Decode only the handshake fields needed for compact battle diagnostics."""

    values = {
        "battle_id": 0,
        "ret": 0,
        "battle_type": 0,
        "location_id": 0,
        "player_units": 0,
        "enemy_units": 0,
        "enemy_team": [],
        "skip_team": False,
        "skip_mode": 0,
        "battle_data": b"",
        "raw_payload": data,
    }
    battle_data: bytes | None = None
    for field_number, wire_type, value in ProtoReader(data).fields():
        if field_number == 1 and wire_type == 0:
            values["battle_id"] = decode_int32(int(value))
        elif field_number == 2 and wire_type == 0:
            values["ret"] = decode_int32(int(value))
        elif field_number == 3 and wire_type == 0:
            values["battle_type"] = decode_int32(int(value))
        elif field_number == 4 and wire_type == 0:
            values["location_id"] = decode_int32(int(value))
        elif field_number == 5 and wire_type == 2:
            battle_data = bytes(value)

    if battle_data is not None:
        values["battle_data"] = battle_data

    if battle_data is not None:
        for field_number, wire_type, value in ProtoReader(battle_data).fields():
            if field_number == 1 and wire_type == 2:
                values["player_units"] += 1
            elif field_number == 2 and wire_type == 2:
                values["enemy_units"] += 1
                enemy = _decode_battle_unit_position(bytes(value))
                if enemy is not None:
                    values["enemy_team"].append(enemy)
            elif field_number == 4 and wire_type == 0:
                values["skip_team"] = bool(value)
            elif field_number == 13 and wire_type == 0:
                values["skip_mode"] = decode_int32(int(value))

    values["enemy_team"] = tuple(values["enemy_team"])
    return BattleInfo(**values)


def decode_battle_start_response(data: bytes) -> BattleStartResponse:
    """Decode the server response to ``Battle_C2S_start`` (18012)."""

    values = {
        "battle_id": 0,
        "ret": 0,
        "server_time_ms": 0,
        "start_param_bytes": 0,
    }
    for field_number, wire_type, value in ProtoReader(data).fields():
        if field_number == 1 and wire_type == 0:
            values["battle_id"] = decode_int32(int(value))
        elif field_number == 2 and wire_type == 0:
            values["ret"] = decode_int32(int(value))
        elif field_number == 3 and wire_type == 0:
            values["server_time_ms"] = int(value)
        elif field_number == 4 and wire_type == 2:
            values["start_param_bytes"] = len(bytes(value))
    return BattleStartResponse(**values)


def decode_scararena_challenge_result(data: bytes) -> DragonArenaChallengeResult:
    values = {
        "win": False,
        "index": 0,
        "clearance": False,
        "score": 0,
        "score_delta": 0,
        "choice_pending": 0,
        "choice_id": 0,
        "daily_reward_num": 0,
    }
    for field_number, wire_type, value in ProtoReader(data).fields():
        if field_number == 1 and wire_type == 0:
            values["win"] = bool(value)
        elif field_number == 2 and wire_type == 0:
            values["index"] = decode_int32(int(value))
        elif field_number == 3 and wire_type == 0:
            values["clearance"] = bool(value)
        elif field_number == 4 and wire_type == 0:
            values["score"] = decode_int32(int(value))
        elif field_number == 5 and wire_type == 0:
            values["score_delta"] = decode_int32(int(value))
        elif field_number == 6 and wire_type == 0:
            values["choice_pending"] = decode_int32(int(value))
        elif field_number == 7 and wire_type == 0:
            values["choice_id"] = decode_int32(int(value))
        elif field_number == 8 and wire_type == 0:
            values["daily_reward_num"] = decode_int32(int(value))
    return DragonArenaChallengeResult(**values)


def encode_scararena_win_choice(choice_id: int) -> bytes:
    """Encode ``Scararena_winchoice``: selected choice is field 1."""

    if choice_id <= 0:
        raise HarvestError("龙痕竞技场抉择 ID 必须为正整数")
    return encode_int_field(1, choice_id)


def decode_scararena_win_choice_response(
    data: bytes, item_changes: Iterable[ItemChange] = ()
) -> DragonArenaChoiceResult:
    values = {
        "ret": 0,
        "choice_id": 0,
        "score": 0,
        "score_delta": 0,
        "clearance": False,
        "buff_choice_id": 0,
        "buff_choice_index": 0,
        "daily_reward_num": 0,
    }
    for field_number, wire_type, value in ProtoReader(data).fields():
        if field_number == 1 and wire_type == 0:
            values["ret"] = decode_int32(int(value))
        elif field_number == 2 and wire_type == 0:
            values["choice_id"] = decode_int32(int(value))
        elif field_number == 3 and wire_type == 0:
            values["score"] = decode_int32(int(value))
        elif field_number == 4 and wire_type == 0:
            values["score_delta"] = decode_int32(int(value))
        elif field_number == 5 and wire_type == 0:
            values["clearance"] = bool(value)
        elif field_number == 6 and wire_type == 0:
            values["buff_choice_id"] = decode_int32(int(value))
        elif field_number == 7 and wire_type == 0:
            values["buff_choice_index"] = decode_int32(int(value))
        elif field_number == 9 and wire_type == 0:
            values["daily_reward_num"] = decode_int32(int(value))
    return DragonArenaChoiceResult(
        **values,
        item_changes=tuple(item_changes),
    )


def encode_battle_timescale(timescale: int = BATTLE_TIMESCALE_X3) -> bytes:
    """Encode ``Battle_C2S_setTimescale``: ``timescale`` is field 1."""

    if timescale not in (1, 2, 3):
        raise HarvestError("战斗速度必须是 1、2 或 3")
    return encode_int_field(1, timescale)


def encode_battle_auto(enabled: bool = True) -> bytes:
    """Encode C2S automatic unique/artifact skill requests: ``auto`` is field 1."""

    return encode_int_field(1, int(enabled)) if enabled else b""


def encode_battle_c2s_start(
    battle: BattleInfo,
    team: Sequence[BattleTeamUnit],
) -> bytes:
    """Encode the production ``Battle_C2S_start`` request (bundle export ``Vdo``)."""

    if battle.battle_id <= 0:
        raise HarvestError("Battle_info 缺少有效战斗 ID")
    if not team:
        raise HarvestError("Battle_C2S_start 不能使用空编队")
    if not battle.enemy_team:
        raise HarvestError("Battle_info 缺少敌方站位，无法构造 Battle_C2S_start")

    payload = encode_int_field(1, battle.battle_id)
    for unit in team:
        position = encode_int_field(1, unit.x) + encode_int_field(2, unit.y)
        entry = encode_int_field(1, unit.hero_id) + encode_bytes_field(2, position)
        payload += encode_bytes_field(2, entry)

    for unit in battle.enemy_team:
        position = encode_int_field(1, unit.x) + encode_int_field(2, unit.y)
        entry = encode_int_field(1, unit.hero_id) + encode_bytes_field(2, position)
        payload += encode_bytes_field(3, entry)

    # ``BattleExtra`` is field 4 in the production request. The debug-only C7x
    # request instead puts battle data in field 2 and must not be sent here.
    extra = encode_int_field(1, 1) + encode_int_field(2, BATTLE_TIMESCALE_X3)
    payload += encode_bytes_field(4, extra)
    return payload


class DragonArenaClient:
    """保持一个游戏服会话并执行龙痕竞技场协议流程。"""

    def __init__(
        self,
        endpoint: GameEndpoint,
        timeout: float,
        *,
        battle_timeout: float = 180.0,
        battle_start_codec: str = "js",
        js_codec_bridge: Path = DEFAULT_JS_CODEC_BRIDGE,
        node_binary: str = "node",
        dragon_coin_item_id: int | None = None,
        log_server_messages: bool = True,
        websocket_log: Path | None = None,
        business_log: Path | None = None,
        state_probe_timeout: float = 0.35,
        socket_factory: SocketFactory = DEFAULT_SOCKET_FACTORY,
        log: Callable[[str], None] = print,
    ) -> None:
        if battle_start_codec not in {"js", "python"}:
            raise HarvestError("battle_start_codec 必须是 js 或 python")
        if state_probe_timeout < 0:
            raise HarvestError("state_probe_timeout 不能为负数")
        if dragon_coin_item_id is not None and dragon_coin_item_id <= 0:
            raise HarvestError("dragon_coin_item_id 必须为正整数")
        self.endpoint = endpoint
        self.timeout = timeout
        self.battle_timeout = battle_timeout
        self.battle_start_codec = battle_start_codec
        self.log_server_messages = log_server_messages
        self.state_probe_timeout = state_probe_timeout
        self._js_codec_bridge = (
            JsCodecBridge(js_codec_bridge, node_binary=node_binary)
            if battle_start_codec == "js"
            else None
        )
        self.dragon_coin_item_id = dragon_coin_item_id
        self.log = log
        self.websocket_log = websocket_log
        self.business_log = business_log
        self.websocket = DragonArenaWebSocket(
            self.endpoint.url,
            self.timeout,
            websocket_log=self.websocket_log,
            business_log=self.business_log,
            socket_factory=socket_factory,
            output=self.log,
        )
        self.business_logger = self.websocket.business_logger
        self._queued_headers: Deque[MessageHeader] = deque()
        self._game_data_payload: bytes | None = None
        self._game_data_context: GameDataBattleContext | None = None
        self._item_totals: dict[int, int] = {}
        self._arena_settlement_changes: list[tuple[int, ItemChange]] = []
        self._initial_battle_state: BattleSessionState | None = None

    def __enter__(self) -> "DragonArenaClient":
        try:
            self.login()
        except Exception:
            self.close()
            raise
        return self

    def __exit__(self, _exc_type: object, _exc: object, _traceback: object) -> None:
        self.close()

    @property
    def socket(self) -> WebSocketTransport | None:
        """兼容旧调用；实际传输由 ``DragonArenaWebSocket`` 管理。"""

        return self.websocket.transport

    @socket.setter
    def socket(self, value: WebSocketTransport | None) -> None:
        self.websocket.transport = value

    @property
    def password(self) -> str | None:
        return self.websocket.password

    @password.setter
    def password(self, value: str | None) -> None:
        self.websocket.password = value

    def close(self) -> None:
        self.websocket.close()

    def _send_message(self, message_id: int, data: bytes = b"", *, encrypted: bool) -> None:
        self.websocket.send_message(message_id, data, encrypted=encrypted)
        if message_id not in {HEARTBEAT_MESSAGE_ID, HEARTBEAT_RET_MESSAGE_ID}:
            self.log(f"[发送] {business_name(message_id)}，载荷={len(data)} 字节")

    def _decode_frame(self, opcode: int, payload: bytes) -> MessageHeader:
        return self.websocket.decode_frame(opcode, payload)

    def _handle_login_message(self, header: MessageHeader) -> bool:
        if header.message_id == PACK_PASSWORD_MESSAGE_ID:
            encrypted_password = decode_pack_password(header.data)
            try:
                self.password = pack1_decode(encrypted_password, SOCKET_PACK_KEY).decode("utf-8")
            except UnicodeDecodeError as exc:
                raise HarvestError("游戏服会话密码不是 UTF-8 文本") from exc
            return False
        if header.message_id == HEARTBEAT_MESSAGE_ID:
            self._send_message(
                HEARTBEAT_RET_MESSAGE_ID,
                b"",
                encrypted=self.password is not None,
            )
            return False
        if header.message_id == LOGIN_FAIL_MESSAGE_ID:
            raise HarvestError("游戏服 Login 失败")
        if header.message_id == KICKOUT_MESSAGE_ID:
            kickout = decode_kickout(header.data)
            raise GameLoginKickout(kickout.ret, kickout.message)
        return header.message_id == LOGIN_REUNIQUE_MESSAGE_ID

    def _remember_header(self, header: MessageHeader) -> None:
        if header.message_id == GAME_DATA_MESSAGE_ID:
            self._game_data_payload = header.data
            self._item_totals = decode_game_data_item_totals(header.data)
            try:
                self._game_data_context = decode_game_data_battle_context(header.data)
            except HarvestError as exc:
                self._game_data_context = None
                self.log(f"[登录] Game_data 编队解析失败：{exc}")
            else:
                context = self._game_data_context
                self.log(
                    "[登录] Game_data："
                    f"当前编队={context.current_team_id}，"
                    f"可用角色={len(context.team)}，"
                    f"服务端时间={context.server_time_ms or '未提供'}。"
                )
        elif header.message_id == STORAGE_ITEM_CHANGE_MESSAGE_ID:
            notice = decode_item_change_notify(header.data)
            for change in notice.items:
                self._item_totals[change.item_id] = change.total
                if notice.source in SCARARENA_REWARD_SOURCES:
                    self._arena_settlement_changes.append((notice.source, change))

    def _begin_arena_settlement(self) -> None:
        self._arena_settlement_changes.clear()

    def _log_current_dragon_coin(self) -> None:
        item_id = self.dragon_coin_item_id
        if item_id is None:
            choice_item_ids = {
                change.item_id
                for source, change in self._arena_settlement_changes
                if source == SCARARENA_CHOICE_REWARD_SOURCE and change.item_id > 0
            }
            if len(choice_item_ids) == 1:
                item_id = next(iter(choice_item_ids))
            else:
                arena_item_ids = {
                    change.item_id
                    for source, change in self._arena_settlement_changes
                    if source in {SCARARENA_REWARD_SOURCE, SCARARENA_CHOICE_REWARD_SOURCE}
                    and change.item_id > 0
                }
                if len(arena_item_ids) == 1:
                    item_id = next(iter(arena_item_ids))
            if item_id is not None:
                self.dragon_coin_item_id = item_id

        if item_id is None:
            self.log(
                "[结算] 当前龙痕币数量：未识别"
                "（请使用 --dragon-coin-id 指定龙痕币物品）。"
            )
        else:
            total = self._item_totals.get(item_id, 0)
            self.log(f"[结算] 当前{item_name(item_id)}数量：{total}。")
        self._arena_settlement_changes.clear()

    def _collect_post_settlement_item_changes(self) -> None:
        """Allow a trailing storage notification to update the settled balance."""

        deadline = time.monotonic() + min(0.25, self.timeout)
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            try:
                header = self._next_header(remaining)
            except GameMessageTimeout:
                return
            if header.message_id == STORAGE_ITEM_CHANGE_MESSAGE_ID:
                continue
            self._queued_headers.appendleft(header)
            return

    @staticmethod
    def _message_label(message_id: int) -> str:
        return MESSAGE_NAMES.get(message_id, "")

    @staticmethod
    def _battle_id_from_header(header: MessageHeader) -> int:
        try:
            if header.message_id == BATTLE_INFO_MESSAGE_ID:
                return decode_battle_info(header.data).battle_id
            if header.message_id == BATTLE_S2C_START_MESSAGE_ID:
                return decode_battle_start_response(header.data).battle_id
        except HarvestError:
            pass
        return 0

    def inspect_initial_battle_state(self) -> BattleSessionState:
        """Inspect cached and immediately following login packets once.

        Received packets are restored to the queue in their original order so
        the later operation-specific state machine can consume them normally.
        """

        if self._initial_battle_state is not None:
            return self._initial_battle_state

        observed: list[MessageHeader] = []
        while self._queued_headers:
            observed.append(self._next_header(0.0))

        deadline = time.monotonic() + self.state_probe_timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                observed.append(self._next_header(remaining))
            except GameMessageTimeout:
                break

        for header in reversed(observed):
            self._queued_headers.appendleft(header)

        active = [
            header for header in observed if header.message_id in BATTLE_ACTIVE_MESSAGE_IDS
        ]
        settlement = [
            header
            for header in observed
            if header.message_id in BATTLE_SETTLEMENT_MESSAGE_IDS
        ]
        relevant = active or settlement
        if active:
            state = BattleSessionState(
                phase="战斗中",
                battle_id=next(
                    (
                        battle_id
                        for header in reversed(active)
                        if (battle_id := self._battle_id_from_header(header)) > 0
                    ),
                    0,
                ),
                message_ids=tuple(header.message_id for header in relevant),
            )
        elif settlement:
            state = BattleSessionState(
                phase="待结算",
                battle_id=0,
                message_ids=tuple(header.message_id for header in relevant),
            )
        else:
            state = BattleSessionState("空闲", 0, ())

        self._initial_battle_state = state
        if state.phase == "空闲":
            self.log("[状态] 登录后战斗状态校验：空闲。")
        else:
            labels = "、".join(business_name(message_id) for message_id in state.message_ids)
            self.log(
                f"[状态] 登录后战斗状态校验：{state.phase}，"
                f"依据={labels}。"
            )
        return state

    def _ensure_arena_idle(self) -> None:
        state = self.inspect_initial_battle_state()
        if state.phase == "空闲":
            return
        message_ids = "、".join(business_name(message_id) for message_id in state.message_ids)
        raise HarvestError(
            f"登录后检测到{state.phase}（{message_ids}），"
            "请先执行 resume 续接遗留战斗"
        )

    def resume_pending_battle(
        self,
        *,
        mercy_choice_id: int = MERCY_CHOICE_ID,
    ) -> DragonArenaRoundResult | None:
        """Continue the battle or settlement packets replayed during login."""

        state = self.inspect_initial_battle_state()
        if state.phase == "空闲":
            return None

        challenge: DragonArenaChallengeResponse | None = None
        for header in self._queued_headers:
            if header.message_id != SCARARENA_CHALLENGE_MESSAGE_ID:
                continue
            candidate = decode_scararena_challenge_response(header.data)
            if candidate.ret == 0:
                challenge = candidate
                break

        quick = challenge.quick if challenge is not None else state.phase == "待结算"
        index = challenge.index if challenge is not None else 0
        self.log(
            f"[恢复] 续接登录期{state.phase}："
            f"序号={index or '待回包确认'}。"
        )
        battle = self.await_challenge_result(quick=quick)
        index = battle.index or index
        if challenge is None:
            challenge = DragonArenaChallengeResponse(0, index, 0, quick, 0)
        elif challenge.index == 0 and index:
            challenge = DragonArenaChallengeResponse(
                challenge.ret,
                index,
                challenge.evaluation,
                challenge.quick,
                challenge.opponent_bytes,
            )

        mercy: DragonArenaChoiceResult | None = None
        if battle.win and battle.choice_pending:
            mercy = self.choose_mercy(mercy_choice_id)
            if mercy.ret != 0:
                self._log_current_dragon_coin()
                raise HarvestError(f"遗留战斗胜利抉择结算返回 ret={mercy.ret}")
            self.log(
                "[恢复] 胜利抉择结算完成："
                f"积分={mercy.score}（{mercy.score_delta:+d}），"
                f"龙痕币日计数={mercy.daily_reward_num}。"
            )
        else:
            self._collect_post_settlement_item_changes()
        self._log_current_dragon_coin()
        self._initial_battle_state = BattleSessionState("空闲", 0, ())
        self.log(f"[恢复] 遗留战斗已完成：序号={index or '未提供'}。")
        return DragonArenaRoundResult(index, challenge, battle, mercy)

    def _server_message_summary(self, header: MessageHeader) -> str:
        """Render known server messages without serializing their raw payload."""

        try:
            if header.message_id == PACK_PASSWORD_MESSAGE_ID:
                return "会话密钥已隐藏"
            if header.message_id == LOGIN_OK_MESSAGE_ID:
                return "登录成功"
            if header.message_id == LOGIN_REUNIQUE_MESSAGE_ID:
                return "登录完成"
            if header.message_id == LOGIN_FAIL_MESSAGE_ID:
                return "登录失败"
            if header.message_id == KICKOUT_MESSAGE_ID:
                kickout = decode_kickout(header.data)
                message = f"，消息={kickout.message}" if kickout.message else ""
                return f"ret={kickout.ret}{message}"
            if header.message_id == GAME_DATA_MESSAGE_ID:
                return f"字段={summarize_protobuf_fields(header.data)}"
            if header.message_id == SCARARENA_INFO_MESSAGE_ID:
                info = decode_scararena_info(header.data)
                return (
                    f"等级={info.level}，积分={info.score}，候选={len(info.opponents)}，"
                    f"待抉择={info.choice_pending}，龙痕币日计数={info.daily_reward_num}"
                )
            if header.message_id == SCARARENA_MATCH_MESSAGE_ID:
                match = decode_scararena_match_response(header.data)
                return f"ret={match.ret}，候选={len(match.opponents)}"
            if header.message_id == SCARARENA_CHALLENGE_MESSAGE_ID:
                challenge = decode_scararena_challenge_response(header.data)
                return (
                    f"ret={challenge.ret}，序号={challenge.index}，"
                    f"快速={int(challenge.quick)}，对手数据={challenge.opponent_bytes} 字节"
                )
            if header.message_id == BATTLE_INFO_MESSAGE_ID:
                battle = decode_battle_info(header.data)
                return (
                    f"ret={battle.ret}，玩家单位={battle.player_units}，"
                    f"敌方单位={battle.enemy_units}，skipTeam={int(battle.skip_team)}，"
                    f"skip={battle.skip_mode}"
                )
            if header.message_id == BATTLE_S2C_START_MESSAGE_ID:
                start = decode_battle_start_response(header.data)
                return (
                    f"ret={start.ret}，"
                    f"服务器时间={start.server_time_ms or '未提供'}，"
                    f"启动参数={start.start_param_bytes} 字节"
                )
            if header.message_id == BATTLE_S2C_END_MESSAGE_ID:
                round_number = _first_int_field(header.data, 1)
                win = _first_int_field(header.data, 2)
                result_code = _first_int_field(header.data, 10)
                return (
                    f"回合={round_number if round_number is not None else '未提供'}，"
                    f"胜利={win if win is not None else '未提供'}，"
                    f"结果={result_code if result_code is not None else '未提供'}"
                )
            if header.message_id == SCARARENA_CHALLENGE_RESULT_MESSAGE_ID:
                result = decode_scararena_challenge_result(header.data)
                return (
                    f"序号={result.index}，胜利={int(result.win)}，积分={result.score}"
                    f"（{result.score_delta:+d}），待抉择={result.choice_pending}，"
                    f"龙痕币日计数={result.daily_reward_num}"
                )
            if header.message_id == SCARARENA_WIN_CHOICE_MESSAGE_ID:
                choice = decode_scararena_win_choice_response(header.data)
                return (
                    f"ret={choice.ret}，选项={choice.choice_id}，积分={choice.score}"
                    f"（{choice.score_delta:+d}），龙痕币日计数={choice.daily_reward_num}"
                )
            if header.message_id == STORAGE_ITEM_CHANGE_MESSAGE_ID:
                notice = decode_item_change_notify(header.data)
                shown = ", ".join(
                    f"{item_name(item.item_id)}:{item.delta:+d}"
                    for item in notice.items[:6]
                )
                if len(notice.items) > 6:
                    shown += ", ..."
                return f"来源={notice.source}，物品={shown or '无'}"
        except HarvestError as exc:
            return f"字段={summarize_protobuf_fields(header.data)}，解码失败={exc}"
        return f"字段={summarize_protobuf_fields(header.data)}"

    def _log_server_message(self, header: MessageHeader) -> None:
        if not self.log_server_messages:
            return
        if header.message_id in {
            HEARTBEAT_MESSAGE_ID,
            BATTLE_S2C_FRAME_BROADCAST_MESSAGE_ID,
            BATTLE_S2C_FRAME_HASH_VERIFY_MESSAGE_ID,
        }:
            return
        self.log(
            f"[服务端] {business_name(header.message_id)}，载荷={len(header.data)} 字节，"
            f"内容={self._server_message_summary(header)}。"
        )

    def login(self) -> None:
        if self.websocket.connected:
            return
        self.websocket.connect()
        self._send_message(
            LOGIN_MESSAGE_ID,
            encode_login_payload(self.endpoint.game_token),
            encrypted=False,
        )
        deadline = time.monotonic() + self.timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise HarvestError("等待游戏服登录完成超时")
            try:
                header = self.websocket.receive_header(remaining)
            except socket.timeout as exc:
                raise HarvestError("等待游戏服登录完成超时") from exc
            except OSError as exc:
                raise HarvestError(f"读取游戏服登录报文失败：{exc}") from exc
            self._log_server_message(header)
            if self._handle_login_message(header):
                break
            if header.message_id not in {
                LOGIN_OK_MESSAGE_ID,
                PACK_PASSWORD_MESSAGE_ID,
                HEARTBEAT_MESSAGE_ID,
                LOGIN_FAIL_MESSAGE_ID,
            }:
                self._remember_header(header)
                self._queued_headers.append(header)
        # Native client delays cached business messages by 100 ms after Login_reunique.
        time.sleep(0.1)

    def _next_header(self, timeout: float) -> MessageHeader:
        if self._queued_headers:
            return self._queued_headers.popleft()
        try:
            header = self.websocket.receive_header(timeout)
        except socket.timeout as exc:
            raise GameMessageTimeout("等待游戏服消息超时") from exc
        except OSError as exc:
            raise HarvestError(f"读取游戏服报文失败：{exc}") from exc
        except HarvestError as exc:
            if "关闭了 WebSocket" in str(exc):
                raise GameSessionClosed(str(exc)) from exc
            raise
        self._remember_header(header)
        if header.message_id == HEARTBEAT_MESSAGE_ID:
            self._send_message(HEARTBEAT_RET_MESSAGE_ID, b"", encrypted=True)
            return self._next_header(timeout)
        self._log_server_message(header)
        return header

    def _wait_for(self, message_ids: set[int], timeout: float) -> Iterator[MessageHeader]:
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                names = "、".join(business_name(message_id) for message_id in sorted(message_ids))
                raise HarvestError(f"等待消息 {names} 超时")
            header = self._next_header(remaining)
            yield header
            if header.message_id in message_ids:
                return

    def _log_background_message(self, header: MessageHeader) -> None:
        """Messages are logged once when they are received in ``_next_header``."""

    def get_info(self) -> DragonArenaInfo:
        self._ensure_arena_idle()
        self._send_message(SCARARENA_INFO_MESSAGE_ID, encrypted=True)
        for header in self._wait_for({SCARARENA_INFO_MESSAGE_ID}, self.timeout):
            if header.message_id == SCARARENA_INFO_MESSAGE_ID:
                return decode_scararena_info(header.data)
            self._log_background_message(header)
        raise AssertionError("_wait_for 未返回竞技场信息")

    def match(self) -> DragonArenaMatchResponse:
        self._ensure_arena_idle()
        self._send_message(SCARARENA_MATCH_MESSAGE_ID, encrypted=True)
        for header in self._wait_for({SCARARENA_MATCH_MESSAGE_ID}, self.timeout):
            if header.message_id == SCARARENA_MATCH_MESSAGE_ID:
                return decode_scararena_match_response(header.data)
            self._log_background_message(header)
        raise AssertionError("_wait_for 未返回匹配结果")

    def challenge(self, index: int) -> DragonArenaChallengeResponse:
        self._ensure_arena_idle()
        self._send_message(
            SCARARENA_CHALLENGE_MESSAGE_ID,
            encode_scararena_challenge(index),
            encrypted=True,
        )
        for header in self._wait_for({SCARARENA_CHALLENGE_MESSAGE_ID}, self.timeout):
            if header.message_id == SCARARENA_CHALLENGE_MESSAGE_ID:
                return decode_scararena_challenge_response(header.data)
            if header.message_id == BATTLE_INFO_MESSAGE_ID:
                battle = decode_battle_info(header.data)
                if battle.ret != 0:
                    raise HarvestError(f"Battle_info 返回 ret={battle.ret}")
                # 服务端可能会等待 18010 后才下发延迟的 21104 响应；将该包
                # 放回队首，使 await_challenge_result 立即发送战斗握手。
                self._queued_headers.appendleft(header)
                self.log(
                    "[挑战] Battle_info 早于挑战响应到达，"
                    "已转入普通战斗握手。"
                )
                return DragonArenaChallengeResponse(
                    ret=0,
                    index=index,
                    evaluation=0,
                    quick=False,
                    opponent_bytes=0,
                )
            self._log_background_message(header)
        raise AssertionError("_wait_for 未返回挑战响应")

    def configure_battle(self) -> None:
        """战斗开始后同步三倍速和两类自动技能开关。"""

        self._send_message(
            BATTLE_C2S_SET_TIMESCALE_MESSAGE_ID,
            encode_battle_timescale(BATTLE_TIMESCALE_X3),
            encrypted=True,
        )
        self._send_message(
            BATTLE_C2S_AUTO_UNIQUE_SKILL_MESSAGE_ID,
            encode_battle_auto(True),
            encrypted=True,
        )
        self._send_message(
            BATTLE_C2S_AUTO_ARTIFACT_SKILL_MESSAGE_ID,
            encode_battle_auto(True),
            encrypted=True,
        )
        self.log("[战斗] 已发送 x3、自动角色技能和自动圣物技能。")

    def start_battle(self, battle: BattleInfo) -> None:
        """Send the required 18010 handshake after a successful Battle_info."""

        context = self._game_data_context
        if context is None:
            raise HarvestError("本会话没有可用的 Game_data 编队，无法构造 Battle_C2S_start")
        if self._js_codec_bridge is not None:
            payload = self._js_codec_bridge.encode_battle_start(
                battle,
                context.team,
            )
            codec_name = "JS codec"
        else:
            payload = encode_battle_c2s_start(
                battle,
                context.team,
            )
            codec_name = "Python codec"
        self._send_message(BATTLE_C2S_START_MESSAGE_ID, payload, encrypted=True)
        positions = ", ".join(
            f"{hero_name(unit.hero_id)}@({unit.x},{unit.y})" for unit in context.team
        )
        self.log(
            "[战斗] 已发送 Battle_C2S_start："
            f"角色={len(context.team)} [{positions}]，敌方站位={len(battle.enemy_team)}，"
            f"编码={codec_name}，"
            f"载荷={len(payload)} 字节，字段={summarize_protobuf_fields(payload)}。"
        )

    def await_challenge_result(self, *, quick: bool) -> DragonArenaChallengeResult:
        """等待挑战结算，并持续输出小体积的协议进度摘要。"""

        configured = False
        battle_end_seen = False
        battle_start_sent = False
        frame_count = 0
        hash_verify_count = 0
        state = "等待竞技场结算" if quick else "等待 Battle_info"
        started_at = time.monotonic()
        deadline = time.monotonic() + self.battle_timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise HarvestError("等待龙痕竞技场战斗结算超时")
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
                    f"敌方单位={info.enemy_units}，skipTeam={int(info.skip_team)}，"
                    f"skip={info.skip_mode}。"
                )
                if info.ret != 0:
                    raise HarvestError(f"Battle_info 返回 ret={info.ret}")
                if battle_start_sent:
                    self.log(
                        "[战斗] 重复收到 Battle_info，已保留先前的 "
                        "Battle_C2S_start。"
                    )
                else:
                    self.start_battle(info)
                    battle_start_sent = True
                    state = "已发送 Battle_C2S_start，等待 Battle_S2C_start"
                continue
            if header.message_id == SCARARENA_CHALLENGE_MESSAGE_ID:
                challenge = decode_scararena_challenge_response(header.data)
                if challenge.ret != 0:
                    raise HarvestError(f"挑战响应延迟返回 ret={challenge.ret}")
                response_mode = "快速结算" if challenge.quick else "普通战斗"
                self.log(
                    "[挑战] 收到延迟挑战响应："
                    f"序号={challenge.index or '未提供'}，模式={response_mode}，"
                    f"对手数据={challenge.opponent_bytes} 字节。"
                )
                continue
            if header.message_id == BATTLE_S2C_START_MESSAGE_ID:
                start = decode_battle_start_response(header.data)
                if start.ret != 0:
                    raise HarvestError(f"Battle_S2C_start 返回 ret={start.ret}")
                state = "战斗中"
                self.log(
                    "[战斗] 服务端开始战斗："
                    f"服务器时间={start.server_time_ms or '未提供'}，"
                    f"启动参数={start.start_param_bytes} 字节。"
                )
                if not configured:
                    self.configure_battle()
                    configured = True
                continue
            if header.message_id == BATTLE_S2C_END_MESSAGE_ID:
                battle_end_seen = True
                state = "等待竞技场结算"
                round_number = _first_int_field(header.data, 1)
                win = _first_int_field(header.data, 2)
                result_code = _first_int_field(header.data, 10)
                self.log(
                    "[战斗] 服务端结束战斗："
                    f"回合={round_number if round_number is not None else '未提供'}，"
                    f"胜利={win if win is not None else '未提供'}，"
                    f"结果={result_code if result_code is not None else '未提供'}，"
                    f"载荷={len(header.data)} 字节。"
                )
                continue
            if header.message_id == BATTLE_S2C_FRAME_BROADCAST_MESSAGE_ID:
                frame_count += 1
                if frame_count == 1 or frame_count % 50 == 0:
                    tick = _first_int_field(header.data, 1)
                    self.log(
                        f"[战斗] 收到服务端战斗帧：累计 {frame_count} 帧，"
                        f"tick={tick if tick is not None else '未提供'}，"
                        f"最新载荷 {len(header.data)} 字节。"
                    )
                continue
            if header.message_id == BATTLE_S2C_FRAME_HASH_VERIFY_MESSAGE_ID:
                hash_verify_count += 1
                if hash_verify_count == 1 or hash_verify_count % 10 == 0:
                    tick = _first_int_field(header.data, 1)
                    self.log(
                        f"[战斗] 收到状态帧哈希校验：累计 {hash_verify_count} 次，"
                        f"tick={tick if tick is not None else '未提供'}，"
                        f"载荷 {len(header.data)} 字节。"
                    )
                continue
            if header.message_id == SCARARENA_CHALLENGE_RESULT_MESSAGE_ID:
                result = decode_scararena_challenge_result(header.data)
                mode = "快速结算" if quick else "普通战斗"
                self.log(
                    "[战斗] "
                    f"{mode}完成：序号={result.index}，"
                    f"胜利={'是' if result.win else '否'}，"
                    f"积分={result.score}（{result.score_delta:+d}），"
                    f"龙痕币日计数={result.daily_reward_num}。"
                )
                if not quick and not battle_end_seen:
                    self.log("[战斗] 已收到竞技场结算，未等待额外结束包。")
                return result
            if battle_start_sent and header.message_id == BATTLE_C2S_START_MESSAGE_ID:
                self.log("[战斗] 收到 Battle_C2S_start 回显。")
            self._log_background_message(header)

    def choose_mercy(self, choice_id: int = MERCY_CHOICE_ID) -> DragonArenaChoiceResult:
        """提交胜利抉择，并采集该结算窗口内的竞技场物品变动。"""

        self._send_message(
            SCARARENA_WIN_CHOICE_MESSAGE_ID,
            encode_scararena_win_choice(choice_id),
            encrypted=True,
        )
        response_data: bytes | None = None
        item_changes: list[ItemChange] = []
        deadline = time.monotonic() + self.timeout
        quiet_deadline: float | None = None
        while True:
            current_deadline = quiet_deadline if quiet_deadline is not None else deadline
            remaining = current_deadline - time.monotonic()
            if remaining <= 0:
                if response_data is None:
                    raise HarvestError("等待龙痕竞技场胜利抉择结算超时")
                return decode_scararena_win_choice_response(response_data, item_changes)
            try:
                header = self._next_header(remaining)
            except GameSessionClosed:
                raise
            except HarvestError:
                if response_data is not None:
                    return decode_scararena_win_choice_response(response_data, item_changes)
                raise
            if header.message_id == SCARARENA_WIN_CHOICE_MESSAGE_ID:
                response_data = header.data
                quiet_deadline = min(deadline, time.monotonic() + 1.0)
                continue
            if header.message_id == STORAGE_ITEM_CHANGE_MESSAGE_ID:
                notice: ItemChangeNotify = decode_item_change_notify(header.data)
                if notice.source in SCARARENA_REWARD_SOURCES:
                    item_changes.extend(notice.items)
                    if response_data is not None:
                        quiet_deadline = min(deadline, time.monotonic() + 0.2)
                continue
            self._log_background_message(header)

    def run_round(
        self,
        index: int,
        *,
        mercy_choice_id: int = MERCY_CHOICE_ID,
    ) -> DragonArenaRoundResult:
        """挑战一个候选对手；胜利且出现抉择时提交指定选项。"""

        self._begin_arena_settlement()
        challenge = self.challenge(index)
        if challenge.ret != 0:
            self._arena_settlement_changes.clear()
            self.log(f"[挑战] 序号={index}，服务端 ret={challenge.ret}，继续下一轮。")
            return DragonArenaRoundResult(index, challenge, None, None)

        opponent_data = (
            f"{challenge.opponent_bytes} 字节"
            if challenge.opponent_bytes
            else "Battle_info 已先到达"
        )
        self.log(
            "[挑战] "
            f"序号={challenge.index or index}，"
            f"模式={'快速结算' if challenge.quick else '普通战斗'}，"
            f"对手数据={opponent_data}。"
        )
        try:
            battle = self.await_challenge_result(quick=challenge.quick)
        except GameSessionClosed:
            self.log(f"[战斗] 序号={index}，游戏服连接已关闭，停止竞技场循环。")
            raise
        except HarvestError as exc:
            self.log(f"[战斗] 序号={index}，本轮未完成：{exc}，继续下一轮。")
            return DragonArenaRoundResult(index, challenge, None, None)
        mercy: DragonArenaChoiceResult | None = None
        if battle.win and battle.choice_pending:
            try:
                mercy = self.choose_mercy(mercy_choice_id)
            except GameSessionClosed:
                self.log(f"[胜利抉择] 序号={index}，游戏服连接已关闭，停止竞技场循环。")
                raise
            except HarvestError as exc:
                self.log(f"[胜利抉择] 序号={index}，结算未完成：{exc}，继续下一轮。")
                self._log_current_dragon_coin()
                return DragonArenaRoundResult(index, challenge, battle, None)
            status = "成功" if mercy.ret == 0 else f"ret={mercy.ret}"
            self.log(
                "[胜利抉择] "
                f"{status}，积分={mercy.score}（{mercy.score_delta:+d}），"
                f"龙痕币日计数={mercy.daily_reward_num}。"
            )
            for change in mercy.item_changes:
                self.log(f"[龙痕币/奖励] {item_change_text(change.item_id, change.delta)}。")
        elif not battle.win:
            self.log("[挑战] 本场失败，按循环规则继续下一位对手。")
        if not (battle.win and battle.choice_pending):
            self._collect_post_settlement_item_changes()
        self._log_current_dragon_coin()
        return DragonArenaRoundResult(index, challenge, battle, mercy)

    def run_loop(
        self,
        *,
        rounds: int,
        mercy_choice_id: int = MERCY_CHOICE_ID,
        refresh_on_exhaustion: bool = True,
    ) -> tuple[DragonArenaRoundResult, ...]:
        """按未挑战顺序循环；``rounds=0`` 表示直到当前循环没有可挑战对手。"""

        if rounds < 0:
            raise HarvestError("循环次数不能为负数")
        results: list[DragonArenaRoundResult] = []
        attempted: set[int] = set()
        while rounds == 0 or len(results) < rounds:
            info = self.get_info()
            candidates = [
                index
                for index, opponent in enumerate(info.opponents, start=1)
                if not opponent.challenged and index not in attempted
            ]
            if not candidates and refresh_on_exhaustion and not attempted:
                matched = self.match()
                self.log(
                    f"[匹配] ret={matched.ret}，候选数={len(matched.opponents)}。"
                )
                if matched.ret == 0:
                    continue
            if not candidates:
                break
            index = candidates[0]
            result = self.run_round(index, mercy_choice_id=mercy_choice_id)
            results.append(result)
            attempted.add(index)
            # 服务端可能在匹配/获胜后替换候选列表；下一轮会以最新 info 决定序号。
            if result.battle is not None and result.battle.win:
                attempted.clear()
        return tuple(results)


def _format_info(info: DragonArenaInfo, dragon_coin_item_id: int | None) -> None:
    challenged = sum(opponent.challenged for opponent in info.opponents)
    print(
        "龙痕竞技场："
        f"等级={info.level}，积分={info.score}，"
        f"候选={len(info.opponents)}，已挑战={challenged}，"
        f"龙痕币日计数={info.daily_reward_num}。"
    )
    if dragon_coin_item_id is not None:
        print(
            "当前龙痕币总量会在每次竞技场结算后输出；"
            f"已筛选{item_name(dragon_coin_item_id)}。"
        )


def run_self_tests() -> None:
    assert encode_scararena_challenge(3) == b"\x08\x03"
    assert encode_scararena_win_choice(MERCY_CHOICE_ID) == b"\x08\x02"
    assert encode_battle_timescale() == b"\x08\x03"
    assert encode_battle_auto(True) == b"\x08\x01"
    assert encode_battle_auto(False) == b""

    opponent = encode_int_field(1, 7001) + encode_int_field(2, 1)
    info_payload = b"".join(
        (
            encode_int_field(1, 19),
            encode_int_field(2, 123),
            encode_bytes_field(3, opponent),
            encode_int_field(4, 10),
            encode_int_field(4, 20),
            encode_int_field(9, 140),
            encode_int_field(10, 1),
            encode_int_field(11, 31),
            encode_int_field(16, 240),
        )
    )
    info = decode_scararena_info(info_payload)
    assert info.level == 19
    assert info.score == 140
    assert info.rewards == (10, 20)
    assert info.opponents == (DragonArenaOpponent(7001, True),)
    assert info.daily_reward_num == 240

    challenge_payload = b"".join(
        (
            encode_int_field(1, 0),
            encode_int_field(2, 1),
            encode_bytes_field(3, b"opponent"),
            encode_int_field(4, 88),
            encode_int_field(5, 1),
        )
    )
    challenge = decode_scararena_challenge_response(challenge_payload)
    assert challenge == DragonArenaChallengeResponse(0, 1, 88, True, 8)

    def battle_unit(unit_id: int, x: int, y: int) -> bytes:
        return b"".join(
            (
                encode_int_field(1, 204601),
                encode_int_field(5, x),
                encode_int_field(6, y),
                encode_int_field(10, unit_id),
            )
        )

    enemy_unit = battle_unit(7, 2, 6)
    battle_data = b"".join(
        (
            encode_bytes_field(1, battle_unit(1001, 1, 2)),
            encode_bytes_field(1, battle_unit(1002, 3, 4)),
            encode_bytes_field(2, enemy_unit),
            encode_int_field(4, 1),
            encode_int_field(13, 2),
        )
    )
    battle_info_payload = b"".join(
        (
            encode_int_field(1, 91),
            encode_int_field(3, 10),
            encode_int_field(4, 7),
            encode_bytes_field(5, battle_data),
        )
    )
    battle_info = decode_battle_info(battle_info_payload)
    assert battle_info == BattleInfo(
        battle_id=91,
        ret=0,
        battle_type=10,
        location_id=7,
        player_units=2,
        enemy_units=1,
        enemy_team=(BattleTeamUnit(7, 2, 6),),
        skip_team=True,
        skip_mode=2,
        battle_data=battle_data,
        raw_payload=battle_info_payload,
    )

    def game_data_with_current_team() -> bytes:
        def team_slot(slot_index: int, hero_id: int, x: int, y: int) -> bytes:
            position = encode_int_field(1, x) + encode_int_field(2, y)
            slot = encode_int_field(1, hero_id) + encode_bytes_field(2, position)
            return encode_int_field(1, slot_index) + encode_bytes_field(2, slot)

        hero_team = b"".join(
            (
                encode_bytes_field(1, team_slot(0, 1001, 1, 2)),
                encode_bytes_field(1, team_slot(1, 1002, 3, 4)),
            )
        )
        team_entry = encode_int_field(1, 2) + encode_bytes_field(2, hero_team)
        teams = encode_bytes_field(1, team_entry)
        hero_data = b"".join(
            (
                encode_bytes_field(1, encode_int_field(1, 1001)),
                encode_bytes_field(1, encode_int_field(1, 1002)),
                encode_bytes_field(4, teams),
                encode_int_field(10, 2),
            )
        )
        dragon_coin = encode_int_field(1, 9901) + encode_int_field(2, 3344)
        items = encode_bytes_field(3, dragon_coin)
        storage = encode_bytes_field(1, items)
        return b"".join(
            (
                encode_int_field(5, 1_700_000_000_000),
                encode_bytes_field(8, storage),
                encode_bytes_field(9, hero_data),
            )
        )

    game_data_payload = game_data_with_current_team()
    assert decode_game_data_item_totals(game_data_payload) == {9901: 3344}
    game_context = decode_game_data_battle_context(
        game_data_payload,
        received_monotonic=100.0,
    )
    assert game_context.current_team_id == 2
    assert game_context.team == (
        BattleTeamUnit(1001, 1, 2),
        BattleTeamUnit(1002, 3, 4),
    )

    start_payload = encode_battle_c2s_start(
        battle_info,
        game_context.team,
    )
    assert _first_int_field(start_payload, 1) == 91
    assert _first_int_field(_first_bytes_field(start_payload, 4) or b"", 1) == 1
    assert _first_int_field(_first_bytes_field(start_payload, 4) or b"", 2) == 3
    start_team: list[BattleTeamUnit] = []
    for field_number, wire_type, value in ProtoReader(start_payload).fields():
        if field_number != 2 or wire_type != 2:
            continue
        entry = bytes(value)
        hero_id = _first_int_field(entry, 1)
        position = _first_bytes_field(entry, 2)
        assert hero_id is not None and position is not None
        x, y = _decode_battle_position(position)
        start_team.append(BattleTeamUnit(hero_id, x, y))
    assert tuple(start_team) == game_context.team
    start_enemy: list[BattleTeamUnit] = []
    for field_number, wire_type, value in ProtoReader(start_payload).fields():
        if field_number != 3 or wire_type != 2:
            continue
        entry = bytes(value)
        unit_id = _first_int_field(entry, 1)
        position = _first_bytes_field(entry, 2)
        assert unit_id is not None and position is not None
        x, y = _decode_battle_position(position)
        start_enemy.append(BattleTeamUnit(unit_id, x, y))
    assert tuple(start_enemy) == battle_info.enemy_team
    assert summarize_protobuf_fields(b"\x08\x01" + b"\x12\x04data") == "f1=v:1, f2=bytes:4"

    js_battle_info = decode_battle_info(battle_info_payload)
    js_start_payload = JsCodecBridge(DEFAULT_JS_CODEC_BRIDGE).encode_battle_start(
        js_battle_info,
        game_context.team,
    )
    assert _first_int_field(js_start_payload, 1) == 91
    assert _first_bytes_field(js_start_payload, 2) is not None
    assert _first_bytes_field(js_start_payload, 3) is not None
    assert _first_bytes_field(js_start_payload, 4) is not None
    assert js_start_payload == start_payload

    result_payload = b"".join(
        (
            encode_int_field(1, 1),
            encode_int_field(2, 1),
            encode_int_field(4, 140),
            encode_int_field(5, 90),
            encode_int_field(6, 1),
            encode_int_field(7, 31),
            encode_int_field(8, 240),
        )
    )
    result = decode_scararena_challenge_result(result_payload)
    assert result.win
    assert result.score == 140
    assert result.choice_pending == 1

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

    session_password = "87654321"
    password_payload = encode_bytes_field(
        1,
        pack1_encode(session_password.encode("utf-8"), SOCKET_PACK_KEY).encode("utf-8"),
    )
    choice_response = b"".join(
        (
            encode_int_field(1, 0),
            encode_int_field(2, MERCY_CHOICE_ID),
            encode_int_field(3, 90),
            encode_int_field(4, -50),
            encode_int_field(9, 320),
        )
    )
    item = encode_int_field(1, 9901) + encode_int_field(2, 80) + encode_int_field(3, 3424)
    item_change = encode_int_field(1, SCARARENA_CHOICE_REWARD_SOURCE) + encode_bytes_field(2, item)
    battle_reward_change = (
        encode_int_field(1, SCARARENA_REWARD_SOURCE) + encode_bytes_field(2, item)
    )
    encrypted = lambda message_id, data=b"": pack1_encode(
        encode_message_header(message_id, data), session_password
    ).encode("utf-8")
    endpoint = GameEndpoint("ws://test.invalid", "game-token", "1", "test")

    heartbeat_packet = encrypted(HEARTBEAT_MESSAGE_ID)
    traffic_socket = TestSocket(
        [
            (0x2, encode_message_header(LOGIN_OK_MESSAGE_ID)),
            (0x2, encode_message_header(PACK_PASSWORD_MESSAGE_ID, password_payload)),
            (0x2, heartbeat_packet),
            (0x2, encrypted(LOGIN_REUNIQUE_MESSAGE_ID)),
        ]
    )
    with tempfile.TemporaryDirectory() as temporary_directory:
        traffic_log = Path(temporary_directory) / "logs" / "websocket.jsonl"
        business_log = Path(temporary_directory) / "logs" / "business.log"
        with DragonArenaClient(
            endpoint,
            1.0,
            websocket_log=traffic_log,
            business_log=business_log,
            socket_factory=lambda _url, _timeout: traffic_socket,
            log=lambda _message: None,
        ):
            pass

        malformed_client = DragonArenaClient(
            endpoint,
            1.0,
            websocket_log=traffic_log,
            log=lambda _message: None,
        )
        try:
            malformed_client._decode_frame(0x2, b"\x00\x00")
        except HarvestError:
            pass
        else:
            raise AssertionError("异常 WebSocket 消息未触发协议错误")
        finally:
            malformed_client.close()
        traffic_records = [
            json.loads(line)
            for line in traffic_log.read_text(encoding="utf-8").splitlines()
        ]
        login_business_records = [
            json.loads(line) for line in business_log.read_text(encoding="utf-8").splitlines()
        ]

        mapping_log = Path(temporary_directory) / "logs" / "mapping.log"
        with WebSocketBusinessLogger(
            path=mapping_log,
            session_id="mapping-test",
            output=lambda _message: None,
        ) as business_logger:
            business_logger.log_send(SCARARENA_INFO_MESSAGE_ID, 0)
            business_logger.log_receive(SCARARENA_INFO_MESSAGE_ID, len(info_payload))
            business_logger.log_send(SCARARENA_MATCH_MESSAGE_ID, 0)
            business_logger.log_receive(SCARARENA_MATCH_MESSAGE_ID, len(opponent))
            business_logger.log_receive(LOGIN_FAIL_MESSAGE_ID, 0)
            business_logger.log_send(99999, 0)
        mapping_records = [
            json.loads(line) for line in mapping_log.read_text(encoding="utf-8").splitlines()
        ]

    successful_traffic_records = traffic_records[:6]
    assert [record["message_id"] for record in successful_traffic_records] == [
        LOGIN_MESSAGE_ID,
        LOGIN_OK_MESSAGE_ID,
        PACK_PASSWORD_MESSAGE_ID,
        HEARTBEAT_MESSAGE_ID,
        HEARTBEAT_RET_MESSAGE_ID,
        LOGIN_REUNIQUE_MESSAGE_ID,
    ]
    assert [record["direction"] for record in successful_traffic_records] == [
        "outbound",
        "inbound",
        "inbound",
        "inbound",
        "outbound",
        "inbound",
    ]
    assert successful_traffic_records[0]["encrypted"] is False
    assert successful_traffic_records[3]["encrypted"] is True
    assert (
        base64.b64decode(successful_traffic_records[3]["wire_payload_base64"])
        == heartbeat_packet
    )
    assert decode_message_header(
        base64.b64decode(successful_traffic_records[3]["decoded_packet_base64"])
    ).message_id == HEARTBEAT_MESSAGE_ID
    assert successful_traffic_records[3]["message_payload_base64"] == ""
    malformed_record = traffic_records[6]
    assert malformed_record["message_id"] is None
    assert malformed_record["wire_payload_base64"] == "AAA="
    assert malformed_record["decode_error"]
    assert {record["details"]["message_id"] for record in login_business_records} >= {10010, 10012, 10022}
    assert len(mapping_records) == 6
    assert mapping_records[0]["operation"] == "send" and mapping_records[0]["details"]["message_name"] == "查询竞技场"
    assert mapping_records[1]["operation"] == "receive" and mapping_records[1]["details"]["message_name"] == "查询竞技场"
    assert mapping_records[2]["operation"] == "send" and mapping_records[2]["details"]["message_name"] == "请求刷新对手"
    assert mapping_records[3]["operation"] == "receive" and mapping_records[3]["details"]["message_name"] == "请求刷新对手"
    assert mapping_records[4]["operation"] == "receive" and mapping_records[4]["details"]["message_name"] == "登录失败"
    assert mapping_records[5]["operation"] == "send" and mapping_records[5]["details"]["message_name"] == "未知业务消息（ID 99999）"
    assert all("再次" not in record["details"]["message_name"] for record in mapping_records)

    kickout_payload = encode_int_field(1, 2) + encode_bytes_field(
        2, "会话已切换".encode("utf-8")
    )
    assert decode_kickout(kickout_payload) == KickoutResponse(2, "会话已切换")
    kickout_socket = TestSocket(
        [(0x2, encode_message_header(KICKOUT_MESSAGE_ID, kickout_payload))]
    )
    try:
        with DragonArenaClient(
            endpoint,
            1.0,
            socket_factory=lambda _url, _timeout: kickout_socket,
            log=lambda _message: None,
        ):
            raise AssertionError("Kickout 登录响应未中止登录")
    except GameLoginKickout as exc:
        assert exc.ret == 2
        assert exc.message == "会话已切换"
    assert kickout_socket.closed

    # The default client path must invoke the extracted JS codec before it
    # encrypts and sends the 18010 handshake.
    js_codec_socket = TestSocket([])
    js_codec_client = DragonArenaClient(
        endpoint,
        1.0,
        socket_factory=lambda _url, _timeout: js_codec_socket,
        log=lambda _message: None,
    )
    js_codec_client.socket = js_codec_socket
    js_codec_client.password = session_password
    js_codec_client._game_data_context = game_context
    js_codec_client.start_battle(js_battle_info)
    js_start_header = decode_message_header(
        pack1_decode(js_codec_socket.text_frames[0], session_password)
    )
    assert js_start_header.message_id == BATTLE_C2S_START_MESSAGE_ID
    assert _first_int_field(js_start_header.data, 1) == 91
    assert _first_bytes_field(js_start_header.data, 2) is not None
    assert _first_bytes_field(js_start_header.data, 3) is not None
    assert _first_bytes_field(js_start_header.data, 4) is not None
    js_codec_client.close()

    fake_socket = TestSocket(
        [
            (0x2, encode_message_header(PACK_PASSWORD_MESSAGE_ID, password_payload)),
            (0x2, encrypted(GAME_DATA_MESSAGE_ID, game_data_payload)),
            (0x2, encrypted(LOGIN_REUNIQUE_MESSAGE_ID)),
            (0x2, encrypted(SCARARENA_CHALLENGE_MESSAGE_ID, challenge_payload)),
            (0x2, encrypted(SCARARENA_CHALLENGE_RESULT_MESSAGE_ID, result_payload)),
            (0x2, encrypted(SCARARENA_WIN_CHOICE_MESSAGE_ID, choice_response)),
            (0x2, encrypted(STORAGE_ITEM_CHANGE_MESSAGE_ID, item_change)),
        ]
    )
    logs: list[str] = []
    with DragonArenaClient(
        endpoint,
        1.0,
        dragon_coin_item_id=9901,
        state_probe_timeout=0.0,
        socket_factory=lambda _url, _timeout: fake_socket,
        log=logs.append,
    ) as client:
        round_result = client.run_round(1)

    assert round_result.battle == result
    assert round_result.mercy is not None
    assert round_result.mercy.score == 90
    assert round_result.mercy.item_changes == (ItemChange(9901, 80, 3424),)
    assert client._game_data_payload == game_data_payload
    assert client._game_data_context is not None
    assert fake_socket.closed
    assert any("[服务端] 竞技场挑战结算" in message and "胜利=1" in message for message in logs)
    assert any("[服务端] 竞技场胜利抉择" in message and "选项=2" in message for message in logs)
    coin_logs = [message for message in logs if f"当前{item_name(9901)}数量" in message]
    assert coin_logs == [f"[结算] 当前{item_name(9901)}数量：3424。"]
    assert any("[服务端] 获取会话密码" in message and "会话密钥已隐藏" in message for message in logs)
    assert all(session_password not in message for message in logs)
    assert decode_message_header(fake_socket.binary_frames[0]).message_id == LOGIN_MESSAGE_ID
    sent_ids = [
        decode_message_header(pack1_decode(packet, session_password)).message_id
        for packet in fake_socket.text_frames
    ]
    assert sent_ids == [SCARARENA_CHALLENGE_MESSAGE_ID, SCARARENA_WIN_CHOICE_MESSAGE_ID]

    # Non-quick battles first send the fully populated 18010 handshake, then
    # send controls after the 18012 response.
    non_quick_payload = encode_int_field(2, 2)
    non_quick_result_payload = encode_int_field(2, 2) + encode_int_field(4, 200)
    battle_start_response = b"".join(
        (
            encode_int_field(1, 91),
            encode_int_field(3, 1_700_000_000_200),
            encode_bytes_field(4, b"start-param"),
        )
    )
    battle_end_payload = b"".join(
        (
            encode_int_field(1, 1),
            encode_int_field(2, 1),
            encode_int_field(10, 0),
        )
    )
    battle_socket = TestSocket(
        [
            (0x2, encode_message_header(PACK_PASSWORD_MESSAGE_ID, password_payload)),
            (0x2, encrypted(GAME_DATA_MESSAGE_ID, game_data_payload)),
            (0x2, encrypted(LOGIN_REUNIQUE_MESSAGE_ID)),
            (0x2, encrypted(BATTLE_INFO_MESSAGE_ID, battle_info_payload)),
            (0x2, encrypted(SCARARENA_CHALLENGE_MESSAGE_ID, non_quick_payload)),
            (0x2, encrypted(BATTLE_S2C_START_MESSAGE_ID, battle_start_response)),
            (0x2, encrypted(BATTLE_S2C_END_MESSAGE_ID, battle_end_payload)),
            (0x2, encrypted(SCARARENA_CHALLENGE_RESULT_MESSAGE_ID, non_quick_result_payload)),
            (0x2, encrypted(STORAGE_ITEM_CHANGE_MESSAGE_ID, battle_reward_change)),
        ]
    )
    battle_logs: list[str] = []
    with DragonArenaClient(
        endpoint,
        1.0,
        battle_start_codec="python",
        state_probe_timeout=0.0,
        socket_factory=lambda _url, _timeout: battle_socket,
        log=battle_logs.append,
    ) as client:
        non_quick = client.run_round(2)
    assert non_quick.battle is not None
    sent_headers = [
        decode_message_header(pack1_decode(packet, session_password))
        for packet in battle_socket.text_frames
    ]
    assert [header.message_id for header in sent_headers] == [
        SCARARENA_CHALLENGE_MESSAGE_ID,
        BATTLE_C2S_START_MESSAGE_ID,
        BATTLE_C2S_SET_TIMESCALE_MESSAGE_ID,
        BATTLE_C2S_AUTO_UNIQUE_SKILL_MESSAGE_ID,
        BATTLE_C2S_AUTO_ARTIFACT_SKILL_MESSAGE_ID,
    ]
    assert _first_int_field(sent_headers[1].data, 1) == 91
    assert _first_bytes_field(sent_headers[1].data, 2) is not None
    assert _first_bytes_field(sent_headers[1].data, 3) is not None
    assert _first_bytes_field(sent_headers[1].data, 4) is not None
    assert decode_battle_start_response(battle_start_response) == BattleStartResponse(
        91,
        0,
        1_700_000_000_200,
        len(b"start-param"),
    )
    assert [message for message in battle_logs if f"当前{item_name(9901)}数量" in message] == [
        f"[结算] 当前{item_name(9901)}数量：3424。"
    ]

    # Login may replay an unfinished Battle_info before Login_reunique. The
    # resume path must consume it and send the production start request without
    # issuing a second arena challenge.
    resume_flow_socket = TestSocket(
        [
            (0x2, encode_message_header(PACK_PASSWORD_MESSAGE_ID, password_payload)),
            (0x2, encrypted(GAME_DATA_MESSAGE_ID, game_data_payload)),
            (0x2, encrypted(BATTLE_INFO_MESSAGE_ID, battle_info_payload)),
            (0x2, encrypted(SCARARENA_CHALLENGE_MESSAGE_ID, non_quick_payload)),
            (0x2, encrypted(LOGIN_REUNIQUE_MESSAGE_ID)),
            (0x2, encrypted(BATTLE_S2C_START_MESSAGE_ID, battle_start_response)),
            (0x2, encrypted(BATTLE_S2C_END_MESSAGE_ID, battle_end_payload)),
            (0x2, encrypted(SCARARENA_CHALLENGE_RESULT_MESSAGE_ID, non_quick_result_payload)),
        ]
    )
    with DragonArenaClient(
        endpoint,
        1.0,
        battle_start_codec="python",
        state_probe_timeout=0.0,
        socket_factory=lambda _url, _timeout: resume_flow_socket,
        log=lambda _message: None,
    ) as client:
        resumed = client.resume_pending_battle()
    assert resumed is not None
    assert resumed.index == 2
    assert resumed.battle is not None
    resume_sent_headers = [
        decode_message_header(pack1_decode(packet, session_password))
        for packet in resume_flow_socket.text_frames
    ]
    assert [header.message_id for header in resume_sent_headers] == [
        BATTLE_C2S_START_MESSAGE_ID,
        BATTLE_C2S_SET_TIMESCALE_MESSAGE_ID,
        BATTLE_C2S_AUTO_UNIQUE_SKILL_MESSAGE_ID,
        BATTLE_C2S_AUTO_ARTIFACT_SKILL_MESSAGE_ID,
    ]

    # Some normal battles defer (or omit) the 21104 acknowledgement until
    # after the 18010 handshake.  A leading Battle_info must therefore start
    # the battle instead of blocking in challenge().
    battle_first_socket = TestSocket(
        [
            (0x2, encode_message_header(PACK_PASSWORD_MESSAGE_ID, password_payload)),
            (0x2, encrypted(GAME_DATA_MESSAGE_ID, game_data_payload)),
            (0x2, encrypted(LOGIN_REUNIQUE_MESSAGE_ID)),
            (0x2, encrypted(BATTLE_INFO_MESSAGE_ID, battle_info_payload)),
            (0x2, encrypted(BATTLE_S2C_START_MESSAGE_ID, battle_start_response)),
            (0x2, encrypted(BATTLE_S2C_END_MESSAGE_ID, battle_end_payload)),
            (0x2, encrypted(SCARARENA_CHALLENGE_RESULT_MESSAGE_ID, non_quick_result_payload)),
        ]
    )
    with DragonArenaClient(
        endpoint,
        1.0,
        battle_start_codec="python",
        state_probe_timeout=0.0,
        socket_factory=lambda _url, _timeout: battle_first_socket,
        log=lambda _message: None,
    ) as client:
        battle_first = client.run_round(3)
    assert battle_first.challenge == DragonArenaChallengeResponse(0, 3, 0, False, 0)
    assert battle_first.battle is not None
    battle_first_sent_ids = [
        decode_message_header(pack1_decode(packet, session_password)).message_id
        for packet in battle_first_socket.text_frames
    ]
    assert battle_first_sent_ids == [
        SCARARENA_CHALLENGE_MESSAGE_ID,
        BATTLE_C2S_START_MESSAGE_ID,
        BATTLE_C2S_SET_TIMESCALE_MESSAGE_ID,
        BATTLE_C2S_AUTO_UNIQUE_SKILL_MESSAGE_ID,
        BATTLE_C2S_AUTO_ARTIFACT_SKILL_MESSAGE_ID,
    ]

    class ClosingSocket(TestSocket):
        def recv_message(self, timeout: float) -> tuple[int, bytes]:
            if self.frames:
                return super().recv_message(timeout)
            raise HarvestError("游戏服关闭了 WebSocket 会话（关闭码 1008）")

    # A close after 18010 is terminal for this socket.  It must not be folded
    # into the ordinary "continue next round" error path.
    closing_socket = ClosingSocket(
        [
            (0x2, encode_message_header(PACK_PASSWORD_MESSAGE_ID, password_payload)),
            (0x2, encrypted(GAME_DATA_MESSAGE_ID, game_data_payload)),
            (0x2, encrypted(LOGIN_REUNIQUE_MESSAGE_ID)),
            (0x2, encrypted(SCARARENA_CHALLENGE_MESSAGE_ID, non_quick_payload)),
            (0x2, encrypted(BATTLE_INFO_MESSAGE_ID, battle_info_payload)),
        ]
    )
    closing_logs: list[str] = []
    try:
        with DragonArenaClient(
            endpoint,
            1.0,
            battle_start_codec="python",
            state_probe_timeout=0.0,
            socket_factory=lambda _url, _timeout: closing_socket,
            log=closing_logs.append,
        ) as client:
            client.run_round(2)
    except GameSessionClosed as exc:
        assert "关闭了 WebSocket" in str(exc)
    else:
        raise AssertionError("关闭的游戏服会话未中止竞技场循环")
    closing_sent_ids = [
        decode_message_header(pack1_decode(packet, session_password)).message_id
        for packet in closing_socket.text_frames
    ]
    assert closing_sent_ids == [
        SCARARENA_CHALLENGE_MESSAGE_ID,
        BATTLE_C2S_START_MESSAGE_ID,
    ]
    assert any("游戏服连接已关闭，停止竞技场循环" in message for message in closing_logs)

    # A battle that arrives immediately after Login_reunique is not an idle
    # arena session.  The preflight must leave it queued and avoid 21100.
    resume_socket = TestSocket(
        [
            (0x2, encode_message_header(PACK_PASSWORD_MESSAGE_ID, password_payload)),
            (0x2, encrypted(GAME_DATA_MESSAGE_ID, game_data_payload)),
            (0x2, encrypted(LOGIN_REUNIQUE_MESSAGE_ID)),
            (0x2, encrypted(BATTLE_INFO_MESSAGE_ID, battle_info_payload)),
        ]
    )
    resume_logs: list[str] = []
    with DragonArenaClient(
        endpoint,
        1.0,
        state_probe_timeout=0.1,
        socket_factory=lambda _url, _timeout: resume_socket,
        log=resume_logs.append,
    ) as client:
        try:
            client.get_info()
        except HarvestError as exc:
            assert "登录后检测到战斗中" in str(exc)
        else:
            raise AssertionError("登录后战斗状态未阻止新的竞技场信息请求")
    assert not resume_socket.text_frames
    assert any("登录后战斗状态校验：战斗中" in message for message in resume_logs)


def build_parser() -> argparse.ArgumentParser:
    parser = build_base_parser()
    parser.prog = "dragon_arena.py"
    parser.description = __doc__
    parser.add_argument(
        "command",
        nargs="?",
        choices=("info", "match", "challenge", "resume", "loop"),
        default="info",
        help="执行的龙痕竞技场操作；resume 用于续接登录期遗留战斗，默认 info。",
    )
    parser.add_argument("--index", type=int, help="challenge 命令使用的候选对手序号。")
    parser.add_argument(
        "--rounds",
        type=int,
        default=10,
        help="loop 的最大轮数；设为 0 时直到当前候选列表无可挑战对手。",
    )
    parser.add_argument(
        "--battle-timeout",
        type=float,
        default=180.0,
        help="单场普通战斗等待服务端结算的最长秒数。",
    )
    parser.add_argument(
        "--state-probe-timeout",
        type=float,
        default=0.35,
        help="登录后扫描遗留战斗回包的秒数；检测到战斗中或待结算时不发送新竞技场请求。",
    )
    parser.add_argument(
        "--battle-start-codec",
        choices=("js", "python"),
        default="js",
        help="18010 编码器；默认 js 复用反编译客户端 codec，python 仅用于诊断对照。",
    )
    parser.add_argument(
        "--js-codec-bridge",
        type=Path,
        default=DEFAULT_JS_CODEC_BRIDGE,
        help="本地 JS protobuf bridge 路径。",
    )
    parser.add_argument(
        "--node-binary",
        default="node",
        help="运行 JS codec bridge 的 Node.js 命令。",
    )
    parser.add_argument(
        "--mercy-choice-id",
        type=int,
        default=MERCY_CHOICE_ID,
        help="仁慈选项 ID，当前客户端默认是 2。",
    )
    parser.add_argument(
        "--dragon-coin-id",
        type=int,
        help="龙痕币物品；未指定时从本轮唯一的竞技场奖励变动自动识别。",
    )
    parser.add_argument(
        "--no-server-log",
        action="store_true",
        help="关闭终端中的服务端回包摘要；不影响完整 WebSocket 文件日志。",
    )
    parser.add_argument(
        "--websocket-log",
        type=Path,
        default=DEFAULT_WEBSOCKET_LOG,
        help=(
            "完整 WebSocket 收发日志路径；以 JSON Lines 追加写入线上原文、"
            "解密消息和业务载荷。"
        ),
    )
    parser.add_argument(
        "--business-log",
        type=Path,
        default=DEFAULT_BUSINESS_LOG,
        help="WebSocket 消息 ID 对应的可读业务操作日志路径。",
    )
    parser.add_argument(
        "--no-refresh-on-exhaustion",
        action="store_true",
        help="候选列表耗尽时不发送寻找对手消息。",
    )
    return parser


def _run_command(args: argparse.Namespace, endpoint: GameEndpoint) -> int:
    with DragonArenaClient(
        endpoint,
        args.timeout,
        battle_timeout=args.battle_timeout,
        battle_start_codec=args.battle_start_codec,
        js_codec_bridge=args.js_codec_bridge,
        node_binary=args.node_binary,
        dragon_coin_item_id=args.dragon_coin_id,
        log_server_messages=not args.no_server_log,
        websocket_log=args.websocket_log,
        business_log=args.business_log,
        state_probe_timeout=args.state_probe_timeout,
    ) as client:
        resumed = client.resume_pending_battle(
            mercy_choice_id=args.mercy_choice_id,
        )
        if args.command == "resume":
            if resumed is None:
                print("当前没有需要续接的龙痕竞技场战斗。")
            return 0
        if args.command == "info":
            _format_info(client.get_info(), args.dragon_coin_id)
            return 0
        if args.command == "match":
            response = client.match()
            print(f"寻找对手完成：ret={response.ret}，候选数={len(response.opponents)}")
            return 0 if response.ret == 0 else 1
        if args.command == "challenge":
            result = client.run_round(
                args.index,
                mercy_choice_id=args.mercy_choice_id,
            )
            return 0 if result.challenge.ret == 0 else 1
        results = client.run_loop(
            rounds=args.rounds,
            mercy_choice_id=args.mercy_choice_id,
            refresh_on_exhaustion=not args.no_refresh_on_exhaustion,
        )
        print(f"龙痕竞技场循环完成：已处理 {len(results)} 场。")
        return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.self_test:
        run_self_tests()
        print("龙痕竞技场本地协议自检通过")
        return 0
    if args.command == "challenge" and args.index is None:
        print("challenge 命令需要 --index。", file=sys.stderr)
        return 2
    if args.mercy_choice_id <= 0:
        print("--mercy-choice-id 必须为正整数。", file=sys.stderr)
        return 2
    if args.state_probe_timeout < 0:
        print("--state-probe-timeout 不能为负数。", file=sys.stderr)
        return 2
    if args.dragon_coin_id is not None and args.dragon_coin_id <= 0:
        print("--dragon-coin-id 必须为正整数。", file=sys.stderr)
        return 2
    try:
        tokens = load_tokens(args.token_file)
        for attempt in range(2):
            endpoint = resolve_game_endpoint(tokens, args)
            try:
                return _run_command(args, endpoint)
            except GameLoginKickout as exc:
                if exc.ret != 2 or attempt > 0:
                    raise
                print(
                    "[登录] 收到 Kickout ret=2，"
                    f"{LOGIN_KICKOUT_RETRY_DELAY:g} 秒后重新获取游戏服会话并重试一次。"
                )
                time.sleep(LOGIN_KICKOUT_RETRY_DELAY)
        raise AssertionError("登录重试循环未返回")
    except HarvestError as exc:
        print(f"龙痕竞技场操作失败：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
