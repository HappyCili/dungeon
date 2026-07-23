"""Project-local lookups for turning protocol IDs into readable names."""

from __future__ import annotations

import json
from functools import lru_cache
from typing import Any, Mapping

from project_paths import NATIVE_APP_ROOT, UI_APP_ROOT

PROJECT_ROOT = UI_APP_ROOT

REWARD_KIND_LABELS = {
    1: "物品",
    2: "奖励箱",
    3: "装备",
    4: "秘宝",
    5: "英雄",
    6: "律文",
    7: "活动装备",
}


def unknown_name(entity: str, identifier: object) -> str:
    """Return the consistent display fallback for an unmapped identifier."""

    return f"未知{entity}（ID {identifier}）"


def _positive_int(value: object) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return value
    if isinstance(value, str):
        try:
            parsed = int(value)
        except ValueError:
            return None
        return parsed if parsed > 0 else None
    return None


def _text(value: object) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


@lru_cache(maxsize=None)
def _json_rows(relative_path: str) -> tuple[Mapping[str, Any], ...]:
    try:
        root = NATIVE_APP_ROOT if relative_path.startswith("decrypted-") else PROJECT_ROOT
        payload = json.loads((root / relative_path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return ()
    if isinstance(payload, Mapping):
        rows = payload.values()
    elif isinstance(payload, list):
        rows = payload
    else:
        return ()
    return tuple(row for row in rows if isinstance(row, Mapping))


@lru_cache(maxsize=None)
def _name_table(relative_path: str) -> dict[int, str]:
    names: dict[int, str] = {}
    for row in _json_rows(relative_path):
        identifier = _positive_int(row.get("id"))
        name = _text(row.get("name"))
        if identifier is not None and name is not None:
            names[identifier] = name
    return names


def _known_name(table: Mapping[int, str], identifier: object) -> str | None:
    parsed = _positive_int(identifier)
    return table.get(parsed) if parsed is not None else None


@lru_cache(maxsize=1)
def _item_names() -> dict[int, str]:
    names = dict(_name_table("decrypted-data/item.json"))
    names.update(_name_table("item_id_map.json"))
    return names


def item_name(item_id: object) -> str:
    return _known_name(_item_names(), item_id) or unknown_name("物品", item_id)


def item_name_or_none(item_id: object) -> str | None:
    return _known_name(_item_names(), item_id)


def dungeon_name(dungeon_id: object) -> str:
    return _known_name(_name_table("decrypted-data/dungeon.json"), dungeon_id) or unknown_name(
        "地下城", dungeon_id
    )


def treasure_area_name(area_id: object) -> str:
    """Resolve a treasure-area map id to a Chinese name when a table exists.

    Prefer ``mapareas`` / ``map`` catalogs under native decrypted data. Without
    an authoritative row, fall back to the standard unknown label — never invent
    a display name from unrelated item text.
    """

    for relative_path in (
        "decrypted-data/mapareas.json",
        "decrypted-data/map.json",
        "decrypted-data/maps.json",
    ):
        known = _known_name(_name_table(relative_path), area_id)
        if known is not None:
            return known
    return unknown_name("聚宝地图", area_id)


def reward_box_name(reward_id: object) -> str:
    return _known_name(_name_table("decrypted-data/rewardbox.json"), reward_id) or unknown_name(
        "奖励箱", reward_id
    )


def rune_name(rune_id: object) -> str:
    return _known_name(
        _name_table("decrypted-data/rune-tables/orderrune.json"), rune_id
    ) or unknown_name("律文", rune_id)


def artifact_name(artifact_id: object) -> str:
    return _known_name(
        _name_table("decrypted-data/tables/artifact.json"), artifact_id
    ) or unknown_name("秘宝", artifact_id)


@lru_cache(maxsize=1)
def _artifact_rarities() -> dict[int, int]:
    rarities: dict[int, int] = {}
    for row in _json_rows("decrypted-data/tables/artifact.json"):
        identifier = _positive_int(row.get("id"))
        rarity = row.get("rarity")
        if (
            identifier is not None
            and isinstance(rarity, int)
            and not isinstance(rarity, bool)
            and rarity > 0
        ):
            rarities[identifier] = rarity
    return rarities


def artifact_rarity(artifact_id: object) -> int:
    """返回秘宝配置 rarity；未知时为 0。"""

    parsed = _positive_int(artifact_id)
    if parsed is None:
        return 0
    return _artifact_rarities().get(parsed, 0)


# 客户端 item type：ArtifactPiece = 15。
ARTIFACT_PIECE_ITEM_TYPE = 15


@lru_cache(maxsize=1)
def _item_types() -> dict[int, int]:
    types: dict[int, int] = {}
    for row in _json_rows("decrypted-data/item.json"):
        identifier = _positive_int(row.get("id"))
        item_type = row.get("type")
        if (
            identifier is not None
            and isinstance(item_type, int)
            and not isinstance(item_type, bool)
        ):
            types[identifier] = item_type
    return types


@lru_cache(maxsize=1)
def _item_qualities() -> dict[int, int]:
    qualities: dict[int, int] = {}
    for row in _json_rows("decrypted-data/item.json"):
        identifier = _positive_int(row.get("id"))
        quality = row.get("quality")
        if (
            identifier is not None
            and isinstance(quality, int)
            and not isinstance(quality, bool)
            and quality > 0
        ):
            qualities[identifier] = quality
    return qualities


@lru_cache(maxsize=1)
def _artifact_ids_by_piece_item() -> dict[int, int]:
    mapping: dict[int, int] = {}
    for row in _json_rows("decrypted-data/tables/artifact.json"):
        artifact_id = _positive_int(row.get("id"))
        piece_id = _positive_int(row.get("itemid"))
        if artifact_id is not None and piece_id is not None:
            mapping[piece_id] = artifact_id
    return mapping


def is_artifact_piece_item(item_id: object) -> bool:
    """物品是否为秘宝碎片（item.type == ArtifactPiece）。"""

    parsed = _positive_int(item_id)
    if parsed is None:
        return False
    return _item_types().get(parsed) == ARTIFACT_PIECE_ITEM_TYPE


def item_quality(item_id: object) -> int:
    parsed = _positive_int(item_id)
    if parsed is None:
        return 0
    return _item_qualities().get(parsed, 0)


def artifact_id_for_piece_item(item_id: object) -> int:
    """碎片物品 ID 对应的秘宝 ID；未知时为 0。"""

    parsed = _positive_int(item_id)
    if parsed is None:
        return 0
    return _artifact_ids_by_piece_item().get(parsed, 0)


def hero_name(hero_id: object) -> str:
    direct = _known_name(_name_table("decrypted-tavern-data/heroname.json"), hero_id)
    if direct is not None:
        return direct
    for row in _json_rows("decrypted-tavern-data/heroes.json"):
        if _positive_int(row.get("id")) != _positive_int(hero_id):
            continue
        name = _known_name(_name_table("decrypted-tavern-data/heroname.json"), row.get("name"))
        title = _text(row.get("title"))
        if name and title:
            return f"{title} {name}"
        if name or title:
            return name or title  # type: ignore[return-value]
    return unknown_name("英雄", hero_id)


def daily_task_name(daily_id: object) -> str:
    parsed = _positive_int(daily_id)
    if parsed is not None:
        for row in _json_rows("decrypted-task-data/daily_quest.json"):
            if _positive_int(row.get("id")) != parsed:
                continue
            quest_id = row.get("questid")
            known = _known_name(_name_table("decrypted-task-data/quests.json"), quest_id)
            if known is not None:
                return known
    return unknown_name("日常任务", daily_id)


def quest_name(quest_id: object) -> str:
    return _known_name(_name_table("decrypted-task-data/quests.json"), quest_id) or unknown_name(
        "任务", quest_id
    )


def activity_reward_name(reward_id: object) -> str:
    parsed = _positive_int(reward_id)
    if parsed is not None:
        for row in _json_rows("decrypted-task-data/activityreward.json"):
            if _positive_int(row.get("id")) != parsed:
                continue
            reward_box_id = row.get("scorereward")
            known = _known_name(_name_table("decrypted-data/rewardbox.json"), reward_box_id)
            if known is not None:
                return known
    return unknown_name("活跃奖励", reward_id)


def reward_name(kind: object, reward_id: object) -> str:
    parsed_kind = _positive_int(kind)
    if parsed_kind == 1:
        return item_name(reward_id)
    if parsed_kind == 2:
        return reward_box_name(reward_id)
    if parsed_kind == 4:
        return artifact_name(reward_id)
    if parsed_kind == 5:
        return hero_name(reward_id)
    if parsed_kind == 6:
        return rune_name(reward_id)

    known = item_name_or_none(reward_id)
    if known is not None:
        return known
    known = _known_name(_name_table("decrypted-data/rewardbox.json"), reward_id)
    if known is not None:
        return known
    return unknown_name(REWARD_KIND_LABELS.get(parsed_kind, "奖励"), reward_id)


def drop_name(reward_id: object) -> str:
    return item_name_or_none(reward_id) or _known_name(
        _name_table("decrypted-data/rewardbox.json"), reward_id
    ) or unknown_name("掉落", reward_id)


@lru_cache(maxsize=None)
def business_name(message_id: object) -> str:
    parsed = _positive_int(message_id)
    if parsed is not None:
        # Import lazily: the business map imports protocol constants from harvest_fief.
        from dragon_arena_business_map import business_name as mapped_business_name

        known = mapped_business_name(parsed)
        if known:
            return known
    return unknown_name("业务消息", message_id)


def zone_name(zone_id: object, configured_name: object) -> str:
    return _text(configured_name) or unknown_name("区服", zone_id)


# 龙痕竞技场胜利抉择：与 dragon_arena.EXECUTE_CHOICE_ID / MERCY_CHOICE_ID 对齐。
_WIN_CHOICE_NAMES = {
    1: "处决",
    2: "仁慈",
}


def win_choice_name(choice_id: object) -> str:
    """解析 Scararena_winchoice 抉择 ID 为人读名称。"""

    parsed = _positive_int(choice_id)
    if parsed is not None and parsed in _WIN_CHOICE_NAMES:
        return _WIN_CHOICE_NAMES[parsed]
    if parsed == 0 or choice_id in (0, "0", None, ""):
        return "无"
    return unknown_name("胜利抉择", choice_id)


def arena_stage_name(stage_id: object) -> str:
    """解析龙痕竞技场阶段 ID。本地暂无权威阶段表时使用标准未知兜底。"""

    parsed = _positive_int(stage_id)
    if parsed is None or parsed == 0:
        return "无"
    return unknown_name("竞技场阶段", parsed)


def item_change_text(item_id: object, delta: int, total: int | None = None) -> str:
    change = f"+{delta}" if delta >= 0 else str(delta)
    current = "" if total is None else f"（当前 {total}）"
    return f"{item_name(item_id)}：{change}{current}"


def reward_text(kind: object, reward_id: object, amount: int) -> str:
    return f"{reward_name(kind, reward_id)} × {amount}"
