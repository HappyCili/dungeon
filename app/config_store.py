from __future__ import annotations

import json
import os
import threading
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping


CONFIG_VERSION = 6
DEFAULT_ENABLED_TASK_IDS = [104, 112, 119]
VALID_OUTCOMES = frozenset({"mercy", "execute"})
MAX_TREASURE_SWEEP_TIMES = 30
MAX_TREASURE_FARM_HEARTH = 10000
MAX_ABYSS_ROUNDS = 900


@dataclass
class AccountSettings:
    username: str = ""
    remember_password: bool = False


@dataclass
class ZoneSettings:
    id: str = ""
    name: str = ""


@dataclass
class DailySettings:
    enabled_task_ids: list[int] = field(
        default_factory=lambda: list(DEFAULT_ENABLED_TASK_IDS)
    )


@dataclass
class ArenaSettings:
    rounds: int = 10
    outcome: str = "mercy"
    refresh_on_exhaustion: bool = True


@dataclass
class TreasureSettings:
    area_id: int = 0
    times: int = 1
    # 默认沉默之城（较早解锁、常见可刷）
    farm_area_id: int = 530101
    farm_target_hearth: int = 100


@dataclass
class DungeonSettings:
    dungeon_id: int = 0


@dataclass
class AbyssSettings:
    """罪者深渊：默认不限轮数（0=直到失败），并自动选赛季增益。"""

    max_rounds: int = 0
    auto_buff: bool = True


@dataclass
class UiSettings:
    version: int = CONFIG_VERSION
    account: AccountSettings = field(default_factory=AccountSettings)
    zone: ZoneSettings = field(default_factory=ZoneSettings)
    daily: DailySettings = field(default_factory=DailySettings)
    arena: ArenaSettings = field(default_factory=ArenaSettings)
    treasure: TreasureSettings = field(default_factory=TreasureSettings)
    dungeon: DungeonSettings = field(default_factory=DungeonSettings)
    abyss: AbyssSettings = field(default_factory=AbyssSettings)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _mapping(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}


def _bounded_string(value: object, maximum: int = 128) -> str:
    if not isinstance(value, str):
        return ""
    return value.strip()[:maximum]


def settings_from_mapping(value: Mapping[str, object]) -> UiSettings:
    account = _mapping(value.get("account"))
    zone = _mapping(value.get("zone"))
    daily = _mapping(value.get("daily"))
    arena = _mapping(value.get("arena"))
    treasure = _mapping(value.get("treasure"))
    dungeon = _mapping(value.get("dungeon"))
    abyss = _mapping(value.get("abyss"))

    selected = daily.get("enabled_task_ids")
    enabled_task_ids = (
        [task_id for task_id in selected if isinstance(task_id, int) and not isinstance(task_id, bool)]
        if isinstance(selected, list)
        else list(DEFAULT_ENABLED_TASK_IDS)
    )
    rounds = arena.get("rounds")
    if not isinstance(rounds, int) or isinstance(rounds, bool) or not 1 <= rounds <= 100:
        rounds = 10
    outcome = arena.get("outcome")
    if outcome not in VALID_OUTCOMES:
        outcome = "mercy"
    refresh_on_exhaustion = arena.get("refresh_on_exhaustion")
    if not isinstance(refresh_on_exhaustion, bool):
        refresh_on_exhaustion = True

    area_id = treasure.get("area_id")
    if (
        not isinstance(area_id, int)
        or isinstance(area_id, bool)
        or not 0 <= area_id <= 0x7FFFFFFF
    ):
        area_id = 0
    times = treasure.get("times")
    if (
        not isinstance(times, int)
        or isinstance(times, bool)
        or not 1 <= times <= MAX_TREASURE_SWEEP_TIMES
    ):
        times = 1
    farm_area_id = treasure.get("farm_area_id")
    if (
        not isinstance(farm_area_id, int)
        or isinstance(farm_area_id, bool)
        or not 0 <= farm_area_id <= 0x7FFFFFFF
    ):
        farm_area_id = 0
    farm_target_hearth = treasure.get("farm_target_hearth")
    if (
        not isinstance(farm_target_hearth, int)
        or isinstance(farm_target_hearth, bool)
        or not 1 <= farm_target_hearth <= MAX_TREASURE_FARM_HEARTH
    ):
        farm_target_hearth = 100

    dungeon_id = dungeon.get("dungeon_id")
    if (
        not isinstance(dungeon_id, int)
        or isinstance(dungeon_id, bool)
        or not 0 <= dungeon_id <= 0x7FFFFFFF
    ):
        dungeon_id = 0

    abyss_max_rounds = abyss.get("max_rounds")
    if (
        not isinstance(abyss_max_rounds, int)
        or isinstance(abyss_max_rounds, bool)
        or not 0 <= abyss_max_rounds <= MAX_ABYSS_ROUNDS
    ):
        abyss_max_rounds = 0
    abyss_auto_buff = abyss.get("auto_buff")
    if not isinstance(abyss_auto_buff, bool):
        abyss_auto_buff = True

    zone_id = _bounded_string(zone.get("id"), 64)
    zone_name = _bounded_string(zone.get("name"), 128)
    if zone_name.startswith("演示"):
        zone_id = ""
        zone_name = ""

    return UiSettings(
        account=AccountSettings(
            username=_bounded_string(account.get("username"), 64),
            remember_password=account.get("remember_password") is True,
        ),
        zone=ZoneSettings(
            id=zone_id,
            name=zone_name,
        ),
        daily=DailySettings(enabled_task_ids=enabled_task_ids),
        arena=ArenaSettings(
            rounds=rounds,
            outcome=outcome,
            refresh_on_exhaustion=refresh_on_exhaustion,
        ),
        treasure=TreasureSettings(
            area_id=area_id,
            times=times,
            farm_area_id=farm_area_id,
            farm_target_hearth=farm_target_hearth,
        ),
        dungeon=DungeonSettings(dungeon_id=dungeon_id),
        abyss=AbyssSettings(
            max_rounds=abyss_max_rounds,
            auto_buff=abyss_auto_buff,
        ),
    )


class ConfigStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.RLock()
        self._settings = self._load()

    def _load(self) -> UiSettings:
        if not self.path.exists():
            return UiSettings()
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return UiSettings()
        if not isinstance(raw, Mapping):
            return UiSettings()
        settings = settings_from_mapping(raw)
        if raw.get("version") != CONFIG_VERSION:
            self._write(settings)
        return settings

    def _write(self, settings: UiSettings) -> None:
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        temporary_path = self.path.with_name(
            f".{self.path.name}.{uuid.uuid4().hex}.tmp"
        )
        try:
            with temporary_path.open("w", encoding="utf-8") as handle:
                os.chmod(temporary_path, 0o600)
                json.dump(settings.to_dict(), handle, ensure_ascii=False, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_path, self.path)
            os.chmod(self.path, 0o600)
        finally:
            if temporary_path.exists():
                temporary_path.unlink(missing_ok=True)

    def snapshot(self) -> UiSettings:
        with self._lock:
            return settings_from_mapping(self._settings.to_dict())

    def update(self, mutate: Callable[[UiSettings], None]) -> UiSettings:
        with self._lock:
            settings = settings_from_mapping(self._settings.to_dict())
            mutate(settings)
            self._write(settings)
            self._settings = settings
            return settings_from_mapping(settings.to_dict())

    def set_account(self, username: str, remember_password: bool) -> UiSettings:
        return self.update(
            lambda settings: (
                setattr(settings.account, "username", username),
                setattr(settings.account, "remember_password", remember_password),
            )
        )

    def set_zone(self, zone_id: str, zone_name: str) -> UiSettings:
        return self.update(
            lambda settings: (
                setattr(settings.zone, "id", zone_id),
                setattr(settings.zone, "name", zone_name),
            )
        )

    def set_daily_selection(self, task_ids: list[int]) -> UiSettings:
        return self.update(
            lambda settings: setattr(settings.daily, "enabled_task_ids", list(task_ids))
        )

    def set_arena(
        self, rounds: int, outcome: str, refresh_on_exhaustion: bool
    ) -> UiSettings:
        return self.update(
            lambda settings: (
                setattr(settings.arena, "rounds", rounds),
                setattr(settings.arena, "outcome", outcome),
                setattr(
                    settings.arena,
                    "refresh_on_exhaustion",
                    refresh_on_exhaustion,
                ),
            )
        )

    def set_treasure(self, area_id: int, times: int) -> UiSettings:
        return self.update(
            lambda settings: (
                setattr(settings.treasure, "area_id", area_id),
                setattr(settings.treasure, "times", times),
            )
        )

    def set_treasure_farm(self, farm_area_id: int, farm_target_hearth: int) -> UiSettings:
        return self.update(
            lambda settings: (
                setattr(settings.treasure, "farm_area_id", farm_area_id),
                setattr(settings.treasure, "farm_target_hearth", farm_target_hearth),
            )
        )

    def set_dungeon(self, dungeon_id: int) -> UiSettings:
        return self.update(
            lambda settings: setattr(settings.dungeon, "dungeon_id", dungeon_id)
        )

    def set_abyss(self, max_rounds: int, auto_buff: bool) -> UiSettings:
        return self.update(
            lambda settings: (
                setattr(settings.abyss, "max_rounds", max_rounds),
                setattr(settings.abyss, "auto_buff", auto_buff),
            )
        )
