"""Project-wide persistence for redacted structured operational logs."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
import json
import os
from pathlib import Path
import re
import stat
import threading
import uuid
from typing import Any, Callable, Mapping


PROJECT_ROOT = Path(__file__).resolve().parent
DEFAULT_MANAGED_ROOT = PROJECT_ROOT / "logs"
MARKER_NAME = ".logging-store-v1"
MARKER_CONTENT = "project-logging-store:1\n"
MAX_FUTURE_SKEW = timedelta(minutes=5)
RETENTION_DAYS = 7
MAX_RECORD_BYTES = 64 * 1024
EVENT_SPECS = frozenset({
    "daily_quest",
    "arcane_tower_daily_free",
    "ancient_law_court_daily_free_engraving",
    "adventurer_guild_daily_auto_refresh",
    "websocket_business",
    "dragon_arena",
    "treasure_area",
    "smithy_forge",
})
REGISTERED_MANAGED_EVENTS = EVENT_SPECS
_EVENT_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_DATE_FILE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})\.jsonl$")
_SENSITIVE_KEY_RE = re.compile(
    r"(?:^|_)(?:token|password|session|authorization|url|wire|packet|cookie|secret)(?:_|$)|(?:raw|decoded|message)_payload",
    re.IGNORECASE,
)
_LOCK = threading.RLock()


class _ManagedDestination:
    def __repr__(self) -> str:
        return "MANAGED_DESTINATION"


MANAGED_DESTINATION = _ManagedDestination()


class LogPersistenceError(RuntimeError):
    def __init__(self, code: str, message: str | None = None) -> None:
        self.code = code
        super().__init__(message or code)


@dataclass(frozen=True)
class LogWriteResult:
    record: dict[str, Any]
    path: Path
    retention_error: str | None = None


def _now() -> datetime:
    return datetime.now().astimezone()


def _parse_timestamp(value: datetime | str | None, reference_time: datetime) -> datetime:
    if value is None:
        timestamp = reference_time
    elif isinstance(value, datetime):
        timestamp = value
    elif isinstance(value, str):
        try:
            timestamp = datetime.fromisoformat(value)
        except ValueError as exc:
            raise LogPersistenceError("invalid_timestamp") from exc
    else:
        raise LogPersistenceError("invalid_timestamp")
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise LogPersistenceError("invalid_timestamp")
    return timestamp


def _normalize_zone(zone: Mapping[str, Any]) -> dict[str, str]:
    zone_id = zone.get("id")
    zone_name = zone.get("name")
    if not isinstance(zone_id, str) or not zone_id.strip():
        raise LogPersistenceError("invalid_zone")
    zone_id = zone_id.strip()
    if not isinstance(zone_name, str) or not zone_name.strip():
        zone_name = zone_id
    else:
        zone_name = zone_name.strip()
    if len(zone_id) > 128 or len(zone_name) > 128:
        raise LogPersistenceError("invalid_zone")
    return {"id": zone_id, "name": zone_name}


def _validate_details(value: Any, *, depth: int = 0) -> None:
    if depth > 8:
        raise LogPersistenceError("details_too_deep")
    if value is None or isinstance(value, (bool, int, float, str)):
        return
    if isinstance(value, list):
        for item in value:
            _validate_details(item, depth=depth + 1)
        return
    if isinstance(value, Mapping):
        for key, item in value.items():
            if not isinstance(key, str) or _SENSITIVE_KEY_RE.search(key):
                raise LogPersistenceError("sensitive_details")
            _validate_details(item, depth=depth + 1)
        return
    raise LogPersistenceError("invalid_details")


def _ensure_mode(path: Path, mode: int) -> None:
    try:
        os.chmod(path, mode)
    except OSError as exc:
        raise LogPersistenceError("permission_failed") from exc


def _is_inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _prepare_managed_root(root: Path) -> None:
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    _ensure_mode(root, 0o700)
    marker = root / MARKER_NAME
    if marker.exists():
        if marker.is_symlink() or marker.read_text(encoding="ascii") != MARKER_CONTENT:
            raise LogPersistenceError("unmanaged_root")
    else:
        marker.write_text(MARKER_CONTENT, encoding="ascii")
    _ensure_mode(marker, 0o600)


def _cleanup(root: Path, reference_time: datetime) -> str | None:
    allowed = set(REGISTERED_MANAGED_EVENTS) | {MARKER_NAME}
    try:
        entries = list(root.iterdir())
        if any(entry.name not in allowed or entry.is_symlink() for entry in entries):
            return "unsafe_managed_entry"
        cutoff = reference_time.date() - timedelta(days=RETENTION_DAYS)
        for event_dir in entries:
            if event_dir.name == MARKER_NAME:
                continue
            if not event_dir.is_dir():
                return "unsafe_managed_entry"
            for candidate in event_dir.iterdir():
                match = _DATE_FILE_RE.fullmatch(candidate.name)
                if match is None:
                    continue
                if candidate.is_symlink() or not candidate.is_file():
                    return "unsafe_managed_entry"
                if datetime.strptime(match.group(1), "%Y-%m-%d").date() < cutoff:
                    candidate.unlink()
    except OSError:
        return "retention_failed"
    return None


def write_standard_log(
    *,
    event: str,
    operation: str,
    zone: Mapping[str, Any],
    details: Mapping[str, Any],
    destination: Path | _ManagedDestination | None = MANAGED_DESTINATION,
    timestamp: datetime | str | None = None,
    run_id: str | None = None,
    outcome: str = "success",
    level: str = "info",
    error: Mapping[str, str | None] | None = None,
    managed_root: Path = DEFAULT_MANAGED_ROOT,
    clock: Callable[[], datetime] = _now,
) -> LogWriteResult | None:
    """Validate and append one standardized record; ``None`` disables persistence."""
    if destination is None:
        return None
    if event not in EVENT_SPECS or not _EVENT_RE.fullmatch(event):
        raise LogPersistenceError("invalid_event")
    if not isinstance(operation, str) or not operation or len(operation) > 64:
        raise LogPersistenceError("invalid_operation")
    if outcome not in {"success", "failure", "skipped"} or level not in {"debug", "info", "warning", "error"}:
        raise LogPersistenceError("invalid_outcome")
    _validate_details(details)
    with _LOCK:
        reference_time = clock()
        if reference_time.tzinfo is None or reference_time.utcoffset() is None:
            raise LogPersistenceError("invalid_reference_time")
        parsed_timestamp = _parse_timestamp(timestamp, reference_time)
        if parsed_timestamp > reference_time + MAX_FUTURE_SKEW:
            raise LogPersistenceError("future_timestamp")
        normalized_zone = _normalize_zone(zone)
        record = {
            "schema_version": 1,
            "timestamp": parsed_timestamp.isoformat(timespec="seconds"),
            "event": event,
            "level": level,
            "run_id": run_id or uuid.uuid4().hex,
            "operation": operation,
            "zone": normalized_zone,
            "outcome": outcome,
            "error": dict(error) if error is not None else None,
            "details": dict(details),
        }
        payload = (json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
        if len(payload) > MAX_RECORD_BYTES:
            raise LogPersistenceError("record_too_large")
        root = Path(managed_root).expanduser()
        managed = destination is MANAGED_DESTINATION
        if managed:
            if parsed_timestamp.date() < reference_time.date() - timedelta(days=RETENTION_DAYS):
                raise LogPersistenceError("managed_date_expired")
            _prepare_managed_root(root)
            target = root / event / f"{parsed_timestamp.date().isoformat()}.jsonl"
        elif isinstance(destination, Path):
            target = destination.expanduser()
            if _is_inside(target, root):
                raise LogPersistenceError("custom_path_inside_managed_root")
        else:
            raise LogPersistenceError("invalid_destination")
        try:
            target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            _ensure_mode(target.parent, 0o700)
            fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            try:
                _ensure_mode(target, 0o600)
                written = os.write(fd, payload)
                if written != len(payload):
                    raise LogPersistenceError("short_write")
            finally:
                os.close(fd)
        except LogPersistenceError:
            raise
        except OSError as exc:
            raise LogPersistenceError("write_failed") from exc
        retention_error = _cleanup(root, reference_time) if managed else None
        return LogWriteResult(record, target, retention_error)
