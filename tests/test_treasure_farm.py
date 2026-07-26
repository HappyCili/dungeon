"""聚宝之地刷取：协议编解码、节点选择与服务编排单测。"""

from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from types import SimpleNamespace

from app.services.treasure_service import TreasureService, format_farm_summary
from harvest_fief import GameEndpoint, ItemChange, encode_bytes_field, encode_int_field
from treasure_farm import (
    BIG_CHEST_KEY_COST,
    HEARTH_ITEM_ID,
    LOC_STATUS_ACTIVE,
    LOC_STATUS_PASSED,
    NODE_KIND_BIG_CHEST,
    NODE_KIND_MONSTER,
    NODE_KIND_SMALL_CHEST,
    REWARD_WAIT_CHEST_SETTLED_S,
    REWARD_WAIT_MONSTER_SETTLED_S,
    SMALL_CHEST_KEY_COST,
    AreaSession,
    FarmProgress,
    MapNodeSpec,
    TreasureFarmKickout,
    chest_key_cost,
    choose_next_action,
    decode_enter_area_response,
    decode_enter_treasure_response,
    decode_processloc_response,
    decode_treasure_battle_end,
    encode_enter_treasure_request,
    encode_processloc_request,
    format_kickout_error,
    get_treasure_map_entry,
    is_treasure_map_area,
    list_treasure_map_catalog,
    load_area_nodes,
    progress_payload,
    reward_wait_timeout,
    run_treasure_farm,
)


class KickoutMessageTestCase(unittest.TestCase):
    def test_kickout_ret2_explains_concurrent_login(self) -> None:
        text = format_kickout_error(2, "会话已切换")
        self.assertIn("其他客户端", text)
        self.assertIn("Kickout ret=2", text)
        self.assertIn("会话已切换", text)
        exc = TreasureFarmKickout(2, "会话已切换")
        self.assertEqual(exc.ret, 2)
        self.assertIn("退出", str(exc))


class PhaseClassifyTestCase(unittest.TestCase):
    def test_login_battle_marker_precedes_landmark_lock(self) -> None:
        from treasure_farm import (
            PHASE_BATTLE_RECOVERY,
            MapLoginSnapshot,
            TreasureFarmClient,
        )

        client = object.__new__(TreasureFarmClient)
        client._map_dead = False
        client._curarea = 530101
        client._pending_battle_info = None
        client._saw_battle_s2c_start = False
        client._saw_battle_frames = False
        client._battle_ended = False
        client._battle_started_by_us = False
        client._last_processloc_ret = 2
        client._map_snapshot = MapLoginSnapshot(
            dead=False,
            needsoul=False,
            curarea=530101,
            pos_x=0,
            pos_y=0,
            events=0,
            loc_status={},
            seat_rands={},
            game_data_has_battle_blob=True,
            battle_state=2,
            battle_type=2,
        )
        self.assertEqual(
            client.classify_phase(processloc_ret=2), PHASE_BATTLE_RECOVERY
        )

    def test_processloc_ret2_is_landmark_locked_without_battle_info(self) -> None:
        from treasure_farm import (
            PHASE_BATTLE_PREPARE,
            PHASE_LANDMARK_LOCKED,
            TreasureFarmClient,
        )

        # 不连网：只测阶段归类逻辑
        client = object.__new__(TreasureFarmClient)
        client._map_dead = False
        client._curarea = 530101
        client._pending_battle_info = None
        client._saw_battle_s2c_start = False
        client._saw_battle_frames = False
        client._battle_ended = False
        client._battle_started_by_us = False
        client._last_processloc_ret = 2
        self.assertEqual(
            client.classify_phase(processloc_ret=2),
            PHASE_LANDMARK_LOCKED,
        )

    def test_battle_info_outranks_landmark_locked(self) -> None:
        from dragon_arena import BattleInfo
        from treasure_farm import PHASE_BATTLE_PREPARE, TreasureFarmClient

        client = object.__new__(TreasureFarmClient)
        client._map_dead = False
        client._curarea = 530101
        client._pending_battle_info = BattleInfo(
            battle_id=1,
            ret=0,
            battle_type=2,
            location_id=1,
            player_units=1,
            enemy_units=1,
            enemy_team=(),
            skip_team=False,
            skip_mode=0,
            battle_data=b"",
            raw_payload=b"",
        )
        client._saw_battle_s2c_start = False
        client._saw_battle_frames = False
        client._battle_ended = False
        client._battle_started_by_us = False
        client._last_processloc_ret = 2
        self.assertEqual(client.classify_phase(processloc_ret=2), PHASE_BATTLE_PREPARE)


class EnterRejectMessageTestCase(unittest.TestCase):
    def test_enter_ret5_mentions_unlock_or_cost(self) -> None:
        from treasure_farm import TreasureFarmRejected

        err = TreasureFarmRejected("进入", 5)
        self.assertIn("道具不足", str(err))
        self.assertIn("解锁", str(err))

    def test_enter_area_ret5_has_guidance(self) -> None:
        from treasure_farm import TreasureFarmRejected

        err = TreasureFarmRejected("进入区域", 5)
        self.assertIn("ret", str(err))
        self.assertIn("拒绝", str(err))

    def test_decode_map_reset_response(self) -> None:
        from treasure_farm import decode_map_reset_response

        # ret=0, areaid=730101, locs empty detail still ok
        body = encode_int_field(1, 0) + encode_int_field(2, 730101)
        ret, area_id, locs = decode_map_reset_response(body)
        self.assertEqual(ret, 0)
        self.assertEqual(area_id, 730101)
        self.assertEqual(locs, {})


