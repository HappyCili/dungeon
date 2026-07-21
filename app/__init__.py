from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from flask import Flask

from .config_store import ConfigStore
from .credentials import CredentialStore, KeyringCredentialStore
from .job_manager import JobManager
from .routes import register_routes
from .services.account_service import AccountService
from .services.arena_service import ArenaService
from .services.daily_service import DailyService
from .services.dungeon_service import DungeonService
from .services.treasure_service import TreasureService


PROJECT_ROOT = Path(__file__).resolve().parent.parent


def create_app(
    test_config: dict[str, Any] | None = None,
    *,
    config_path: Path | None = None,
    credential_store: CredentialStore | None = None,
    account_service_factory: Callable[[ConfigStore, CredentialStore], AccountService]
    | None = None,
    job_manager: JobManager | None = None,
    daily_service: DailyService | None = None,
    arena_service: ArenaService | None = None,
    treasure_service: TreasureService | None = None,
    dungeon_service: DungeonService | None = None,
) -> Flask:
    app = Flask(__name__)
    app.config.from_mapping(
        SECRET_KEY="local-stage-zero-only",
        JSON_AS_ASCII=False,
        SIMULATION_DELAY=0.35,
    )
    if test_config:
        app.config.update(test_config)

    settings_path = config_path or PROJECT_ROOT / "config" / "ui-settings.json"
    config_store = ConfigStore(settings_path)
    credentials = credential_store or KeyringCredentialStore()
    account = (
        account_service_factory(config_store, credentials)
        if account_service_factory is not None
        else AccountService(config_store, credentials)
    )
    services = {
        "config_store": config_store,
        "account": account,
        "daily": daily_service or DailyService(),
        "arena": arena_service or ArenaService(),
        "treasure": treasure_service or TreasureService(),
        "dungeon": dungeon_service or DungeonService(),
        "jobs": job_manager or JobManager(),
    }
    app.extensions["daily_console"] = services
    app.jinja_env.globals["asset_version"] = "dungeon-arena-v8"
    register_routes(app)
    return app
