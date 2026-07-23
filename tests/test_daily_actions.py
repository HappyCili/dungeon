from __future__ import annotations

import unittest
from types import SimpleNamespace

from daily_actions import (
    ActionExecution,
    CallableDailyAction,
    DAILY_GUILD_GOLD_COST_LIMIT,
    DailyActionRunner,
    run_adventurer_guild_action,
    run_ancient_law_court_action,
    run_arcane_tower_action,
    run_dragon_arena_action,
    run_fief_harvest_action,
    run_fief_quick_harvest_action,
    run_knight_arena_action,
    run_smithy_forge_action,
)
from daily_quest import load_daily_catalog
from harvest_fief import (
    HARVEST_FREE,
    HARVEST_NORMAL,
    HARVEST_PAY,
    FiefHarvestRejected,
    GameEndpoint,
    HarvestError,
    describe_fief_harvest_rejection,
)
from app.services.daily_service import DailyService, DailyServiceError
from tests.daily_fixtures import (
    InMemoryDailyGateway,
    build_test_daily_action_runner,
)


class DailyActionRunnerTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.bundle = build_test_daily_action_runner()

    def test_all_stage_b_tasks_complete_then_claim_scores_and_rewards(self) -> None:
        task_ids = [101, 103, 104, 105, 106, 109, 112, 119]

        result = self.bundle.runner.run(task_ids)

        self.assertFalse(result.cancelled)
        self.assertEqual([item.status for item in result.tasks], ["completed"] * 8)
        self.assertEqual(result.claims.claimed_task_ids, tuple(task_ids))
        self.assertTrue(all(result.status.task(task_id).finished for task_id in task_ids))
        self.assertTrue(
            all(result.status.task(task_id).score_claimed for task_id in task_ids)
        )
        expected_score = sum(
            self.bundle.runner.catalog.tasks[task_id].activity_score
            for task_id in task_ids
        )
        expected_rewards = tuple(
            reward.reward_id
            for reward in self.bundle.runner.catalog.activity_rewards
            if reward.score <= expected_score
        )
        self.assertEqual(result.claims.claimed_reward_ids, expected_rewards)

    def test_daily_batch_also_claims_available_weekly_rewards(self) -> None:
        self.bundle.gateway.set_progress(203, 1)

        result = self.bundle.runner.run([104])

        self.assertEqual(result.claims.claimed_daily_task_ids, (104,))
        self.assertEqual(result.claims.claimed_weekly_task_ids, (203,))
        self.assertEqual(result.claims.claimed_daily_reward_ids, ())
        self.assertEqual(result.claims.claimed_weekly_reward_ids, (201,))
        self.assertEqual(result.status.weekly_reward_ids, (201,))

        second = self.bundle.runner.run([104])
        self.assertEqual(second.claims.claimed_weekly_task_ids, ())
        self.assertEqual(second.claims.claimed_weekly_reward_ids, ())

    def test_runner_uses_server_remaining_progress_for_guild_and_fief(self) -> None:
        self.bundle.gateway.set_progress(101, 4)

        guild_result = self.bundle.runner.run_task(101)
        fief_result = self.bundle.runner.run_task(105)

        self.assertEqual(guild_result.status, "completed")
        self.assertEqual(self.bundle.actions[101].calls, [1])
        self.assertEqual(fief_result.status, "completed")
        self.assertEqual(self.bundle.actions[105].calls, [2])

    def test_each_action_queries_status_before_and_after(self) -> None:
        self.bundle.runner.run_task(104)

        self.assertEqual(self.bundle.gateway.status_calls, 2)

    def test_policy_and_resource_stops_leave_tasks_incomplete(self) -> None:
        cases = {
            101: "ss_detected",
            103: "no_resources",
            104: "no_free",
            105: "no_resources",
            112: "lost",
            119: "no_free",
        }
        for task_id, mode in cases.items():
            with self.subTest(task_id=task_id, mode=mode):
                bundle = build_test_daily_action_runner(modes={task_id: mode})

                result = bundle.runner.run_task(task_id)

                self.assertEqual(result.status, "incomplete")
                self.assertFalse(bundle.gateway.status().task(task_id).finished)
                self.assertEqual(len(bundle.actions[task_id].calls), 1)

    def test_second_run_skips_finished_actions_without_repeating_them(self) -> None:
        task_ids = [101, 103, 104, 105, 109, 112, 119]
        self.bundle.runner.run(task_ids)
        calls_after_first_run = {
            task_id: list(action.calls)
            for task_id, action in self.bundle.actions.items()
        }

        second = self.bundle.runner.run(task_ids)

        self.assertEqual([item.status for item in second.tasks], ["skipped"] * 7)
        self.assertEqual(
            {
                task_id: action.calls
                for task_id, action in self.bundle.actions.items()
            },
            calls_after_first_run,
        )
        self.assertEqual(second.claims.claimed_task_ids, ())
        self.assertEqual(second.claims.claimed_reward_ids, ())

    def test_local_action_result_does_not_override_unfinished_server_state(self) -> None:
        catalog = load_daily_catalog()
        gateway = InMemoryDailyGateway(catalog)
        runner = DailyActionRunner(
            gateway,
            catalog,
            {
                104: CallableDailyAction(
                    lambda remaining: ActionExecution(
                        remaining, remaining, "本地动作返回成功"
                    )
                )
            },
        )

        result = runner.run_task(104)

        self.assertEqual(result.status, "incomplete")
        self.assertFalse(gateway.status().task(104).finished)

    def test_runner_includes_harvest_error_detail(self) -> None:
        catalog = load_daily_catalog()
        gateway = InMemoryDailyGateway(catalog)
        runner = DailyActionRunner(
            gateway,
            catalog,
            {
                105: CallableDailyAction(
                    lambda _remaining: (_ for _ in ()).throw(
                        HarvestError("等待 Fief_harvest_res 响应超时")
                    )
                )
            },
        )

        result = runner.run_task(105)

        self.assertEqual(result.status, "failed")
        self.assertIn("HarvestError：等待 Fief_harvest_res 响应超时", result.message)

    def test_daily_service_exposes_closed_loop_result_and_claims(self) -> None:
        service = DailyService(
            live_runner_builder=lambda _endpoint: self.bundle.runner
        )
        service.use_game_server(
            GameEndpoint("ws://test.invalid", "game-token", "1", "test")
        )
        events: list[dict[str, object]] = []

        result = service.run(
            [101, 103, 104, 105, 109, 112, 119],
            [101, 103, 104, 105, 109, 112, 119],
            lambda _level, _message, data: events.append(data),
            lambda: False,
        )

        self.assertEqual(result["completed_tasks"], 7)
        self.assertEqual(
            [item["status"] for item in result["task_results"]],
            ["completed"] * 7,
        )
        self.assertEqual(result["daily"]["summary"]["activity_score"], 90)
        self.assertEqual(
            result["claimed_daily_task_ids"], [101, 103, 104, 105, 109, 112, 119]
        )
        self.assertEqual(result["claimed_weekly_task_ids"], [])
        self.assertIn("claimed_daily_reward_ids", result)
        self.assertIn("claimed_weekly_reward_ids", result)
        self.assertIn(
            "claimed_weekly_reward_ids", result["daily"]["summary"]
        )
        self.assertIn("claimed", [event.get("phase") for event in events])

    def test_daily_service_requires_a_real_game_server_session(self) -> None:
        service = DailyService()

        with self.assertRaises(DailyServiceError):
            service.snapshot([])


class ExistingActionWrapperTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.endpoint = GameEndpoint("ws://test.invalid", "game-token", "1", "test")

    def test_guild_wrapper_passes_remaining_count_to_existing_client(self) -> None:
        captured: dict[str, object] = {}

        class Client:
            def run_daily(self, catalog, **kwargs):
                captured["catalog"] = catalog
                captured.update(kwargs)
                return SimpleNamespace(
                    attempts=(object(), object()),
                    paused=False,
                    stop_reason="daily_target_reached",
                )

        execution = run_adventurer_guild_action(
            self.endpoint,
            1.0,
            3,
            client_factory=lambda *_args: Client(),
            catalog=object(),
        )

        self.assertEqual(captured["target_refreshes"], 3)
        self.assertEqual(captured["max_refreshes"], 3)
        self.assertEqual(captured["gold_cost_limit"], DAILY_GUILD_GOLD_COST_LIMIT)
        self.assertGreater(DAILY_GUILD_GOLD_COST_LIMIT, 200)
        self.assertEqual(execution.requested_count, 3)
        self.assertEqual(execution.attempted_count, 2)
        self.assertEqual(execution.message, "已请求 2/3 次冒险者公会刷新")

    def test_guild_wrapper_includes_policy_stop_reason_when_short(self) -> None:
        class Client:
            def run_daily(self, catalog, **kwargs):
                return SimpleNamespace(
                    attempts=(object(),),
                    paused=False,
                    stop_reason="policy_complete",
                )

        execution = run_adventurer_guild_action(
            self.endpoint,
            1.0,
            5,
            client_factory=lambda *_args: Client(),
            catalog=object(),
        )

        self.assertEqual(execution.attempted_count, 1)
        self.assertIn("1/5", execution.message)
        self.assertIn("符合条件的金币和免费宝石刷新均已完成", execution.message)

    def test_free_action_wrappers_cap_attempts_at_remaining_count(self) -> None:
        tower_calls: list[int] = []
        ancient_calls: list[int] = []
        forge_calls: list[int] = []

        class TowerClient:
            def explore_daily_free(self, *, max_attempts: int):
                tower_calls.append(max_attempts)
                return SimpleNamespace(
                    attempts=(SimpleNamespace(response=SimpleNamespace(result=0)),)
                )

        class AncientClient:
            def engrave_daily_free(self, *, max_attempts: int):
                ancient_calls.append(max_attempts)
                return SimpleNamespace(
                    attempts=(
                        SimpleNamespace(
                            response=SimpleNamespace(result=0),
                            safety_verified=True,
                            rune_infos=(),
                        ),
                    )
                )

        class ForgeClient:
            def forge_for_daily(self, *, max_times: int):
                forge_calls.append(max_times)
                return SimpleNamespace(
                    attempts=(
                        SimpleNamespace(
                            requested_times=max_times,
                            response=SimpleNamespace(ret=0, times=max_times),
                        ),
                    ),
                    stop_reason="completed",
                )

        tower = run_arcane_tower_action(
            self.endpoint, 1.0, 1, client_factory=lambda *_args: TowerClient()
        )
        ancient = run_ancient_law_court_action(
            self.endpoint, 1.0, 1, client_factory=lambda *_args: AncientClient()
        )
        forge = run_smithy_forge_action(
            self.endpoint, 1.0, 5, client_factory=lambda *_args: ForgeClient()
        )

        self.assertEqual(tower_calls, [1])
        self.assertEqual(ancient_calls, [1])
        self.assertEqual(forge_calls, [5])
        self.assertEqual(tower.attempted_count, 1)
        self.assertEqual(ancient.attempted_count, 1)
        self.assertEqual(forge.attempted_count, 5)
        self.assertIn("5/5", forge.message)

    def test_smithy_forge_wrapper_reports_resource_stop(self) -> None:
        class ForgeClient:
            def forge_for_daily(self, *, max_times: int):
                return SimpleNamespace(
                    attempts=(),
                    stop_reason="insufficient_iron",
                )

        execution = run_smithy_forge_action(
            self.endpoint,
            1.0,
            5,
            client_factory=lambda *_args: ForgeClient(),
        )

        self.assertEqual(execution.requested_count, 5)
        self.assertEqual(execution.attempted_count, 0)
        self.assertIn("未执行", execution.message)
        self.assertIn("铁", execution.message)

    def test_ancient_law_wrapper_logs_purple_and_yellow_runes(self) -> None:
        class AncientClient:
            def engrave_daily_free(self, *, max_attempts: int):
                purple = SimpleNamespace(
                    order_rune_id=310101,
                    instance_id=11,
                    rarity=3,
                    level=1,
                    stage_level=0,
                    affix_attributes=(),
                    pattern_attributes=(),
                )
                yellow = SimpleNamespace(
                    order_rune_id=410101,
                    instance_id=12,
                    rarity=4,
                    level=1,
                    stage_level=0,
                    affix_attributes=(),
                    pattern_attributes=(),
                )
                common = SimpleNamespace(
                    order_rune_id=110101,
                    instance_id=13,
                    rarity=1,
                    level=1,
                    stage_level=0,
                    affix_attributes=(),
                    pattern_attributes=(),
                )
                return SimpleNamespace(
                    attempts=(
                        SimpleNamespace(
                            response=SimpleNamespace(result=0, instance_ids=(), pull_info=None),
                            safety_verified=True,
                            rune_infos=(purple, yellow, common),
                            notifications=(
                                SimpleNamespace(
                                    source=410,
                                    items=(),
                                    equipment_instance_ids=(),
                                    artifact_instance_ids=(90001,),
                                    rune_instance_ids=(),
                                    costs=(),
                                    props=(
                                        SimpleNamespace(kind=4, item_id=101, amount=1),
                                    ),
                                ),
                            ),
                        ),
                    )
                )

        execution = run_ancient_law_court_action(
            self.endpoint,
            1.0,
            1,
            client_factory=lambda *_args: AncientClient(),
        )

        self.assertIn("高品质掉落", execution.message)
        self.assertIn("紫色", execution.message)
        self.assertIn("黄色", execution.message)
        self.assertIn("秘宝", execution.message)
        self.assertIn("整体", execution.message)
        self.assertNotIn("灰色", execution.message)

    def test_ancient_law_wrapper_logs_green_quality_drops(self) -> None:
        class AncientClient:
            def engrave_daily_free(self, *, max_attempts: int):
                green_rune = SimpleNamespace(
                    order_rune_id=510101,
                    instance_id=21,
                    rarity=5,
                    level=1,
                    stage_level=0,
                    affix_attributes=(),
                    pattern_attributes=(),
                )
                return SimpleNamespace(
                    attempts=(
                        SimpleNamespace(
                            response=SimpleNamespace(
                                result=0, instance_ids=(), pull_info=None
                            ),
                            safety_verified=True,
                            rune_infos=(green_rune,),
                            notifications=(
                                SimpleNamespace(
                                    source=410,
                                    items=(),
                                    equipment_instance_ids=(),
                                    artifact_instance_ids=(90002,),
                                    rune_instance_ids=(),
                                    costs=(),
                                    props=(
                                        # artifact id 301 → rarity 3 → 绿色整体
                                        SimpleNamespace(kind=4, item_id=301, amount=1),
                                        # piece item 40101 → quality 3 → 紫色碎片
                                        SimpleNamespace(kind=1, item_id=40101, amount=2),
                                    ),
                                ),
                            ),
                        ),
                    )
                )

        execution = run_ancient_law_court_action(
            self.endpoint,
            1.0,
            1,
            client_factory=lambda *_args: AncientClient(),
        )

        self.assertIn("高品质掉落", execution.message)
        self.assertIn("绿色", execution.message)
        self.assertIn("整体", execution.message)
        self.assertIn("碎片", execution.message)
        self.assertIn("秘宝", execution.message)

    def test_fief_wrapper_requests_one_normal_harvest_per_run(self) -> None:
        clients: list[object] = []
        modes: list[object] = []

        class Client:
            def __init__(self) -> None:
                self.calls = 0

            def harvest(self, mode=HARVEST_NORMAL, **_kwargs):
                self.calls += 1
                modes.append(mode)
                return object()

        def factory(*_args):
            client = Client()
            clients.append(client)
            return client

        execution = run_fief_harvest_action(
            self.endpoint, 1.0, 2, client_factory=factory
        )

        self.assertEqual(execution.requested_count, 1)
        self.assertEqual(execution.attempted_count, 1)
        self.assertEqual(len(clients), 1)
        self.assertEqual(modes, [HARVEST_NORMAL])
        self.assertIn("需等待新的资源产出", execution.message)
        with self.assertRaises(ValueError):
            run_fief_harvest_action(self.endpoint, 1.0, 3, client_factory=factory)

    def test_fief_wrapper_reports_server_rejection_as_incomplete(self) -> None:
        class Client:
            def harvest(self, mode=HARVEST_NORMAL, **_kwargs):
                raise FiefHarvestRejected(4)

        execution = run_fief_harvest_action(
            self.endpoint, 1.0, 1, client_factory=lambda *_args: Client()
        )

        self.assertEqual(execution.requested_count, 1)
        self.assertEqual(execution.attempted_count, 0)
        self.assertEqual(execution.message, "第 1 次庄园普通收取未执行：暂无可收取资源")

    def test_fief_quick_wrapper_uses_free_mode_only_by_default(self) -> None:
        modes: list[object] = []

        class Client:
            def harvest(self, mode=HARVEST_NORMAL, **_kwargs):
                modes.append(mode)
                return object()

        execution = run_fief_quick_harvest_action(
            self.endpoint, 1.0, 1, client_factory=lambda *_args: Client()
        )

        self.assertEqual(execution.requested_count, 1)
        self.assertEqual(execution.attempted_count, 1)
        self.assertEqual(modes, [HARVEST_FREE])
        self.assertIn("庄园快速收取", execution.message)
        with self.assertRaises(ValueError):
            run_fief_quick_harvest_action(
                self.endpoint, 1.0, 2, client_factory=lambda *_args: Client()
            )

    def test_fief_quick_wrapper_reports_no_free_times(self) -> None:
        class Client:
            def harvest(self, mode=HARVEST_NORMAL, **_kwargs):
                raise FiefHarvestRejected(1)

        execution = run_fief_quick_harvest_action(
            self.endpoint, 1.0, 1, client_factory=lambda *_args: Client()
        )

        self.assertEqual(execution.requested_count, 1)
        self.assertEqual(execution.attempted_count, 0)
        self.assertEqual(execution.message, "第 1 次庄园快速收取未执行：免费收取次数不足")

    def test_fief_quick_wrapper_optional_pay_fallback(self) -> None:
        modes: list[object] = []

        class Client:
            def harvest(self, mode=HARVEST_NORMAL, **_kwargs):
                modes.append(mode)
                if mode == HARVEST_FREE:
                    raise FiefHarvestRejected(1)
                return object()

        execution = run_fief_quick_harvest_action(
            self.endpoint,
            1.0,
            1,
            client_factory=lambda *_args: Client(),
            allow_pay=True,
        )

        self.assertEqual(execution.requested_count, 1)
        self.assertEqual(execution.attempted_count, 1)
        self.assertEqual(modes, [HARVEST_FREE, HARVEST_PAY])

    def test_fief_rejection_message_uses_localized_reason(self) -> None:
        self.assertEqual(describe_fief_harvest_rejection(1), "免费收取次数不足")
        self.assertEqual(describe_fief_harvest_rejection(2), "付费道具不足")
        self.assertEqual(describe_fief_harvest_rejection(3), "今日快速收取使用次数已达上限")
        self.assertEqual(describe_fief_harvest_rejection(4), "暂无可收取资源")
        self.assertEqual(describe_fief_harvest_rejection(9), "服务端拒绝收取（ret=9）")

    def test_arena_wrapper_uses_redacted_logging_options(self) -> None:
        captured: dict[str, object] = {}

        class Client:
            def __enter__(self):
                captured["entered"] = True
                return self

            def resume_pending_battle(self, *, win_choice_id: int | None = None, **_kwargs):
                captured["choice_id"] = win_choice_id
                return None

            def run_loop(self, **kwargs):
                captured["loop"] = kwargs
                return (
                    SimpleNamespace(battle=SimpleNamespace(win=False)),
                    SimpleNamespace(battle=SimpleNamespace(win=True)),
                )

            def close(self) -> None:
                captured["closed"] = True

        def factory(*_args, **kwargs):
            captured["factory_kwargs"] = kwargs
            return Client()

        execution = run_dragon_arena_action(
            self.endpoint, 1.0, 1, client_factory=factory
        )

        self.assertTrue(captured["entered"])
        self.assertTrue(captured["closed"])
        self.assertIsNone(captured["factory_kwargs"]["websocket_log"])
        self.assertIsNone(captured["factory_kwargs"]["business_log"])
        self.assertFalse(captured["factory_kwargs"]["log_server_messages"])
        self.assertEqual(captured["loop"]["rounds"], 1)
        self.assertTrue(captured["loop"]["require_wins"])
        self.assertEqual(captured["choice_id"], 2)
        self.assertEqual(execution.attempted_count, 2)
        self.assertIn("胜利 1/1", execution.message)

    def test_knight_arena_wrapper_counts_challenges_not_wins(self) -> None:
        captured: dict[str, object] = {}

        class Client:
            def __enter__(self):
                captured["entered"] = True
                return self

            def run_loop(self, **kwargs):
                captured["loop"] = kwargs
                return (
                    SimpleNamespace(battle=SimpleNamespace(win=False)),
                    SimpleNamespace(battle=SimpleNamespace(win=True)),
                    SimpleNamespace(battle=SimpleNamespace(win=False)),
                )

            def close(self) -> None:
                captured["closed"] = True

        def factory(*_args, **kwargs):
            captured["factory_kwargs"] = kwargs
            return Client()

        execution = run_knight_arena_action(
            self.endpoint, 1.0, 3, client_factory=factory
        )

        self.assertTrue(captured["entered"])
        self.assertTrue(captured["closed"])
        self.assertIsNone(captured["factory_kwargs"]["websocket_log"])
        self.assertFalse(captured["factory_kwargs"]["log_server_messages"])
        self.assertEqual(captured["loop"]["rounds"], 3)
        self.assertNotIn("require_wins", captured["loop"])
        self.assertEqual(execution.attempted_count, 3)
        self.assertIn("完成 3/3 场挑战", execution.message)
        self.assertIn("1 胜 / 2 负", execution.message)


if __name__ == "__main__":
    unittest.main()
