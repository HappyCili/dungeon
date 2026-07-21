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
    """Resolve a treasure map only when a dedicated map table exists."""

    for relative_path in ("decrypted-data/map.json", "decrypted-data/maps.json"):
        known = _known_name(_name_table(relative_path), area_id)
        if known is not None:
            return known
    return unknown_name("地图", area_id)


def reward_box_name(reward_id: object) -> str:
    return _known_name(_name_table("decrypted-data/rewardbox.json"), reward_id) or unknown_name(
        "奖励箱", reward_id
    )


def rune_name(rune_id: object) -> str:
    return _known_name(
        _name_table("decrypted-data/rune-tables/orderrune.json"), rune_id
    ) or unknown_name("律文", rune_id)


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


def item_change_text(item_id: object, delta: int, total: int | None = None) -> str:
    change = f"+{delta}" if delta >= 0 else str(delta)
    current = "" if total is None else f"（当前 {total}）"
    return f"{item_name(item_id)}：{change}{current}"


def reward_text(kind: object, reward_id: object, amount: int) -> str:
    return f"{reward_name(kind, reward_id)} × {amount}"
