#!/usr/bin/env python3
"""用 secKey 从 data.unityfs 批量解密配置 TextAsset 并落盘。"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from collections import Counter
from pathlib import Path

# ui_app 根
UI_APP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(UI_APP))

from harvest_fief import pack1_decode  # noqa: E402
from project_paths import NATIVE_APP_ROOT  # noqa: E402

DEFAULT_SECKEY = "RO#4k%m1"
DEFAULT_BUNDLE = NATIVE_APP_ROOT / "decrypted-data" / "data.unityfs"
OUT_ROOT = NATIVE_APP_ROOT / "decrypted-data"
TABLES_DIR = OUT_ROOT / "tables"
ZONES_DIR = OUT_ROOT / "zone-layouts"

# 与历史目录对齐的「权威」副本（工程代码已引用的路径）
CANONICAL_ROUTES: dict[str, Path] = {
    "item": OUT_ROOT / "item.json",
    "item_bas": OUT_ROOT / "item_bas.json",
    "itemtype": OUT_ROOT / "itemtype.json",
    "dungeon": OUT_ROOT / "dungeon.json",
    "rewardbox": OUT_ROOT / "rewardbox.json",
    "mapareas": OUT_ROOT / "mapareas.json",
    "daily_quest": NATIVE_APP_ROOT / "decrypted-task-data" / "daily_quest.json",
    "activityreward": NATIVE_APP_ROOT / "decrypted-task-data" / "activityreward.json",
    "gotothis": NATIVE_APP_ROOT / "decrypted-task-data" / "gotothis.json",
    "questfinishcond": NATIVE_APP_ROOT / "decrypted-task-data" / "questfinishcond.json",
    "quests": NATIVE_APP_ROOT / "decrypted-task-data" / "quests.json",
    "systemfunc": NATIVE_APP_ROOT / "decrypted-task-data" / "systemfunc.json",
    "zh-Hans": NATIVE_APP_ROOT / "decrypted-task-data" / "zh-Hans.json",
    "heroes": NATIVE_APP_ROOT / "decrypted-tavern-data" / "heroes.json",
    "heroname": NATIVE_APP_ROOT / "decrypted-tavern-data" / "heroname.json",
    "refreshfee": NATIVE_APP_ROOT / "decrypted-tavern-data" / "refreshfee.json",
    "orderrune": OUT_ROOT / "rune-tables" / "orderrune.json",
    "orderrune_evaluate": OUT_ROOT / "rune-tables" / "orderrune_evaluate.json",
    "orderrune_group": OUT_ROOT / "rune-tables" / "orderrune_group.json",
    "orderrune_level": OUT_ROOT / "rune-tables" / "orderrune_level.json",
    "orderrune_pattern": OUT_ROOT / "rune-tables" / "orderrune_pattern.json",
    "orderrune_pity": OUT_ROOT / "rune-tables" / "orderrune_pity.json",
    "orderrune_pool": OUT_ROOT / "rune-tables" / "orderrune_pool.json",
    "orderrune_rebate": OUT_ROOT / "rune-tables" / "orderrune_rebate.json",
    "orderrune_reforge": OUT_ROOT / "rune-tables" / "orderrune_reforge.json",
    "orderrune_upstage": OUT_ROOT / "rune-tables" / "orderrune_upstage.json",
    "orderrune_wishlist": OUT_ROOT / "rune-tables" / "orderrune_wishlist.json",
}


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _pretty_json_bytes(raw: bytes) -> bytes:
    """尽量格式化为 UTF-8 JSON 文本；失败则原样返回。"""
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return raw
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    if not text.endswith("\n"):
        text += "\n"
    return text.encode("utf-8")


def _safe_name(name: str) -> str:
    cleaned = re.sub(r"[^\w.\-]+", "_", name.strip()) or "unnamed"
    return cleaned[:180]


def _write(path: Path, data: bytes) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.read_bytes() == data:
        return "unchanged"
    path.write_bytes(data)
    return "written"


def extract(
    *,
    bundle: Path,
    seckey: str,
    write_zones: bool,
) -> dict:
    import UnityPy
    from UnityPy import config

    config.FALLBACK_UNITY_VERSION = "2022.3.0f1"
    env = UnityPy.load(str(bundle))

    stats: Counter[str] = Counter()
    assets: list[dict] = []
    # 同名 TextAsset 可能重复；按「解密后内容 hash」去重，文件名冲突时加后缀
    written_names: dict[str, int] = {}

    for obj in env.objects:
        if obj.type.name != "TextAsset":
            continue
        try:
            data = obj.read()
        except Exception:
            stats["read_error"] += 1
            continue
        name = getattr(data, "m_Name", None) or ""
        script = data.m_Script
        if isinstance(script, bytes):
            try:
                text = script.decode("utf-8")
            except UnicodeDecodeError:
                stats["binary_skip"] += 1
                continue
        else:
            text = script if isinstance(script, str) else ""
        if not text or len(text.strip()) < 8:
            stats["empty_skip"] += 1
            continue

        stripped = text.strip()
        # 已是明文 JSON
        if stripped[:1] in "{[":
            try:
                plain = stripped.encode("utf-8")
                kind = "already_plain"
            except Exception:
                stats["plain_encode_fail"] += 1
                continue
        else:
            try:
                plain = pack1_decode(stripped, seckey)
                kind = "pack1"
            except Exception:
                stats["decrypt_fail"] += 1
                continue

        if plain[:1] not in (b"{", b"["):
            stats["not_json"] += 1
            continue

        pretty = _pretty_json_bytes(plain)
        digest = _sha256(pretty)
        base = _safe_name(name)
        is_numeric = bool(re.fullmatch(r"\d+", base))

        if is_numeric:
            if not write_zones:
                stats["zone_skipped"] += 1
                continue
            rel_dir = "zone-layouts"
            out_path = ZONES_DIR / f"{base}.json"
        else:
            rel_dir = "tables"
            # 处理重名
            count = written_names.get(base, 0)
            written_names[base] = count + 1
            filename = f"{base}.json" if count == 0 else f"{base}__{count + 1}.json"
            out_path = TABLES_DIR / filename

        action = _write(out_path, pretty)
        stats[f"{kind}_{action}"] += 1
        stats["ok"] += 1

        entry = {
            "name": name,
            "path": str(out_path.relative_to(OUT_ROOT)),
            "bytes": len(pretty),
            "sha256": digest,
            "source": kind,
            "action": action,
        }
        assets.append(entry)

        # 权威路径副本（仅命名表、首次/唯一名）
        if not is_numeric and base in CANONICAL_ROUTES and written_names[base] == 1:
            canon = CANONICAL_ROUTES[base]
            c_action = _write(canon, pretty)
            stats[f"canonical_{c_action}"] += 1
            entry["canonical"] = str(canon.relative_to(NATIVE_APP_ROOT))

    # manifest
    TABLES_DIR.mkdir(parents=True, exist_ok=True)
    manifest = {
        "source_bundle": str(bundle),
        "seckey_hint": "BuildTimeData.secKey (runtime; do not commit if public)",
        "table_count": sum(1 for a in assets if a["path"].startswith("tables/")),
        "zone_count": sum(1 for a in assets if a["path"].startswith("zone-layouts/")),
        "stats": dict(stats),
        "assets": sorted(assets, key=lambda a: a["path"]),
    }
    man_path = TABLES_DIR / "manifest.json"
    man_bytes = (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    _write(man_path, man_bytes)

    if write_zones:
        ZONES_DIR.mkdir(parents=True, exist_ok=True)
        zone_man = {
            "source_bundle": str(bundle),
            "count": sum(1 for a in assets if a["path"].startswith("zone-layouts/")),
            "note": "Numeric TextAsset names: map/zone layout JSON",
        }
        _write(
            ZONES_DIR / "manifest.json",
            (json.dumps(zone_man, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
        )

    return manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", type=Path, default=DEFAULT_BUNDLE)
    parser.add_argument("--seckey", default=DEFAULT_SECKEY)
    parser.add_argument(
        "--skip-zones",
        action="store_true",
        help="不导出纯数字命名的 zone 布局表",
    )
    args = parser.parse_args(argv)
    if not args.bundle.is_file():
        print(f"找不到 bundle: {args.bundle}", file=sys.stderr)
        return 1
    print(f"bundle: {args.bundle}")
    print(f"seckey: {args.seckey!r}")
    manifest = extract(
        bundle=args.bundle,
        seckey=args.seckey,
        write_zones=not args.skip_zones,
    )
    print("stats:", json.dumps(manifest["stats"], ensure_ascii=False, indent=2))
    print(f"tables: {manifest['table_count']}, zones: {manifest['zone_count']}")
    print(f"manifest: {TABLES_DIR / 'manifest.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