class ProtocolCodecTestCase(unittest.TestCase):
    def test_shared_login_does_not_replay_stale_game_data_after_entry(self) -> None:
        from treasure_farm import decode_game_data_curarea, TreasureFarmClient

        def game_data_with_area(area_id: int) -> bytes:
            area_info = encode_int_field(2, area_id)
            return encode_bytes_field(11, encode_bytes_field(6, area_info))

        class SharedSession:
            def __init__(self, game_data: bytes) -> None:
                self.game_data = game_data
                self.password = "test-password"
                self.sent: list[int] = []

            def ensure_ready(self, _endpoint: GameEndpoint) -> None:
                return None

            def send_message(
                self, message_id: int, _data: bytes = b"", *, encrypted: bool = True
            ) -> None:
                self.sent.append(message_id)

        shared = SharedSession(game_data_with_area(10002))
        client = TreasureFarmClient(
            GameEndpoint("ws://test.invalid", "token", "1", "test"),
            session=shared,
            websocket_log=False,
        )
        recovery_calls = {"collect": 0, "stage": 0, "refresh": 0}
        client._collect_post_login_battle_signals = lambda _seconds: recovery_calls.__setitem__(
            "collect", recovery_calls["collect"] + 1
        )
        client._signal_stage_ready = lambda: recovery_calls.__setitem__(
            "stage", recovery_calls["stage"] + 1
        )
        client._refresh_treasure_info = lambda: recovery_calls.__setitem__(
            "refresh", recovery_calls["refresh"] + 1
        )
        applied_areas: list[int] = []

        def apply_game_data(data: bytes, *, request_client_data: bool) -> None:
            del request_client_data
            area_id = decode_game_data_curarea(data)
            applied_areas.append(area_id)
            client._raw_game_data = data
            client._curarea = area_id
            client._initial_locs = {}
            client._client_data_requested = True

        client._apply_game_data = apply_game_data

        client.login()
        self.assertEqual(client._curarea, 10002)
        self.assertEqual(applied_areas, [10002])

        # Map_enter_area has already confirmed the target map.  A nested
        # ensure_actionable() login must preserve that newer state rather than
        # restoring the shared session's login-time snapshot.
        client._curarea = 530101
        client._initial_locs = {1: LOC_STATUS_ACTIVE}
        client.login()

        self.assertEqual(client._curarea, 530101)
        self.assertEqual(client._initial_locs, {1: LOC_STATUS_ACTIVE})
        self.assertEqual(applied_areas, [10002])
        self.assertEqual(recovery_calls, {"collect": 3, "stage": 2, "refresh": 1})

        # A newly connected shared session publishes a distinct Game_data
        # object, which must still initialize the client.
        shared.game_data = bytes(bytearray(game_data_with_area(730101)))
        client.login()
        self.assertEqual(client._curarea, 730101)
        self.assertEqual(applied_areas, [10002, 730101])

    def test_decode_treasure_battle_end_requires_victory(self) -> None:
        win = decode_treasure_battle_end(
            encode_int_field(1, 7)
            + encode_int_field(2, 1)
            + encode_int_field(10, 2)
        )
        self.assertTrue(win.win)
        self.assertEqual(win.result_code, 2)
        self.assertEqual(win.round_number, 7)

        loss = decode_treasure_battle_end(
            encode_int_field(1, 3)
            + encode_int_field(2, 0)
            + encode_int_field(10, 1)
        )
        self.assertFalse(loss.win)
        self.assertEqual(loss.result_code, 1)

    def test_login_snapshot_decodes_area_runtime_and_move_trigger(self) -> None:
        from treasure_farm import MoveTriggerState, decode_game_data_map_snapshot

        area_id = 530101
        area_record = (
            encode_int_field(1, area_id)
            + encode_int_field(2, 1)
            + encode_int_field(3, 2)
            + encode_int_field(6, 0)
        )
        area_entry = encode_int_field(1, area_id) + encode_bytes_field(2, area_record)
        move_trigger = (
            encode_int_field(1, 3)
            + encode_int_field(3, area_id)
            + encode_int_field(4, 7)
        )
        area_detail = encode_bytes_field(7, move_trigger)
        area_info = (
            encode_bytes_field(1, area_entry)
            + encode_int_field(2, area_id)
            + encode_bytes_field(3, area_detail)
            + encode_int_field(4, 2147483647)
        )
        map_data = encode_int_field(4, 10) + encode_bytes_field(6, area_info)
        game_data = encode_bytes_field(11, map_data)

        snap = decode_game_data_map_snapshot(game_data)

        self.assertEqual(snap.events, 10)
        self.assertEqual(snap.area_state, 1)
        self.assertEqual(snap.area_flag, 2)
        self.assertEqual(snap.area_locked, 0)
        self.assertEqual(snap.area_remains, 2147483647)
        self.assertEqual(
            snap.move_trigger,
            MoveTriggerState(max=3, remain=0, area=area_id, triggernum=7),
        )

    def test_move_does_not_send_move_trigger_without_mtdata(self) -> None:
        from treasure_farm import MAP_MOVE_MESSAGE_ID, TreasureFarmClient

        client = object.__new__(TreasureFarmClient)
        client._seat_rands = {99: (1, 1)}
        client._pos_x = 1
        client._pos_y = 1
        client._move_trigger = None
        client.timeout = 0.2
        sent: list[int] = []
        client._send_message = lambda mid, data=b"", *, encrypted: sent.append(mid)
        replies = [SimpleNamespace(message_id=MAP_MOVE_MESSAGE_ID, data=b"")]
        client._receive_header = lambda _deadline, _context: replies.pop(0)
        client._handle_common_message = lambda _header: False
        client._note_battle_message = lambda _header: None

        self.assertTrue(client.move_toward_node(530101, 99))
        self.assertEqual(sent, [MAP_MOVE_MESSAGE_ID])

    def test_process_node_ret60_requires_a_ready_move_trigger(self) -> None:
        from treasure_farm import (
            MAP_PROCESSLOC_MESSAGE_ID,
            TreasureFarmClient,
            TreasureFarmError,
        )

        client = object.__new__(TreasureFarmClient)
        client.login = lambda: None
        client.battle_timeout = 1.0
        client._item_totals = {}
        client._move_trigger = None
        client._send_message = lambda *_args, **_kwargs: sent.append(_args[0])
        client._handle_common_message = lambda _header: False
        sent: list[int] = []
        replies = [
            SimpleNamespace(
                message_id=MAP_PROCESSLOC_MESSAGE_ID,
                data=encode_int_field(1, 60),
            )
        ]
        client._receive_header = lambda _deadline, _context: replies.pop(0)

        with self.assertRaisesRegex(TreasureFarmError, "无可激活移动触发器"):
            client.process_node(530101, 99)

        self.assertEqual(sent, [MAP_PROCESSLOC_MESSAGE_ID])

    def test_process_node_ret60_activates_once_then_stops_after_one_retry(self) -> None:
        from treasure_farm import (
            MAP_MOVETRIGGER_ACTIVE_MESSAGE_ID,
            MAP_PROCESSLOC_MESSAGE_ID,
            MoveTriggerState,
            TreasureFarmClient,
            TreasureFarmError,
        )

        client = object.__new__(TreasureFarmClient)
        client.login = lambda: None
        client.battle_timeout = 1.0
        client._item_totals = {}
        client._move_trigger = MoveTriggerState(
            max=1,
            remain=0,
            area=530101,
            triggernum=1,
        )
        client._handle_common_message = lambda _header: False
        sent: list[int] = []
        client._send_message = lambda mid, data=b"", *, encrypted: sent.append(mid)
        replies = [
            SimpleNamespace(
                message_id=MAP_PROCESSLOC_MESSAGE_ID,
                data=encode_int_field(1, 60),
            ),
            SimpleNamespace(
                message_id=MAP_PROCESSLOC_MESSAGE_ID,
                data=encode_int_field(1, 60),
            ),
        ]

        def receive(_deadline: float, _context: str) -> SimpleNamespace:
            reply = replies.pop(0)
            return reply

        client._receive_header = receive

        with self.assertRaisesRegex(TreasureFarmError, "移动触发重试已达上限"):
            client.process_node(530101, 99)

        self.assertEqual(
            sent,
            [
                MAP_PROCESSLOC_MESSAGE_ID,
                MAP_MOVETRIGGER_ACTIVE_MESSAGE_ID,
                MAP_PROCESSLOC_MESSAGE_ID,
            ],
        )

    def test_reset_area_exits_then_reenters_with_normal_enterway(self) -> None:
        from treasure_farm import AreaSession, TreasureFarmClient

        client = object.__new__(TreasureFarmClient)
        client._curarea = 530101
        client.battle_timeout = 2.0
        calls: list[object] = []
        client.login = lambda: calls.append("login")
        client.clear_pending_map_activity = lambda *, timeout: calls.append(
            ("clear", timeout)
        )
        client.finish_pending_battle = lambda *, timeout: calls.append(
            ("finish", timeout)
        )
        client.exit_area = lambda: calls.append("exit")
        expected = AreaSession(area_id=530101, loc_status={1: LOC_STATUS_ACTIVE})

        def enter(area_id: int, *, reset: bool = False) -> AreaSession:
            calls.append(("enter", area_id, reset))
            return expected

        client.enter_treasure = enter

        self.assertEqual(client.reset_area(530101), expected)
        self.assertEqual(calls[0], "login")
        self.assertIn("exit", calls)
        self.assertEqual(calls[-1], ("enter", 530101, False))

    def test_idle_preflight_does_not_interact_with_a_node(self) -> None:
        from treasure_farm import PHASE_ACTIONABLE, PHASE_MAP_IDLE, TreasureFarmClient

        client = object.__new__(TreasureFarmClient)
        client.login = lambda: None
        client._curarea = 530101
        client._saw_battle_s2c_start = False
        client._saw_battle_frames = False
        client._pending_battle_info = None
        client.classify_phase = lambda *args, **kwargs: PHASE_MAP_IDLE
        sent: list[int] = []
        client._send_message = lambda mid, data=b"", *, encrypted: sent.append(mid)

        status = client.ensure_actionable(preferred_area_id=530101)

        self.assertEqual(status["phase"], PHASE_ACTIONABLE)
        self.assertTrue(status["non_mutating_preflight"])
        self.assertEqual(sent, [])

    def test_status_query_does_not_send_processloc(self) -> None:
        from treasure_farm import MapLoginSnapshot, TreasureFarmClient

        client = object.__new__(TreasureFarmClient)
        event_progress_modes: list[bool] = []
        client.login = lambda: event_progress_modes.append(client._auto_event_progress)
        client._map_snapshot = MapLoginSnapshot(
            dead=False,
            needsoul=False,
            curarea=530101,
            pos_x=16,
            pos_y=26,
            events=10,
            loc_status={},
            seat_rands={},
            game_data_has_battle_blob=True,
            battle_state=0,
            battle_type=7,
        )
        client._map_dead = False
        client._curarea = 530101
        client._pos_x = 16
        client._pos_y = 26
        client._initial_locs = {}
        client._seat_rands = {}
        client._open_times = 0
        client._battle_context = None
        client._pending_battle_info = None
        client._saw_battle_s2c_start = False
        client._saw_battle_frames = False
        client._battle_ended = False
        client._battle_started_by_us = False
        client._last_processloc_ret = None
        client._client_data_ready = True
        client._login_battle_message_ids = []
        client._auto_event_progress = True
        client._collect_post_login_battle_signals = lambda _timeout: None
        client._signal_stage_ready = lambda: None
        client.item_total = lambda _item_id: 0
        sent: list[int] = []
        client._send_message = lambda mid, data=b"", *, encrypted: sent.append(mid)

        status = client.inspect_status()

        self.assertIsNone(status["processloc_probe"])
        self.assertIsNone(status["exit_probe"])
        self.assertEqual(sent, [])
        self.assertEqual(event_progress_modes, [False])
        self.assertTrue(client._auto_event_progress)
        with self.assertRaises(ValueError):
            client.inspect_status(probe=True)

    def test_event_func_action_confirms_native_reward_display(self) -> None:
        from dragon_arena_business_map import (
            EVENT_FUNC_ACTION_MESSAGE_ID,
            EVENT_FUNC_NEXT_MESSAGE_ID,
        )
        from treasure_farm import TreasureFarmClient, encode_event_func_next

        client = object.__new__(TreasureFarmClient)
        client._auto_event_progress = True
        client._pending_event_action = None
        client._pending_event_start = None
        client._event_progress_notes = []
        sent: list[tuple[int, bytes, bool]] = []
        client._send_message = lambda mid, data=b"", *, encrypted: sent.append(
            (mid, data, encrypted)
        )
        event_info = (
            encode_int_field(1, 1001)
            + encode_int_field(4, 2001)
            + encode_int_field(7, 6)
        )
        payload = encode_bytes_field(1, event_info) + encode_int_field(2, 1)

        self.assertTrue(
            client._handle_common_message(
                SimpleNamespace(message_id=EVENT_FUNC_ACTION_MESSAGE_ID, data=payload)
            )
        )
        self.assertEqual(
            sent,
            [(EVENT_FUNC_NEXT_MESSAGE_ID, encode_event_func_next(), True)],
        )
        self.assertEqual(encode_event_func_next(), b"")
        self.assertIsNone(client._pending_event_action)
        self.assertIn("地图事件获得物品提示已确认", client.drain_event_progress_notes())

    def test_process_node_waits_for_reward_event_completion(self) -> None:
        from dragon_arena_business_map import (
            EVENT_END_MESSAGE_ID,
            EVENT_FUNC_ACTION_MESSAGE_ID,
            EVENT_FUNC_NEXT_MESSAGE_ID,
        )
        from treasure_farm import (
            MAP_PROCESSLOC_MESSAGE_ID,
            TreasureFarmClient,
            TreasureFarmError,
            encode_processloc_request,
        )

        client = object.__new__(TreasureFarmClient)
        client.login = lambda: None
        client.battle_timeout = 1.0
        client._item_totals = {}
        client._auto_event_progress = True
        client._pending_event_action = None
        client._pending_event_start = None
        client._event_progress_notes = []
        sent: list[tuple[int, bytes, bool]] = []
        client._send_message = lambda mid, data=b"", *, encrypted: sent.append(
            (mid, data, encrypted)
        )
        event_payload = encode_int_field(2, 1)
        loc_entry = encode_int_field(1, 6) + encode_int_field(2, LOC_STATUS_PASSED)
        loc_changes = encode_bytes_field(1, loc_entry)
        result_payload = encode_int_field(1, 0) + encode_bytes_field(2, loc_changes)
        replies = [
            SimpleNamespace(message_id=EVENT_FUNC_ACTION_MESSAGE_ID, data=event_payload),
            SimpleNamespace(message_id=EVENT_END_MESSAGE_ID, data=b""),
            SimpleNamespace(message_id=MAP_PROCESSLOC_MESSAGE_ID, data=result_payload),
        ]

        def receive(_deadline: float, _context: str) -> SimpleNamespace:
            if replies:
                return replies.pop(0)
            raise TreasureFarmError("测试收包结束")

        client._receive_header = receive

        result = client.process_node(530101, 6)

        self.assertEqual(result.loc_updates, {6: LOC_STATUS_PASSED})
        self.assertEqual(
            sent,
            [
                (
                    MAP_PROCESSLOC_MESSAGE_ID,
                    encode_processloc_request(6, 530101),
                    True,
                ),
                (EVENT_FUNC_NEXT_MESSAGE_ID, b"", True),
            ],
        )
        self.assertIn("地图事件已结束", client.drain_event_progress_notes())

    def test_process_node_rejects_battle_loss_before_key_reward(self) -> None:
        from dragon_arena_business_map import BATTLE_S2C_END_MESSAGE_ID
        from treasure_farm import (
            TreasureFarmClient,
            TreasureFarmError,
        )

        client = object.__new__(TreasureFarmClient)
        client.login = lambda: None
        client.battle_timeout = 1.0
        client._item_totals = {1801: 0}
        client._battle_won = None
        client._battle_result_code = None
        client._event_chain_active = False
        client._event_progress_notes = []
        client._send_message = lambda *_args, **_kwargs: None
        replies = [
            SimpleNamespace(
                message_id=BATTLE_S2C_END_MESSAGE_ID,
                data=encode_int_field(2, 0) + encode_int_field(10, 1),
            )
        ]
        client._receive_header = lambda _deadline, _context: replies.pop(0)

        with self.assertRaisesRegex(TreasureFarmError, "战败|未胜利|钥匙奖励"):
            client.process_node(
                230101,
                1,
                node_kind=NODE_KIND_MONSTER,
                expected_reward_item_id=1801,
            )

    def test_process_node_waits_for_end_after_early_locchanges(self) -> None:
        """locchanges may precede the final Battle_S2C_end packet."""

        from dragon_arena_business_map import (
            BATTLE_INFO_MESSAGE_ID,
            BATTLE_S2C_END_MESSAGE_ID,
        )
        from harvest_fief import STORAGE_ITEM_CHANGE_MESSAGE_ID
        from treasure_farm import (
            MAP_PROCESSLOC_MESSAGE_ID,
            TreasureFarmClient,
            TreasureFarmError,
        )

        client = object.__new__(TreasureFarmClient)
        client.login = lambda: None
        client.battle_timeout = 0.2
        client._item_totals = {1801: 0}
        client._battle_won = None
        client._battle_result_code = None
        client._event_chain_active = False
        client._event_progress_notes = []
        client._send_message = lambda *_args, **_kwargs: None
        client._start_battle = lambda _info: None
        client._configure_battle = lambda: None

        loc_entry = encode_int_field(1, 1) + encode_int_field(2, LOC_STATUS_PASSED)
        loc_changes = encode_bytes_field(1, loc_entry)
        loc_result = encode_int_field(1, 0) + encode_bytes_field(2, loc_changes)
        item_change = (
            encode_int_field(1, 1801)
            + encode_int_field(2, 1)
            + encode_int_field(3, 1)
        )
        item_notice = encode_bytes_field(2, item_change)
        replies: list[object] = [
            SimpleNamespace(
                message_id=BATTLE_INFO_MESSAGE_ID,
                data=encode_int_field(1, 91),
            ),
            SimpleNamespace(message_id=MAP_PROCESSLOC_MESSAGE_ID, data=loc_result),
            TreasureFarmError("短暂收包超时"),
            SimpleNamespace(
                message_id=BATTLE_S2C_END_MESSAGE_ID,
                data=encode_int_field(2, 1) + encode_int_field(10, 2),
            ),
            SimpleNamespace(
                message_id=STORAGE_ITEM_CHANGE_MESSAGE_ID,
                data=item_notice,
            ),
            TreasureFarmError("测试收包结束"),
        ]

        def receive(_deadline: float, _context: str) -> SimpleNamespace:
            reply = replies.pop(0)
            if isinstance(reply, TreasureFarmError):
                raise reply
            return reply

        client._receive_header = receive
        events: list[str] = []

        result = client.process_node(
            230101,
            1,
            node_kind=NODE_KIND_MONSTER,
            expected_reward_item_id=1801,
            emit=lambda _level, _message, data: events.append(
                str(data.get("workflow_step"))
            ),
        )

        self.assertEqual(result.battle_won, True)
        self.assertEqual(result.reward_delta, 1)
        self.assertEqual(
            events,
            [
                "monster_interact",
                "battle_prepare",
                "battle_enter",
                "battle_victory",
                "key_take",
                "key_reward",
            ],
        )

    def test_event_start_confirms_only_native_auto_option(self) -> None:
        from dragon_arena_business_map import EVENT_OPTION_MESSAGE_ID, EVENT_START_MESSAGE_ID
        from treasure_farm import TreasureFarmClient, encode_event_option

        client = object.__new__(TreasureFarmClient)
        client._auto_event_progress = True
        client._pending_event_action = None
        client._pending_event_start = None
        client._event_progress_notes = []
        client._preferred_event_item_ids = frozenset()
        client._item_totals = {}
        client._last_confirmed_key_option = None
        client._active_event_context = {"node_kind": NODE_KIND_MONSTER, "key_item_id": 0}
        sent: list[tuple[int, bytes, bool]] = []
        client._send_message = lambda mid, data=b"", *, encrypted: sent.append(
            (mid, data, encrypted)
        )
        event_info = encode_int_field(1, 1001) + encode_int_field(4, 2001)
        payload = encode_bytes_field(1, event_info) + encode_int_field(4, 100)

        self.assertTrue(
            client._handle_common_message(
                SimpleNamespace(message_id=EVENT_START_MESSAGE_ID, data=payload)
            )
        )
        self.assertEqual(
            sent,
            [(EVENT_OPTION_MESSAGE_ID, encode_event_option(100, 2001, 1001), True)],
        )
        self.assertIsNone(client._pending_event_start)

    def test_event_start_confirms_take_key_option_after_battle(self) -> None:
        """战后：autoidx=0 但 option 标题为「带走钥匙」且 optidx=100 时自动确认。"""

        from dragon_arena_business_map import EVENT_OPTION_MESSAGE_ID, EVENT_START_MESSAGE_ID
        from treasure_farm import (
            TreasureFarmClient,
            decode_event_start,
            encode_event_option,
            encode_string_field,
        )

        option_blob = (
            encode_string_field(1, "带走钥匙")
            + encode_int_field(2, 100)
        )
        event_info = encode_int_field(1, 730101) + encode_int_field(4, 6)
        payload = (
            encode_bytes_field(1, event_info)
            + encode_bytes_field(3, option_blob)
            + encode_int_field(4, 0)
        )
        start = decode_event_start(payload)
        self.assertEqual(start.auto_option, 0)
        self.assertTrue(start.auto_confirmable)
        chosen = start.choose_take_key_option()
        self.assertIsNotNone(chosen)
        assert chosen is not None
        self.assertEqual(chosen.optidx, 100)
        self.assertEqual(chosen.title, "带走钥匙")

        client = object.__new__(TreasureFarmClient)
        client._auto_event_progress = True
        client._pending_event_action = None
        client._pending_event_start = None
        client._event_progress_notes = []
        client._preferred_event_item_ids = frozenset()
        client._item_totals = {}
        client._last_confirmed_key_option = None
        client._active_event_context = {"node_kind": NODE_KIND_MONSTER, "key_item_id": 0}
        sent: list[tuple[int, bytes, bool]] = []
        client._send_message = lambda mid, data=b"", *, encrypted: sent.append(
            (mid, data, encrypted)
        )
        self.assertTrue(
            client._handle_common_message(
                SimpleNamespace(message_id=EVENT_START_MESSAGE_ID, data=payload)
            )
        )
        self.assertEqual(
            sent,
            [(EVENT_OPTION_MESSAGE_ID, encode_event_option(100, 6, 730101), True)],
        )
        self.assertIsNone(client._pending_event_start)
        self.assertIn("带走钥匙已确认", client.drain_event_progress_notes()[0])

    def test_event_start_confirms_single_no_cost_continue_after_battle(self) -> None:
        """战后唯一的无消耗「继续前进」分支会自动确认。"""

        from dragon_arena_business_map import EVENT_OPTION_MESSAGE_ID, EVENT_START_MESSAGE_ID
        from treasure_farm import (
            EventOptionEntry,
            EventStart,
            TreasureFarmClient,
            decode_event_start,
            encode_event_option,
            encode_string_field,
        )

        option_blob = (
            encode_string_field(1, "继续前进")
            + encode_int_field(2, 100)
        )
        event_info = encode_int_field(1, 530101) + encode_int_field(4, 4)
        payload = (
            encode_bytes_field(1, event_info)
            + encode_bytes_field(3, option_blob)
            + encode_int_field(4, 0)
        )
        start = decode_event_start(payload)
        self.assertFalse(start.auto_confirmable)
        chosen = start.choose_battle_continue_option()
        self.assertIsNotNone(chosen)
        assert chosen is not None
        self.assertEqual(chosen.optidx, 100)

        # 同名但带消耗，或存在第二个分支时，均不能按战后继续处理。
        self.assertIsNone(
            EventStart(
                options=(
                    EventOptionEntry(title="继续前进", optidx=100, use=1809, use_num=1),
                )
            ).choose_battle_continue_option()
        )
        self.assertIsNone(
            EventStart(
                options=(
                    EventOptionEntry(title="继续前进", optidx=100),
                    EventOptionEntry(title="离开", optidx=101),
                )
            ).choose_battle_continue_option()
        )

        client = object.__new__(TreasureFarmClient)
        client._auto_event_progress = True
        client._pending_event_action = None
        client._pending_event_start = None
        client._event_progress_notes = []
        client._preferred_event_item_ids = frozenset()
        client._item_totals = {}
        client._last_confirmed_key_option = None
        client._active_event_context = {"node_kind": NODE_KIND_MONSTER, "key_item_id": 0}
        sent: list[tuple[int, bytes, bool]] = []
        client._send_message = lambda mid, data=b"", *, encrypted: sent.append(
            (mid, data, encrypted)
        )

        self.assertTrue(
            client._handle_common_message(
                SimpleNamespace(message_id=EVENT_START_MESSAGE_ID, data=payload)
            )
        )
        self.assertEqual(
            sent,
            [(EVENT_OPTION_MESSAGE_ID, encode_event_option(100, 4, 530101), True)],
        )
        self.assertIsNone(client._pending_event_start)
        self.assertIn("战后继续前进已确认", client.drain_event_progress_notes()[0])

    def test_event_start_confirms_touch_magic_circle_over_leave_after_battle(
        self,
    ) -> None:
        """战后「触碰法阵 / 暂时离开」时明确选择前者。"""

        from dragon_arena_business_map import EVENT_OPTION_MESSAGE_ID, EVENT_START_MESSAGE_ID
        from treasure_farm import (
            EventOptionEntry,
            EventStart,
            TreasureFarmClient,
            decode_event_start,
            encode_event_option,
            encode_string_field,
        )

        touch_option_blob = (
            encode_string_field(1, "触碰法阵")
            + encode_int_field(2, 1)
        )
        leave_option_blob = (
            encode_string_field(1, "暂时离开")
            + encode_int_field(2, 2)
        )
        event_info = encode_int_field(1, 530101) + encode_int_field(4, 4)
        payload = (
            encode_bytes_field(1, event_info)
            + encode_bytes_field(3, touch_option_blob)
            + encode_bytes_field(3, leave_option_blob)
            + encode_int_field(4, 0)
        )
        start = decode_event_start(payload)
        chosen = start.choose_touch_magic_circle_option()
        self.assertIsNotNone(chosen)
        assert chosen is not None
        self.assertEqual(chosen.optidx, 1)

        # 标题相同但有消耗或存在歧义时，不得自动确认。
        self.assertIsNone(
            EventStart(
                options=(
                    EventOptionEntry(title="触碰法阵", optidx=100, use=1809, use_num=1),
                )
            ).choose_touch_magic_circle_option()
        )
        self.assertIsNone(
            EventStart(
                options=(
                    EventOptionEntry(title="触碰法阵", optidx=1),
                    EventOptionEntry(title="触碰法阵", optidx=2),
                )
            ).choose_touch_magic_circle_option()
        )
        self.assertIsNone(
            EventStart(
                options=(
                    EventOptionEntry(title="触碰法阵", optidx=100),
                    EventOptionEntry(title="离开", optidx=101),
                )
            ).choose_battle_continue_option()
        )

        client = object.__new__(TreasureFarmClient)
        client._auto_event_progress = True
        client._pending_event_action = None
        client._pending_event_start = None
        client._event_progress_notes = []
        client._preferred_event_item_ids = frozenset()
        client._item_totals = {}
        client._last_confirmed_key_option = None
        client._active_event_context = {"node_kind": NODE_KIND_MONSTER, "key_item_id": 0}
        sent: list[tuple[int, bytes, bool]] = []
        client._send_message = lambda mid, data=b"", *, encrypted: sent.append(
            (mid, data, encrypted)
        )

        self.assertTrue(
            client._handle_common_message(
                SimpleNamespace(message_id=EVENT_START_MESSAGE_ID, data=payload)
            )
        )
        self.assertEqual(
            sent,
            [(EVENT_OPTION_MESSAGE_ID, encode_event_option(1, 4, 530101), True)],
        )
        self.assertIsNone(client._pending_event_start)
        self.assertIn("触碰法阵已确认", client.drain_event_progress_notes()[0])

    def test_event_start_confirms_key_cost_open_chest_option(self) -> None:
        """宝箱交互后：Event_start 带 use/useNum 钥匙消耗时自动 Event_option。"""

        from dragon_arena_business_map import EVENT_OPTION_MESSAGE_ID, EVENT_START_MESSAGE_ID
        from treasure_farm import (
            EventOptionEntry,
            TreasureFarmClient,
            decode_event_start,
            encode_event_option,
            encode_string_field,
        )

        key_item_id = 1809
        option_blob = (
            encode_string_field(1, "打开宝箱")
            + encode_int_field(2, 1)
            + encode_int_field(4, key_item_id)
            + encode_int_field(5, 1)
        )
        event_info = encode_int_field(1, 5001) + encode_int_field(4, 6001)
        payload = (
            encode_bytes_field(1, event_info)
            + encode_bytes_field(3, option_blob)
        )
        start = decode_event_start(payload)
        self.assertEqual(start.auto_option, 0)
        self.assertEqual(len(start.options), 1)
        self.assertEqual(
            start.options[0],
            EventOptionEntry(
                title="打开宝箱",
                optidx=1,
                use=key_item_id,
                use_num=1,
            ),
        )
        self.assertFalse(start.auto_confirmable)
        chosen = start.choose_item_cost_option(
            preferred_item_ids={key_item_id},
            item_totals={key_item_id: 3},
        )
        self.assertIsNotNone(chosen)
        assert chosen is not None
        self.assertEqual(chosen.optidx, 1)
        # 钥匙为 0 时不得自动确认开箱
        self.assertIsNone(
            start.choose_item_cost_option(
                preferred_item_ids={key_item_id},
                item_totals={key_item_id: 0},
            )
        )

        client = object.__new__(TreasureFarmClient)
        client._auto_event_progress = True
        client._pending_event_action = None
        client._pending_event_start = None
        client._event_progress_notes = []
        client._preferred_event_item_ids = frozenset({key_item_id})
        client._item_totals = {key_item_id: 3}
        client._last_confirmed_key_option = None
        client._active_event_context = {
            "node_kind": NODE_KIND_SMALL_CHEST,
            "key_item_id": key_item_id,
        }
        sent: list[tuple[int, bytes, bool]] = []
        client._send_message = lambda mid, data=b"", *, encrypted: sent.append(
            (mid, data, encrypted)
        )

        self.assertTrue(
            client._handle_common_message(
                SimpleNamespace(message_id=EVENT_START_MESSAGE_ID, data=payload)
            )
        )
        self.assertEqual(
            sent,
            [(EVENT_OPTION_MESSAGE_ID, encode_event_option(1, 6001, 5001), True)],
        )
        self.assertIsNone(client._pending_event_start)
        self.assertIsNotNone(client._last_confirmed_key_option)
        self.assertIn("确定用钥匙打开宝箱已确认", client.drain_event_progress_notes()[0])

        # 背包无钥匙：不应发送 Event_option
        sent.clear()
        client._item_totals = {key_item_id: 0}
        client._pending_event_start = None
        client._event_progress_notes = []
        self.assertTrue(
            client._handle_common_message(
                SimpleNamespace(message_id=EVENT_START_MESSAGE_ID, data=payload)
            )
        )
        self.assertEqual(sent, [])
        self.assertIsNotNone(client._pending_event_start)

    def test_event_start_never_spends_unrelated_item_cost(self) -> None:
        from dragon_arena_business_map import EVENT_START_MESSAGE_ID
        from treasure_farm import EventStart, EventOptionEntry, TreasureFarmClient, encode_string_field

        key_item_id = 1809
        unrelated_item_id = 9999
        start = EventStart(
            event_id=5001,
            dialog_id=6001,
            options=(
                EventOptionEntry(
                    title="继续",
                    optidx=1,
                    use=unrelated_item_id,
                    use_num=1,
                ),
            ),
        )
        self.assertIsNone(
            start.choose_open_chest_option(
                preferred_item_ids={key_item_id},
                item_totals={key_item_id: 5, unrelated_item_id: 5},
            )
        )

        client = object.__new__(TreasureFarmClient)
        client._auto_event_progress = True
        client._pending_event_action = None
        client._pending_event_start = None
        client._event_progress_notes = []
        client._preferred_event_item_ids = frozenset({key_item_id})
        client._item_totals = {key_item_id: 5, unrelated_item_id: 5}
        client._last_confirmed_key_option = None
        client._active_event_context = {
            "node_kind": NODE_KIND_SMALL_CHEST,
            "key_item_id": key_item_id,
        }
        sent: list[int] = []
        client._send_message = lambda mid, data=b"", *, encrypted: sent.append(mid)

        option_blob = (
            encode_string_field(1, "继续")
            + encode_int_field(2, 1)
            + encode_int_field(4, unrelated_item_id)
            + encode_int_field(5, 1)
        )
        event_info = encode_int_field(1, 5001) + encode_int_field(4, 6001)
        payload = encode_bytes_field(1, event_info) + encode_bytes_field(3, option_blob)

        self.assertTrue(
            client._handle_common_message(
                SimpleNamespace(
                    message_id=EVENT_START_MESSAGE_ID,
                    data=payload,
                )
            )
        )
        self.assertEqual(sent, [])
        self.assertIsNotNone(client._pending_event_start)

    def test_process_node_chest_confirms_key_then_opens(self) -> None:
        """开箱主路径：processloc → Event_start 钥匙确认 → locchanges → 炉温。"""

        from dragon_arena_business_map import (
            EVENT_END_MESSAGE_ID,
            EVENT_OPTION_MESSAGE_ID,
            EVENT_START_MESSAGE_ID,
        )
        from harvest_fief import STORAGE_ITEM_CHANGE_MESSAGE_ID
        from treasure_farm import (
            FARM_STEP_CHEST_KEY_CONFIRM,
            FARM_STEP_CHEST_OPEN,
            FARM_STEP_CHEST_REWARD,
            MAP_MOVETRIGGER_ACTIVE_MESSAGE_ID,
            MAP_PROCESSLOC_MESSAGE_ID,
            MoveTriggerState,
            TreasureFarmClient,
            TreasureFarmError,
            encode_event_option,
            encode_processloc_request,
            encode_string_field,
        )

        key_item_id = 1809
        option_blob = (
            encode_string_field(1, "打开宝箱")
            + encode_int_field(2, 1)
            + encode_int_field(4, key_item_id)
            + encode_int_field(5, 1)
        )
        event_info = encode_int_field(1, 5001) + encode_int_field(4, 6001)
        event_payload = encode_bytes_field(1, event_info) + encode_bytes_field(
            3, option_blob
        )
        loc_entry = encode_int_field(1, 6) + encode_int_field(2, LOC_STATUS_PASSED)
        loc_changes = encode_bytes_field(1, loc_entry)
        result_payload = encode_int_field(1, 0) + encode_bytes_field(2, loc_changes)
        item_change = (
            encode_int_field(1, HEARTH_ITEM_ID)
            + encode_int_field(2, 30)
            + encode_int_field(3, 130)
        )
        item_notice = encode_bytes_field(2, item_change)

        client = object.__new__(TreasureFarmClient)
        client.login = lambda: None
        client.battle_timeout = 2.0
        client._item_totals = {key_item_id: 5, HEARTH_ITEM_ID: 100}
        client._auto_event_progress = True
        client._pending_event_action = None
        client._pending_event_start = None
        client._event_progress_notes = []
        client._event_chain_active = False
        client._preferred_event_item_ids = frozenset({key_item_id})
        client._last_confirmed_key_option = None
        client._move_trigger = MoveTriggerState(
            max=1,
            remain=0,
            area=530101,
            triggernum=1,
        )
        sent: list[tuple[int, bytes, bool]] = []
        client._send_message = lambda mid, data=b"", *, encrypted: sent.append(
            (mid, data, encrypted)
        )
        replies = [
            SimpleNamespace(
                message_id=MAP_PROCESSLOC_MESSAGE_ID,
                data=encode_int_field(1, 60),
            ),
            SimpleNamespace(message_id=EVENT_START_MESSAGE_ID, data=event_payload),
            SimpleNamespace(message_id=MAP_PROCESSLOC_MESSAGE_ID, data=result_payload),
            SimpleNamespace(
                message_id=STORAGE_ITEM_CHANGE_MESSAGE_ID, data=item_notice
            ),
            SimpleNamespace(message_id=EVENT_END_MESSAGE_ID, data=b""),
        ]

        def receive(_deadline: float, _context: str) -> SimpleNamespace:
            if replies:
                return replies.pop(0)
            raise TreasureFarmError("测试收包结束")

        client._receive_header = receive
        steps: list[str] = []

        result = client.process_node(
            530101,
            6,
            node_kind=NODE_KIND_SMALL_CHEST,
            expected_reward_item_id=HEARTH_ITEM_ID,
            emit=lambda _level, _message, data: steps.append(
                str(data.get("workflow_step") or "")
            ),
        )

        self.assertEqual(result.loc_updates, {6: LOC_STATUS_PASSED})
        self.assertEqual(result.reward_delta, 30)
        self.assertIn(
            (
                EVENT_OPTION_MESSAGE_ID,
                encode_event_option(1, 6001, 5001),
                True,
            ),
            sent,
        )
        self.assertEqual(
            sent[0],
            (MAP_PROCESSLOC_MESSAGE_ID, encode_processloc_request(6, 530101), True),
        )
        self.assertEqual(sent[1][0], MAP_MOVETRIGGER_ACTIVE_MESSAGE_ID)
        self.assertEqual(
            sent[2],
            (MAP_PROCESSLOC_MESSAGE_ID, encode_processloc_request(6, 530101), True),
        )
        self.assertIn(FARM_STEP_CHEST_KEY_CONFIRM, steps)
        self.assertIn(FARM_STEP_CHEST_OPEN, steps)
        self.assertIn(FARM_STEP_CHEST_REWARD, steps)

    def test_process_node_refuses_chest_without_keys(self) -> None:
        """无钥匙时 process_node 不得发 processloc 开箱。"""
        from treasure_farm import TreasureFarmClient, TreasureFarmError

        client = object.__new__(TreasureFarmClient)
        client.login = lambda: None
        client._item_totals = {1809: 0, HEARTH_ITEM_ID: 100}
        client._auto_event_progress = True
        sent: list[object] = []
        client._send_message = lambda *a, **k: sent.append(a)

        with self.assertRaisesRegex(TreasureFarmError, "钥匙不足"):
            client.process_node(
                530101,
                6,
                node_kind=NODE_KIND_SMALL_CHEST,
                expected_reward_item_id=HEARTH_ITEM_ID,
            )
        self.assertEqual(sent, [])
        # 大宝箱 4 把不够 5
        client._item_totals = {1809: 4}
        with self.assertRaisesRegex(TreasureFarmError, "钥匙不足"):
            client.process_node(
                530101,
                33,
                node_kind=NODE_KIND_BIG_CHEST,
                expected_reward_item_id=HEARTH_ITEM_ID,
            )
        self.assertEqual(sent, [])

    def test_event_status_mode_observes_but_does_not_confirm(self) -> None:
        from dragon_arena_business_map import EVENT_FUNC_ACTION_MESSAGE_ID
        from treasure_farm import TreasureFarmClient

        client = object.__new__(TreasureFarmClient)
        client._auto_event_progress = False
        client._pending_event_action = None
        client._pending_event_start = None
        client._event_progress_notes = []
        sent: list[int] = []
        client._send_message = lambda mid, data=b"", *, encrypted: sent.append(mid)
        payload = encode_int_field(2, 2)

        self.assertTrue(
            client._handle_common_message(
                SimpleNamespace(message_id=EVENT_FUNC_ACTION_MESSAGE_ID, data=payload)
            )
        )
        self.assertEqual(sent, [])
        self.assertIsNotNone(client._pending_event_action)

    def test_event_option_failure_restores_pending_event_state(self) -> None:
        from dragon_arena_business_map import EVENT_OPTION_FAILED_MESSAGE_ID
        from treasure_farm import EventStart, TreasureFarmClient

        client = object.__new__(TreasureFarmClient)
        start = EventStart(auto_option=100, event_id=1001, dialog_id=2001)
        client._last_event_start = start
        client._pending_event_start = None
        client._event_progress_notes = []

        self.assertTrue(
            client._handle_common_message(
                SimpleNamespace(message_id=EVENT_OPTION_FAILED_MESSAGE_ID, data=b"")
            )
        )
        self.assertEqual(client._pending_event_start, start)
        self.assertIn("地图事件自动选项未通过", client.drain_event_progress_notes()[0])

    def test_encode_client_data_get_matches_generated_codec(self) -> None:
        from treasure_farm import encode_client_data_get

        # main.js module 26136 export k6D: {keys: "*"}.
        self.assertEqual(encode_client_data_get(), bytes.fromhex("0a012a"))

    def test_stage_recovery_signal_uses_client_order(self) -> None:
        from treasure_farm import (
            CLIENT_TALOG_MESSAGE_ID,
            EVT_SCRIPT_TRIGGER_MESSAGE_ID,
            TreasureFarmClient,
            encode_client_talog,
            encode_evt_script_trigger,
        )

        client = object.__new__(TreasureFarmClient)
        client._curarea = 530101
        client._pos_x = 16
        client._pos_y = 26
        sent: list[tuple[int, bytes, bool]] = []
        client._send_message = lambda mid, data=b"", *, encrypted: sent.append(
            (mid, data, encrypted)
        )

        client._signal_stage_ready()

        self.assertEqual(
            sent,
            [
                (
                    CLIENT_TALOG_MESSAGE_ID,
                    encode_client_talog(
                        "scene_change",
                        int_params={"scene_id_old": 0, "scene_id_new": 530101},
                    ),
                    True,
                ),
                (
                    CLIENT_TALOG_MESSAGE_ID,
                    encode_client_talog(
                        "enter_map",
                        int_params={"map_id": 530101, "x": 16, "y": 26},
                    ),
                    True,
                ),
                (
                    EVT_SCRIPT_TRIGGER_MESSAGE_ID,
                    encode_evt_script_trigger(2, 0),
                    True,
                ),
            ],
        )

    def test_return_to_map_start_uses_empty_native_payload(self) -> None:
        from treasure_farm import (
            MAP_RETURN_START_MESSAGE_ID,
            TreasureFarmClient,
            TreasureFarmError,
        )

        client = object.__new__(TreasureFarmClient)
        client._curarea = 530101
        sent: list[tuple[int, bytes, bool]] = []
        client._send_message = lambda mid, data=b"", *, encrypted: sent.append(
            (mid, data, encrypted)
        )
        client._receive_header = lambda _deadline, _context: (_ for _ in ()).throw(
            TreasureFarmError("timeout")
        )

        self.assertFalse(client._return_to_map_start(timeout=0.2))
        self.assertEqual(sent, [(MAP_RETURN_START_MESSAGE_ID, b"", True)])

    def test_status_report_always_displays_inactive_battle_marker(self) -> None:
        from treasure_farm import format_status_report

        report = format_status_report(
            {
                "phase": "landmark_locked",
                "dead": False,
                "needsoul": False,
                "area_name": "沉默之城",
                "area_id": 530101,
                "is_treasure_map": True,
                "pos": {"x": 16, "y": 26},
                "loc_total": 27,
                "loc_status_counts": {},
                "active_monsters": 15,
                "active_small_chests": 10,
                "active_big_chests": 0,
                "treasure_big_chest_open_times": 1,
                "items": {},
                "has_team": True,
                "game_data_battle_marker": {
                    "active": False,
                    "battle_state": 0,
                    "battle_type": 7,
                    "battle_type_name": "类型0",
                    "client_data_ready": True,
                },
            }
        )
        self.assertIn("登录战斗标识：未激活", report)
        self.assertIn("battleState=0", report)
        self.assertIn("battleType=7", report)

    def test_encode_client_talog_enter_map(self) -> None:
        from treasure_farm import encode_client_talog

        body = encode_client_talog(
            "enter_map",
            int_params={"map_id": 530101, "x": 16, "y": 26},
        )
        # 与 decrypted-js/main.js 的 X2P codec 对该对象的输出一致。
        self.assertEqual(
            body.hex(),
            "0a09656e7465725f6d617012372a350a130a066d61705f6964120911"
            "000000006a2d20410a0e0a017812091100000000000030400a0e0a0179"
            "1209110000000000003a40",
        )

    def test_encode_client_talog_scene_change_keeps_zero_value(self) -> None:
        from treasure_farm import encode_client_talog

        body = encode_client_talog(
            "scene_change",
            int_params={"scene_id_old": 0, "scene_id_new": 530101},
        )
        self.assertEqual(
            body.hex(),
            "0a0c7363656e655f6368616e676512382a360a190a0c7363656e655f6964"
            "5f6f6c6412091100000000000000000a190a0c7363656e655f69645f6e65"
            "77120911000000006a2d2041",
        )

    def test_encode_enter_treasure_normal_omits_zero_enterway(self) -> None:
        payload = encode_enter_treasure_request(21, 0)
        self.assertEqual(payload, encode_int_field(1, 21))

    def test_encode_enter_treasure_with_reset(self) -> None:
        payload = encode_enter_treasure_request(21, 1)
        self.assertIn(encode_int_field(1, 21), payload)
        self.assertIn(encode_int_field(2, 1), payload)

    def test_encode_processloc(self) -> None:
        payload = encode_processloc_request(6, 230101)
        self.assertEqual(
            payload,
            encode_int_field(1, 6) + encode_int_field(2, 230101),
        )

    def test_encode_map_move_and_path(self) -> None:
        from treasure_farm import build_move_path, encode_map_move, load_area_seat_positions

        payload = encode_map_move([(10, 26), (15, 31)])
        self.assertTrue(len(payload) > 4)
        path = build_move_path((12, 26), (15, 31))
        self.assertEqual(path[-1], (15, 31))
        seats = load_area_seat_positions(730101)
        self.assertIn(1, seats)
        self.assertEqual(seats[6], (10, 26))

    def test_decode_enter_treasure_response(self) -> None:
        self.assertEqual(decode_enter_treasure_response(encode_int_field(1, 5)), 5)
        self.assertEqual(decode_enter_treasure_response(b""), 0)

    def test_decode_enter_area_response(self) -> None:
        # field3 = AreaDetail；其中 field1 为 locs map entry key=3 value=0 (ACTIVE)
        loc_entry = encode_int_field(1, 3) + encode_int_field(2, 0)
        area_detail = b"\x0a" + bytes([len(loc_entry)]) + loc_entry
        body = (
            encode_int_field(1, 0)
            + encode_int_field(2, 230101)
            + b"\x1a"  # field 3 length-delimited
            + bytes([len(area_detail)])
            + area_detail
        )
        ret, area_id, status = decode_enter_area_response(body)
        self.assertEqual(ret, 0)
        self.assertEqual(area_id, 230101)
        self.assertEqual(status.get(3), 0)

    def test_decode_processloc_response(self) -> None:
        # locchanges.locs[11]=2：locchanges 字段 1 直接是 map 条目
        entry = encode_int_field(1, 11) + encode_int_field(2, 2)
        locchanges = b"\x0a" + bytes([len(entry)]) + entry
        body = (
            encode_int_field(1, 0)
            + b"\x12"
            + bytes([len(locchanges)])
            + locchanges
            + encode_int_field(4, 1)
        )
        result = decode_processloc_response(body)
        self.assertEqual(result.ret, 0)
        self.assertEqual(result.flag, 1)
        self.assertEqual(result.loc_updates.get(11), 2)

    def test_decode_game_data_battle_recovery_marker(self) -> None:
        from treasure_farm import decode_game_data_map_snapshot

        # Game_data field35 -> battle: field3 battleState, field4 battleType.
        battle = encode_int_field(3, 2) + encode_int_field(4, 2)
        payload = encode_bytes_field(35, battle)
        snapshot = decode_game_data_map_snapshot(payload)
        self.assertTrue(snapshot.game_data_has_battle_blob)
        self.assertEqual(snapshot.battle_state, 2)
        self.assertEqual(snapshot.battle_type, 2)


