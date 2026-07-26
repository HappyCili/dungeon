#!/usr/bin/env python3
"""罪者深渊（Grave activity / Abyss）自动挑战客户端。

从当前可挑战层开始，连续挑战直到战斗失败或用户停止。
复用龙痕竞技场的登录、编队与 Battle_* 握手；业务消息为 Grave_*。

协议来源：
- ``Grave_chanllenge_start`` (19400)：``{id, type}``，type=1 为活动（罪者深渊）
- ``Grave_activity_sync`` (19420)：同步 activity（passes / season / optbuf）
- ``Grave_activity_buff_select`` (19418)：选择赛季可选增益
- 战斗：``Battle_info`` → ``Battle_C2S_start`` → ``Battle_S2C_end``（win/result）

用法：
    .venv/bin/python grave_abyss.py status
    .venv/bin/python grave_abyss.py loop
    .venv/bin/python grave_abyss.py --self-test
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Callable, Mapping, Sequence

from dragon_arena import (
    BATTLE_C2S_START_MESSAGE_ID,
    BATTLE_INFO_MESSAGE_ID,
    BATTLE_S2C_END_MESSAGE_ID,
    BATTLE_S2C_FRAME_BROADCAST_MESSAGE_ID,
    BATTLE_S2C_FRAME_HASH_VERIFY_MESSAGE_ID,
    BATTLE_S2C_START_MESSAGE_ID,
    GAME_DATA_MESSAGE_ID,
    BattleSessionState,
    DragonArenaClient,
    GameLoginKickout,
    GameMessageTimeout,
    GameSessionClosed,
    _first_bytes_field,
    _first_int_field,
    decode_battle_info,
    decode_battle_start_response,
)
from dragon_arena_business_map import (
    GRAVE_ACTIVITY_BUFF_SELECT_MESSAGE_ID,
    GRAVE_ACTIVITY_SYNC_MESSAGE_ID,
    GRAVE_CHALLENGE_START_MESSAGE_ID,
    GRAVE_PASS_NOTIFY_MESSAGE_ID,
)
from harvest_fief import (
    GameEndpoint,
    HarvestError,
    ProtoReader,
    decode_int32,
    encode_int_field,
    load_tokens,
    resolve_game_endpoint,
)
from harvest_fief import build_parser as build_base_parser
from project_paths import NATIVE_APP_ROOT

# 配置表：group=3、unlock type=1 为罪者深渊（活动）。
ABYSS_GROUP_ID = 3
GRAVE_TYPE_MAINLINE = 0
GRAVE_TYPE_ACTIVITY = 1

BATTLE_END_RESULT_LOSE = 1
BATTLE_END_RESULT_WIN = 2

DEFAULT_GRAVE_TABLE = NATIVE_APP_ROOT / "decrypted-data" / "tables" / "grave.json"
DEFAULT_GRAVE_ACTIVITY_TABLE = (
    NATIVE_APP_ROOT / "decrypted-data" / "tables" / "grave_activity.json"
)
DEFAULT_GRAVE_SEASON_TABLE = (
    NATIVE_APP_ROOT / "decrypted-data" / "tables" / "grave_season.json"
)
DEFAULT_GRAVE_OPTBUFF_TABLE = (
    NATIVE_APP_ROOT / "decrypted-data" / "tables" / "grave_optbuff.json"
)

# 挑战 start ret（notice.grave.startRet*）
START_RET_LABELS = {
    1: "尚未解锁",
    2: "开启失败",
    3: "已经在战斗中",
    4: "战斗失败",
    999: "装备仓库已满",
}


@dataclass(frozen=True)
class GraveFloor:
    """``grave`` 表一行的精简视图。"""

    id: int
    group: int
    level: int
    name: str
    recommend_level: int
    battle_id: int
    canselect: int


@dataclass(frozen=True)
class GraveActivityState:
    """罪者深渊 activity 状态（Game_data.grave.activity 或 sync 响应）。"""

    currgrave: int = 0
    passes: Mapping[int, int] = field(default_factory=dict)
    bests: Mapping[int, int] = field(default_factory=dict)
    season: int = 0
    actives: int = 0
    active_remains: int = 0
    last_passes: Mapping[int, int] = field(default_factory=dict)
    optbuf: int = 0


@dataclass(frozen=True)
class GraveDataState:
    """Game_data.grave 精简视图。"""

    currgrave: int = 0
    passes: Mapping[int, int] = field(default_factory=dict)
    activity: GraveActivityState | None = None


@dataclass(frozen=True)
class AbyssStatus:
    """对外展示的罪者深渊状态。"""

    season_id: int
    season_name: str
    season_open: bool
    left_seconds: int
    group_id: int
    pass_id: int
    pass_level: int
    next_id: int
    next_level: int
    next_name: str
    max_level: int
    currgrave: int
    optbuf: int
    optbuf_desc: str
    actives: int
    total_floors: int


@dataclass(frozen=True)
class GraveChallengeResponse:
    id: int
    ret: int
    type: int


@dataclass(frozen=True)
class AbyssBattleResult:
    challenge_id: int
    level: int
    name: str
    win: bool
    result_code: int | None
    round_number: int | None


@dataclass(frozen=True)
class AbyssRoundResult:
    challenge_id: int
    level: int
    name: str
    start: GraveChallengeResponse
    battle: AbyssBattleResult | None


def _decode_int_map_entry(data: bytes) -> tuple[int, int] | None:
    key = 0
    value = 0
    saw_key = False
    saw_value = False
    for field_number, wire_type, field_value in ProtoReader(data).fields():
        if wire_type != 0:
            continue
        if field_number == 1:
            key = decode_int32(int(field_value))
            saw_key = True
        elif field_number == 2:
            value = decode_int32(int(field_value))
            saw_value = True
    if not saw_key and not saw_value:
        return None
    return key, value


def decode_grave_activity(data: bytes) -> GraveActivityState:
    """Decode ``Zd``（grave.activity）。"""

    currgrave = 0
    passes: dict[int, int] = {}
    bests: dict[int, int] = {}
    season = 0
    actives = 0
    active_remains = 0
    last_passes: dict[int, int] = {}
    optbuf = 0
    for field_number, wire_type, value in ProtoReader(data).fields():
        if field_number == 1 and wire_type == 0:
            currgrave = decode_int32(int(value))
        elif field_number == 2 and wire_type == 2:
            entry = _decode_int_map_entry(bytes(value))
            if entry is not None:
                passes[entry[0]] = entry[1]
        elif field_number == 3 and wire_type == 2:
            entry = _decode_int_map_entry(bytes(value))
            if entry is not None:
                bests[entry[0]] = entry[1]
        elif field_number == 6 and wire_type == 0:
            season = decode_int32(int(value))
        elif field_number == 7 and wire_type == 0:
            actives = decode_int32(int(value))
        elif field_number == 9 and wire_type == 0:
            active_remains = decode_int32(int(value))
        elif field_number == 10 and wire_type == 2:
            entry = _decode_int_map_entry(bytes(value))
            if entry is not None:
                last_passes[entry[0]] = entry[1]
        elif field_number == 11 and wire_type == 0:
            optbuf = decode_int32(int(value))
    return GraveActivityState(
        currgrave=currgrave,
        passes=passes,
        bests=bests,
        season=season,
        actives=actives,
        active_remains=active_remains,
        last_passes=last_passes,
        optbuf=optbuf,
    )


def decode_grave_data(data: bytes) -> GraveDataState:
    """Decode ``Xd``（Game_data.grave，field 15）。"""

    currgrave = 0
    passes: dict[int, int] = {}
    activity: GraveActivityState | None = None
    for field_number, wire_type, value in ProtoReader(data).fields():
        if field_number == 3 and wire_type == 0:
            currgrave = decode_int32(int(value))
        elif field_number == 5 and wire_type == 2:
            entry = _decode_int_map_entry(bytes(value))
            if entry is not None:
                passes[entry[0]] = entry[1]
        elif field_number == 9 and wire_type == 2:
            activity = decode_grave_activity(bytes(value))
    return GraveDataState(currgrave=currgrave, passes=passes, activity=activity)


def decode_game_data_grave(data: bytes) -> GraveDataState | None:
    """从 ``Game_data`` 提取 grave（field 15）。"""

    grave = _first_bytes_field(data, 15)
    if grave is None:
        return None
    return decode_grave_data(grave)


def decode_grave_activity_sync_response(data: bytes) -> GraveActivityState | None:
    """Decode ``Grave_activity_sync`` 响应 ``su``：``activity`` 在 field 1。"""

    activity_bytes = _first_bytes_field(data, 1)
    if activity_bytes is None:
        # 部分回包可能直接是 activity 本体
        if data:
            return decode_grave_activity(data)
        return None
    return decode_grave_activity(activity_bytes)


def encode_grave_challenge_start(
    grave_id: int, *, grave_type: int = GRAVE_TYPE_ACTIVITY
) -> bytes:
    """Encode ``Grave_chanllenge_start``（``cu``：id=1, type=2）。"""

    parts: list[bytes] = []
    if grave_id:
        parts.append(encode_int_field(1, grave_id))
    if grave_type:
        parts.append(encode_int_field(2, grave_type))
    return b"".join(parts)


def decode_grave_challenge_start_response(data: bytes) -> GraveChallengeResponse:
    """Decode ``Vqf/du``：id=1, ret=2, type=3。"""

    values = {"id": 0, "ret": 0, "type": 0}
    for field_number, wire_type, value in ProtoReader(data).fields():
        if wire_type != 0:
            continue
        if field_number == 1:
            values["id"] = decode_int32(int(value))
        elif field_number == 2:
            values["ret"] = decode_int32(int(value))
        elif field_number == 3:
            values["type"] = decode_int32(int(value))
    return GraveChallengeResponse(**values)


def encode_grave_buff_select(optbuf: int) -> bytes:
    """Encode ``Grave_activity_buff_select`` 请求（``au``：optbuf field 1）。"""

    if not optbuf:
        return b""
    return encode_int_field(1, optbuf)


def decode_grave_buff_select_response(data: bytes) -> tuple[int, int]:
    """Decode 响应 ``ou``：ret field 1, optbuf field 3。返回 ``(ret, optbuf)``。"""

    ret = 0
    optbuf = 0
    for field_number, wire_type, value in ProtoReader(data).fields():
        if wire_type != 0:
            continue
        if field_number == 1:
            ret = decode_int32(int(value))
        elif field_number == 3:
            optbuf = decode_int32(int(value))
    return ret, optbuf


def decode_battle_end_result(data: bytes) -> tuple[bool, int | None, int | None]:
    """Decode ``Battle_S2C_end``：win(field2), result(field10), round(field1)。"""

    win_flag: int | None = None
    result_code: int | None = None
    round_number: int | None = None
    for field_number, wire_type, value in ProtoReader(data).fields():
        if wire_type != 0:
            continue
        if field_number == 1:
            round_number = decode_int32(int(value))
        elif field_number == 2:
            win_flag = int(value)
        elif field_number == 10:
            result_code = decode_int32(int(value))
    if result_code == BATTLE_END_RESULT_WIN:
        win = True
    elif result_code == BATTLE_END_RESULT_LOSE:
        win = False
    elif win_flag is not None:
        win = bool(win_flag)
    else:
        win = False
    return win, result_code, round_number


def load_abyss_floors(path: Path = DEFAULT_GRAVE_TABLE) -> tuple[GraveFloor, ...]:
    """加载罪者深渊层表（group=3），按 id 升序。"""

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise HarvestError(f"无法读取 grave 配置表：{path}") from exc
    if not isinstance(payload, list):
        raise HarvestError("grave 配置表格式错误")
    floors: list[GraveFloor] = []
    for row in payload:
        if not isinstance(row, Mapping):
            continue
        group = row.get("group")
        floor_id = row.get("id")
        if group != ABYSS_GROUP_ID:
            continue
        if not isinstance(floor_id, int) or isinstance(floor_id, bool) or floor_id <= 0:
            continue
        level = row.get("level")
        name = row.get("name")
        recommend = row.get("recommendlevel")
        battle_id = row.get("battleid")
        canselect = row.get("canselect")
        floors.append(
            GraveFloor(
                id=floor_id,
                group=ABYSS_GROUP_ID,
                level=int(level) if isinstance(level, int) and not isinstance(level, bool) else 0,
                name=str(name).strip() if isinstance(name, str) else f"罪者深渊-{floor_id}",
                recommend_level=(
                    int(recommend)
                    if isinstance(recommend, int) and not isinstance(recommend, bool)
                    else 0
                ),
                battle_id=(
                    int(battle_id)
                    if isinstance(battle_id, int) and not isinstance(battle_id, bool)
                    else 0
                ),
                canselect=(
                    int(canselect)
                    if isinstance(canselect, int) and not isinstance(canselect, bool)
                    else 0
                ),
            )
        )
    floors.sort(key=lambda item: item.id)
    if not floors:
        raise HarvestError("grave 配置表中没有罪者深渊层")
    return tuple(floors)


def load_season_optional_buffs(
    *,
    season_id: int,
    season_path: Path = DEFAULT_GRAVE_SEASON_TABLE,
    activity_path: Path = DEFAULT_GRAVE_ACTIVITY_TABLE,
) -> tuple[int, ...]:
    """按赛季 effectgroup 取 optionalbuff1 列表。"""

    try:
        seasons = json.loads(season_path.read_text(encoding="utf-8"))
        activities = json.loads(activity_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return ()
    if not isinstance(seasons, list) or not isinstance(activities, list):
        return ()
    effectgroup = 0
    for row in seasons:
        if isinstance(row, Mapping) and row.get("id") == season_id:
            eg = row.get("effectgroup")
            if isinstance(eg, int) and not isinstance(eg, bool):
                effectgroup = eg
            break
    if effectgroup <= 0:
        return ()
    for row in activities:
        if not isinstance(row, Mapping) or row.get("id") != effectgroup:
            continue
        raw = row.get("optionalbuff1")
        if not isinstance(raw, str) or not raw.strip():
            return ()
        buffs: list[int] = []
        for part in raw.split("#"):
            part = part.strip()
            if not part:
                continue
            try:
                buffs.append(int(part))
            except ValueError:
                continue
        return tuple(buffs)
    return ()


def load_optbuff_desc(
    optbuf: int, path: Path = DEFAULT_GRAVE_OPTBUFF_TABLE
) -> str:
    if optbuf <= 0:
        return ""
    try:
        rows = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return ""
    if not isinstance(rows, list):
        return ""
    for row in rows:
        if isinstance(row, Mapping) and row.get("id") == optbuf:
            desc = row.get("desc1")
            return str(desc).strip() if isinstance(desc, str) else ""
    return ""


def load_season_meta(
    season_id: int,
    *,
    server_time_s: int | None = None,
    path: Path = DEFAULT_GRAVE_SEASON_TABLE,
) -> tuple[str, bool, int]:
    """返回 ``(name, open, left_seconds)``。"""

    try:
        seasons = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return "", False, -1
    if not isinstance(seasons, list):
        return "", False, -1
    now = server_time_s if server_time_s is not None else int(time.time())
    for row in seasons:
        if not isinstance(row, Mapping) or row.get("id") != season_id:
            continue
        name = str(row.get("name") or f"赛季 {season_id}")
        begin = row.get("begintime")
        end = row.get("endtime")
        if not isinstance(begin, int) or not isinstance(end, int):
            return name, False, -1
        open_now = begin <= now < end
        left = end - now if open_now else -1
        return name, open_now, left
    # 未命中配置时，若有 season_id 仍视为可能在赛季内
    if season_id > 0:
        return f"赛季 {season_id}", True, -1
    return "", False, -1


def max_challenge_id_from_last_passes(
    activity: GraveActivityState,
    floors: Sequence[GraveFloor],
    *,
    fallback_id: int,
) -> int:
    """对齐客户端 ``getAbyssMaxChallengeId``。

    上赛季 ``lastPasses`` 层的 ``canselect`` 表示本赛季可直接挑战的最高
    *level*（不是 last+1）。若 lastPasses 无效则返回 ``fallback_id``。
    """

    group = ABYSS_GROUP_ID
    last = int(activity.last_passes.get(group, 0) or 0)
    if last <= 0:
        return fallback_id
    last_floor = floor_by_id(floors, last)
    if last_floor is None:
        return fallback_id
    canselect_level = int(last_floor.canselect or 0)
    if canselect_level <= 0:
        return fallback_id
    max_id = fallback_id
    for floor in floors:
        if floor.level <= canselect_level and floor.id > max_id:
            max_id = floor.id
    return max_id


def resolve_next_challenge_id(
    activity: GraveActivityState | None,
    floors: Sequence[GraveFloor],
) -> int:
    """计算下一可挑战层 id；已通关全部返回 0。

    对齐客户端 ``getAbyssChallengeId`` / ``getAbyssMaxChallengeId``：
    - 本赛季 ``passes==0`` 时，取 lastPasses 对应层 ``canselect`` 允许的最高层
      （常见为上赛季进度回退若干层后的起点，而非 last+1）
    - 已有通关进度时，下一层为 pass+1；若 ``currgrave`` 停在该层则优先重试
    """

    if not floors:
        return 0
    floor_ids = [floor.id for floor in floors]
    first_id = floor_ids[0]
    if activity is None:
        return first_id

    group = ABYSS_GROUP_ID
    passed = int(activity.passes.get(group, 0) or 0)
    curr = int(activity.currgrave or 0)

    # 未通关任何层：用 lastPasses.canselect 上限（客户端 getAbyssMaxChallengeId）
    if passed <= 0:
        return max_challenge_id_from_last_passes(
            activity, floors, fallback_id=first_id
        )

    if passed not in floor_ids:
        # 异常 pass id：尝试 +1 或回退 lastPasses / 第一层
        candidate = passed + 1
        if candidate in floor_ids:
            return candidate
        return max_challenge_id_from_last_passes(
            activity, floors, fallback_id=first_id
        )

    idx = floor_ids.index(passed)
    if idx >= len(floor_ids) - 1:
        return 0  # 全部通关

    next_id = floor_ids[idx + 1]
    # 若 currgrave 仍停在未通过层且可重试，优先重试
    if curr and curr in floor_ids and curr <= next_id:
        curr_idx = floor_ids.index(curr)
        pass_idx = idx
        if curr_idx == pass_idx + 1:
            return curr
    return next_id


def floor_by_id(
    floors: Sequence[GraveFloor], floor_id: int
) -> GraveFloor | None:
    for floor in floors:
        if floor.id == floor_id:
            return floor
    return None


def build_abyss_status(
    activity: GraveActivityState | None,
    floors: Sequence[GraveFloor],
    *,
    server_time_s: int | None = None,
) -> AbyssStatus:
    season_id = int(activity.season) if activity else 0
    season_name, season_open, left = load_season_meta(
        season_id, server_time_s=server_time_s
    )
    # 有 activity 即视为已开启相关模块；left 未知时仍允许挑战
    if activity is not None and season_id > 0 and left < 0 and not season_open:
        # 配置表未命中但服务端给了 season：仍视为开放
        season_open = True

    pass_id = int(activity.passes.get(ABYSS_GROUP_ID, 0) or 0) if activity else 0
    pass_floor = floor_by_id(floors, pass_id)
    next_id = resolve_next_challenge_id(activity, floors)
    next_floor = floor_by_id(floors, next_id)
    max_level = floors[-1].level if floors else 0
    optbuf = int(activity.optbuf) if activity else 0

    return AbyssStatus(
        season_id=season_id,
        season_name=season_name,
        season_open=season_open if activity is not None else False,
        left_seconds=left,
        group_id=ABYSS_GROUP_ID,
        pass_id=pass_id,
        pass_level=pass_floor.level if pass_floor else 0,
        next_id=next_id,
        next_level=next_floor.level if next_floor else 0,
        next_name=next_floor.name if next_floor else "",
        max_level=max_level,
        currgrave=int(activity.currgrave) if activity else 0,
        optbuf=optbuf,
        optbuf_desc=load_optbuff_desc(optbuf),
        actives=int(activity.actives) if activity else 0,
        total_floors=len(floors),
    )


class GraveAbyssClient(DragonArenaClient):
    """罪者深渊客户端：登录复用龙痕，挑战走 Grave_*。"""

    def __init__(
        self,
        endpoint: GameEndpoint,
        timeout: float,
        *,
        battle_timeout: float = 180.0,
        battle_start_codec: str = "js",
        grave_table: Path = DEFAULT_GRAVE_TABLE,
        log_server_messages: bool = True,
        websocket_log: Path | bool | None = True,
        business_log: Path | None = None,
        state_probe_timeout: float = 0.35,
        socket_factory: object | None = None,
        log: Callable[[str], None] = print,
        task: str = "grave_abyss",
        **kwargs: object,
    ) -> None:
        init_kwargs: dict[str, object] = {
            "battle_timeout": battle_timeout,
            "battle_start_codec": battle_start_codec,
            "log_server_messages": log_server_messages,
            "websocket_log": websocket_log,
            "business_log": business_log,
            "state_probe_timeout": state_probe_timeout,
            "log": log,
            "task": task,
        }
        if socket_factory is not None:
            init_kwargs["socket_factory"] = socket_factory
        init_kwargs.update(kwargs)
        super().__init__(endpoint, timeout, **init_kwargs)  # type: ignore[arg-type]
        self._floors = load_abyss_floors(grave_table)
        self._activity: GraveActivityState | None = None
        self._grave_data: GraveDataState | None = None
        self._server_time_ms: int = 0

    @property
    def floors(self) -> tuple[GraveFloor, ...]:
        return self._floors

    @property
    def activity(self) -> GraveActivityState | None:
        return self._activity

    def login(self) -> None:  # type: ignore[override]
        super().login()
        payload = getattr(self, "_game_data_payload", None)
        if isinstance(payload, (bytes, bytearray)) and payload:
            grave = decode_game_data_grave(bytes(payload))
            self._grave_data = grave
            if grave and grave.activity is not None:
                self._activity = grave.activity
            context = getattr(self, "_game_data_context", None)
            if context is not None and getattr(context, "server_time_ms", 0):
                self._server_time_ms = int(context.server_time_ms)
            self.log(
                "[罪者深渊] 登录 Game_data："
                f"season={self._activity.season if self._activity else 0}，"
                f"pass={self._activity.passes.get(ABYSS_GROUP_ID, 0) if self._activity else 0}，"
                f"optbuf={self._activity.optbuf if self._activity else 0}。"
            )

    def sync_activity(self) -> GraveActivityState:
        """请求 ``Grave_activity_sync`` 并更新本地 activity。"""

        self._send_message(GRAVE_ACTIVITY_SYNC_MESSAGE_ID, encrypted=True)
        for header in self._wait_for({GRAVE_ACTIVITY_SYNC_MESSAGE_ID}, self.timeout):
            if header.message_id == GRAVE_ACTIVITY_SYNC_MESSAGE_ID:
                activity = decode_grave_activity_sync_response(header.data)
                if activity is None:
                    raise HarvestError("Grave_activity_sync 未返回 activity")
                self._activity = activity
                self.log(
                    "[罪者深渊] 已同步："
                    f"season={activity.season}，"
                    f"pass={activity.passes.get(ABYSS_GROUP_ID, 0)}，"
                    f"curr={activity.currgrave}，"
                    f"optbuf={activity.optbuf}。"
                )
                return activity
            self._log_background_message(header)
        raise AssertionError("_wait_for 未返回 Grave_activity_sync")

    def ensure_buff_selected(self) -> int:
        """若未选增益，自动选当前赛季第一个可选 buff。返回 optbuf。"""

        activity = self._activity
        if activity is None:
            activity = self.sync_activity()
        if activity.optbuf > 0:
            return activity.optbuf
        season_id = activity.season
        if season_id <= 0:
            self.log("[罪者深渊] 无赛季信息，跳过增益选择。")
            return 0
        options = load_season_optional_buffs(season_id=season_id)
        if not options:
            self.log("[罪者深渊] 当前赛季无可选增益。")
            return 0
        chosen = options[0]
        self.log(
            f"[罪者深渊] 未选择增益，自动选择 optbuf={chosen}"
            f"（{load_optbuff_desc(chosen) or '无描述'}）。"
        )
        self._send_message(
            GRAVE_ACTIVITY_BUFF_SELECT_MESSAGE_ID,
            encode_grave_buff_select(chosen),
            encrypted=True,
        )
        for header in self._wait_for(
            {GRAVE_ACTIVITY_BUFF_SELECT_MESSAGE_ID}, self.timeout
        ):
            if header.message_id == GRAVE_ACTIVITY_BUFF_SELECT_MESSAGE_ID:
                ret, optbuf = decode_grave_buff_select_response(header.data)
                if ret != 0:
                    raise HarvestError(
                        f"选择罪者深渊增益失败 ret={ret}"
                    )
                selected = optbuf or chosen
                if self._activity is not None:
                    self._activity = replace(self._activity, optbuf=selected)
                self.log(f"[罪者深渊] 增益已选择：optbuf={selected}。")
                return selected
            self._log_background_message(header)
        raise AssertionError("_wait_for 未返回 buff_select")

    def get_status(self, *, sync: bool = True) -> AbyssStatus:
        if sync or self._activity is None:
            try:
                self.sync_activity()
            except HarvestError as exc:
                # 登录期已有 activity 时允许降级
                if self._activity is None:
                    raise
                self.log(f"[罪者深渊] 同步失败，使用登录缓存：{exc}")
        server_s = (
            self._server_time_ms // 1000 if self._server_time_ms > 0 else None
        )
        return build_abyss_status(
            self._activity, self._floors, server_time_s=server_s
        )

    def _mark_pass(self, challenge_id: int) -> None:
        if self._activity is None:
            self._activity = GraveActivityState(
                passes={ABYSS_GROUP_ID: challenge_id},
                currgrave=0,
            )
            return
        new_passes = dict(self._activity.passes)
        new_passes[ABYSS_GROUP_ID] = challenge_id
        self._activity = replace(
            self._activity, passes=new_passes, currgrave=0
        )

    def resume_pending_battle(self) -> AbyssBattleResult | None:
        """结算登录期遗留的 Battle_info（地图/地牢/深渊等），解除 ret=3 占用。

        龙痕竞技场有同名能力；罪者深渊共用登录会话时，任意未结束战斗都会
        导致 ``Grave_chanllenge_start`` 返回 startRet3「已经在战斗中」。
        """

        state = self.inspect_initial_battle_state()
        if state.phase == "空闲":
            return None
        labels = "、".join(str(mid) for mid in state.message_ids) or "未知"
        self.log(
            f"[恢复] 检测到登录期{state.phase}"
            f"（battle_id={state.battle_id or '未知'}，消息={labels}），"
            "先结算遗留战斗再开罪者深渊。"
        )
        battle = self.await_battle_result(
            challenge_id=0,
            level=0,
            name="遗留战斗",
        )
        self._initial_battle_state = BattleSessionState("空闲", 0, ())
        self.log(
            f"[恢复] 遗留战斗已结束：胜利={'是' if battle.win else '否'}。"
        )
        return battle

    def challenge_start(
        self, grave_id: int, *, grave_type: int = GRAVE_TYPE_ACTIVITY
    ) -> GraveChallengeResponse:
        """发送挑战开始；若 Battle_info 先到则视作 ret=0。"""

        payload = encode_grave_challenge_start(grave_id, grave_type=grave_type)
        self._send_message(
            GRAVE_CHALLENGE_START_MESSAGE_ID, payload, encrypted=True
        )
        for header in self._wait_for(
            {GRAVE_CHALLENGE_START_MESSAGE_ID, BATTLE_INFO_MESSAGE_ID},
            self.timeout,
        ):
            if header.message_id == GRAVE_CHALLENGE_START_MESSAGE_ID:
                response = decode_grave_challenge_start_response(header.data)
                return response
            if header.message_id == BATTLE_INFO_MESSAGE_ID:
                battle = decode_battle_info(header.data)
                if battle.ret != 0:
                    raise HarvestError(f"Battle_info 返回 ret={battle.ret}")
                self._queued_headers.appendleft(header)
                self.log(
                    "[挑战] Battle_info 早于挑战响应到达，已转入战斗握手。"
                )
                return GraveChallengeResponse(
                    id=grave_id, ret=0, type=grave_type
                )
            self._log_background_message(header)
        raise AssertionError("_wait_for 未返回挑战响应")

    def await_battle_result(
        self, *, challenge_id: int, level: int, name: str
    ) -> AbyssBattleResult:
        """等待战斗结束；完成握手、三倍速与自动技能。"""

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
                raise HarvestError("等待罪者深渊战斗结算超时")
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

            if header.message_id == GRAVE_CHALLENGE_START_MESSAGE_ID:
                delayed = decode_grave_challenge_start_response(header.data)
                if delayed.ret != 0:
                    label = START_RET_LABELS.get(
                        delayed.ret, f"ret={delayed.ret}"
                    )
                    raise HarvestError(f"挑战响应延迟返回：{label}")
                self.log(
                    f"[挑战] 收到延迟挑战响应：id={delayed.id or challenge_id}。"
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

            if header.message_id == BATTLE_S2C_FRAME_BROADCAST_MESSAGE_ID:
                frame_count += 1
                if frame_count == 1 or frame_count % 50 == 0:
                    self.log(f"[战斗] 收到服务端战斗帧：累计 {frame_count} 帧。")
                continue

            if header.message_id == BATTLE_S2C_FRAME_HASH_VERIFY_MESSAGE_ID:
                hash_verify_count += 1
                continue

            if header.message_id == BATTLE_S2C_END_MESSAGE_ID:
                win, result_code, round_number = decode_battle_end_result(
                    header.data
                )
                self.log(
                    "[战斗] 服务端结束战斗："
                    f"胜利={'是' if win else '否'}，"
                    f"结果={result_code if result_code is not None else '未提供'}，"
                    f"回合={round_number if round_number is not None else '未提供'}。"
                )
                # 通关通知可能紧随其后，短暂吞掉
                self._drain_post_battle_notices(challenge_id if win else 0)
                return AbyssBattleResult(
                    challenge_id=challenge_id,
                    level=level,
                    name=name,
                    win=win,
                    result_code=result_code,
                    round_number=round_number,
                )

            if header.message_id == GRAVE_ACTIVITY_SYNC_MESSAGE_ID:
                activity = decode_grave_activity_sync_response(header.data)
                if activity is not None:
                    self._activity = activity
                    self.log("[罪者深渊] 战斗中收到 activity 同步。")
                continue

            if header.message_id == GRAVE_PASS_NOTIFY_MESSAGE_ID:
                self.log("[罪者深渊] 收到通关通知。")
                continue

            if battle_start_sent and header.message_id == BATTLE_C2S_START_MESSAGE_ID:
                self.log("[战斗] 收到 Battle_C2S_start 回显。")
                continue

            self._log_background_message(header)

    def _drain_post_battle_notices(self, won_challenge_id: int) -> None:
        deadline = time.monotonic() + min(0.8, self.timeout)
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            try:
                header = self._next_header(remaining)
            except (GameMessageTimeout, GameSessionClosed):
                return
            if header.message_id == GRAVE_ACTIVITY_SYNC_MESSAGE_ID:
                activity = decode_grave_activity_sync_response(header.data)
                if activity is not None:
                    self._activity = activity
                continue
            if header.message_id == GRAVE_PASS_NOTIFY_MESSAGE_ID:
                continue
            if header.message_id == GAME_DATA_MESSAGE_ID:
                continue
            # 其它消息放回队列
            self._queued_headers.appendleft(header)
            return

    def run_round(self, challenge_id: int) -> AbyssRoundResult:
        floor = floor_by_id(self._floors, challenge_id)
        if floor is None:
            raise HarvestError(f"未知罪者深渊层 id={challenge_id}")
        name = floor.name
        level = floor.level
        self.log(
            f"[挑战] 第 {level} 层（id={challenge_id}，{name}）开始。"
        )
        start = self.challenge_start(challenge_id)
        # startRet3：已在战斗中。再尝试清一次遗留战斗后重试（竞态/漏检）
        if start.ret == 3:
            self.log("[挑战] 开始返回已在战斗中，尝试结算遗留战斗后重试。")
            try:
                self.resume_pending_battle()
            except HarvestError as exc:
                self.log(f"[挑战] 遗留战斗恢复失败：{exc}")
                return AbyssRoundResult(challenge_id, level, name, start, None)
            start = self.challenge_start(challenge_id)
        if start.ret != 0:
            label = START_RET_LABELS.get(start.ret, f"ret={start.ret}")
            self.log(f"[挑战] 开始失败：{label}。")
            return AbyssRoundResult(challenge_id, level, name, start, None)

        # 服务端可能不回填 id
        if start.id == 0:
            start = GraveChallengeResponse(
                id=challenge_id, ret=start.ret, type=start.type
            )

        try:
            battle = self.await_battle_result(
                challenge_id=challenge_id, level=level, name=name
            )
        except GameSessionClosed:
            self.log(f"[战斗] 第 {level} 层连接关闭。")
            raise
        except HarvestError as exc:
            self.log(f"[战斗] 第 {level} 层未完成：{exc}。")
            return AbyssRoundResult(challenge_id, level, name, start, None)

        if battle.win:
            self._mark_pass(challenge_id)
            self.log(f"[挑战] 第 {level} 层胜利，已更新本地通关进度。")
        else:
            self.log(f"[挑战] 第 {level} 层失败，停止自动挑战。")
        return AbyssRoundResult(challenge_id, level, name, start, battle)

    def run_loop(
        self,
        *,
        stop_requested: Callable[[], bool] | None = None,
        max_rounds: int = 0,
        ensure_buff: bool = True,
        on_round: Callable[[AbyssRoundResult, AbyssStatus], None] | None = None,
    ) -> tuple[AbyssRoundResult, ...]:
        """连续挑战直到失败、全部通关、达上限或 stop。

        ``max_rounds=0`` 表示不限制轮数（仅失败/通关/停止结束）。
        """

        if max_rounds < 0:
            raise HarvestError("max_rounds 不能为负数")

        should_stop = stop_requested or (lambda: False)
        results: list[AbyssRoundResult] = []

        status = self.get_status(sync=True)
        if not status.season_open and status.season_id <= 0:
            self.log("[罪者深渊] 当前无进行中的赛季，停止。")
            return tuple(results)

        if ensure_buff:
            try:
                self.ensure_buff_selected()
            except HarvestError as exc:
                self.log(f"[罪者深渊] 增益选择跳过：{exc}")

        # 登录可能回放未结束的地图/其它战斗；不结算则 start 恒为 ret=3
        try:
            self.resume_pending_battle()
        except HarvestError as exc:
            self.log(f"[罪者深渊] 遗留战斗恢复失败：{exc}")
            raise

        status = self.get_status(sync=False)
        self.log(
            "[罪者深渊] "
            f"{status.season_name or '未知赛季'}，"
            f"已通关 {status.pass_level}/{status.max_level}，"
            f"下一层={status.next_level or '无'}。"
        )

        while True:
            if should_stop():
                self.log("[罪者深渊] 收到停止请求。")
                break
            if max_rounds > 0 and len(results) >= max_rounds:
                self.log(f"[罪者深渊] 已达轮数上限 {max_rounds}。")
                break

            next_id = resolve_next_challenge_id(self._activity, self._floors)
            if next_id <= 0:
                self.log("[罪者深渊] 已全部通关。")
                break

            result = self.run_round(next_id)
            results.append(result)
            status = build_abyss_status(
                self._activity,
                self._floors,
                server_time_s=(
                    self._server_time_ms // 1000
                    if self._server_time_ms > 0
                    else None
                ),
            )
            if on_round is not None:
                on_round(result, status)

            if result.battle is None:
                # 开始失败或中断：startRet4 等也视为停止
                if result.start.ret != 0:
                    break
                self.log("[罪者深渊] 本轮无结算，停止以防死循环。")
                break
            if not result.battle.win:
                break

        wins = sum(1 for item in results if item.battle and item.battle.win)
        losses = sum(
            1 for item in results if item.battle and not item.battle.win
        )
        self.log(
            f"[罪者深渊] 循环结束：挑战 {len(results)} 场"
            f"（{wins} 胜 / {losses} 负）。"
        )
        return tuple(results)


def _build_parser() -> argparse.ArgumentParser:
    parser = build_base_parser()
    parser.prog = "grave_abyss.py"
    parser.description = __doc__
    parser.add_argument(
        "command",
        nargs="?",
        choices=("status", "loop"),
        default="status",
        help="status=查询；loop=自动挑战直到失败（默认 status）",
    )
    parser.add_argument(
        "--max-rounds",
        type=int,
        default=0,
        help="loop 最大挑战次数；0 表示不限制（直到失败）",
    )
    parser.add_argument(
        "--no-auto-buff",
        action="store_true",
        help="不自动选择赛季增益",
    )
    parser.add_argument(
        "--battle-timeout",
        type=float,
        default=180.0,
        help="单场战斗等待结算的最长秒数",
    )
    return parser


def run_self_tests() -> None:
    """离线校验编码/解码与下一层解析。"""

    # challenge start
    payload = encode_grave_challenge_start(30005, grave_type=GRAVE_TYPE_ACTIVITY)
    assert payload == encode_int_field(1, 30005) + encode_int_field(2, 1)
    resp = decode_grave_challenge_start_response(
        encode_int_field(1, 30005) + encode_int_field(2, 0) + encode_int_field(3, 1)
    )
    assert resp.id == 30005 and resp.ret == 0 and resp.type == 1

    # buff select
    assert encode_grave_buff_select(201) == encode_int_field(1, 201)
    ret, optbuf = decode_grave_buff_select_response(
        encode_int_field(1, 0) + encode_int_field(3, 201)
    )
    assert ret == 0 and optbuf == 201

    # activity decode
    act_payload = b"".join(
        (
            encode_int_field(1, 30010),  # currgrave
            encode_bytes_field_map(2, 3, 30009),  # passes group3=30009
            encode_int_field(6, 1024),  # season
            encode_int_field(11, 201),  # optbuf
        )
    )
    activity = decode_grave_activity(act_payload)
    assert activity.currgrave == 30010
    assert activity.passes[3] == 30009
    assert activity.season == 1024
    assert activity.optbuf == 201

    sync_payload = encode_bytes_field_activity(activity=act_payload)
    synced = decode_grave_activity_sync_response(sync_payload)
    assert synced is not None
    assert synced.season == 1024

    # battle end
    win, code, rnd = decode_battle_end_result(
        encode_int_field(1, 12)
        + encode_bool_field(2, True)
        + encode_int_field(10, BATTLE_END_RESULT_WIN)
    )
    assert win is True and code == BATTLE_END_RESULT_WIN and rnd == 12
    lose, code2, _ = decode_battle_end_result(
        encode_int_field(1, 8)
        + encode_bool_field(2, False)
        + encode_int_field(10, BATTLE_END_RESULT_LOSE)
    )
    assert lose is False and code2 == BATTLE_END_RESULT_LOSE

    floors = (
        GraveFloor(30001, 3, 1, "罪者深渊-1", 68, 1, 1),
        GraveFloor(30002, 3, 2, "罪者深渊-2", 68, 1, 1),
        GraveFloor(30003, 3, 3, "罪者深渊-3", 68, 1, 1),
    )
    assert resolve_next_challenge_id(None, floors) == 30001
    assert (
        resolve_next_challenge_id(
            GraveActivityState(passes={3: 0}), floors
        )
        == 30001
    )
    assert (
        resolve_next_challenge_id(
            GraveActivityState(passes={3: 30001}), floors
        )
        == 30002
    )
    assert (
        resolve_next_challenge_id(
            GraveActivityState(passes={3: 30003}), floors
        )
        == 0
    )
    # 重试当前失败层
    assert (
        resolve_next_challenge_id(
            GraveActivityState(passes={3: 30001}, currgrave=30002), floors
        )
        == 30002
    )
    # lastPasses.canselect：上赛季 30003、canselect=2 → 本赛季从 30002 起
    floors_with_select = (
        GraveFloor(30001, 3, 1, "罪者深渊-1", 68, 1, 1),
        GraveFloor(30002, 3, 2, "罪者深渊-2", 68, 1, 1),
        GraveFloor(30003, 3, 3, "罪者深渊-3", 68, 1, 2),
    )
    assert (
        resolve_next_challenge_id(
            GraveActivityState(passes={3: 0}, last_passes={3: 30003}),
            floors_with_select,
        )
        == 30002
    )

    status = build_abyss_status(
        GraveActivityState(passes={3: 30001}, season=1024, optbuf=201),
        floors,
    )
    assert status.pass_level == 1
    assert status.next_id == 30002
    assert status.next_level == 2

    # 真实配置表可加载；复现日志：lastPasses=30031、canselect=25 → 30025
    real_floors = load_abyss_floors()
    assert len(real_floors) == 900
    assert real_floors[0].id == 30001
    assert real_floors[-1].id == 30900
    floor_31 = floor_by_id(real_floors, 30031)
    assert floor_31 is not None and floor_31.canselect == 25
    assert (
        resolve_next_challenge_id(
            GraveActivityState(passes={3: 0}, last_passes={3: 30031}),
            real_floors,
        )
        == 30025
    )

    print("grave_abyss self-test OK")


def encode_bytes_field_map(field_number: int, key: int, value: int) -> bytes:
    from harvest_fief import encode_bytes_field

    entry = encode_int_field(1, key) + encode_int_field(2, value)
    return encode_bytes_field(field_number, entry)


def encode_bytes_field_activity(*, activity: bytes) -> bytes:
    from harvest_fief import encode_bytes_field

    return encode_bytes_field(1, activity)


def encode_bool_field(field_number: int, value: bool) -> bytes:
    return encode_int_field(field_number, 1 if value else 0)


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.self_test:
        run_self_tests()
        return 0

    tokens = load_tokens(args.token_file)
    endpoint = resolve_game_endpoint(tokens, args)
    with GraveAbyssClient(
        endpoint,
        timeout=float(args.timeout),
        battle_timeout=float(args.battle_timeout),
        log_server_messages=False,
        business_log=None,
        task="grave_abyss",
    ) as client:
        if args.command == "status":
            status = client.get_status(sync=True)
            print(
                f"赛季={status.season_name}({status.season_id}) "
                f"开放={status.season_open} 剩余秒={status.left_seconds}"
            )
            print(
                f"已通关={status.pass_level}/{status.max_level} "
                f"(id={status.pass_id}) "
                f"下一层={status.next_level} {status.next_name} "
                f"(id={status.next_id})"
            )
            print(
                f"增益={status.optbuf} {status.optbuf_desc or ''} "
                f"活跃={status.actives}"
            )
            return 0

        results = client.run_loop(
            max_rounds=int(args.max_rounds),
            ensure_buff=not args.no_auto_buff,
        )
        wins = sum(1 for item in results if item.battle and item.battle.win)
        print(f"完成 {len(results)} 场，胜利 {wins}")
        return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (HarvestError, GameLoginKickout, OSError, ValueError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        raise SystemExit(1) from exc
