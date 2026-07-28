from __future__ import annotations

import json
import stat
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from app import create_app
from app.config_store import ConfigStore
from app.services.account_service import AccountService
from app.services.daily_service import DailyService
from app.services.dungeon_service import DungeonService, _load_item_details
from app.services.treasure_service import TreasureService
from dungeon_sweep import DungeonDrawResponse, DungeonStatus, DungeonSweepRejected
from game_session import SessionRecoveryIssue, SessionRecoverySnapshot
from harvest_fief import AccountZone, GameEndpoint, HarvestError, ItemChange, RewardProp
from login import GameTokens, IdentityTokens, LoginError, LoginResult
from tests.daily_fixtures import build_test_daily_action_runner
from treasure_area import TreasureAreaStatus, TreasureSweepResponse


class InMemoryCredentialStore:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    def set_password(self, username: str, password: str) -> None:
        self.values[username] = password

    def delete_password(self, username: str) -> None:
        self.values.pop(username, None)

    def get_password(self, username: str) -> str | None:
        return self.values.get(username)

    def is_configured(self, username: str) -> bool:
        return username in self.values


class InMemoryTokenStore:
    def __init__(self) -> None:
        self.saved: list[LoginResult] = []

    def save(self, result: LoginResult) -> None:
        self.saved.append(result)

    def load_game_tokens(self) -> dict[str, str] | None:
        if not self.saved:
            return None
        result = self.saved[-1]
        return {
            "userid": result.game.userid,
            "verify_token": result.game.verify_token,
        }

    def load_refresh_token(self) -> str | None:
        if not self.saved:
            return None
        return self.saved[-1].identity.refresh_token


class FakeLoginClient:
    def __init__(self) -> None:
        self.calls: list[dict[str, str]] = []
        self.refresh_calls: list[dict[str, str]] = []
        self.error: Exception | None = None
        self.refresh_error: Exception | None = None
        self.result = LoginResult(
            IdentityTokens("header.payload.signature", "refresh-token", "openid-1"),
            GameTokens("user-1", "verify-token", "pay-token", None),
        )
        self.refresh_result = LoginResult(
            IdentityTokens("refreshed.id", "refresh-token-2", "openid-1"),
            GameTokens("user-1", "verify-token-refreshed", "pay-token-2", None),
        )

    def login_with_password(self, **kwargs: str) -> LoginResult:
        self.calls.append(dict(kwargs))
        if self.error is not None:
            raise self.error
        return self.result

    def login_with_refresh(self, **kwargs: str) -> LoginResult:
        self.refresh_calls.append(dict(kwargs))
        if self.refresh_error is not None:
            raise self.refresh_error
        return self.refresh_result


class InMemoryTreasureClient:
    def __init__(self, state: dict[str, object]) -> None:
        self.state = state
        self.closed = False

    def get_status(self) -> TreasureAreaStatus:
        return TreasureAreaStatus(
            open_times=3,
            refresh_seconds=120,
            swept_today=self.state["swept_today"],
            daily_sweep_limit=self.state["daily_sweep_limit"],
            area_ids=self.state["area_ids"],
        )

    def sweep(self, area_id: int, times: int) -> TreasureSweepResponse:
        self.state["sweep_calls"].append((area_id, times))
        self.state["swept_today"] += times
        return TreasureSweepResponse(ret=0, status=self.get_status())

    def close(self) -> None:
        self.closed = True


class InMemoryDungeonClient:
    def __init__(self, state: dict[str, object]) -> None:
        self.state = state
        self.closed = False

    def get_status(self) -> DungeonStatus:
        return DungeonStatus(
            unlocked_ids=self.state["unlocked_ids"],
            visible_ids=self.state["visible_ids"],
            best_scores=self.state["best_scores"],
            current_dungeon_id=self.state["current_dungeon_id"],
            draw_times=self.state["draw_times"],
            total_draw_times=self.state["total_draw_times"],
            challenge_times=self.state["challenge_times"],
        )

    def sweep(self, dungeon_id: int) -> None:
        self.state["operations"].append(("sweep", dungeon_id))
        sweep_error = self.state.get("sweep_error")
        if isinstance(sweep_error, Exception):
            raise sweep_error
        self.state["total_draw_times"] += 1

    def draw_all(self, dungeon_id: int) -> DungeonDrawResponse:
        self.state["operations"].append(("draw_all", dungeon_id))
        draw_error = self.state.get("draw_error")
        if isinstance(draw_error, Exception):
            raise draw_error
        self.state["draw_times"] = self.state["total_draw_times"]
        return DungeonDrawResponse(
            ret=0,
            dungeon_id=dungeon_id,
            draw_times=self.state["draw_times"],
            total_draw_times=self.state["total_draw_times"],
            reward_ids=self.state["reward_ids"],
            probabilities=self.state["probabilities"],
            all_drawn=True,
            item_changes=self.state["item_changes"],
            reward_props=self.state["reward_props"],
            reward_notice_received=True,
        )

    def close(self) -> None:
        self.closed = True