class CatalogTestCase(unittest.TestCase):
    def test_catalog_has_nine_maps_with_keys(self) -> None:
        catalog = list_treasure_map_catalog()
        self.assertEqual(len(catalog), 9)
        tip = get_treasure_map_entry(230101)
        self.assertEqual(tip.name, "尖啸山谷")
        self.assertEqual(tip.key_item_id, 1801)
        self.assertIn("钥匙", tip.key_item_name)

    def test_load_nodes_classifies_monster_and_chests(self) -> None:
        nodes = load_area_nodes(230101)
        kinds = {node.kind for node in nodes}
        self.assertIn(NODE_KIND_MONSTER, kinds)
        self.assertIn(NODE_KIND_SMALL_CHEST, kinds)
        self.assertIn(NODE_KIND_BIG_CHEST, kinds)

    def test_load_nodes_includes_boss_not_inn(self) -> None:
        """Boss 座必须可刷；旅店事件仍跳过。

        2026-07-24 石化森林日志：清普通怪/箱后仍有 node 27(boss)/26(旅店)
        为 ACTIVE，旧逻辑漏掉 Boss 导致误报地图已清空。
        """
        from treasure_farm import load_area_nodes as load_nodes

        load_nodes.cache_clear()
        nodes = load_nodes(730101)
        by_id = {node.nodeid: node for node in nodes}
        self.assertIn(27, by_id)
        self.assertEqual(by_id[27].kind, NODE_KIND_MONSTER)
        self.assertEqual(str(by_id[27].notes).lower(), "boss")
        self.assertNotIn(26, by_id)
        # 尖啸山谷 boss 座号不同，同样应纳入
        load_nodes.cache_clear()
        tip_nodes = {node.nodeid: node for node in load_nodes(230101)}
        self.assertIn(48, tip_nodes)
        self.assertEqual(tip_nodes[48].kind, NODE_KIND_MONSTER)

    def test_hub_and_story_maps_are_not_treasure_areas(self) -> None:
        # 潮汐之门 / 流放者之岛·1年后 是进图中转或剧情图，不是聚宝目标
        self.assertFalse(is_treasure_map_area(9004))
        self.assertFalse(is_treasure_map_area(10001))
        self.assertTrue(is_treasure_map_area(730101))
        self.assertTrue(is_treasure_map_area(530101))
        with self.assertRaisesRegex(Exception, "未知聚宝地图"):
            get_treasure_map_entry(9004)


