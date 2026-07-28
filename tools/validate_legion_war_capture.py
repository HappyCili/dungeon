#!/usr/bin/env python3
"""Validate a JSONL capture emitted by ``capture_legion_war_frida.py``."""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict, deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Deque, Iterable, Mapping


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from harvest_fief import ProtoReader, decode_int32  # noqa: E402
from legion_war import (  # noqa: E402
    LEGION_BATTLE_CHOOSE_STRATEGY_MESSAGE_ID,
    LEGION_BATTLE_END_SUMMARY_MESSAGE_ID,
    LEGION_BATTLE_START_MESSAGE_ID,
    LEGION_BATTLE_SYNC_MESSAGE_ID,
    LEGION_INFO_SYNC_MESSAGE_ID,
    LEGION_UPGRADE_TROOPS_LEVEL_MESSAGE_ID,
    PULL_GACHA_BANNER_V2_MESSAGE_ID,
    SIEGE_COLLECT_TAX_MESSAGE_ID,
    SIEGE_RESCUE_TOWN_MESSAGE_ID,
    SIEGE_SYNC_MESSAGE_ID,
    decode_legion_battle_state,
    decode_legion_info,
    decode_siege_state,
    load_strategy_rarities,
    select_battle_officers,
)


REQUEST_RESPONSE_IDS = frozenset(
    {
        PULL_GACHA_BANNER_V2_MESSAGE_ID,
        LEGION_BATTLE_START_MESSAGE_ID,
        LEGION_BATTLE_CHOOSE_STRATEGY_MESSAGE_ID,
        LEGION_UPGRADE_TROOPS_LEVEL_MESSAGE_ID,
        SIEGE_SYNC_MESSAGE_ID,
        SIEGE_RESCUE_TOWN_MESSAGE_ID,
        SIEGE_COLLECT_TAX_MESSAGE_ID,
    }
)


def first_int(data: bytes, field_number: int, default: int = 0) -> int:
    for number, wire_type, value in ProtoReader(data).fields():
        if number == field_number and wire_type == 0:
            return decode_int32(int(value))
    return default


def first_bytes(data: bytes, field_number: int) -> bytes | None:
    for number, wire_type, value in ProtoReader(data).fields():
        if number == field_number and wire_type == 2:
            return bytes(value)
    return None


def packed_ints(data: bytes) -> tuple[int, ...]:
    reader = ProtoReader(data)
    values: list[int] = []
    while reader.position < len(data):
        values.append(decode_int32(reader.read_varint()))
    return tuple(values)


@dataclass
class CaptureReport:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    request_counts: dict[int, int] = field(default_factory=lambda: defaultdict(int))
    response_counts: dict[int, int] = field(default_factory=lambda: defaultdict(int))
    siege_wins: int = 0
    last_siege_state: object | None = None
    current_battle: object | None = None
    latest_officers: object | None = None
    battle_failed: bool = False


def load_records(path: Path) -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as error:
            raise ValueError(f"第 {line_number} 行不是 JSON：{error}") from error
        if record.get("type") != "legion_ws":
            continue
        if record.get("truncated"):
            raise ValueError(f"第 {line_number} 行报文被截断，不能用于校验")
        records.append(record)
    return records


def payload(record: Mapping[str, object]) -> bytes:
    value = record.get("payload_hex")
    if not isinstance(value, str):
        raise ValueError("记录缺少 payload_hex")
    return bytes.fromhex(value)