class InMemoryArenaService:
    def __init__(self) -> None:
        self.snapshot_endpoints: list[GameEndpoint] = []
        self.run_endpoints: list[GameEndpoint] = []

    def snapshot(
        self, endpoint: GameEndpoint, *, refresh_endpoint=None
    ) -> dict[str, object]:
        self.snapshot_endpoints.append(endpoint)
        return {
            "level": 32,
            "score": 97,
            "stage": {"id": 33, "name": "未知竞技场阶段（ID 33）"},
            "opponents": {"total": 15, "available": 12, "challenged": 3},
            "choice": {"pending": False, "id": 0},
            "daily_reward": {"received": True, "count": 4},
        }

    def run(
        self,
        endpoint: GameEndpoint,
        rounds: int,
        outcome: str,
        refresh_on_exhaustion: bool,
        emit,
        stop_requested,
        *,
        refresh_endpoint=None,
    ) -> dict[str, object]:
        self.run_endpoints.append(endpoint)
        stats: dict[str, object] = {
            "requested_rounds": rounds,
            "completed_rounds": 0,
            "wins": 0,
            "losses": 0,
            "score": 97,
            "score_delta": 0,
            "dragon_coin_delta": 0,
            "stage": "准备中",
            "last_result": "等待开始",
            "outcome": outcome,
            "refresh_on_exhaustion": refresh_on_exhaustion,
            "opponents": {"total": 15, "available": 12, "challenged": 3},
            "daily_reward": {"received": True, "count": 4},
            "rewards": [],
        }
        for round_number in range(1, rounds + 1):
            if stop_requested():
                return {"cancelled": True, "arena": stats}
            stats["completed_rounds"] = round_number
            stats["wins"] = round_number
            stats["stage"] = "本轮完成"
            stats["last_result"] = f"第 {round_number} 场胜利"
            emit("success", stats["last_result"], {"arena": dict(stats)})
            time.sleep(0.01)
        stats["stage"] = "已完成"
        return {"cancelled": False, "arena": stats}


class InMemoryRecoverySession:
    def __init__(self) -> None:
        self.snapshot = SessionRecoverySnapshot(
            generation=1,
            game_data_present=True,
            issues=(
                SessionRecoveryIssue(
                    kind="battle",
                    message_ids=(18002,),
                    battle_state=1,
                    battle_type=7,
                ),
            ),
        )

    @property
    def recovery_snapshot(self) -> SessionRecoverySnapshot:
        return self.snapshot


class InMemoryRecoveryManager:
    def __init__(self) -> None:
        self.session = InMemoryRecoverySession()
        self.snapshot_endpoints: list[GameEndpoint] = []
        self.recovery_endpoints: list[GameEndpoint] = []

    def session_for_snapshot(self, endpoint: GameEndpoint) -> InMemoryRecoverySession:
        self.snapshot_endpoints.append(endpoint)
        return self.session

    def session_for(self, endpoint: GameEndpoint) -> InMemoryRecoverySession:
        self.recovery_endpoints.append(endpoint)
        self.session.snapshot = SessionRecoverySnapshot(
            generation=1,
            game_data_present=True,
            issues=(),
        )
        return self.session


class UiAppTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = TemporaryDirectory()
        self.config_path = Path(self.temporary_directory.name) / "config" / "ui-settings.json"
        self.credentials = InMemoryCredentialStore()
        self.token_store = InMemoryTokenStore()
        self.login_client = FakeLoginClient()
        self.zone_requests: list[dict[str, str]] = []
        self.endpoint_requests: list[dict[str, object]] = []
        self.live_endpoints: list[GameEndpoint] = []
        self.arena_service = InMemoryArenaService()
        self.treasure_live_endpoints: list[GameEndpoint] = []
        self.treasure_state: dict[str, object] = {
            "swept_today": 3,
            "daily_sweep_limit": 8,
            "area_ids": (1001, 1002),
            "sweep_calls": [],
        }
        self.dungeon_live_endpoints: list[GameEndpoint] = []
        self.dungeon_state: dict[str, object] = {
            "unlocked_ids": (2301, 2302),
            "visible_ids": (2301, 2302),
            "best_scores": {2301: 88, 2302: 165},
            "current_dungeon_id": 2301,
            "draw_times": 1,
            "total_draw_times": 2,
            "challenge_times": {2301: 0, 2302: 0},
            "reward_ids": (9001, 9002),
            "probabilities": (250, 750),
            "item_changes": (ItemChange(9001, 2, 12),),
            "reward_props": (
                RewardProp(1, 9001, 2),
                RewardProp(2, 9002, 1),
            ),
            "operations": [],
        }
        self.live_bundle = build_test_daily_action_runner()

        def zone_loader(tokens: dict[str, str]):
            self.zone_requests.append(dict(tokens))
            return (
                AccountZone(4101, "真实一区"),
                AccountZone("4102", "真实二区"),
            )

        def endpoint_resolver(tokens: dict[str, str], args: object) -> GameEndpoint:
            zone_id = getattr(args, "zone_id")
            self.endpoint_requests.append(
                {"tokens": dict(tokens), "zone_id": zone_id}
            )
            return GameEndpoint("ws://test.invalid", "game-token", zone_id, "真实一区")

        def live_runner_builder(endpoint: GameEndpoint):
            self.live_endpoints.append(endpoint)
            return self.live_bundle.runner

        def account_service_factory(config_store, credentials):
            return AccountService(
                config_store,
                credentials,
                login_client_factory=lambda: self.login_client,
                token_store=self.token_store,
                zone_loader=zone_loader,
                endpoint_resolver=endpoint_resolver,
                restore_on_init=True,
            )

        daily_service = DailyService(live_runner_builder=live_runner_builder)

        def treasure_client_builder(endpoint: GameEndpoint):
            self.treasure_live_endpoints.append(endpoint)
            return InMemoryTreasureClient(self.treasure_state)

        treasure_service = TreasureService(
            live_client_builder=treasure_client_builder,
            result_log_destination=None,
        )

        def dungeon_client_builder(endpoint: GameEndpoint):
            self.dungeon_live_endpoints.append(endpoint)
            return InMemoryDungeonClient(self.dungeon_state)

        dungeon_service = DungeonService(
            live_client_builder=dungeon_client_builder,
            dungeon_names={2301: "暮霭地窟", 2302: "风蚀遗迹"},
            reward_names={9001: "远古秘宝", 9002: "星辉宝箱"},
        )

        self.app = create_app(
            {"TESTING": True, "SIMULATION_DELAY": 0.01},
            config_path=self.config_path,
            credential_store=self.credentials,
            account_service_factory=account_service_factory,
            daily_service=daily_service,
            arena_service=self.arena_service,
            treasure_service=treasure_service,
            dungeon_service=dungeon_service,
        )
        self.client = self.app.test_client()
        self.headers = {"Origin": "http://localhost"}

    def tearDown(self) -> None:
        self.app.extensions["daily_console"]["jobs"].shutdown()
        self.temporary_directory.cleanup()

    def post_json(self, path: str, payload: dict[str, object]):
        return self.client.post(path, json=payload, headers=self.headers)

    def put_json(self, path: str, payload: dict[str, object]):
        return self.client.put(path, json=payload, headers=self.headers)

    def login(self, remember_password: bool = False):
        return self.post_json(
            "/api/account/login",
            {
                "username": "demo-user",
                "password": "demo-secret",
                "remember_password": remember_password,
            },
        )

    def wait_for_terminal_job(self, job_id: str) -> dict[str, object]:
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            response = self.client.get(f"/api/jobs/{job_id}")
            self.assertEqual(response.status_code, 200)
            payload = response.get_json()
            if payload["status"] in {"succeeded", "failed", "cancelled"}:
                return payload
            time.sleep(0.01)
        self.fail("模拟作业未在预期时间内完成")

    def test_index_waits_for_a_real_game_server_session(self) -> None:
        response = self.client.get("/")

        self.assertEqual(response.status_code, 200)
        self.assertNotIn(b"data-task-id=", response.data)
        self.assertIn("每日任务操作台".encode(), response.data)
        self.assertIn("登录并选择区服后刷新日常状态".encode(), response.data)
        self.assertIn(b'"daily": null', response.data)
        self.assertNotIn(b"demo-secret", response.data)

    def test_index_includes_dungeon_selection_and_reward_output(self) -> None:
        response = self.client.get("/")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b'id="dungeon-tab"', response.data)
        self.assertIn(b'id="dungeon-select"', response.data)
        self.assertIn(b'id="dungeon-reward-list"', response.data)
        script = (Path(__file__).resolve().parents[1] / "app" / "static" / "app.js").read_text(
            encoding="utf-8"
        )
        self.assertIn('"/api/dungeon"', script)
        self.assertIn('"/api/jobs/dungeon"', script)
        self.assertIn("item.append(name);", script)
        self.assertNotIn("ID ${reward.id}", script)
        self.assertNotIn("reward.description", script)

    def test_index_includes_monopoly_auto_roll_entry(self) -> None:
        response = self.client.get("/")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b'id="monopoly-tab"', response.data)
        self.assertIn(b'id="run-monopoly"', response.data)
        self.assertIn("选择第二个按钮".encode(), response.data)
        script = (Path(__file__).resolve().parents[1] / "app" / "static" / "app.js").read_text(
            encoding="utf-8"
        )
        self.assertIn('"/api/jobs/monopoly"', script)
        self.assertIn("renderMonopolyStats", script)

    def test_index_includes_treasure_settlement_and_farm_transition_fields(self) -> None:
        response = self.client.get("/")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b'id="treasure-cleared-result"', response.data)
        self.assertNotIn(b'id="treasure-farm-actions"', response.data)
        self.assertNotIn(b'id="treasure-farm-resets"', response.data)
        self.assertIn(b'id="treasure-farm-transition"', response.data)
        script = (Path(__file__).resolve().parents[1] / "app" / "static" / "app.js").read_text(
            encoding="utf-8"
        )
        self.assertIn("cleared_result", script)
        self.assertNotIn("treasureFarmActions", script)
        self.assertNotIn("treasureFarmResets", script)
        self.assertIn("last_transition", script)

    def test_index_hides_saved_zone_until_current_login_loads_zones(self) -> None:
        self.app.extensions["daily_console"]["config_store"].set_zone(
            "4101", "旧缓存一区"
        )

        response = self.client.get("/")

        self.assertEqual(response.status_code, 200)
        self.assertNotIn("旧缓存一区".encode(), response.data)

    def test_legacy_demo_zone_is_removed_from_config(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "config" / "ui-settings.json"
            path.parent.mkdir(parents=True)
            path.write_text(
                json.dumps(
                    {
                        "version": 2,
                        "zone": {"id": "any-zone", "name": "演示区服"},
                    }
                ),
                encoding="utf-8",
            )

            settings = ConfigStore(path).snapshot()

            self.assertEqual(settings.zone.id, "")
            self.assertEqual(settings.zone.name, "")

    def test_login_returns_zones_and_never_returns_password(self) -> None:
        response = self.login(remember_password=True)

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload["connection"]["status"], "available")
        self.assertEqual(
            payload["zones"],
            [{"id": "4101", "name": "真实一区"}, {"id": "4102", "name": "真实二区"}],
        )
        self.assertEqual(payload["config"]["zones"], payload["zones"])
        self.assertTrue(payload["config"]["account"]["password_configured"])
        self.assertEqual(len(self.token_store.saved), 1)
        self.assertEqual(
            self.zone_requests, [{"userid": "user-1", "verify_token": "verify-token"}]
        )
        self.assertNotIn("demo-secret", response.get_data(as_text=True))
        self.assertNotIn("verify-token", response.get_data(as_text=True))
        self.assertNotIn("demo-secret", self.config_path.read_text(encoding="utf-8"))

    def test_session_restores_from_saved_tokens_after_recreate(self) -> None:
        self.assertEqual(self.login(remember_password=True).status_code, 200)
        self.assertEqual(
            self.put_json(
                "/api/config/zone", {"id": "4101", "name": "真实一区"}
            ).status_code,
            200,
        )
        self.zone_requests.clear()
        self.app.extensions["daily_console"]["jobs"].shutdown()

        restored_app = create_app(
            {"TESTING": True, "SIMULATION_DELAY": 0.01},
            config_path=self.config_path,
            credential_store=self.credentials,
            account_service_factory=lambda config_store, credentials: AccountService(
                config_store,
                credentials,
                login_client_factory=lambda: self.login_client,
                token_store=self.token_store,
                zone_loader=lambda tokens: (
                    self.zone_requests.append(dict(tokens)) or (
                        AccountZone(4101, "真实一区"),
                        AccountZone("4102", "真实二区"),
                    )
                ),
                endpoint_resolver=lambda tokens, args: GameEndpoint(
                    "ws://test.invalid", "game-token", getattr(args, "zone_id"), "真实一区"
                ),
            ),
            arena_service=self.arena_service,
        )
        self.addCleanup(
            restored_app.extensions["daily_console"]["jobs"].shutdown
        )
        restored_client = restored_app.test_client()

        config = restored_client.get("/api/config").get_json()
        self.assertEqual(config["connection"]["status"], "available")
        self.assertEqual(
            config["zones"],
            [{"id": "4101", "name": "真实一区"}, {"id": "4102", "name": "真实二区"}],
        )
        self.assertEqual(config["zone"]["id"], "4101")
        self.assertEqual(
            self.zone_requests, [{"userid": "user-1", "verify_token": "verify-token"}]
        )
        self.assertEqual(len(self.login_client.calls), 1)

        index = restored_client.get("/")
        self.assertEqual(index.status_code, 200)
        self.assertIn(b'"status": "available"', index.data)
        self.assertIn(b'"id": "4101"', index.data)
        # Jinja tojson 会把中文区服名转义为 \\uXXXX。
        self.assertIn(b"\\u771f\\u5b9e\\u4e00\\u533a", index.data)

    def test_login_with_empty_password_uses_remembered_credential(self) -> None:
        self.assertEqual(self.login(remember_password=True).status_code, 200)
        self.login_client.calls.clear()

        response = self.post_json(
            "/api/account/login",
            {
                "username": "demo-user",
                "password": "",
                "remember_password": True,
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(self.login_client.calls), 1)
        self.assertEqual(self.login_client.calls[0]["password"], "demo-secret")

    def test_tab_auto_refresh_hooks_exist_in_frontend(self) -> None:
        script = (
            Path(__file__).resolve().parents[1] / "app" / "static" / "app.js"
        ).read_text(encoding="utf-8")
        self.assertIn("function refreshTab(name", script)
        self.assertIn("activateTab(button.dataset.tab)", script)
        self.assertIn("refreshTab(state.activeTab)", script)
        self.assertIn("已恢复登录", script)

    def test_zone_selection_requires_current_login_result(self) -> None:
        response = self.put_json(
            "/api/config/zone", {"id": "4101", "name": "真实一区"}
        )
        self.assertEqual(response.status_code, 400)

        self.assertEqual(self.login().status_code, 200)
        response = self.put_json(
            "/api/config/zone", {"id": "4101", "name": "真实一区"}
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["config"]["zone"]["id"], "4101")

    def test_login_failure_is_desensitized_and_does_not_return_demo_zones(self) -> None:
        self.login_client.error = LoginError("upstream detail must stay private")

        response = self.login()

        self.assertEqual(response.status_code, 502)
        self.assertEqual(
            response.get_json()["error"],
            "登录或获取区服失败，请检查账号、密码、网络和区服状态",
        )
        self.assertNotIn("demo-secret", response.get_data(as_text=True))
        self.assertNotIn("upstream detail", response.get_data(as_text=True))
        self.assertEqual(self.client.get("/api/config").get_json()["zones"], [])

    def test_daily_selection_persists_and_rejects_unavailable_task(self) -> None:
        response = self.put_json(
            "/api/daily-tasks/selection", {"task_ids": [101, 104, 119]}
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.get_json()["config"]["daily"]["enabled_task_ids"], [101, 104, 119]
        )

        response = self.put_json(
            "/api/daily-tasks/selection", {"task_ids": [102]}
        )
        self.assertEqual(response.status_code, 400)

        reloaded = ConfigStore(self.config_path).snapshot()
        self.assertEqual(reloaded.daily.enabled_task_ids, [101, 104, 119])

    def test_arena_settings_validate_the_boundary(self) -> None:
        invalid_rounds = self.put_json(
            "/api/config/arena",
            {"rounds": 0, "outcome": "mercy", "refresh_on_exhaustion": True},
        )
        self.assertEqual(invalid_rounds.status_code, 400)

        invalid_outcome = self.put_json(
            "/api/config/arena",
            {"rounds": 10, "outcome": "unknown", "refresh_on_exhaustion": True},
        )
        self.assertEqual(invalid_outcome.status_code, 400)

        valid = self.put_json(
            "/api/config/arena",
            {"rounds": 25, "outcome": "execute", "refresh_on_exhaustion": False},
        )
        self.assertEqual(valid.status_code, 200)
        self.assertEqual(valid.get_json()["config"]["arena"]["rounds"], 25)
        self.assertEqual(valid.get_json()["config"]["arena"]["outcome"], "execute")

    def test_arena_status_reads_the_current_game_server(self) -> None:
        self.assertEqual(self.login().status_code, 200)
        self.assertEqual(
            self.put_json(
                "/api/config/zone", {"id": "4101", "name": "真实一区"}
            ).status_code,
            200,
        )

        response = self.client.get("/api/arena")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.get_json()["opponents"],
            {"total": 15, "available": 12, "challenged": 3},
        )
        self.assertEqual(response.get_json()["daily_reward"], {"received": True, "count": 4})
        self.assertEqual(len(self.arena_service.snapshot_endpoints), 1)

    def test_treasure_status_exposes_server_maps_and_daily_remaining_quota(self) -> None:
        self.assertEqual(self.login().status_code, 200)
        self.assertEqual(
            self.put_json(
                "/api/config/zone", {"id": "4101", "name": "真实一区"}
            ).status_code,
            200,
        )

        response = self.client.get("/api/treasure")

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(
            payload["areas"],
            [
                {"id": 1001, "name": "审判庭城门前", "selected": False},
                {"id": 1002, "name": "幽暗林地", "selected": False},
            ],
        )
        self.assertEqual(
            payload["sweep"],
            {"used": 3, "limit": 8, "available": 5, "request_limit": 5},
        )
        self.assertEqual(len(self.treasure_live_endpoints), 1)

    def test_treasure_farm_catalog_exposes_farm_areas_without_budgets(self) -> None:
        response = self.client.get("/api/treasure/farm-catalog")

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()["farm"]
        self.assertNotIn("limits", payload)
        self.assertTrue(payload["farm_areas"])

    def test_treasure_settings_and_job_enforce_30_per_request(self) -> None:
        invalid = self.put_json(
            "/api/config/treasure", {"area_id": 1001, "times": 31}
        )
        self.assertEqual(invalid.status_code, 400)

        self.assertEqual(self.login().status_code, 200)
        self.assertEqual(
            self.put_json(
                "/api/config/zone", {"id": "4101", "name": "真实一区"}
            ).status_code,
            200,
        )
        response = self.post_json(
            "/api/jobs/treasure", {"area_id": 1001, "times": 4}
        )
        self.assertEqual(response.status_code, 200)
        terminal = self.wait_for_terminal_job(response.get_json()["job"]["id"])

        self.assertEqual(terminal["status"], "succeeded")
        self.assertEqual(self.treasure_state["sweep_calls"], [(1001, 4)])
        self.assertEqual(
            terminal["result"]["request"],
            {"area_id": 1001, "area_name": "审判庭城门前", "times": 4},
        )
        self.assertEqual(terminal["result"]["treasure"]["sweep"]["available"], 1)
        self.assertIn("审判庭城门前", terminal["result"]["summary"])
        self.assertEqual(
            self.client.get("/api/config").get_json()["treasure"],
            {
                "area_id": 1001,
                "area_name": "审判庭城门前",
                "times": 4,
                "farm_area_id": 530101,
                "farm_area_name": "沉默之城",
                "farm_target_hearth": 100,
            },
        )

    def test_treasure_job_rejects_times_above_remaining_server_quota(self) -> None:
        self.assertEqual(self.login().status_code, 200)
        self.assertEqual(
            self.put_json(
                "/api/config/zone", {"id": "4101", "name": "真实一区"}
            ).status_code,
            200,
        )

        response = self.post_json(
            "/api/jobs/treasure", {"area_id": 1001, "times": 6}
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()["error"], "今日聚宝之地仅剩 5 次可扫荡")
        self.assertEqual(self.treasure_state["sweep_calls"], [])

    def test_dungeon_status_exposes_names_and_highest_scores(self) -> None:
        self.assertEqual(self.login().status_code, 200)
        self.assertEqual(
            self.put_json(
                "/api/config/zone", {"id": "4101", "name": "真实一区"}
            ).status_code,
            200,
        )

        response = self.client.get("/api/dungeon")

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(
            payload["dungeons"],
            [
                {
                    "id": 2301,
                    "name": "暮霭地窟",
                    "highest_score": 88,
                    "selected": False,
                },
                {
                    "id": 2302,
                    "name": "风蚀遗迹",
                    "highest_score": 165,
                    "selected": False,
                },
            ],
        )
        self.assertEqual(
            payload["draw"], {"used": 1, "total": 2, "available": 1}
        )
        self.assertEqual(len(self.dungeon_live_endpoints), 1)

    def test_dungeon_job_sweeps_then_draws_all_and_returns_rewards(self) -> None:
        self.assertEqual(self.login().status_code, 200)
        self.assertEqual(
            self.put_json(
                "/api/config/zone", {"id": "4101", "name": "真实一区"}
            ).status_code,
            200,
        )

        response = self.post_json("/api/jobs/dungeon", {"dungeon_id": 2302})

        self.assertEqual(response.status_code, 200)
        terminal = self.wait_for_terminal_job(response.get_json()["job"]["id"])

        self.assertEqual(terminal["status"], "succeeded")
        self.assertEqual(
            self.dungeon_state["operations"],
            [("sweep", 2302), ("draw_all", 2302)],
        )
        result = terminal["result"]
        self.assertTrue(result["sweep_completed"])
        self.assertEqual(result["dungeon"]["draw"], {"used": 3, "total": 3, "available": 0})
        self.assertEqual(
            result["rewards"],
            [
                {
                    "id": 9001,
                    "name": "远古秘宝",
                    "kind": 1,
                    "kind_label": "物品",
                    "quantity": 2,
                    "source": "item_change",
                },
                {
                    "id": 9002,
                    "name": "星辉宝箱",
                    "kind": 2,
                    "kind_label": "奖励箱",
                    "quantity": 1,
                    "source": "item_change",
                },
            ],
        )
        completion_event = next(
            event
            for event in terminal["events"]
            if event["data"].get("phase") == "completed"
        )
        self.assertEqual(
            completion_event["message"],
            "风蚀遗迹 全部抽取完成，获得 2 项服务端结算奖励："
            "远古秘宝 × 2、星辉宝箱 × 1",
        )
        self.assertEqual(completion_event["data"]["rewards"], result["rewards"])
        self.assertEqual(
            result["draw"],
            {
                "all_drawn": True,
                "draw_times": 3,
                "total_draw_times": 3,
                "count": 2,
                "reward_notice_received": True,
            },
        )
        self.assertEqual(
            self.client.get("/api/config").get_json()["dungeon"],
            {"dungeon_id": 2302, "dungeon_name": "风蚀遗迹"},
        )

    def test_dungeon_job_reports_draw_protocol_failure(self) -> None:
        self.assertEqual(self.login().status_code, 200)
        self.assertEqual(
            self.put_json(
                "/api/config/zone", {"id": "4101", "name": "真实一区"}
            ).status_code,
            200,
        )
        self.dungeon_state["draw_error"] = HarvestError("游戏服关闭了 WebSocket 连接")

        response = self.post_json("/api/jobs/dungeon", {"dungeon_id": 2302})

        self.assertEqual(response.status_code, 200)
        terminal = self.wait_for_terminal_job(response.get_json()["job"]["id"])
        self.assertEqual(terminal["status"], "failed")
        self.assertEqual(
            terminal["error_message"],
            "风蚀遗迹 全部抽取未完成，请检查游戏服连接后重试",
        )
        self.assertNotEqual(terminal["error_message"], "任务执行失败")

    def test_item_catalog_list_resolves_reward_details(self) -> None:
        item_data_path = Path(self.temporary_directory.name) / "items.json"
        item_data_path.write_text(
            json.dumps(
                [
                    {
                        "id": 9101,
                        "name": "远古徽记",
                        "text": "用于兑换地下城奖励。",
                    }
                ],
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

        self.assertEqual(
            _load_item_details(item_data_path),
            {
                9101: {
                    "name": "远古徽记",
                    "description": "用于兑换地下城奖励。",
                }
            },
        )

    def test_dungeon_draw_ids_are_shown_when_notice_has_no_reward_entries(self) -> None:
        service = self.app.extensions["daily_console"]["dungeon"]
        rewards = service.reward_payload(
            DungeonDrawResponse(
                ret=0,
                dungeon_id=2302,
                draw_times=3,
                total_draw_times=3,
                reward_ids=(9001, 9002),
                probabilities=(250, 750),
                all_drawn=True,
                reward_notice_received=True,
            )
        )

        self.assertEqual(
            rewards,
            [
                {
                    "id": 9001,
                    "name": "远古秘宝",
                    "kind_label": "抽取掉落",
                    "quantity": 1,
                    "source": "draw_response",
                    "probability_code": 250,
                },
                {
                    "id": 9002,
                    "name": "星辉宝箱",
                    "kind_label": "抽取掉落",
                    "quantity": 1,
                    "source": "draw_response",
                    "probability_code": 750,
                },
            ],
        )

    def test_dungeon_job_rejects_a_dungeon_not_available_from_server_state(self) -> None:
        self.assertEqual(self.login().status_code, 200)
        self.assertEqual(
            self.put_json(
                "/api/config/zone", {"id": "4101", "name": "真实一区"}
            ).status_code,
            200,
        )

        response = self.post_json("/api/jobs/dungeon", {"dungeon_id": 9999})

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()["error"], "所选地下城当前不可扫荡")
        self.assertEqual(self.dungeon_state["operations"], [])

    def test_cross_origin_and_non_json_writes_are_rejected(self) -> None:
        cross_origin = self.client.post(
            "/api/jobs/daily", json={}, headers={"Origin": "https://invalid.example"}
        )
        self.assertEqual(cross_origin.status_code, 403)

        non_json = self.client.post("/api/jobs/daily", data="not-json")
        self.assertEqual(non_json.status_code, 415)

    def test_single_job_lock_and_cooperative_stop(self) -> None:
        self.assertEqual(self.login().status_code, 200)
        self.assertEqual(
            self.put_json(
                "/api/config/zone", {"id": "4101", "name": "真实一区"}
            ).status_code,
            200,
        )
        arena = self.post_json(
            "/api/jobs/arena",
            {"rounds": 100, "outcome": "mercy", "refresh_on_exhaustion": True},
        )
        self.assertEqual(arena.status_code, 200)
        job_id = arena.get_json()["job"]["id"]

        concurrent_daily = self.post_json("/api/jobs/daily", {})
        self.assertEqual(concurrent_daily.status_code, 409)

        cancellation = self.post_json(f"/api/jobs/{job_id}/cancel", {})
        self.assertEqual(cancellation.status_code, 200)
        terminal = self.wait_for_terminal_job(job_id)
        self.assertEqual(terminal["status"], "cancelled")
        self.assertEqual(len(self.arena_service.run_endpoints), 1)
        self.assertTrue(any(event["message"] == "任务已停止" for event in terminal["events"]))

    def test_daily_job_emits_progress_and_completes_selected_tasks(self) -> None:
        self.assertEqual(
            self.put_json(
                "/api/daily-tasks/selection", {"task_ids": [104, 112, 119]}
            ).status_code,
            200,
        )
        self.assertEqual(self.login().status_code, 200)
        self.assertEqual(
            self.put_json(
                "/api/config/zone", {"id": "4101", "name": "真实一区"}
            ).status_code,
            200,
        )
        response = self.post_json("/api/jobs/daily", {})
        self.assertEqual(response.status_code, 200)
        terminal = self.wait_for_terminal_job(response.get_json()["job"]["id"])

        self.assertEqual(terminal["status"], "succeeded")
        daily = terminal["result"]["daily"]
        selected = [task for task in daily["tasks"] if task["selected"]]
        self.assertEqual([task["progress"] for task in selected], [1, 1, 1])
        self.assertEqual(daily["summary"]["activity_score"], 30)
        self.assertEqual(len(self.live_endpoints), 1)
        self.assertEqual(
            self.endpoint_requests,
            [
                {
                    "tokens": {"userid": "user-1", "verify_token": "verify-token"},
                    "zone_id": "4101",
                }
            ],
        )

    def test_recovery_job_settles_server_state_and_refreshes_daily(self) -> None:
        recovery_manager = InMemoryRecoveryManager()
        self.app.extensions["daily_console"]["game_session"] = recovery_manager
        self.assertEqual(self.login().status_code, 200)
        self.assertEqual(
            self.put_json(
                "/api/config/zone", {"id": "4101", "name": "真实一区"}
            ).status_code,
            200,
        )

        response = self.post_json("/api/jobs/recovery", {})

        self.assertEqual(response.status_code, 200)
        terminal = self.wait_for_terminal_job(response.get_json()["job"]["id"])
        self.assertEqual(terminal["status"], "succeeded")
        self.assertEqual(len(recovery_manager.snapshot_endpoints), 1)
        self.assertEqual(len(recovery_manager.recovery_endpoints), 1)
        self.assertEqual(
            terminal["result"]["recovery"],
            {
                "stage": "已完成",
                "pending": False,
                "description": "空闲",
                "issues": [],
            },
        )
        self.assertFalse(terminal["result"]["daily"]["actions_blocked"])
        self.assertTrue(
            any(
                event["message"] == "遗留状态已处理，日常任务可继续执行"
                for event in terminal["events"]
            )
        )

    def test_daily_job_requires_current_login_and_selected_zone(self) -> None:
        not_logged_in = self.post_json("/api/jobs/daily", {})

        self.assertEqual(not_logged_in.status_code, 400)
        self.assertEqual(not_logged_in.get_json()["error"], "请先登录并获取区服")

        self.assertEqual(self.login().status_code, 200)
        no_zone = self.post_json("/api/jobs/daily", {})

        self.assertEqual(no_zone.status_code, 400)
        self.assertEqual(no_zone.get_json()["error"], "请先选择区服")

    def test_daily_status_requires_current_game_server_session(self) -> None:
        response = self.client.get("/api/daily-tasks")

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()["error"], "请先登录并获取区服")
        self.assertEqual(self.live_endpoints, [])

    def test_daily_status_refresh_uses_current_game_server_session(self) -> None:
        self.assertEqual(self.login().status_code, 200)
        self.assertEqual(
            self.put_json(
                "/api/config/zone", {"id": "4101", "name": "真实一区"}
            ).status_code,
            200,
        )

        response = self.client.get("/api/daily-tasks")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(self.live_endpoints), 1)
        self.assertNotIn("game-token", response.get_data(as_text=True))


class ConfigStoreTestCase(unittest.TestCase):
    def test_legacy_config_migrates_without_password_and_uses_private_mode(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "config" / "ui-settings.json"
            path.parent.mkdir(parents=True)
            path.write_text(
                json.dumps(
                    {
                        "version": 0,
                        "account": {
                            "username": "legacy-user",
                            "remember_password": True,
                            "password": "must-not-survive",
                        },
                        "daily": {"enabled_task_ids": [104, "bad"]},
                        "arena": {"rounds": 101, "outcome": "unknown"},
                    }
                ),
                encoding="utf-8",
            )

            settings = ConfigStore(path).snapshot()
            persisted = path.read_text(encoding="utf-8")

            self.assertEqual(settings.account.username, "legacy-user")
            self.assertEqual(settings.daily.enabled_task_ids, [104])
            self.assertEqual(settings.arena.rounds, 10)
            self.assertNotIn("must-not-survive", persisted)
            self.assertNotIn('"password"', persisted)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)


if __name__ == "__main__":
    unittest.main()