class BigChestDailyLimitTestCase(unittest.TestCase):
    def test_is_big_chest_daily_limit_by_times_and_text(self) -> None:
        from treasure_farm import DAILY_BIG_CHEST_OPEN_LIMIT, is_big_chest_daily_limit

        self.assertTrue(
            is_big_chest_daily_limit(open_times=DAILY_BIG_CHEST_OPEN_LIMIT)
        )
        self.assertFalse(is_big_chest_daily_limit(open_times=0, ret=2))
        self.assertTrue(
            is_big_chest_daily_limit(
                open_times=0,
                ret=99,
                detail="开启大宝箱被拒绝：当日已开启上限",
            )
        )


class RewardWaitTimeoutTestCase(unittest.TestCase):
    """结算后奖励等待：不得再空等 8s（日志里约一半小怪不掉钥匙）。"""

    def test_monster_settled_without_key_uses_short_wait(self) -> None:
        wait = reward_wait_timeout(
            node_kind=NODE_KIND_MONSTER,
            reward_delta=0,
            event_active=False,
            battle_won=True,
            has_loc_updates=True,
        )
        self.assertEqual(wait, REWARD_WAIT_MONSTER_SETTLED_S)
        self.assertLess(wait, 1.0)

    def test_reward_already_received_skips_wait(self) -> None:
        self.assertEqual(
            reward_wait_timeout(
                node_kind=NODE_KIND_MONSTER,
                reward_delta=1,
                event_active=False,
                battle_won=True,
                has_loc_updates=True,
            ),
            0.0,
        )

    def test_chest_settled_uses_short_wait(self) -> None:
        wait = reward_wait_timeout(
            node_kind=NODE_KIND_SMALL_CHEST,
            reward_delta=0,
            event_active=False,
            battle_won=None,
            has_loc_updates=True,
        )
        self.assertEqual(wait, REWARD_WAIT_CHEST_SETTLED_S)
        self.assertLess(wait, 1.0)

    def test_active_event_still_waits_briefly(self) -> None:
        wait = reward_wait_timeout(
            node_kind=NODE_KIND_MONSTER,
            reward_delta=1,
            event_active=True,
            battle_won=True,
            has_loc_updates=True,
        )
        self.assertGreater(wait, 0.0)
        self.assertLessEqual(wait, 3.0)


class ChooseActionTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.nodes = (
            MapNodeSpec(1, NODE_KIND_MONSTER),
            MapNodeSpec(2, NODE_KIND_SMALL_CHEST),
            MapNodeSpec(3, NODE_KIND_BIG_CHEST),
        )
        self.session = AreaSession(
            area_id=230101,
            loc_status={1: LOC_STATUS_ACTIVE, 2: LOC_STATUS_ACTIVE, 3: LOC_STATUS_ACTIVE},
            open_times=0,
        )

    def test_prefers_small_chest_when_one_key(self) -> None:
        action = choose_next_action(self.session, self.nodes, keys=SMALL_CHEST_KEY_COST)
        assert action is not None
        self.assertEqual(action.kind, NODE_KIND_SMALL_CHEST)

    def test_prefers_big_chest_when_five_keys(self) -> None:
        action = choose_next_action(
            self.session, self.nodes, keys=BIG_CHEST_KEY_COST, prefer_big_chest=True
        )
        assert action is not None
        self.assertEqual(action.kind, NODE_KIND_BIG_CHEST)

    def test_skips_big_chest_when_daily_limit_open_times(self) -> None:
        from treasure_farm import DAILY_BIG_CHEST_OPEN_LIMIT

        session = AreaSession(
            area_id=230101,
            loc_status={1: LOC_STATUS_ACTIVE, 2: LOC_STATUS_ACTIVE, 3: LOC_STATUS_ACTIVE},
            open_times=DAILY_BIG_CHEST_OPEN_LIMIT,
        )
        action = choose_next_action(session, self.nodes, keys=10)
        assert action is not None
        self.assertEqual(action.kind, NODE_KIND_SMALL_CHEST)

    def test_skips_big_chest_when_allow_big_chest_false(self) -> None:
        action = choose_next_action(
            self.session,
            self.nodes,
            keys=10,
            allow_big_chest=False,
        )
        assert action is not None
        self.assertEqual(action.kind, NODE_KIND_SMALL_CHEST)

    def test_fights_monster_without_keys(self) -> None:
        action = choose_next_action(self.session, self.nodes, keys=0)
        assert action is not None
        self.assertEqual(action.kind, NODE_KIND_MONSTER)

    def test_never_opens_chest_when_keys_zero(self) -> None:
        """无钥匙时只能打怪，不能返回任何宝箱节点。"""
        for keys in (0, -1):
            action = choose_next_action(self.session, self.nodes, keys=keys)
            assert action is not None
            self.assertEqual(action.kind, NODE_KIND_MONSTER)
        # 4 把钥匙够小箱不够大箱：只能开小箱，不能误开大箱
        action = choose_next_action(self.session, self.nodes, keys=4)
        assert action is not None
        self.assertEqual(action.kind, NODE_KIND_SMALL_CHEST)
        self.assertEqual(chest_key_cost(NODE_KIND_SMALL_CHEST), 1)
        self.assertEqual(chest_key_cost(NODE_KIND_BIG_CHEST), 5)

    def test_open_chest_option_prefers_open_over_leave(self) -> None:
        """石化森林日志：打开宝箱(100)/暂时离开(101)，use 常为 0。"""
        from treasure_farm import EventOptionEntry, EventStart

        start = EventStart(
            auto_option=0,
            event_id=730103,
            dialog_id=1,
            options=(
                EventOptionEntry(title="打开宝箱", optidx=100, use=0, use_num=0),
                EventOptionEntry(title="暂时离开", optidx=101, use=0, use_num=0),
            ),
        )
        chosen = start.choose_open_chest_option(
            preferred_item_ids={1810},
            item_totals={1810: 2},
        )
        assert chosen is not None
        self.assertEqual(chosen.optidx, 100)
        self.assertIsNone(
            start.choose_open_chest_option(
                preferred_item_ids={1810},
                item_totals={1810: 0},
            )
        )

    def test_returns_none_when_all_passed(self) -> None:
        session = AreaSession(
            area_id=230101,
            loc_status={1: LOC_STATUS_PASSED, 2: LOC_STATUS_PASSED, 3: LOC_STATUS_PASSED},
        )
        self.assertIsNone(choose_next_action(session, self.nodes, keys=10))

    def test_ignores_nodes_absent_from_server_locs(self) -> None:
        # 服务端未下发的节点不可交互（避免默认当 ACTIVE 乱点）
        session = AreaSession(area_id=230101, loc_status={2: LOC_STATUS_ACTIVE})
        action = choose_next_action(session, self.nodes, keys=0)
        # 只有宝箱 2 在 locs 中，钥匙不足时应返回 None（不能去打未激活的怪 1）
        self.assertIsNone(action)
        action = choose_next_action(session, self.nodes, keys=1)
        assert action is not None
        self.assertEqual(action.nodeid, 2)

    def test_fights_boss_when_only_boss_and_big_chest_remain(self) -> None:
        # 复现日志终态：钥匙不足开大箱时，应优先打仍存活的 Boss
        nodes = (
            MapNodeSpec(27, NODE_KIND_MONSTER, notes="boss"),
            MapNodeSpec(33, NODE_KIND_BIG_CHEST, notes="大宝箱"),
        )
        session = AreaSession(
            area_id=730101,
            loc_status={27: LOC_STATUS_ACTIVE, 33: LOC_STATUS_ACTIVE},
        )
        action = choose_next_action(session, nodes, keys=1)
        assert action is not None
        self.assertEqual(action.nodeid, 27)
        self.assertEqual(action.kind, NODE_KIND_MONSTER)


