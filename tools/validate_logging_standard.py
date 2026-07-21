#!/usr/bin/env python3
"""Validate project structured logging sources and emitted JSONL artifacts."""

from __future__ import annotations

import argparse
import ast
from datetime import datetime
import json
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from logging_store import EVENT_SPECS, MAX_FUTURE_SKEW


RAW_TRAFFIC_SOURCE_EXEMPTIONS = {
    ("dragon_arena_websocket.py", "WebSocketTrafficLogger", "_open"),
    ("dragon_arena_websocket.py", "WebSocketTrafficLogger", "write_frame"),
}


def validate_source(root: Path) -> list[str]:
    errors: list[str] = []
    for path in root.rglob("*.py"):
        if any(part in {"venv", ".venv", "tests", "__pycache__"} for part in path.parts):
            continue
        if path.name == "logging_store.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            if node.func.attr not in {"open", "write", "write_text", "write_bytes"}:
                continue
            owner = None
            for parent in ast.walk(tree):
                if isinstance(parent, ast.ClassDef) and node in ast.walk(parent):
                    owner = parent.name
                    break
            method = None
            for parent in ast.walk(tree):
                if isinstance(parent, ast.FunctionDef) and node in ast.walk(parent):
                    method = parent.name
                    break
            if (path.name, owner, method) in RAW_TRAFFIC_SOURCE_EXEMPTIONS:
                continue
            text = ast.unparse(node)
            if ".jsonl" in text or ".log" in text:
                errors.append(f"direct_persistent_write {path.relative_to(root)}:{node.lineno}")
    return errors


def validate_output(root: Path, reference_time: datetime) -> list[str]:
    errors: list[str] = []
    if not root.is_dir():
        return ["unreadable_managed_root"]
    for path in root.rglob("*.jsonl"):
        for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            try:
                record = json.loads(line)
                timestamp = datetime.fromisoformat(record["timestamp"])
                if timestamp.tzinfo is None or record["event"] not in EVENT_SPECS:
                    raise ValueError
                if timestamp > reference_time + MAX_FUTURE_SKEW:
                    raise ValueError
                required = {"schema_version", "timestamp", "event", "level", "run_id", "operation", "zone", "outcome", "error", "details"}
                if set(record) != required:
                    raise ValueError
            except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                errors.append(f"invalid_record {path}:{line_number}")
    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", action="store_true")
    parser.add_argument("--output", action="store_true")
    parser.add_argument("--project-root", type=Path, default=Path.cwd())
    parser.add_argument("--managed-root", type=Path, default=Path("logs"))
    parser.add_argument("--reference-time")
    args = parser.parse_args(argv)
    if args.source == args.output:
        return 2
    reference = datetime.now().astimezone() if args.reference_time is None else datetime.fromisoformat(args.reference_time)
    if reference.tzinfo is None:
        return 2
    errors = validate_source(args.project_root) if args.source else validate_output(args.managed_root, reference)
    for error in errors:
        print(error, file=sys.stderr)
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
