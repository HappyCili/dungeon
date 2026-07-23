"""罪者深渊协议编解码与进度解析自检。"""

from __future__ import annotations

import unittest

from grave_abyss import (
    ABYSS_GROUP_ID,
    BATTLE_END_RESULT_LOSE,
    BATTLE_END_RESULT_WIN,
    GRAVE_TYPE_ACTIVITY,
    GraveActivityState,
    GraveFloor,
    build_abyss_status,
    decode_battle_end_result,
    decode_grave_activity,
    decode_grave_activity_sync_response,
    decode_grave_buff_select_response,
    decode_grave_challenge_start_response,
    encode_bool_field,
    encode_bytes_field_activity,
    encode_bytes_field_map,
    encode_grave_buff_select,
    encode_grave_challenge_start,
    load_abyss_floors,
    resolve_next_challenge_id,
    run_self_tests,
)
from harvest_fief import encode_int_field


class GraveAbyssCodecTests(unittest.TestCase):
    def test_self_tests(self) -> None:
        run_self_tests()

    def test_challenge_encode_decode(self) -> None:
        payload = encode_grave_challenge_start(30005, grave_type=GRAVE_TYPE_ACTIVITY)
        self.assertEqual(
            payload, encode_int_field(1, 30005) + encode_int_field(2, 1)
        )
        response = decode_grave_challenge_start_response(
            encode_int_field(1, 30005)
            + encode_int_field(2, 0)
            + encode_int_field(3, 1)
        )
        self.assertEqual(response.id, 30005)
        self.assertEqual(response.ret, 0)
        self.assertEqual(response.type, 1)

    def test_activity_and_next_floor(self) -> None:
        act_payload = b"".join(
            (
                encode_int_field(1, 30010),
                encode_bytes_field_map(2, ABYSS_GROUP_ID, 30009),
                encode_int_field(6, 1024),
                encode_int_field(11, 201),
            )
        )
        activity = decode_grave_activity(act_payload)
        self.assertEqual(activity.passes[ABYSS_GROUP_ID], 30009)
        self.assertEqual(activity.optbuf, 201)

        synced = decode_grave_activity_sync_response(
            encode_bytes_field_activity(activity=act_payload)
        )
        self.assertIsNotNone(synced)
        assert synced is not None
        self.assertEqual(synced.season, 1024)

        floors = (
            GraveFloor(30001, 3, 1, "罪者深渊-1", 68, 1, 1),
            GraveFloor(30002, 3, 2, "罪者深渊-2", 68, 1, 1),
            GraveFloor(30003, 3, 3, "罪者深渊-3", 68, 1, 1),
        )
        self.assertEqual(
            resolve_next_challenge_id(
                GraveActivityState(passes={3: 30001}), floors
            ),
            30002,
        )
        self.assertEqual(
            resolve_next_challenge_id(
                GraveActivityState(passes={3: 30003}), floors
            ),
            0,
        )
        status = build_abyss_status(
            GraveActivityState(passes={3: 30001}, season=1024, optbuf=201),
            floors,
        )
        self.assertEqual(status.pass_level, 1)
        self.assertEqual(status.next_level, 2)

    def test_battle_end_and_buff(self) -> None:
        win, code, rnd = decode_battle_end_result(
            encode_int_field(1, 12)
            + encode_bool_field(2, True)
            + encode_int_field(10, BATTLE_END_RESULT_WIN)
        )
        self.assertTrue(win)
        self.assertEqual(code, BATTLE_END_RESULT_WIN)
        self.assertEqual(rnd, 12)

        lose, code2, _ = decode_battle_end_result(
            encode_int_field(1, 3)
            + encode_bool_field(2, False)
            + encode_int_field(10, BATTLE_END_RESULT_LOSE)
        )
        self.assertFalse(lose)
        self.assertEqual(code2, BATTLE_END_RESULT_LOSE)

        self.assertEqual(encode_grave_buff_select(201), encode_int_field(1, 201))
        ret, optbuf = decode_grave_buff_select_response(
            encode_int_field(1, 0) + encode_int_field(3, 201)
        )
        self.assertEqual(ret, 0)
        self.assertEqual(optbuf, 201)

    def test_real_floor_table(self) -> None:
        floors = load_abyss_floors()
        self.assertEqual(len(floors), 900)
        self.assertEqual(floors[0].id, 30001)
        self.assertEqual(floors[-1].level, 900)


if __name__ == "__main__":
    unittest.main()