class FakeFarmClient:
    """按真实 maplocations 分类响应：怪掉钥匙，箱给炉温。"""

    def __init__(self) -> None:
        self._items = {HEARTH_ITEM_ID: 50, 1801: 0}
        self.closed = False
        self._nodes = {node.nodeid: node for node in load_area_nodes(230101)}
        self._curarea = 230101
        self._initial_locs = {
            nodeid: LOC_STATUS_ACTIVE for nodeid in self._nodes
        }
        self.session = AreaSession(
            area_id=230101,
            loc_status={
                nodeid: LOC_STATUS_ACTIVE for nodeid in self._nodes
            },
            open_times=0,
        )
        self.verify_calls = 0
        self.processed_nodes: list[int] = []

    def login(self) -> None:
        return None

    def clear_pending_map_activity(self, *, timeout: float = 12.0) -> bool:
        return False

    def ensure_actionable(self, **kwargs: object) -> dict[str, object]:
        return {
            "phase": "actionable",
            "phase_label": "可执行(可点怪/开箱)",
            "area_id": 230101,
        }

    def verify_can_interact(self, area_id: int) -> int | None:
        self.verify_calls += 1
        return 0

    def close(self) -> None:
        self.closed = True

    def item_total(self, item_id: int) -> int:
        return int(self._items.get(item_id, 0))

    def open_times(self) -> int:
        return 0

    def enter_treasure(self, area_id: int, *, reset: bool = False) -> AreaSession:
        self._curarea = area_id
        return self.session

    def reset_area(self, area_id: int | None = None) -> AreaSession:
        target = int(area_id or 230101)
        self._curarea = target
        self.session = AreaSession(
            area_id=target,
            loc_status={nodeid: LOC_STATUS_ACTIVE for nodeid in self._nodes},
            open_times=0,
        )
        return self.session

    def exit_area(self) -> None:
        return None

    def process_node(self, area_id: int, nodeid: int) -> Any:
        from treasure_farm import ProcessLocResult

        self.processed_nodes.append(nodeid)
        node = self._nodes.get(nodeid)
        items: list[ItemChange] = []
        if node is not None and node.kind == NODE_KIND_MONSTER:
            self._items[1801] = self._items.get(1801, 0) + 1
            items.append(ItemChange(1801, 1, self._items[1801]))
        elif node is not None and node.kind in (
            NODE_KIND_SMALL_CHEST,
            NODE_KIND_BIG_CHEST,
        ):
            cost = (
                BIG_CHEST_KEY_COST
                if node.kind == NODE_KIND_BIG_CHEST
                else SMALL_CHEST_KEY_COST
            )
            self._items[1801] = max(0, self._items.get(1801, 0) - cost)
            gain = 50 if node.kind == NODE_KIND_BIG_CHEST else 30
            self._items[HEARTH_ITEM_ID] = self._items.get(HEARTH_ITEM_ID, 0) + gain
            items.extend(
                [
                    ItemChange(HEARTH_ITEM_ID, gain, self._items[HEARTH_ITEM_ID]),
                    ItemChange(1801, -cost, self._items[1801]),
                ]
            )
        return ProcessLocResult(
            ret=0,
            loc_updates={nodeid: LOC_STATUS_PASSED},
            flag=1,
            items=tuple(items),
        )