def validate(records: Iterable[Mapping[str, object]]) -> CaptureReport:
    report = CaptureReport()
    pending: dict[int, Deque[tuple[int, bytes]]] = defaultdict(deque)
    strategy_rarities: Mapping[int, int] = {}
    try:
        strategy_rarities = load_strategy_rarities()
    except Exception as error:
        report.warnings.append(f"未加载战术品质表，跳过最高品质检查：{error}")

    for index, record in enumerate(records, 1):
        direction = record.get("direction")
        message_id = record.get("message_id")
        if direction not in {"C->S", "S->C"} or not isinstance(message_id, int):
            report.errors.append(f"#{index}: 无效的方向或消息 ID")
            continue
        try:
            data = payload(record)
        except ValueError as error:
            report.errors.append(f"#{index}: {error}")
            continue

        if direction == "C->S":
            report.request_counts[message_id] += 1
            if message_id in REQUEST_RESPONSE_IDS:
                pending[message_id].append((index, data))

            if message_id == SIEGE_RESCUE_TOWN_MESSAGE_ID:
                town_id = first_int(data, 1)
                state = report.last_siege_state
                current_towns = {town.town_id for town in state.towns if town.trapped} if state else set()
                if town_id <= 0:
                    report.errors.append(f"#{index}: Siege_rescue_town 缺少城堡 ID")
                elif state is None:
                    report.errors.append(f"#{index}: 未同步围攻状态就出击城堡 {town_id}")
                elif town_id not in current_towns:
                    report.errors.append(f"#{index}: 城堡 {town_id} 不在当前可出击围攻列表")

            elif message_id == LEGION_BATTLE_START_MESSAGE_ID:
                battle_id = first_int(data, 1)
                event_id = first_int(data, 2)
                officers = packed_ints(first_bytes(data, 3) or b"")
                battle = report.current_battle
                if battle is None or battle.turn > 0:
                    report.errors.append(f"#{index}: 非开始阶段发送 Legion_battle_start")
                elif (battle_id, event_id) != (battle.battle_id, battle.event_id):
                    report.errors.append(
                        f"#{index}: 出击 battle/event 不匹配，收到 {battle_id}/{event_id}，"
                        f"期望 {battle.battle_id}/{battle.event_id}"
                    )
                if not officers:
                    report.errors.append(f"#{index}: Legion_battle_start 未携带军官")
                if report.latest_officers is not None:
                    expected = select_battle_officers(report.latest_officers.officers)
                    if officers != expected:
                        report.errors.append(
                            f"#{index}: 军官顺序 {officers}，期望按品质/等级/ID 排序的 {expected}"
                        )

            elif message_id == LEGION_BATTLE_CHOOSE_STRATEGY_MESSAGE_ID:
                strategy_id = first_int(data, 1)
                battle = report.current_battle
                if battle is None or battle.turn <= 0:
                    report.errors.append(f"#{index}: 没有可选战术回合时发送战术 {strategy_id}")
                elif strategy_id not in battle.strategy_ids:
                    report.errors.append(f"#{index}: 战术 {strategy_id} 不在服务端候选 {battle.strategy_ids}")
                elif strategy_rarities:
                    offered = [strategy_rarities.get(item) for item in battle.strategy_ids]
                    chosen = strategy_rarities.get(strategy_id)
                    if None in offered or chosen is None:
                        report.errors.append(f"#{index}: 战术配置缺少候选或所选 ID")
                    elif chosen != max(offered):
                        report.errors.append(f"#{index}: 战术 {strategy_id} 不是候选中的最高品质")

            elif message_id == SIEGE_COLLECT_TAX_MESSAGE_ID:
                state = report.last_siege_state
                if report.battle_failed:
                    report.errors.append(f"#{index}: 存在失败围攻后仍继续收税")
                elif state is None:
                    report.errors.append(f"#{index}: 未同步围攻状态就收税")
                elif any(town.trapped for town in state.towns):
                    report.errors.append(f"#{index}: 尚有围攻城堡时收税")
                elif state.today_tax_collected:
                    report.errors.append(f"#{index}: 当日税收已领取仍发送收税请求")

            elif message_id == PULL_GACHA_BANNER_V2_MESSAGE_ID:
                expected = (first_int(data, 1), first_int(data, 2), first_int(data, 3), first_int(data, 6))
                if expected not in {(1, 1, 2, 80), (1, 5, 2, 80)}:
                    report.errors.append(f"#{index}: 军官招募参数错误 {expected}，期望 banner/category/costItem=1/*/2/80")
                if report.battle_failed:
                    report.errors.append(f"#{index}: 存在失败围攻后仍继续招募")

            elif message_id == LEGION_UPGRADE_TROOPS_LEVEL_MESSAGE_ID:
                if data:
                    report.errors.append(f"#{index}: 募兵升级请求应为空载荷")
                if report.battle_failed:
                    report.errors.append(f"#{index}: 存在失败围攻后仍继续升级募兵")

            continue

        report.response_counts[message_id] += 1
        request = pending[message_id].popleft() if pending[message_id] else None
        if message_id in REQUEST_RESPONSE_IDS and request is None:
            report.warnings.append(f"#{index}: 收到无匹配请求的响应 {message_id}")

        if message_id == LEGION_INFO_SYNC_MESSAGE_ID:
            report.latest_officers = decode_legion_info(data)
        elif message_id == SIEGE_SYNC_MESSAGE_ID:
            report.last_siege_state = decode_siege_state(data)
        elif message_id == SIEGE_RESCUE_TOWN_MESSAGE_ID and request is not None:
            requested_town = first_int(request[1], 1)
            returned_town = first_int(data, 1)
            result = first_int(data, 10)
            if result != 0:
                report.errors.append(f"#{index}: 城堡 {requested_town} 出击响应 ret={result}")
            if returned_town not in {0, requested_town}:
                report.errors.append(f"#{index}: 城堡出击响应 ID={returned_town}，请求 ID={requested_town}")
        elif message_id == LEGION_BATTLE_SYNC_MESSAGE_ID:
            report.current_battle = decode_legion_battle_state(data)
        elif message_id == LEGION_BATTLE_START_MESSAGE_ID and request is not None:
            result = first_int(data, 10)
            if result != 0:
                report.errors.append(f"#{index}: 军团出击响应 ret={result}")
        elif message_id == LEGION_BATTLE_CHOOSE_STRATEGY_MESSAGE_ID and request is not None:
            result = first_int(data, 2)
            if result != 0:
                report.errors.append(f"#{index}: 战术选择响应 ret={result}")
        elif message_id == LEGION_BATTLE_END_SUMMARY_MESSAGE_ID:
            result = first_int(data, 1)
            if result == 1:
                report.siege_wins += 1
            else:
                report.battle_failed = True
                report.errors.append(f"#{index}: 军团战结算失败 result={result}")
        elif message_id == SIEGE_COLLECT_TAX_MESSAGE_ID and request is not None:
            result = first_int(data, 10)
            if result != 0:
                report.errors.append(f"#{index}: 收税响应 ret={result}")
        elif message_id == PULL_GACHA_BANNER_V2_MESSAGE_ID and request is not None:
            result = first_int(data, 20)
            if result != 0:
                report.errors.append(f"#{index}: 军官招募响应 ret={result}")
        elif message_id == LEGION_UPGRADE_TROOPS_LEVEL_MESSAGE_ID and request is not None:
            result = first_int(data, 10)
            if result != 0:
                report.errors.append(f"#{index}: 募兵升级响应 ret={result}")

    for message_id, requests in pending.items():
        for request_index, _ in requests:
            report.errors.append(f"#{request_index}: 请求 {message_id} 未收到对应响应")
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("capture", type=Path, help="Frida JSONL 日志")
    args = parser.parse_args()
    records = load_records(args.capture)
    if not records:
        print("军团战 Frida 校验失败：日志没有军团战协议。")
        return 1
    report = validate(records)
    state = "通过" if not report.errors else "失败"
    print(
        f"军团战 Frida 校验{state}：报文 {len(records)} 条；"
        f"围攻胜利 {report.siege_wins} 场；"
        f"收税请求 {report.request_counts[SIEGE_COLLECT_TAX_MESSAGE_ID]} 次；"
        f"招募请求 {report.request_counts[PULL_GACHA_BANNER_V2_MESSAGE_ID]} 次；"
        f"募兵升级请求 {report.request_counts[LEGION_UPGRADE_TROOPS_LEVEL_MESSAGE_ID]} 次。"
    )
    for warning in report.warnings:
        print(f"警告：{warning}")
    for error in report.errors:
        print(f"错误：{error}")
    return 0 if not report.errors else 2


if __name__ == "__main__":
    raise SystemExit(main())
