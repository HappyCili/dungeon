from __future__ import annotations

import unittest

from id_descriptions import (
    activity_reward_name,
    business_name,
    daily_task_name,
    dungeon_name,
    hero_name,
    item_name,
    quest_name,
    reward_box_name,
    reward_name,
    rune_name,
    unknown_name,
)


class IdDescriptionsTestCase(unittest.TestCase):
    def test_known_catalogs_resolve_names(self) -> None:
        self.assertEqual(item_name(1), "金币")
        self.assertEqual(dungeon_name(1), "林间地牢")
        self.assertEqual(rune_name(110101), "固守")
        self.assertEqual(hero_name(10101), "菲欧娜🌟")
        self.assertEqual(business_name(21106), "竞技场挑战结算")

    def test_reward_and_task_catalogs_resolve_names(self) -> None:
        self.assertNotIn("ID", reward_box_name(1))
        self.assertNotIn("ID", reward_name(1, 1))
        self.assertNotIn("ID", daily_task_name(101))
        self.assertNotIn("ID", quest_name(50001))
        self.assertNotIn("ID", activity_reward_name(101))

    def test_unknown_values_are_explicitly_labeled(self) -> None:
        self.assertEqual(item_name(999999999), "未知物品（ID 999999999）")
        self.assertEqual(unknown_name("地图", 9999), "未知地图（ID 9999）")


if __name__ == "__main__":
    unittest.main()