class MissingRewardFarmClient(FakeFarmClient):
    """A node can be marked passed without a reward; the farm must stop."""

    def process_node(self, area_id: int, nodeid: int) -> Any:
        from treasure_farm import ProcessLocResult

        self.processed_nodes.append(nodeid)
        return ProcessLocResult(
            ret=0,
            loc_updates={nodeid: LOC_STATUS_PASSED},
            flag=1,
            items=(),
        )


class HubThenTreasureFarmClient(FakeFarmClient):
    """模拟登录落在潮汐之门：首次 enter 误回中转图，第二次才进目标聚宝图。"""

    def __init__(self) -> None:
        super().__init__()
        self._curarea = 9004
        self._enter_calls = 0
        self.session = AreaSession(area_id=9004, loc_status={}, open_times=0)

    def enter_treasure(self, area_id: int, *, reset: bool = False) -> AreaSession:
        self._enter_calls += 1
        if self._enter_calls == 1:
            # 第一次：仍停在中转图（旧逻辑会 get_treasure_map_entry 报未知）
            self._curarea = 9004
            self.session = AreaSession(area_id=9004, loc_status={1: 0}, open_times=0)
            return self.session
        self._curarea = area_id
        self.session = AreaSession(
            area_id=area_id,
            loc_status={nodeid: LOC_STATUS_ACTIVE for nodeid in self._nodes},
            open_times=0,
        )
        return self.session


