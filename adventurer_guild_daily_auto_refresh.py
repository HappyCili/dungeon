#!/usr/bin/env python3
"""每日执行冒险者公会的受限自动刷新。

流程先读取 ``Game_data`` 中的当前候选英雄和刷新状态。若当前列表已有 SS，
不会覆盖它；否则依次执行：

1. 金币单次价格严格小于 200 时，持续使用金币“换一批”；
2. 仅在宝石刷新明确拥有免费次数时，使用宝石“换一批”；
3. 任意新列表出现 SS（``heroes.rare >= 6``）后立即停止发送请求并写入 JSONL 日志。

脚本只刷新候选列表，不会招募、购买或替换英雄。

用法：
    .venv/bin/python adventurer_guild_daily_auto_refresh.py
    .venv/bin/python adventurer_guild_daily_auto_refresh.py --zone-id 4101
    .venv/bin/python adventurer_guild_daily_auto_refresh.py --self-test
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable

from harvest_fief import (
    HEARTBEAT_MESSAGE_ID,
    HEARTBEAT_RET_MESSAGE_ID,
    LOGIN_FAIL_MESSAGE_ID,
    LOGIN_MESSAGE_ID,
    LOGIN_REUNIQUE_MESSAGE_ID,
    PACK_PASSWORD_MESSAGE_ID,
    SOCKET_PACK_KEY,
    GameEndpoint,
    HarvestError,
    MessageHeader,
    NativeWebSocket,
    ProtoReader,
    decode_int32,
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
from ws_traffic_log import bind_traffic_logging
from harvest_fief import build_parser as build_base_parser
from id_descriptions import item_name, zone_name
from logging_store import MANAGED_DESTINATION, LogPersistenceError, write_standard_log
from project_paths import NATIVE_APP_ROOT


GAME_DATA_MESSAGE_ID = 10490
KICKOUT_MESSAGE_ID = 10030
TAVERN_REFRESH_MESSAGE_ID = 15006

REFRESH_TYPE_AUTO = 0
REFRESH_TYPE_MANUAL = 1
REFRESH_COST_GOLD = 0
REFRESH_COST_GEM = 1

POOL_GROUP_HERO = 0
POOL_GROUP_BARREL = 1
POOL_GROUP_ITEM = 2
SS_RARITY = 6

PROJECT_ROOT = Path(__file__).resolve().parent
DEFAULT_CONFIG_DIR = NATIVE_APP_ROOT / "decrypted-tavern-data"
DEFAULT_HERO_TABLE = DEFAULT_CONFIG_DIR / "heroes.json"
DEFAULT_HERO_NAME_TABLE = DEFAULT_CONFIG_DIR / "heroname.json"
DEFAULT_REFRESH_FEE_TABLE = DEFAULT_CONFIG_DIR / "refreshfee.json"
DEFAULT_RESULT_LOG = MANAGED_DESTINATION
DEFAULT_GOLD_COST_LIMIT = 200
DEFAULT_MAX_REFRESHES = 20
LOGIN_KICKOUT_RETRY_DELAY = 3.0

RESULT_SUCCESS = 0
RESULT_LABELS = {
    0: "刷新成功",
    1: "英雄掉落错误",
    2: "消耗类型错误",
    3: "花费配置错误",
    4: "材料不足",
    5: "未到自动刷新时间",
    10: "今日宝石刷新次数已用完",
}
RARITY_LABELS = {
    1: "D",
    2: "C",
    3: "B",
    4: "A",
    5: "S",
    6: "SS",
    7: "SSS",
}
STOP_LABELS = {
    "policy_complete": "符合条件的金币和免费宝石刷新均已完成",
    "daily_target_reached": "已达到日常任务所需刷新次数",
    "ss_detected_initial": "初始候选列表已有 SS，已停止以免覆盖",
    "ss_detected_after_gold": "金币刷新出现 SS，已立即停止",
    "ss_detected_after_free_gem": "免费宝石刷新出现 SS，已立即停止",
    "max_refreshes_reached": "达到本次请求数上限，流程提前停止",
    "gem_daily_limit_reached": "今日宝石刷新次数已达上限",
}


class GameSessionKickout(HarvestError):
    """The game server ended the newly opened session with a reason code."""

    def __init__(self, ret: int, message: str = "") -> None:
        self.ret = ret
        self.message = message
        detail = f"，消息={message}" if message else ""
        super().__init__(f"游戏服终止会话：ret={ret}{detail}")


@dataclass(frozen=True)
class HeroDesign:
    cid: int
    rare: int
    title: str
    name_group: int


@dataclass(frozen=True)
class RefreshFee:
    times: int
    gold_item_id: int
    gold_cost: int
    gem_item_id: int
    gem_cost: int


@dataclass(frozen=True)
class TavernCatalog:
    heroes: dict[int, HeroDesign]
    hero_names: dict[int, str]
    refresh_fees: tuple[RefreshFee, ...]

    def hero_design(self, cid: int) -> HeroDesign:
        try:
            return self.heroes[cid]
        except KeyError as exc:
            raise HarvestError("英雄配置缺少候选项，为避免漏判 SS 已停止") from exc

    def refresh_fee(self, cost_type: int, used_count: int) -> tuple[int, int]:
        if used_count < 0:
            raise HarvestError(f"刷新计数不能为负数：{used_count}")
        next_count = used_count + 1
        selected = self.refresh_fees[0]
        for fee in self.refresh_fees:
            if fee.times > next_count:
                break
            selected = fee
        if cost_type == REFRESH_COST_GOLD:
            return selected.gold_item_id, selected.gold_cost
        if cost_type == REFRESH_COST_GEM:
            return selected.gem_item_id, selected.gem_cost
        raise HarvestError(f"不支持的刷新消耗类型：{cost_type}")


@dataclass(frozen=True)
class TavernHero:
    hero_id: int
    cid: int
    level: int
    name_id: int
    tid: int
    server_name: str
    potential: int
    pool_group_type: int


@dataclass(frozen=True)
class TavernRefreshParam:
    left_seconds: int
    cost_counts: dict[int, int]
    last_refresh: int
    free_count: int


@dataclass(frozen=True)
class TavernState:
    heroes: tuple[TavernHero, ...]
    refresh_params: tuple[TavernRefreshParam, ...]
    refresh_times_limit: int
    refresh_times: int


@dataclass(frozen=True)
class TavernRefreshResponse:
    ret: int
    heroes: tuple[TavernHero, ...]
    refresh_param: TavernRefreshParam | None
    free: int
    refresh_times_limit: int
    refresh_times: int
    guarantee_count: int
    cost_type: int


@dataclass(frozen=True)
class CandidateView:
    slot: int
    kind: str
    hero: TavernHero
    rare: int | None
    title: str
    display_name: str

    @property
    def is_ss(self) -> bool:
        return self.kind == "hero" and self.rare is not None and self.rare >= SS_RARITY


@dataclass(frozen=True)
class RefreshQuote:
    channel: str
    refresh_type: int
    cost_type: int
    item_id: int
    cost: int
    nominal_cost: int
    free_before: int
    used_count: int


@dataclass(frozen=True)
class TavernRefreshAttempt:
    sequence: int
    quote: RefreshQuote
    response: TavernRefreshResponse
    candidates: tuple[CandidateView, ...]
    ss_hits: tuple[CandidateView, ...]


@dataclass(frozen=True)
class TavernDailyResult:
    initial_state: TavernState
    initial_candidates: tuple[CandidateView, ...]
    attempts: tuple[TavernRefreshAttempt, ...]
    final_candidates: tuple[CandidateView, ...]
    final_auto_param: TavernRefreshParam
    final_manual_param: TavernRefreshParam
    final_refresh_times_limit: int
    final_refresh_times: int
    paused: bool
    stop_reason: str
    ss_hits: tuple[CandidateView, ...]


def _load_json_rows(path: Path, label: str) -> list[object]:
    try:
        payload = json.loads(path.expanduser().read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise HarvestError(f"读取{label}失败：{path}：{exc}") from exc
    if not isinstance(payload, list):
        raise HarvestError(f"{label}顶层必须是数组：{path}")
    return payload


def _row_int(row: object, key: str, label: str) -> int:
    if not isinstance(row, dict) or not isinstance(row.get(key), int):
        raise HarvestError(f"{label}字段 {key} 必须是整数")
    return row[key]


def _row_str(row: object, key: str, label: str) -> str:
    if not isinstance(row, dict) or not isinstance(row.get(key), str):
        raise HarvestError(f"{label}字段 {key} 必须是字符串")
    return row[key]


def _parse_cost(value: str, label: str) -> tuple[int, int]:
    parts = value.split(",")
    if len(parts) != 2:
        raise HarvestError(f"{label}格式应为 item_id,cost：{value!r}")
    try:
        item_id, cost = (int(part) for part in parts)
    except ValueError as exc:
        raise HarvestError(f"{label}包含非整数：{value!r}") from exc
    if item_id <= 0 or cost < 0:
        raise HarvestError(f"{label}的物品或费用无效：{value!r}")
    return item_id, cost


def load_tavern_catalog(
    hero_table: Path,
    hero_name_table: Path,
    refresh_fee_table: Path,
) -> TavernCatalog:
    heroes: dict[int, HeroDesign] = {}
    for index, row in enumerate(_load_json_rows(hero_table, "英雄配置")):
        label = f"英雄配置第 {index + 1} 行"
        cid = _row_int(row, "id", label)
        rare = _row_int(row, "rare", label)
        title = _row_str(row, "title", label)
        name_group = _row_int(row, "name", label)
        if cid <= 0 or rare <= 0:
            raise HarvestError(f"{label}的 id 或 rare 无效")
        if cid in heroes:
            raise HarvestError("英雄配置存在重复候选项")
        heroes[cid] = HeroDesign(cid, rare, title, name_group)

    hero_names: dict[int, str] = {}
    for index, row in enumerate(_load_json_rows(hero_name_table, "英雄名称配置")):
        label = f"英雄名称配置第 {index + 1} 行"
        name_id = _row_int(row, "id", label)
        name = _row_str(row, "name", label)
        if name_id <= 0:
            raise HarvestError(f"{label}的 id 无效")
        hero_names[name_id] = name

    refresh_fees: list[RefreshFee] = []
    for index, row in enumerate(_load_json_rows(refresh_fee_table, "刷新费用配置")):
        label = f"刷新费用配置第 {index + 1} 行"
        times = _row_int(row, "times", label)
        gold_item_id, gold_cost = _parse_cost(_row_str(row, "cost1", label), f"{label}.cost1")
        gem_item_id, gem_cost = _parse_cost(_row_str(row, "cost2", label), f"{label}.cost2")
        if times <= 0:
            raise HarvestError(f"{label}的 times 必须为正整数")
        if refresh_fees and times <= refresh_fees[-1].times:
            raise HarvestError("刷新费用配置的 times 必须严格递增")
        refresh_fees.append(
            RefreshFee(times, gold_item_id, gold_cost, gem_item_id, gem_cost)
        )

    if not heroes:
        raise HarvestError("英雄配置为空")
    if not refresh_fees or refresh_fees[0].times != 1:
        raise HarvestError("刷新费用配置必须从 times=1 开始")
    return TavernCatalog(heroes, hero_names, tuple(refresh_fees))


def _decode_int64(value: int) -> int:
    value &= (1 << 64) - 1
    return value - (1 << 64) if value & (1 << 63) else value


def _decode_utf8(value: bytes, label: str) -> str:
    try:
        return value.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise HarvestError(f"{label}不是 UTF-8 文本") from exc


def decode_tavern_hero(data: bytes) -> TavernHero:
    values: dict[str, int | str] = {
        "hero_id": 0,
        "cid": 0,
        "level": 0,
        "name_id": 0,
        "tid": 0,
        "server_name": "",
        "potential": 0,
        "pool_group_type": 0,
    }
    for field_number, wire_type, value in ProtoReader(data).fields():
        if wire_type == 0:
            number = int(value)
            if field_number == 1:
                values["hero_id"] = _decode_int64(number)
            elif field_number == 2:
                values["cid"] = decode_int32(number)
            elif field_number == 7:
                values["level"] = decode_int32(number)
            elif field_number == 9:
                values["name_id"] = decode_int32(number)
            elif field_number == 18:
                values["tid"] = _decode_int64(number)
            elif field_number == 32:
                values["potential"] = decode_int32(number)
            elif field_number == 39:
                values["pool_group_type"] = decode_int32(number)
        elif field_number == 23 and wire_type == 2:
            values["server_name"] = _decode_utf8(bytes(value), "候选英雄名称")
    return TavernHero(**values)


def _decode_refresh_cost_entry(data: bytes) -> tuple[int, int]:
    key = 0
    value = 0
    for field_number, wire_type, raw in ProtoReader(data).fields():
        if wire_type != 0:
            continue
        if field_number == 1:
            key = _decode_int64(int(raw))
        elif field_number == 2:
            value = _decode_int64(int(raw))
    return key, value


def decode_tavern_refresh_param(data: bytes) -> TavernRefreshParam:
    left_seconds = 0
    cost_counts: dict[int, int] = {}
    last_refresh = 0
    free_count = 0
    for field_number, wire_type, value in ProtoReader(data).fields():
        if field_number == 1 and wire_type == 0:
            left_seconds = _decode_int64(int(value))
        elif field_number == 2 and wire_type == 2:
            key, count = _decode_refresh_cost_entry(bytes(value))
            cost_counts[key] = count
        elif field_number == 3 and wire_type == 0:
            last_refresh = _decode_int64(int(value))
        elif field_number == 4 and wire_type == 0:
            free_count = decode_int32(int(value))
    if any(key < 0 or count < 0 for key, count in cost_counts.items()):
        raise HarvestError("服务端刷新费用计数包含负数")
    if free_count < 0:
        raise HarvestError("服务端免费刷新次数包含负数")
    return TavernRefreshParam(left_seconds, cost_counts, last_refresh, free_count)


def decode_tavern_state(data: bytes) -> TavernState:
    heroes: list[TavernHero] = []
    refresh_params: list[TavernRefreshParam] = []
    refresh_times_limit = 0
    refresh_times = 0
    for field_number, wire_type, value in ProtoReader(data).fields():
        if field_number == 2 and wire_type == 2:
            heroes.append(decode_tavern_hero(bytes(value)))
        elif field_number == 5 and wire_type == 2:
            refresh_params.append(decode_tavern_refresh_param(bytes(value)))
        elif field_number == 11 and wire_type == 0:
            refresh_times_limit = decode_int32(int(value))
        elif field_number == 12 and wire_type == 0:
            refresh_times = decode_int32(int(value))
    if len(refresh_params) < 2:
        raise HarvestError(
            f"Game_data.hero 仅包含 {len(refresh_params)} 组刷新参数，预期至少 2 组"
        )
    if refresh_times_limit < 0 or refresh_times < 0:
        raise HarvestError("Game_data.hero 的宝石刷新次数无效")
    manual = refresh_params[REFRESH_TYPE_MANUAL]
    for cost_type in (REFRESH_COST_GOLD, REFRESH_COST_GEM):
        if cost_type not in manual.cost_counts:
            raise HarvestError(f"手动刷新参数缺少 costType={cost_type} 的计数")
    return TavernState(
        tuple(heroes), tuple(refresh_params), refresh_times_limit, refresh_times
    )


def decode_tavern_state_from_game_data(data: bytes) -> TavernState:
    hero_data: bytes | None = None
    for field_number, wire_type, value in ProtoReader(data).fields():
        if field_number == 9 and wire_type == 2:
            hero_data = bytes(value)
            break
    if hero_data is None:
        raise HarvestError("Game_data 缺少 hero 数据")
    return decode_tavern_state(hero_data)


def encode_tavern_refresh_payload(
    refresh_type: int,
    cost_type: int,
    *,
    times: int = 0,
) -> bytes:
    if refresh_type not in (REFRESH_TYPE_AUTO, REFRESH_TYPE_MANUAL):
        raise HarvestError(f"刷新方式无效：{refresh_type}")
    if cost_type not in (REFRESH_COST_GOLD, REFRESH_COST_GEM):
        raise HarvestError(f"刷新消耗类型无效：{cost_type}")
    if times < 0:
        raise HarvestError("批量刷新次数不能为负数")
    payload = b""
    if refresh_type:
        payload += encode_int_field(1, refresh_type)
    if cost_type:
        payload += encode_int_field(2, cost_type)
    if times:
        payload += encode_int_field(3, times)
    return payload


def decode_tavern_refresh_response(data: bytes) -> TavernRefreshResponse:
    ret = 0
    heroes: list[TavernHero] = []
    refresh_param: TavernRefreshParam | None = None
    free = 0
    refresh_times_limit = 0
    refresh_times = 0
    guarantee_count = 0
    cost_type = 0
    for field_number, wire_type, value in ProtoReader(data).fields():
        if field_number == 1 and wire_type == 0:
            ret = decode_int32(int(value))
        elif field_number == 2 and wire_type == 2:
            heroes.append(decode_tavern_hero(bytes(value)))
        elif field_number == 3 and wire_type == 2:
            refresh_param = decode_tavern_refresh_param(bytes(value))
        elif field_number == 4 and wire_type == 0:
            free = decode_int32(int(value))
        elif field_number == 5 and wire_type == 0:
            refresh_times_limit = decode_int32(int(value))
        elif field_number == 6 and wire_type == 0:
            refresh_times = decode_int32(int(value))
        elif field_number == 7 and wire_type == 0:
            guarantee_count = decode_int32(int(value))
        elif field_number == 8 and wire_type == 0:
            cost_type = decode_int32(int(value))
    return TavernRefreshResponse(
        ret,
        tuple(heroes),
        refresh_param,
        free,
        refresh_times_limit,
        refresh_times,
        guarantee_count,
        cost_type,
    )


def decode_kickout(data: bytes) -> tuple[int, str]:
    ret = 0
    message = ""
    for field_number, wire_type, value in ProtoReader(data).fields():
        if field_number == 1 and wire_type == 0:
            ret = decode_int32(int(value))
        elif field_number == 2 and wire_type == 2:
            message = _decode_utf8(bytes(value), "Kickout.msg")
    return ret, message


def inspect_candidates(
    heroes: tuple[TavernHero, ...], catalog: TavernCatalog
) -> tuple[CandidateView, ...]:
    candidates: list[CandidateView] = []
    for slot, hero in enumerate(heroes):
        if hero.tid == 0:
            candidates.append(CandidateView(slot, "empty", hero, None, "", ""))
            continue
        if hero.pool_group_type == POOL_GROUP_HERO:
            if hero.cid <= 0:
                raise HarvestError(f"候选栏位 {slot} 的英雄 cid 无效")
            design = catalog.hero_design(hero.cid)
            display_name = hero.server_name or catalog.hero_names.get(hero.name_id, "")
            candidates.append(
                CandidateView(
                    slot,
                    "hero",
                    hero,
                    design.rare,
                    design.title,
                    display_name,
                )
            )
        elif hero.pool_group_type == POOL_GROUP_BARREL:
            candidates.append(CandidateView(slot, "barrel", hero, None, "酒桶", ""))
        elif hero.pool_group_type == POOL_GROUP_ITEM:
            candidates.append(CandidateView(slot, "item", hero, None, "命格灵核", ""))
        else:
            raise HarvestError(
                f"候选栏位 {slot} 的 poolGroupType={hero.pool_group_type} 未知"
            )
    return tuple(candidates)


def _ss_hits(candidates: tuple[CandidateView, ...]) -> tuple[CandidateView, ...]:
    return tuple(candidate for candidate in candidates if candidate.is_ss)


def quote_gold_refresh(
    catalog: TavernCatalog, manual_param: TavernRefreshParam
) -> RefreshQuote:
    used_count = manual_param.cost_counts[REFRESH_COST_GOLD]
    item_id, nominal_cost = catalog.refresh_fee(REFRESH_COST_GOLD, used_count)
    actual_cost = 0 if manual_param.free_count > 0 else nominal_cost
    return RefreshQuote(
        "gold",
        REFRESH_TYPE_MANUAL,
        REFRESH_COST_GOLD,
        item_id,
        actual_cost,
        nominal_cost,
        manual_param.free_count,
        used_count,
    )


def quote_free_gem_refresh(
    catalog: TavernCatalog,
    auto_param: TavernRefreshParam,
    manual_param: TavernRefreshParam,
) -> RefreshQuote:
    if auto_param.free_count <= 0:
        raise HarvestError("免费宝石刷新报价要求 free_count > 0")
    used_count = manual_param.cost_counts[REFRESH_COST_GEM]
    item_id, nominal_cost = catalog.refresh_fee(REFRESH_COST_GEM, used_count)
    return RefreshQuote(
        "free_gem",
        REFRESH_TYPE_AUTO,
        REFRESH_COST_GEM,
        item_id,
        0,
        nominal_cost,
        auto_param.free_count,
        used_count,
    )


def _param_signature(param: TavernRefreshParam) -> tuple[object, ...]:
    return (
        param.left_seconds,
        tuple(sorted(param.cost_counts.items())),
        param.last_refresh,
        param.free_count,
    )


class AdventurerGuildClient:
    """Read tavern state and execute only policy-approved single refreshes."""

    def __init__(
        self,
        endpoint: GameEndpoint,
        timeout: float,
        *,
        session: object | None = None,
        socket_factory: Callable[[str, float], NativeWebSocket] = NativeWebSocket.connect,
        websocket_log: Path | bool | None = True,
    ) -> None:
        from game_session import bind_shared_session

        self.endpoint = endpoint
        self.timeout = timeout
        self.socket_factory = socket_factory
        self.socket: NativeWebSocket | None = None
        self.password: str | None = None
        bind_shared_session(
            self,
            session,  # type: ignore[arg-type]
            error_cls=HarvestError,
            task="adventurer_guild",
            websocket_log=websocket_log,
        )

    def _send_message(self, message_id: int, data: bytes = b"", *, encrypted: bool) -> None:
        from game_session import session_send_message

        if session_send_message(self, message_id, data, encrypted=encrypted):
            return
        if self.socket is None:
            raise HarvestError("WebSocket 尚未连接")
        packet = encode_message_header(message_id, data)
        if encrypted:
            if not self.password:
                raise HarvestError("游戏服尚未下发会话密码")
            self.socket.send_text(pack1_encode(packet, self.password))
        else:
            self.socket.send_binary(packet)

    def _decode_frame(self, opcode: int, payload: bytes) -> MessageHeader:
        if self.password is not None:
            if opcode not in (0x1, 0x2):
                raise HarvestError(f"加密游戏报文 opcode 异常：{opcode}")
            payload = pack1_decode(payload, self.password)
        return decode_message_header(payload)

    def _receive_header(self, deadline: float, context: str) -> MessageHeader:
        from game_session import try_session_receive_header

        handled, header = try_session_receive_header(self, deadline, context)
        if handled:
            assert header is not None
            return header
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise HarvestError(f"等待{context}超时")
        try:
            assert self.socket is not None
            opcode, frame = self.socket.recv_message(remaining)
        except socket.timeout as exc:
            raise HarvestError(f"等待{context}超时") from exc
        except OSError as exc:
            raise HarvestError(f"读取{context}报文失败：{exc}") from exc
        return self._decode_frame(opcode, frame)

    def _handle_common_message(self, header: MessageHeader) -> bool:
        if header.message_id == HEARTBEAT_MESSAGE_ID:
            self._send_message(
                HEARTBEAT_RET_MESSAGE_ID,
                b"",
                encrypted=self.password is not None,
            )
            return True
        if header.message_id == LOGIN_FAIL_MESSAGE_ID:
            raise HarvestError("游戏服 Login 失败")
        if header.message_id == KICKOUT_MESSAGE_ID:
            ret, message = decode_kickout(header.data)
            raise GameSessionKickout(ret, message)
        return False

    def _login_and_read_state(self) -> TavernState:
        from game_session import try_session_ensure_ready

        if try_session_ensure_ready(self, self.endpoint):
            game_data = getattr(self._session, "game_data", None)
            if not game_data:
                raise HarvestError("未收到冒险者公会 Game_data")
            return decode_tavern_state_from_game_data(bytes(game_data))

        self.socket = self.socket_factory(self.endpoint.url, self.timeout)
        self._send_message(
            LOGIN_MESSAGE_ID,
            encode_login_payload(self.endpoint.game_token),
            encrypted=False,
        )
        login_complete = False
        state: TavernState | None = None
        deadline = time.monotonic() + self.timeout
        while not (login_complete and state is not None):
            header = self._receive_header(deadline, "游戏服登录及冒险者公会状态")
            if header.message_id == PACK_PASSWORD_MESSAGE_ID:
                encrypted_password = decode_pack_password(header.data)
                try:
                    self.password = pack1_decode(
                        encrypted_password, SOCKET_PACK_KEY
                    ).decode("utf-8")
                except UnicodeDecodeError as exc:
                    raise HarvestError("游戏服会话密码不是 UTF-8 文本") from exc
                continue
            if self._handle_common_message(header):
                continue
            if header.message_id == GAME_DATA_MESSAGE_ID:
                state = decode_tavern_state_from_game_data(header.data)
            elif header.message_id == LOGIN_REUNIQUE_MESSAGE_ID:
                login_complete = True
        time.sleep(0.1)
        return state

    def _refresh_once(self, quote: RefreshQuote) -> TavernRefreshResponse:
        payload = encode_tavern_refresh_payload(quote.refresh_type, quote.cost_type)
        self._send_message(TAVERN_REFRESH_MESSAGE_ID, payload, encrypted=True)
        deadline = time.monotonic() + self.timeout
        while True:
            header = self._receive_header(deadline, "冒险者公会刷新响应")
            if self._handle_common_message(header):
                continue
            if header.message_id != TAVERN_REFRESH_MESSAGE_ID:
                continue
            response = decode_tavern_refresh_response(header.data)
            if response.cost_type != quote.cost_type:
                raise HarvestError(
                    f"刷新响应 costType 不匹配：请求 {quote.cost_type}，"
                    f"响应 {response.cost_type}"
                )
            return response

    @staticmethod
    def _result(
        initial_state: TavernState,
        initial_candidates: tuple[CandidateView, ...],
        attempts: list[TavernRefreshAttempt],
        final_candidates: tuple[CandidateView, ...],
        auto_param: TavernRefreshParam,
        manual_param: TavernRefreshParam,
        refresh_times_limit: int,
        refresh_times: int,
        *,
        paused: bool,
        stop_reason: str,
        ss_hits: tuple[CandidateView, ...] = (),
    ) -> TavernDailyResult:
        return TavernDailyResult(
            initial_state,
            initial_candidates,
            tuple(attempts),
            final_candidates,
            auto_param,
            manual_param,
            refresh_times_limit,
            refresh_times,
            paused,
            stop_reason,
            ss_hits,
        )

    def run_daily(
        self,
        catalog: TavernCatalog,
        *,
        gold_cost_limit: int = DEFAULT_GOLD_COST_LIMIT,
        max_refreshes: int = DEFAULT_MAX_REFRESHES,
        target_refreshes: int | None = None,
    ) -> TavernDailyResult:
        if gold_cost_limit <= 0:
            raise HarvestError("金币刷新价格上限必须为正整数")
        if max_refreshes <= 0:
            raise HarvestError("单次流程刷新请求上限必须为正整数")
        if target_refreshes is not None:
            if target_refreshes <= 0:
                raise HarvestError("日常目标刷新次数必须为正整数")
            if target_refreshes > max_refreshes:
                raise HarvestError("日常目标刷新次数不能超过单次请求上限")

        try:
            initial_state = self._login_and_read_state()
            initial_candidates = inspect_candidates(initial_state.heroes, catalog)
            auto_param = initial_state.refresh_params[REFRESH_TYPE_AUTO]
            manual_param = initial_state.refresh_params[REFRESH_TYPE_MANUAL]
            refresh_times_limit = initial_state.refresh_times_limit
            refresh_times = initial_state.refresh_times
            attempts: list[TavernRefreshAttempt] = []
            final_candidates = initial_candidates

            initial_ss = _ss_hits(initial_candidates)
            if initial_ss:
                return self._result(
                    initial_state,
                    initial_candidates,
                    attempts,
                    final_candidates,
                    auto_param,
                    manual_param,
                    refresh_times_limit,
                    refresh_times,
                    paused=True,
                    stop_reason="ss_detected_initial",
                    ss_hits=initial_ss,
                )

            def target_result() -> TavernDailyResult | None:
                if (
                    target_refreshes is None
                    or len(attempts) < target_refreshes
                ):
                    return None
                return self._result(
                    initial_state,
                    initial_candidates,
                    attempts,
                    final_candidates,
                    auto_param,
                    manual_param,
                    refresh_times_limit,
                    refresh_times,
                    paused=False,
                    stop_reason="daily_target_reached",
                )

            while True:
                reached = target_result()
                if reached is not None:
                    return reached
                quote = quote_gold_refresh(catalog, manual_param)
                if quote.cost >= gold_cost_limit:
                    break
                if len(attempts) >= max_refreshes:
                    return self._result(
                        initial_state,
                        initial_candidates,
                        attempts,
                        final_candidates,
                        auto_param,
                        manual_param,
                        refresh_times_limit,
                        refresh_times,
                        paused=False,
                        stop_reason="max_refreshes_reached",
                    )

                response = self._refresh_once(quote)
                candidates = inspect_candidates(response.heroes, catalog)
                hits = _ss_hits(candidates)
                attempts.append(
                    TavernRefreshAttempt(len(attempts) + 1, quote, response, candidates, hits)
                )
                if response.ret != RESULT_SUCCESS:
                    return self._result(
                        initial_state,
                        initial_candidates,
                        attempts,
                        final_candidates,
                        auto_param,
                        manual_param,
                        refresh_times_limit,
                        refresh_times,
                        paused=False,
                        stop_reason=f"server_ret_{response.ret}",
                    )

                final_candidates = candidates
                refresh_times_limit = response.refresh_times_limit
                refresh_times = response.refresh_times
                if hits:
                    return self._result(
                        initial_state,
                        initial_candidates,
                        attempts,
                        final_candidates,
                        auto_param,
                        manual_param,
                        refresh_times_limit,
                        refresh_times,
                        paused=True,
                        stop_reason="ss_detected_after_gold",
                        ss_hits=hits,
                    )
                reached = target_result()
                if reached is not None:
                    return reached
                if response.refresh_param is None:
                    raise HarvestError("金币刷新成功响应缺少 refreshParam")
                updated_manual = response.refresh_param
                if REFRESH_COST_GOLD not in updated_manual.cost_counts:
                    raise HarvestError("金币刷新响应返回了非手动 refreshParam")
                if _param_signature(updated_manual) == _param_signature(manual_param):
                    raise HarvestError("金币刷新成功后刷新参数未推进，为避免循环已停止")
                manual_param = updated_manual

            while auto_param.free_count > 0:
                reached = target_result()
                if reached is not None:
                    return reached
                if refresh_times_limit > 0 and refresh_times >= refresh_times_limit:
                    return self._result(
                        initial_state,
                        initial_candidates,
                        attempts,
                        final_candidates,
                        auto_param,
                        manual_param,
                        refresh_times_limit,
                        refresh_times,
                        paused=False,
                        stop_reason="gem_daily_limit_reached",
                    )
                if len(attempts) >= max_refreshes:
                    return self._result(
                        initial_state,
                        initial_candidates,
                        attempts,
                        final_candidates,
                        auto_param,
                        manual_param,
                        refresh_times_limit,
                        refresh_times,
                        paused=False,
                        stop_reason="max_refreshes_reached",
                    )

                quote = quote_free_gem_refresh(catalog, auto_param, manual_param)
                response = self._refresh_once(quote)
                candidates = inspect_candidates(response.heroes, catalog)
                hits = _ss_hits(candidates)
                attempts.append(
                    TavernRefreshAttempt(len(attempts) + 1, quote, response, candidates, hits)
                )
                if response.ret != RESULT_SUCCESS:
                    return self._result(
                        initial_state,
                        initial_candidates,
                        attempts,
                        final_candidates,
                        auto_param,
                        manual_param,
                        refresh_times_limit,
                        refresh_times,
                        paused=False,
                        stop_reason=f"server_ret_{response.ret}",
                    )

                final_candidates = candidates
                refresh_times_limit = response.refresh_times_limit
                refresh_times = response.refresh_times
                if hits:
                    return self._result(
                        initial_state,
                        initial_candidates,
                        attempts,
                        final_candidates,
                        auto_param,
                        manual_param,
                        refresh_times_limit,
                        refresh_times,
                        paused=True,
                        stop_reason="ss_detected_after_free_gem",
                        ss_hits=hits,
                    )
                reached = target_result()
                if reached is not None:
                    return reached
                if response.refresh_param is None:
                    raise HarvestError("免费宝石刷新成功响应缺少 refreshParam")
                updated_auto = response.refresh_param
                if REFRESH_COST_GOLD in updated_auto.cost_counts:
                    raise HarvestError("免费宝石刷新响应返回了手动 refreshParam")
                if updated_auto.free_count >= auto_param.free_count:
                    raise HarvestError("免费宝石刷新成功后免费次数未减少，为避免循环已停止")
                auto_param = updated_auto

            return self._result(
                initial_state,
                initial_candidates,
                attempts,
                final_candidates,
                auto_param,
                manual_param,
                refresh_times_limit,
                refresh_times,
                paused=False,
                stop_reason="policy_complete",
            )
        finally:
            from game_session import shared_close

            if shared_close(self) and self.socket is not None:
                self.socket.close()
                self.socket = None


def _param_log(param: TavernRefreshParam) -> dict[str, object]:
    return {
        "left_seconds": param.left_seconds,
        "cost_counts": {str(key): value for key, value in sorted(param.cost_counts.items())},
        "last_refresh": param.last_refresh,
        "free_count": param.free_count,
    }


def _candidate_log(candidate: CandidateView) -> dict[str, object]:
    hero = candidate.hero
    return {
        "slot": candidate.slot,
        "kind": candidate.kind,
        "tid": hero.tid,
        "hero_id": hero.hero_id,
        "cid": hero.cid,
        "title": candidate.title,
        "name": candidate.display_name,
        "rare": candidate.rare,
        "rare_label": RARITY_LABELS.get(candidate.rare or 0, ""),
        "potential": hero.potential,
        "level": hero.level,
        "name_id": hero.name_id,
        "pool_group_type": hero.pool_group_type,
        "is_ss": candidate.is_ss,
    }


def build_daily_log_record(
    endpoint: GameEndpoint,
    result: TavernDailyResult,
    *,
    gold_cost_limit: int,
    max_refreshes: int,
    timestamp: str | None = None,
) -> dict[str, object]:
    initial_auto = result.initial_state.refresh_params[REFRESH_TYPE_AUTO]
    initial_manual = result.initial_state.refresh_params[REFRESH_TYPE_MANUAL]
    return {
        "timestamp": timestamp or datetime.now().astimezone().isoformat(timespec="seconds"),
        "event": "adventurer_guild_daily_auto_refresh",
        "zone": {"id": endpoint.zone_id, "name": endpoint.zone_name},
        "policy": {
            "gold_cost_operator": "<",
            "gold_cost_limit": gold_cost_limit,
            "gem_requires_free": True,
            "ss_rarity_min": SS_RARITY,
            "max_refreshes": max_refreshes,
            "channel_order": ["gold", "free_gem"],
        },
        "paused": result.paused,
        "stop_reason": result.stop_reason,
        "stop_label": STOP_LABELS.get(
            result.stop_reason,
            RESULT_LABELS.get(
                int(result.stop_reason.removeprefix("server_ret_"))
                if result.stop_reason.startswith("server_ret_")
                else -1,
                result.stop_reason,
            ),
        ),
        "request_count": len(result.attempts),
        "initial": {
            "auto_param": _param_log(initial_auto),
            "manual_param": _param_log(initial_manual),
            "refresh_times_limit": result.initial_state.refresh_times_limit,
            "refresh_times": result.initial_state.refresh_times,
            "candidates": [_candidate_log(item) for item in result.initial_candidates],
        },
        "attempts": [
            {
                "sequence": attempt.sequence,
                "channel": attempt.quote.channel,
                "request": {
                    "refresh_type": attempt.quote.refresh_type,
                    "cost_type": attempt.quote.cost_type,
                    "item_id": attempt.quote.item_id,
                    "item_name": item_name(attempt.quote.item_id),
                    "cost": attempt.quote.cost,
                    "nominal_cost": attempt.quote.nominal_cost,
                    "free_before": attempt.quote.free_before,
                    "used_count": attempt.quote.used_count,
                },
                "response": {
                    "ret": attempt.response.ret,
                    "ret_label": RESULT_LABELS.get(
                        attempt.response.ret,
                        f"服务端返回 ret={attempt.response.ret}",
                    ),
                    "cost_type": attempt.response.cost_type,
                    "free": attempt.response.free,
                    "refresh_times_limit": attempt.response.refresh_times_limit,
                    "refresh_times": attempt.response.refresh_times,
                    "guarantee_count": attempt.response.guarantee_count,
                    "refresh_param": (
                        _param_log(attempt.response.refresh_param)
                        if attempt.response.refresh_param is not None
                        else None
                    ),
                },
                "candidates": [_candidate_log(item) for item in attempt.candidates],
                "ss_hits": [_candidate_log(item) for item in attempt.ss_hits],
            }
            for attempt in result.attempts
        ],
        "final": {
            "auto_param": _param_log(result.final_auto_param),
            "manual_param": _param_log(result.final_manual_param),
            "refresh_times_limit": result.final_refresh_times_limit,
            "refresh_times": result.final_refresh_times,
            "candidates": [_candidate_log(item) for item in result.final_candidates],
        },
        "ss_hits": [_candidate_log(item) for item in result.ss_hits],
    }


def append_daily_log(
    path: Path | object | None,
    endpoint: GameEndpoint,
    result: TavernDailyResult,
    *,
    gold_cost_limit: int,
    max_refreshes: int,
    timestamp: str | None = None,
) -> None:
    record = build_daily_log_record(
        endpoint,
        result,
        gold_cost_limit=gold_cost_limit,
        max_refreshes=max_refreshes,
        timestamp=timestamp,
    )
    if result.stop_reason.startswith("server_ret_") or result.stop_reason == "max_refreshes_reached":
        outcome, level, error = "failure", "error", {"type": "TavernOutcome", "code": "refresh_failed", "message": "自动刷新未完成"}
    elif result.stop_reason.startswith("ss_detected_"):
        outcome, level, error = "skipped", "warning", {"type": "TavernOutcome", "code": "ss_detected", "message": "检测到 SS 候选，按策略暂停"}
    else:
        outcome, level, error = "success", "info", None
    details = {key: value for key, value in record.items() if key not in {"timestamp", "event", "zone"}}
    try:
        write_standard_log(event="adventurer_guild_daily_auto_refresh", operation="daily_auto_refresh", zone=record["zone"], details=details, destination=path, timestamp=record["timestamp"], outcome=outcome, level=level, error=error)
    except LogPersistenceError as exc:
        raise HarvestError(f"写入冒险者公会刷新日志失败：{exc}") from exc


def _candidate_text(candidate: CandidateView) -> str:
    if candidate.kind != "hero":
        return f"栏位 {candidate.slot + 1}: {candidate.title or candidate.kind}"
    name = f"/{candidate.display_name}" if candidate.display_name else ""
    rare = RARITY_LABELS.get(candidate.rare or 0, str(candidate.rare))
    return (
        f"栏位 {candidate.slot + 1}: {candidate.title}{name} "
        f"[{rare}] 潜力={candidate.hero.potential}"
    )


def print_daily_result(
    endpoint: GameEndpoint,
    result: TavernDailyResult,
    catalog: TavernCatalog,
    *,
    gold_cost_limit: int,
) -> None:
    initial_manual = result.initial_state.refresh_params[REFRESH_TYPE_MANUAL]
    initial_auto = result.initial_state.refresh_params[REFRESH_TYPE_AUTO]
    initial_gold = quote_gold_refresh(catalog, initial_manual)
    print(f"冒险者公会每日自动刷新完成，区服：{zone_name(endpoint.zone_id, endpoint.zone_name)}")
    print(
        f"初始金币单次价格：{initial_gold.cost}（规则 < {gold_cost_limit}）；"
        f"免费宝石次数：{initial_auto.free_count}"
    )
    for attempt in result.attempts:
        channel = "金币" if attempt.quote.channel == "gold" else "免费宝石"
        status = RESULT_LABELS.get(
            attempt.response.ret, f"服务端返回 ret={attempt.response.ret}"
        )
        print(
            f"第 {attempt.sequence} 次 [{channel}]：{status}；"
            f"实际费用={attempt.quote.cost}"
        )
        for candidate in attempt.candidates:
            if candidate.kind == "hero":
                marker = " [命中 SS，停止]" if candidate.is_ss else ""
                print(f"  {_candidate_text(candidate)}{marker}")
    print(f"停止原因：{STOP_LABELS.get(result.stop_reason, result.stop_reason)}")
    if result.paused:
        print("自动流程已暂停，未发送任何后续刷新请求。")
        for candidate in result.ss_hits:
            print(f"SS：{_candidate_text(candidate)}")


def _encode_refresh_param_for_test(
    *,
    gold_count: int | None = None,
    gem_count: int | None = None,
    free_count: int = 0,
) -> bytes:
    payload = b""
    if gold_count is not None:
        entry = encode_int_field(1, REFRESH_COST_GOLD) + encode_int_field(2, gold_count)
        payload += encode_bytes_field(2, entry)
    if gem_count is not None:
        entry = encode_int_field(1, REFRESH_COST_GEM) + encode_int_field(2, gem_count)
        payload += encode_bytes_field(2, entry)
    if free_count:
        payload += encode_int_field(4, free_count)
    return payload


def _encode_tavern_hero_for_test(
    cid: int,
    *,
    tid: int,
    potential: int,
    name: str = "",
) -> bytes:
    payload = (
        encode_int_field(1, tid)
        + encode_int_field(2, cid)
        + encode_int_field(7, 1)
        + encode_int_field(18, tid)
        + encode_int_field(32, potential)
    )
    if name:
        payload += encode_bytes_field(23, name.encode("utf-8"))
    return payload


def _encode_game_data_for_test(
    hero: bytes,
    auto_param: bytes,
    manual_param: bytes,
    *,
    refresh_times_limit: int = 10,
    refresh_times: int = 0,
) -> bytes:
    hero_state = (
        encode_bytes_field(2, hero)
        + encode_bytes_field(5, auto_param)
        + encode_bytes_field(5, manual_param)
        + encode_int_field(11, refresh_times_limit)
        + encode_int_field(12, refresh_times)
    )
    return encode_bytes_field(9, hero_state)


def _encode_refresh_response_for_test(
    hero: bytes,
    refresh_param: bytes,
    *,
    cost_type: int,
    ret: int = 0,
    refresh_times_limit: int = 10,
    refresh_times: int = 0,
) -> bytes:
    payload = b""
    if ret:
        payload += encode_int_field(1, ret)
    payload += encode_bytes_field(2, hero)
    payload += encode_bytes_field(3, refresh_param)
    payload += encode_int_field(5, refresh_times_limit)
    payload += encode_int_field(6, refresh_times)
    if cost_type:
        payload += encode_int_field(8, cost_type)
    return payload


def run_self_tests(
    hero_table: Path = DEFAULT_HERO_TABLE,
    hero_name_table: Path = DEFAULT_HERO_NAME_TABLE,
    refresh_fee_table: Path = DEFAULT_REFRESH_FEE_TABLE,
) -> None:
    catalog = load_tavern_catalog(hero_table, hero_name_table, refresh_fee_table)
    s_cid = next(cid for cid, design in catalog.heroes.items() if design.rare == 5)
    ss_cid = next(cid for cid, design in catalog.heroes.items() if design.rare == SS_RARITY)

    assert catalog.refresh_fee(REFRESH_COST_GOLD, 0)[1] == 100
    assert catalog.refresh_fee(REFRESH_COST_GOLD, 2)[1] == 120
    assert catalog.refresh_fee(REFRESH_COST_GOLD, 8)[1] == 180
    assert catalog.refresh_fee(REFRESH_COST_GOLD, 10)[1] == 200

    manual_free = TavernRefreshParam(0, {0: 10, 1: 0}, 0, 1)
    assert quote_gold_refresh(catalog, manual_free).cost == 0
    assert encode_tavern_refresh_payload(REFRESH_TYPE_MANUAL, REFRESH_COST_GOLD) == b"\x08\x01"
    assert encode_tavern_refresh_payload(REFRESH_TYPE_AUTO, REFRESH_COST_GEM) == b"\x10\x01"

    decoded_hero = decode_tavern_hero(
        _encode_tavern_hero_for_test(ss_cid, tid=7001, potential=88, name="测试英雄")
    )
    assert decoded_hero.cid == ss_cid
    assert decoded_hero.potential == 88
    assert decoded_hero.server_name == "测试英雄"

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

        def recv_message(self, timeout: float) -> tuple[int, bytes]:
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

    def encrypted(message_id: int, payload: bytes = b"") -> tuple[int, bytes]:
        packet = encode_message_header(message_id, payload)
        return 0x2, pack1_encode(packet, session_password).encode("utf-8")

    initial_s = _encode_tavern_hero_for_test(s_cid, tid=6001, potential=70)
    initial_auto = _encode_refresh_param_for_test(free_count=1)
    initial_manual = _encode_refresh_param_for_test(gold_count=0, gem_count=0)
    game_data = _encode_game_data_for_test(initial_s, initial_auto, initial_manual)
    ss_response = _encode_refresh_response_for_test(
        _encode_tavern_hero_for_test(ss_cid, tid=6002, potential=91, name="命中测试"),
        _encode_refresh_param_for_test(gold_count=1, gem_count=0),
        cost_type=REFRESH_COST_GOLD,
    )
    gold_socket = TestSocket(
        [
            (0x2, encode_message_header(PACK_PASSWORD_MESSAGE_ID, password_payload)),
            encrypted(GAME_DATA_MESSAGE_ID, game_data),
            encrypted(LOGIN_REUNIQUE_MESSAGE_ID),
            encrypted(TAVERN_REFRESH_MESSAGE_ID, ss_response),
        ]
    )
    endpoint = GameEndpoint("ws://test.invalid", "game-token", "1", "test")
    gold_result = AdventurerGuildClient(
        endpoint,
        1.0,
        socket_factory=lambda _url, _timeout: gold_socket,
    ).run_daily(catalog)
    assert gold_result.paused
    assert gold_result.stop_reason == "ss_detected_after_gold"
    assert len(gold_result.attempts) == 1
    assert gold_result.ss_hits[0].hero.cid == ss_cid
    assert gold_socket.closed
    assert len(gold_socket.text_frames) == 1
    gold_request = decode_message_header(
        pack1_decode(gold_socket.text_frames[0], session_password)
    )
    assert gold_request.data == encode_tavern_refresh_payload(
        REFRESH_TYPE_MANUAL, REFRESH_COST_GOLD
    )

    threshold_manual = _encode_refresh_param_for_test(gold_count=10, gem_count=0)
    gem_game_data = _encode_game_data_for_test(initial_s, initial_auto, threshold_manual)
    gem_response = _encode_refresh_response_for_test(
        _encode_tavern_hero_for_test(s_cid, tid=6003, potential=72),
        _encode_refresh_param_for_test(free_count=0),
        cost_type=REFRESH_COST_GEM,
        refresh_times=1,
    )
    gem_socket = TestSocket(
        [
            (0x2, encode_message_header(PACK_PASSWORD_MESSAGE_ID, password_payload)),
            encrypted(GAME_DATA_MESSAGE_ID, gem_game_data),
            encrypted(LOGIN_REUNIQUE_MESSAGE_ID),
            encrypted(TAVERN_REFRESH_MESSAGE_ID, gem_response),
        ]
    )
    gem_result = AdventurerGuildClient(
        endpoint,
        1.0,
        socket_factory=lambda _url, _timeout: gem_socket,
    ).run_daily(catalog)
    assert gem_result.stop_reason == "policy_complete"
    assert len(gem_result.attempts) == 1
    assert gem_result.attempts[0].quote.channel == "free_gem"
    gem_request = decode_message_header(
        pack1_decode(gem_socket.text_frames[0], session_password)
    )
    assert gem_request.data == encode_tavern_refresh_payload(
        REFRESH_TYPE_AUTO, REFRESH_COST_GEM
    )

    # 日常补足模式允许 200 金币单价，以便在已用过高价刷新后仍能完成 5 次目标。
    daily_gold_limit = 201
    assert quote_gold_refresh(
        catalog, TavernRefreshParam(0, {REFRESH_COST_GOLD: 10, REFRESH_COST_GEM: 0}, 0, 0)
    ).cost == 200
    max_price_game_data = _encode_game_data_for_test(
        initial_s,
        _encode_refresh_param_for_test(free_count=0),
        threshold_manual,
    )
    max_price_responses = [
        _encode_refresh_response_for_test(
            _encode_tavern_hero_for_test(s_cid, tid=6010 + index, potential=70),
            _encode_refresh_param_for_test(gold_count=10 + index, gem_count=0),
            cost_type=REFRESH_COST_GOLD,
        )
        for index in range(1, 3)
    ]
    max_price_socket = TestSocket(
        [
            (0x2, encode_message_header(PACK_PASSWORD_MESSAGE_ID, password_payload)),
            encrypted(GAME_DATA_MESSAGE_ID, max_price_game_data),
            encrypted(LOGIN_REUNIQUE_MESSAGE_ID),
            encrypted(TAVERN_REFRESH_MESSAGE_ID, max_price_responses[0]),
            encrypted(TAVERN_REFRESH_MESSAGE_ID, max_price_responses[1]),
        ]
    )
    max_price_result = AdventurerGuildClient(
        endpoint,
        1.0,
        socket_factory=lambda _url, _timeout: max_price_socket,
    ).run_daily(
        catalog,
        gold_cost_limit=daily_gold_limit,
        max_refreshes=2,
        target_refreshes=2,
    )
    assert max_price_result.stop_reason == "daily_target_reached"
    assert len(max_price_result.attempts) == 2
    assert all(item.quote.channel == "gold" for item in max_price_result.attempts)
    assert all(item.quote.cost == 200 for item in max_price_result.attempts)

    initial_ss_game_data = _encode_game_data_for_test(
        _encode_tavern_hero_for_test(ss_cid, tid=6004, potential=95),
        initial_auto,
        initial_manual,
    )
    initial_ss_socket = TestSocket(
        [
            (0x2, encode_message_header(PACK_PASSWORD_MESSAGE_ID, password_payload)),
            encrypted(GAME_DATA_MESSAGE_ID, initial_ss_game_data),
            encrypted(LOGIN_REUNIQUE_MESSAGE_ID),
        ]
    )
    initial_ss_result = AdventurerGuildClient(
        endpoint,
        1.0,
        socket_factory=lambda _url, _timeout: initial_ss_socket,
    ).run_daily(catalog)
    assert initial_ss_result.stop_reason == "ss_detected_initial"
    assert initial_ss_result.paused
    assert not initial_ss_socket.text_frames

    with tempfile.TemporaryDirectory() as temporary_directory:
        result_log = Path(temporary_directory) / "tavern.jsonl"
        append_daily_log(
            result_log,
            endpoint,
            gold_result,
            gold_cost_limit=DEFAULT_GOLD_COST_LIMIT,
            max_refreshes=DEFAULT_MAX_REFRESHES,
            timestamp="2026-07-19T21:00:00+08:00",
        )
        log_text = result_log.read_text(encoding="utf-8")
        log_record = json.loads(log_text)
        assert log_record["details"]["paused"] is True
        assert log_record["details"]["stop_reason"] == "ss_detected_after_gold"
        assert log_record["details"]["request_count"] == 1
        assert log_record["details"]["attempts"][0]["request"]["cost"] == 100
        assert log_record["details"]["ss_hits"][0]["rare_label"] == "SS"
        assert "token" not in log_text.lower()


def build_parser() -> argparse.ArgumentParser:
    parser = build_base_parser()
    parser.prog = "adventurer_guild_daily_auto_refresh.py"
    parser.description = __doc__
    parser.add_argument(
        "--hero-table",
        type=Path,
        default=DEFAULT_HERO_TABLE,
        help="英雄品质配置，默认 decrypted-tavern-data/heroes.json。",
    )
    parser.add_argument(
        "--hero-name-table",
        type=Path,
        default=DEFAULT_HERO_NAME_TABLE,
        help="英雄名称配置，默认 decrypted-tavern-data/heroname.json。",
    )
    parser.add_argument(
        "--refresh-fee-table",
        type=Path,
        default=DEFAULT_REFRESH_FEE_TABLE,
        help="刷新费用配置，默认 decrypted-tavern-data/refreshfee.json。",
    )
    parser.add_argument(
        "--gold-cost-limit",
        type=int,
        default=DEFAULT_GOLD_COST_LIMIT,
        help="仅当金币单次价格严格小于该值时刷新；默认 200。",
    )
    parser.add_argument(
        "--max-refreshes",
        type=int,
        default=DEFAULT_MAX_REFRESHES,
        help="单次运行最多发送的刷新请求数；默认 20。",
    )
    parser.add_argument(
        "--result-log",
        type=Path,
        default=DEFAULT_RESULT_LOG,
        help="JSONL 结果日志路径。",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.self_test:
        try:
            run_self_tests(args.hero_table, args.hero_name_table, args.refresh_fee_table)
        except HarvestError as exc:
            print(f"冒险者公会本地协议自检失败：{exc}", file=sys.stderr)
            return 1
        print("冒险者公会每日自动刷新本地协议自检通过")
        return 0

    try:
        catalog = load_tavern_catalog(
            args.hero_table, args.hero_name_table, args.refresh_fee_table
        )
        tokens = load_tokens(args.token_file)
        for attempt in range(2):
            endpoint = resolve_game_endpoint(tokens, args)
            try:
                result = AdventurerGuildClient(endpoint, args.timeout).run_daily(
                    catalog,
                    gold_cost_limit=args.gold_cost_limit,
                    max_refreshes=args.max_refreshes,
                )
                break
            except GameSessionKickout as exc:
                if exc.ret != 2 or attempt > 0:
                    raise
                print(
                    "[登录] 收到 Kickout ret=2，"
                    f"{LOGIN_KICKOUT_RETRY_DELAY:g} 秒后重新获取游戏服会话并重试一次。"
                )
                time.sleep(LOGIN_KICKOUT_RETRY_DELAY)
        else:
            raise AssertionError("登录重试循环未返回")
    except HarvestError as exc:
        print(f"冒险者公会每日自动刷新失败：{exc}", file=sys.stderr)
        return 1

    print_daily_result(
        endpoint,
        result,
        catalog,
        gold_cost_limit=args.gold_cost_limit,
    )
    try:
        append_daily_log(
            args.result_log,
            endpoint,
            result,
            gold_cost_limit=args.gold_cost_limit,
            max_refreshes=args.max_refreshes,
        )
    except HarvestError as exc:
        print(f"冒险者公会刷新日志记录失败：{exc}", file=sys.stderr)
        return 1
    if args.result_log is MANAGED_DESTINATION:
        print("结果日志：logs/adventurer_guild_daily_auto_refresh/<日期>.jsonl")
    else:
        print(f"结果日志：{args.result_log.expanduser().resolve()}")

    if result.stop_reason.startswith("server_ret_"):
        return 1
    if result.stop_reason == "max_refreshes_reached":
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