class FarmLoopTestCase(unittest.TestCase):
    def test_run_reaches_target_hearth(self) -> None:
        client = FakeFarmClient()
        events: list[tuple[str, str]] = []

        # 打怪再开箱一次即可 +30 炉温
        progress = run_treasure_farm(
            client,  # type: ignore[arg-type]
            230101,
            30,
            emit=lambda level, message, _data: events.append((level, message)),
            stop_requested=lambda: False,
        )
        self.assertTrue(progress.completed)
        self.assertGreaterEqual(progress.hearth_gained, 30)
        self.assertGreaterEqual(progress.monsters_killed, 1)
        self.assertGreaterEqual(progress.small_chests_opened, 1)
        self.assertEqual(client.verify_calls, 0)
        self.assertTrue(any(level == "success" for level, _ in events))

    def test_empty_map_reloads_past_legacy_reset_budget_until_stopped(self) -> None:
        """Map reloads are unbounded; cooperative stop remains available."""

        class EmptyMapClient(FakeFarmClient):
            def __init__(self) -> None:
                super().__init__()
                self.reset_calls = 0
                self._initial_locs = {999: LOC_STATUS_PASSED}
                self.session = AreaSession(
                    area_id=230101,
                    loc_status=dict(self._initial_locs),
                    open_times=0,
                )

            def reset_area(self, area_id: int | None = None) -> AreaSession:
                self.reset_calls += 1
                self._curarea = int(area_id or 230101)
                self.session = AreaSession(
                    area_id=self._curarea,
                    loc_status=dict(self._initial_locs),
                    open_times=0,
                )
                return self.session

        client = EmptyMapClient()
        progress = run_treasure_farm(
            client,  # type: ignore[arg-type]
            230101,
            1,
            stop_requested=lambda: client.reset_calls >= 21,
        )

        self.assertFalse(progress.completed)
        self.assertEqual(client.reset_calls, 21)
        self.assertEqual(progress.last_transition, "已请求停止")

    def test_hearth_target_can_require_more_than_legacy_action_budget(self) -> None:
        """Successful nodes continue until the requested hearth target is reached."""

        class RepeatingChestClient(FakeFarmClient):
            def __init__(self) -> None:
                super().__init__()
                chest = next(
                    node
                    for node in self._nodes.values()
                    if node.kind == NODE_KIND_SMALL_CHEST
                )
                self._nodes = {chest.nodeid: chest}
                self._items[1801] = SMALL_CHEST_KEY_COST
                self._initial_locs = {chest.nodeid: LOC_STATUS_ACTIVE}
                self.session = AreaSession(
                    area_id=230101,
                    loc_status=dict(self._initial_locs),
                    open_times=0,
                )

            def process_node(self, area_id: int, nodeid: int) -> Any:
                from treasure_farm import ProcessLocResult

                self.processed_nodes.append(nodeid)
                self._items[HEARTH_ITEM_ID] += 1
                return ProcessLocResult(
                    ret=0,
                    loc_updates={nodeid: LOC_STATUS_ACTIVE},
                    flag=1,
                    items=(
                        ItemChange(
                            HEARTH_ITEM_ID,
                            1,
                            self._items[HEARTH_ITEM_ID],
                        ),
                    ),
                )

        client = RepeatingChestClient()
        progress = run_treasure_farm(client, 230101, 501)  # type: ignore[arg-type]

        self.assertTrue(progress.completed)
        self.assertEqual(progress.hearth_gained, 501)
        self.assertEqual(len(client.processed_nodes), 501)

    def test_typed_node_receives_stop_callback_and_returns_stopped_progress(self) -> None:
        from treasure_farm import TreasureFarmCancelled

        stop_state = {"requested": False}

        class StoppingFarmClient(FakeFarmClient):
            def process_farm_node(
                self,
                _area_id: int,
                _nodeid: int,
                _node_kind: str,
                *,
                emit=None,
                stop_requested=None,
            ) -> Any:
                self.stop_callback_received = callable(stop_requested)
                stop_state["requested"] = True
                if callable(stop_requested) and stop_requested():
                    raise TreasureFarmCancelled("测试请求停止")
                raise AssertionError("停止回调未传入")

        client = StoppingFarmClient()
        events: list[tuple[str, str, dict[str, object]]] = []
        progress = run_treasure_farm(
            client,  # type: ignore[arg-type]
            230101,
            30,
            emit=lambda level, message, data: events.append((level, message, data)),
            stop_requested=lambda: bool(stop_state["requested"]),
        )

        self.assertTrue(client.stop_callback_received)
        self.assertFalse(progress.completed)
        self.assertEqual(progress.last_transition, "已请求停止")
        self.assertTrue(any(level == "warning" for level, _, _ in events))

    def test_preloop_ret2_recovery_cancellation_returns_partial_progress(self) -> None:
        from treasure_farm import TreasureFarmCancelled

        stop_state = {"requested": False}

        class CancellingRecoveryClient(FakeFarmClient):
            def __init__(self) -> None:
                super().__init__()
                self._last_processloc_ret = 2
                self.stop_callback_received = False

            def recover_from_landmark_lock(
                self, *, stop_requested=None, **_kwargs: object
            ) -> bool:
                self.stop_callback_received = callable(stop_requested)
                stop_state["requested"] = True
                if callable(stop_requested) and stop_requested():
                    raise TreasureFarmCancelled("测试恢复阶段停止")
                raise AssertionError("停止回调未传入")

        client = CancellingRecoveryClient()
        progress = run_treasure_farm(
            client,  # type: ignore[arg-type]
            230101,
            30,
            stop_requested=lambda: bool(stop_state["requested"]),
        )

        self.assertTrue(client.stop_callback_received)
        self.assertFalse(progress.completed)
        self.assertEqual(progress.last_transition, "已请求停止")

    def test_rejected_node_uses_ret2_recovery_branch_before_generic_error(self) -> None:
        from treasure_farm import TreasureFarmRejected

        class Ret2ThenRecoverClient(FakeFarmClient):
            def __init__(self) -> None:
                super().__init__()
                self._rejected_once = False
                self.recovery_calls = 0

            def process_node(self, area_id: int, nodeid: int) -> Any:
                if not self._rejected_once:
                    self._rejected_once = True
                    self.processed_nodes.append(nodeid)
                    raise TreasureFarmRejected("处理节点", 2)
                return super().process_node(area_id, nodeid)

            def recover_from_landmark_lock(self, **_kwargs: object) -> bool:
                self.recovery_calls += 1
                return True

        client = Ret2ThenRecoverClient()
        progress = run_treasure_farm(client, 230101, 30)

        self.assertTrue(progress.completed)
        self.assertEqual(client.recovery_calls, 1)

    def test_preflight_stops_when_landmark_recovery_does_not_clear_ret2(self) -> None:
        """未收到挂起战斗结算时，不得清掉 ret=2 后继续点下一地标。"""

        from treasure_farm import TreasureFarmError

        class LockedLandmarkClient(FakeFarmClient):
            def __init__(self) -> None:
                super().__init__()
                self._last_processloc_ret = 2
                self.recovery_calls = 0

            def recover_from_landmark_lock(self, **_kwargs: object) -> bool:
                self.recovery_calls += 1
                return False

        client = LockedLandmarkClient()
        with self.assertRaisesRegex(TreasureFarmError, "地标交互仍未结算"):
            run_treasure_farm(client, 230101, 30)

        self.assertEqual(client.recovery_calls, 1)
        self.assertEqual(client.processed_nodes, [])
        self.assertEqual(client._last_processloc_ret, 2)

    def test_unknown_hub_area_returns_to_city_and_reenters(self) -> None:
        """落在潮汐之门时不得报「未知聚宝地图」，应回城重进目标图。"""
        client = HubThenTreasureFarmClient()
        events: list[tuple[str, str]] = []
        progress = run_treasure_farm(
            client,  # type: ignore[arg-type]
            230101,
            30,
            emit=lambda level, message, _data: events.append((level, message)),
            stop_requested=lambda: False,
        )
        self.assertTrue(progress.completed)
        self.assertGreaterEqual(client._enter_calls, 2)
        self.assertTrue(
            any("非聚宝地图" in message and "潮汐之门" in message for _, message in events)
        )
        self.assertFalse(any("未知聚宝地图" in message for _, message in events))

    def test_workflow_emits_monster_and_chest_checkpoints(self) -> None:
        client = FakeFarmClient()
        steps: list[str] = []
        progress = run_treasure_farm(
            client, 230101, 30,
            emit=lambda _level, _message, data: steps.extend(
                [str(data["workflow"]["step"])]
            ) if "workflow" in data else None,
        )
        self.assertTrue(progress.completed)
        self.assertEqual(
            steps[:6],
            [
                "monster_interact",
                "battle_prepare",
                "battle_enter",
                "battle_victory",
                "key_take",
                "key_reward",
            ],
        )
        self.assertEqual(
            steps[6:9], ["chest_interact", "chest_open", "chest_reward"]
        )
        self.assertEqual(progress.phase, "complete")
        self.assertEqual(progress.last_reward_item_id, HEARTH_ITEM_ID)

    def test_missing_key_reward_does_not_count_kill(self) -> None:
        client = MissingRewardFarmClient()
        with self.assertRaisesRegex(Exception, "未收到.*奖励"):
            run_treasure_farm(client, 230101, 30)
        first_monster = next(
            node.nodeid for node in client._nodes.values() if node.kind == NODE_KIND_MONSTER
        )
        self.assertEqual(client.processed_nodes, [first_monster])

    def test_missing_hearth_reward_does_not_count_chest(self) -> None:
        client = MissingRewardFarmClient()
        client._items[1801] = 1
        with self.assertRaisesRegex(Exception, "未收到.*奖励"):
            run_treasure_farm(client, 230101, 30)
        first_small = next(
            node.nodeid
            for node in client._nodes.values()
            if node.kind == NODE_KIND_SMALL_CHEST
        )
        self.assertEqual(client.processed_nodes, [first_small])

    def test_existing_key_opens_chest_before_any_monster(self) -> None:
        client = FakeFarmClient()
        client._items[1801] = 1

        progress = run_treasure_farm(
            client,  # type: ignore[arg-type]
            230101,
            30,
        )

        self.assertTrue(progress.completed)
        self.assertEqual(client.verify_calls, 0)
        self.assertEqual(len(client.processed_nodes), 1)
        first = client._nodes[client.processed_nodes[0]]
        self.assertEqual(first.kind, NODE_KIND_SMALL_CHEST)
        self.assertEqual(progress.monsters_killed, 0)
        self.assertEqual(progress.small_chests_opened, 1)

    def test_format_and_payload_use_names(self) -> None:
        progress = FarmProgress(
            area_id=230101,
            area_name="尖啸山谷",
            target_hearth=100,
            hearth_gained=30,
            hearth_total=80,
            keys_total=2,
            key_item_id=1801,
            key_item_name="神秘钥匙·尖啸",
            monsters_killed=3,
            small_chests_opened=2,
            big_chests_opened=0,
            open_times=0,
            settled_monsters=4,
            no_key_monsters=1,
            missing_hearth_chests=2,
            last_transition="节点耗尽后重置",
            last_reset_reason="节点耗尽后重置",
        )
        summary = format_farm_summary(progress)
        self.assertIn("尖啸山谷", summary)
        self.assertIn("炉温", summary)
        payload = progress_payload(progress)
        self.assertEqual(payload["area_name"], "尖啸山谷")
        self.assertEqual(payload["hearth_item_name"], "炉温")
        self.assertEqual(payload["settled_monsters"], 4)
        self.assertEqual(payload["no_key_monsters"], 1)
        self.assertEqual(payload["missing_hearth_chests"], 2)
        self.assertNotIn("actions", payload)
        self.assertNotIn("resets", payload)
        self.assertEqual(payload["last_transition"], "节点耗尽后重置")


class TreasureFarmServiceTestCase(unittest.TestCase):
    def test_run_farm_persists_standard_log(self) -> None:
        endpoint = GameEndpoint("ws://test.invalid", "token", "4101", "真实一区")
        events: list[tuple[str, str, dict]] = []

        with TemporaryDirectory() as directory:
            log_path = Path(directory) / "farm.jsonl"
            service = TreasureService(
                farm_client_builder=lambda _ep: FakeFarmClient(),  # type: ignore[return-value,arg-type]
                result_log_destination=log_path,
            )
            result = service.run_farm(
                endpoint,
                230101,
                30,
                emit=lambda level, message, data: events.append((level, message, data)),
                stop_requested=lambda: False,
            )
            self.assertFalse(result["cancelled"])
            self.assertTrue(result["farm"]["completed"])
            self.assertIn("尖啸山谷", result["summary"])
            text = log_path.read_text(encoding="utf-8")
            self.assertIn('"operation":"farm"', text.replace(" ", ""))
            self.assertIn("尖啸山谷", text)
            self.assertIn("炉温", text)


if __name__ == "__main__":
    unittest.main()
