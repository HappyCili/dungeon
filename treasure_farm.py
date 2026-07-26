#!/usr/bin/env python3
"""聚宝之地自动刷取宝箱：进图、击杀小怪取钥匙、开箱拿炉温。

流程：
1. ``Map_enter_treasure`` 进入指定地图组；
2. 选择一个明确的 ACTIVE/OPEN 地标，只发送一次 ``Map_processloc``；
3. 宝箱按「宝箱交互 → 确定用钥匙打开宝箱（``Event_option``）→
   打开宝箱 → 获得炉温奖励」推进；
4. 怪物按「怪物交互 → 战斗准备（非自动战斗）→ 进入战斗 →
   战斗胜利 → 带走钥匙 → 获得钥匙奖励」推进；
5. 只有对应奖励到账后才提交节点计数并选择下一个地标。

普通宝箱消耗 1 枚地图钥匙，大宝箱消耗 5 枚；大宝箱每日开启上限 3 次
（与客户端 ``treasureopentimes2`` 一致）。

开箱确认：``Map_processloc`` 后服务端常下发 ``Event_start``，选项带
``use``/``useNum``（地图钥匙与数量）。官方客户端在 UI 点确认后发送
``Event_option``；脚本在收到该选项时自动补发，否则会卡在交互后无结算。
"""

from __future__ import annotations

import argparse
import inspect
import json
import socket
import struct
import sys
import time
from dataclasses import dataclass, field, replace
from functools import lru_cache
from pathlib import Path
from typing import Callable, Mapping

from dragon_arena import (
    BATTLE_TIMESCALE_X3,
    BattleInfo,
    GameDataBattleContext,
    decode_battle_info,
    decode_battle_start_response,
    decode_game_data_battle_context,
    decode_game_data_item_totals,
    encode_battle_auto,
    encode_battle_c2s_start,
    encode_battle_timescale,
)
from dragon_arena_business_map import (
    BATTLE_C2S_AUTO_ARTIFACT_SKILL_MESSAGE_ID,
    BATTLE_C2S_AUTO_UNIQUE_SKILL_MESSAGE_ID,
    BATTLE_C2S_SET_TIMESCALE_MESSAGE_ID,
    BATTLE_C2S_START_MESSAGE_ID,
    BATTLE_INFO_MESSAGE_ID,
    BATTLE_OFFLINE_MESSAGE_ID,
    BATTLE_S2C_END_MESSAGE_ID,
    BATTLE_S2C_FRAME_BROADCAST_MESSAGE_ID,
    BATTLE_S2C_FRAME_HASH_VERIFY_MESSAGE_ID,
    BATTLE_S2C_START_MESSAGE_ID,
    BATTLE_UNIT_INFO_MESSAGE_ID,
    EVENT_END_MESSAGE_ID,
    EVENT_FUNC_ACTION_MESSAGE_ID,
    EVENT_FUNC_NEXT_MESSAGE_ID,
    EVENT_OPTION_MESSAGE_ID,
    EVENT_OPTION_FAILED_MESSAGE_ID,
    EVENT_START_MESSAGE_ID,
    GAME_DATA_MESSAGE_ID,
    MESSAGE_NAMES,
)
from harvest_fief import (
    HEARTBEAT_MESSAGE_ID,
    HEARTBEAT_RET_MESSAGE_ID,
    LOGIN_FAIL_MESSAGE_ID,
    LOGIN_MESSAGE_ID,
    LOGIN_REUNIQUE_MESSAGE_ID,
    PACK_PASSWORD_MESSAGE_ID,
    SOCKET_PACK_KEY,
    STORAGE_ITEM_CHANGE_MESSAGE_ID,
    GameEndpoint,
    HarvestError,
    ItemChange,
    MessageHeader,
    NativeWebSocket,
    ProtoReader,
    decode_int32,
    decode_item_change_notify,
    decode_message_header,
    decode_pack_password,
    encode_bytes_field,
    encode_int_field,
    encode_login_payload,
    encode_message_header,
    load_tokens,
    pack1_decode,
    pack1_encode,
    resolve_game_endpoint,
)
from ws_traffic_log import bind_traffic_logging
from id_descriptions import item_name, treasure_area_name
from project_paths import NATIVE_APP_ROOT, UI_APP_ROOT


PROJECT_ROOT = UI_APP_ROOT


MAP_ENTER_AREA_MESSAGE_ID = 15502
MAP_EXIT_AREA_MESSAGE_ID = 15504
MAP_PROCESSLOC_MESSAGE_ID = 15516
MAP_MOVE_MESSAGE_ID = 15554
EVT_SCRIPT_TRIGGER_MESSAGE_ID = 15550
MAP_RESET_AREA_MESSAGE_ID = 15555
MAP_RETURN_START_MESSAGE_ID = 15560
MAP_ENTER_TREASURE_MESSAGE_ID = 15562
MAP_TREASURE_INFO_MESSAGE_ID = 15570
MAP_MOVETRIGGER_ACTIVE_MESSAGE_ID = 15574
KICKOUT_MESSAGE_ID = 10030
CLIENT_TALOG_MESSAGE_ID = 10531
CLIENT_DATA_GET_MESSAGE_ID = 10520

# ``Battle_info``/战斗帧提供可开战的完整数据；Game_data.field35 则提供
# 登录恢复标识（battleState/battleType），两者在状态报告中分别保留。
BATTLE_ACTIVE_MESSAGE_IDS = frozenset(
    {
        BATTLE_INFO_MESSAGE_ID,
        BATTLE_UNIT_INFO_MESSAGE_ID,
        BATTLE_OFFLINE_MESSAGE_ID,
        BATTLE_S2C_START_MESSAGE_ID,
        BATTLE_S2C_FRAME_BROADCAST_MESSAGE_ID,
        BATTLE_S2C_FRAME_HASH_VERIFY_MESSAGE_ID,
    }
)

# EventModule 的等待语义（来自客户端 main.js）。战后钥匙提示会以
# Event_func_action 下发；客户端关闭提示后发送 Event_func_next 才会推进服务端
# 事件状态机并释放地图地标。
EVENT_WAIT_SHOW_GET_ITEM = 1
EVENT_WAIT_SHOW_DIALOG = 2
EVENT_WAIT_ACTION_ITEM_INTERACTION = 53
EVENT_WAIT_LABELS = {
    EVENT_WAIT_SHOW_GET_ITEM: "获得物品提示",
    EVENT_WAIT_SHOW_DIALOG: "对话提示",
}
EVENT_WAIT_ACTION_LABELS = {
    EVENT_WAIT_ACTION_ITEM_INTERACTION: "物品交互提示",
}

# 会话阶段（切图/刷取前必须收敛到 ACTIONABLE）
PHASE_DEAD = "dead"
PHASE_CITY = "city"
PHASE_MAP_IDLE = "map_idle"
PHASE_BATTLE_RECOVERY = "battle_recovery"  # Game_data.field35.battleState != 0
PHASE_BATTLE_PREPARE = "battle_prepare"  # 已有 Battle_info，客户端=布阵/准备界面
PHASE_BATTLE_RUNNING = "battle_running"  # 已开战 / 战斗帧
PHASE_LANDMARK_LOCKED = "landmark_locked"  # processloc ret=2
PHASE_INTERACT_BLOCKED = "interact_blocked"  # processloc ret=10 等
PHASE_ACTIONABLE = "actionable"  # 可安全点怪/开箱

PHASE_LABELS = {
    PHASE_DEAD: "已阵亡",
    PHASE_CITY: "主城/未在区域",
    PHASE_MAP_IDLE: "地图内·空闲(待确认可交互)",
    PHASE_BATTLE_RECOVERY: "登录期战斗恢复(等待Battle_info)",
    PHASE_BATTLE_PREPARE: "战斗准备界面(已收到Battle_info，未开战)",
    PHASE_BATTLE_RUNNING: "战斗中",
    PHASE_LANDMARK_LOCKED: "地图内·地标占用/未结算",
    PHASE_INTERACT_BLOCKED: "地图内·交互被拒",
    PHASE_ACTIONABLE: "可执行(可点怪/开箱)",
}

# 物品 25：炉温（锻造资源；玩家口语常称炉火）
HEARTH_ITEM_ID = 25
# 物品 1800：门票——进入聚宝之地消耗（见 item 文案与 Map_enter_treasure ret=5）
TREASURE_TICKET_ITEM_ID = 1800
SMALL_CHEST_KEY_COST = 1
BIG_CHEST_KEY_COST = 5
# Keep spending accumulated map keys before collecting more from monsters.
CHEST_ONLY_KEY_THRESHOLD = 20


def chest_key_cost(node_kind: str) -> int:
    """开启指定宝箱所需的地图钥匙数量；非宝箱返回 0。"""

    if node_kind == NODE_KIND_SMALL_CHEST:
        return SMALL_CHEST_KEY_COST
    if node_kind == NODE_KIND_BIG_CHEST:
        return BIG_CHEST_KEY_COST
    return 0
DAILY_BIG_CHEST_OPEN_LIMIT = 3

ENTERWAY_NORMAL = 0
ENTERWAY_RESET = 1

# Map_enter_treasure 拒绝码（客户端 enum + label.map.entertreasureareafailed*）
ENTER_TREASURE_RET_LABELS = {
    1: "没有该地图组",
    2: "地图组无可进入区域",
    3: "随机结果异常（常见于图内地标/战斗未清就重置）",
    4: "道具扣除数量异常（进图/重置时服务端扣费校验失败；聚宝 costid 多为空，更常见于状态异常而非真缺道具）",
    5: "道具不足或条件不满足（客户端进图无门票校验；请确认地图已解锁）",
    6: "进入目标区域失败（节点占用/未退出旧图/战斗未结算）",
    7: "已在当前地图组区域",
    8: "当前不在藏宝地，无法重置",
}

# Map_enter_area 客户端有文案的部分 ret
ENTER_AREA_RET_LABELS = {
    1: "不满足进入要求",
    3: "进入区域随机/状态异常（常见于重置时节点占用未清）",
    5: "进入区域失败（可能仍卡在其它图内、地图未解锁，或服务端拒绝切换）",
    10: "处理节点失败 ret=10（会话未就绪/条件不满足，可尝试重新进图后再交互）",
    6: "审判官等级不足，无法进入",
    9: "未满足解锁票据要求",
    103: "已达到今日挑战次数上限",
}

# Map_reset_area 与 Map_enter_treasure 拒绝码不同，禁止混用文案
MAP_RESET_AREA_RET_LABELS = {
    1: "重置失败",
    3: "重置条件不满足",
    4: "重置被拒（魂域式 Map_reset_area 不适用于聚宝；请用进图重置）",
    5: "重置道具/条件不足",
}

LOC_STATUS_ACTIVE = 0
LOC_STATUS_OPEN = 1
LOC_STATUS_PASSED = 2

NODE_KIND_MONSTER = "monster"
NODE_KIND_SMALL_CHEST = "small_chest"
NODE_KIND_BIG_CHEST = "big_chest"

# A farm action is deliberately split into the same checkpoints visible in the
# game UI.  ``Map_processloc`` is still the transport operation, but it is not
# treated as the whole business action until the matching reward is observed.
FARM_STEP_IDLE = "idle"
FARM_STEP_CHEST_INTERACT = "chest_interact"
FARM_STEP_CHEST_KEY_CONFIRM = "chest_key_confirm"
FARM_STEP_CHEST_OPEN = "chest_open"
FARM_STEP_CHEST_REWARD = "chest_reward"
FARM_STEP_MONSTER_INTERACT = "monster_interact"
FARM_STEP_BATTLE_PREPARE = "battle_prepare"
FARM_STEP_BATTLE_ENTER = "battle_enter"
FARM_STEP_BATTLE_VICTORY = "battle_victory"
FARM_STEP_KEY_TAKE = "key_take"
FARM_STEP_KEY_REWARD = "key_reward"
FARM_STEP_COMPLETE = "complete"

FARM_STEP_LABELS = {
    FARM_STEP_IDLE: "等待选择节点",
    FARM_STEP_CHEST_INTERACT: "宝箱交互",
    FARM_STEP_CHEST_KEY_CONFIRM: "确定用钥匙打开宝箱",
    FARM_STEP_CHEST_OPEN: "打开宝箱",
    FARM_STEP_CHEST_REWARD: "获得炉温奖励",
    FARM_STEP_MONSTER_INTERACT: "怪物交互",
    FARM_STEP_BATTLE_PREPARE: "战斗准备",
    FARM_STEP_BATTLE_ENTER: "进入战斗",
    FARM_STEP_BATTLE_VICTORY: "战斗胜利",
    FARM_STEP_KEY_TAKE: "带走钥匙",
    FARM_STEP_KEY_REWARD: "获得钥匙奖励",
    FARM_STEP_COMPLETE: "节点流程完成",
}

NODE_KIND_LABELS = {
    NODE_KIND_MONSTER: "怪物地标",
    NODE_KIND_SMALL_CHEST: "普通宝箱地标",
    NODE_KIND_BIG_CHEST: "大宝箱地标",
}


def map_node_name(nodeid: int, kind: str = "") -> str:
    """Resolve a seat ID to a readable label without inventing map data."""

    if nodeid <= 0:
        return ""
    known = NODE_KIND_LABELS.get(kind)
    if known:
        return known
    return f"未知地标（ID {nodeid}）"

DEFAULT_BATTLE_TIMEOUT = 180.0
DEFAULT_ACTION_TIMEOUT = 30.0
MAX_MOVETRIGGER_RETRIES_PER_ACTION = 1
# 节点已结算后的奖励收尾：旧逻辑固定等满 8s，日志显示约一半小怪不掉钥匙
# 也会空等到超时（Event_end→下一 processloc 中位数从 0.5s 被拉到 8.2s）。
REWARD_WAIT_MONSTER_SETTLED_S = 0.3
REWARD_WAIT_CHEST_SETTLED_S = 0.6
REWARD_WAIT_EVENT_ACTIVE_S = 2.0
REWARD_WAIT_FALLBACK_S = 1.5
LOC_UPDATE_DRAIN_S = 0.25


def reward_wait_timeout(
    *,
    node_kind: str | None,
    reward_delta: int,
    event_active: bool,
    battle_won: bool | None,
    has_loc_updates: bool,
) -> float:
    """节点结算后还要等多久等 Storage / 事件收尾。

    返回 0 表示无需再等。有掉落且事件已结束时应立刻继续下一节点。
    """

    if reward_delta > 0 and not event_active:
        return 0.0
    if event_active:
        return REWARD_WAIT_EVENT_ACTIVE_S
    if (
        node_kind == NODE_KIND_MONSTER
        and battle_won is True
        and has_loc_updates
    ):
        # 已 PASSED 且事件结束：多数怪不掉钥匙，只短等迟到的 Storage
        return REWARD_WAIT_MONSTER_SETTLED_S
    if node_kind in (NODE_KIND_SMALL_CHEST, NODE_KIND_BIG_CHEST) and has_loc_updates:
        return REWARD_WAIT_CHEST_SETTLED_S
    return REWARD_WAIT_FALLBACK_S


class TreasureFarmError(HarvestError):
    """聚宝之地刷取会话或业务状态错误。"""


class TreasureFarmCancelled(TreasureFarmError):
    """调用方请求在协议步骤之间协作停止。"""


class TreasureFarmRejected(TreasureFarmError):
    """服务端拒绝了进入或处理节点请求。"""

    def __init__(self, action: str, ret: int, *, detail: str = "") -> None:
        self.action = action
        self.ret = ret
        self.detail = detail
        label = detail or _reject_label(action, ret)
        super().__init__(f"聚宝之地{action}被拒绝：{label}（ret {ret}）")


def _reject_label(action: str, ret: int) -> str:
    if action in ("进入", "重置进入", "重置"):
        # 当前重置流程先退出区域再按 Normal 重进，拒绝码仍与进图一致。
        return ENTER_TREASURE_RET_LABELS.get(ret, "未登记的拒绝原因")
    if action in ("进入区域", "重置进入区域"):
        return ENTER_AREA_RET_LABELS.get(ret, "未登记的拒绝原因")
    if action == "处理节点":
        if ret == 2:
            return "已有地标激活（服务端仍有未结束的战斗/节点，需先结算或重置）"
        if ret == 10:
            return (
                "无法交互（ret=10；常见于会话未真正进 Stage、移动被拒或地标条件不满足）"
            )
        if ret in PROCESSLOC_BIG_CHEST_DAILY_LIMIT_RETS:
            return (
                f"今日大宝箱已达开启上限（{DAILY_BIG_CHEST_OPEN_LIMIT} 次，"
                "与客户端 treasureopentimes2 一致）"
            )
        return PROCESSLOC_RET_LABELS.get(ret, "未登记的拒绝原因")
    return "未登记的拒绝原因"


# Map_processloc 常见 ret（客户端 OnMapProcessloc + 实测）
PROCESSLOC_RET_LABELS = {
    2: "已有地标激活",
    7: "死亡相关",
    50: "装备仓库已满",
    60: "需 Map_movetrigger_active",
}

# 大宝箱日限：以 Map_treasure_info.times 为准；部分区服 processloc 也会直接拒
# （ret 因版本可能不同，识别时同时看 times 文案）
PROCESSLOC_BIG_CHEST_DAILY_LIMIT_RETS = frozenset()


def is_big_chest_daily_limit(
    *,
    ret: int | None = None,
    open_times: int = 0,
    detail: str = "",
) -> bool:
    """是否已达/命中「今日大宝箱开启上限」。"""

    if open_times >= DAILY_BIG_CHEST_OPEN_LIMIT:
        return True
    if ret is not None and ret in PROCESSLOC_BIG_CHEST_DAILY_LIMIT_RETS:
        return True
    text = detail or ""
    markers = (
        "当日已开启上限",
        "今日开启大宝箱",
        "大宝箱次数",
        "开启次数上限",
        "已达开启上限",
        "已达到今日",
        "次数已满",
        "treasureopentimes",
    )
    return any(m in text for m in markers)


class TreasureFarmKickout(TreasureFarmError):
    """游戏服 Kickout：会话被中止（常见于顶号）。"""

    def __init__(self, ret: int, message: str = "") -> None:
        self.ret = ret
        self.server_message = message
        super().__init__(format_kickout_error(ret, message))


# Kickout.ret 与客户端 notice / 实测对照（见 dragon_arena 样本「会话已切换」）。
KICKOUT_RET_LABELS = {
    2: "账号已在其他客户端登录，当前自动化会话被顶下线",
}


def format_kickout_error(ret: int, message: str = "") -> str:
    """把 Kickout 转成可操作的中文说明，避免只显示裸 ret。"""

    label = KICKOUT_RET_LABELS.get(ret, "游戏服终止了当前会话")
    parts = [f"{label}（Kickout ret={ret}）"]
    if message:
        parts.append(message)
    if ret == 2:
        parts.append(
            "请先完全退出手机/模拟器上的游戏客户端，"
            "并确认没有其他自动化任务在跑，再重新开始刷取"
        )
    return "。".join(parts)


@dataclass(frozen=True)
class TreasureMapCatalogEntry:
    area_id: int
    name: str
    mapgroup: int
    key_item_id: int
    key_item_name: str


@dataclass(frozen=True)
class MapNodeSpec:
    nodeid: int
    kind: str
    monsterid: int = 0
    notes: str = ""


@dataclass(frozen=True)
class AreaSession:
    area_id: int
    loc_status: Mapping[int, int]
    open_times: int = 0

    def status_of(self, nodeid: int) -> int | None:
        """返回服务端状态；未出现在 locs 中的节点视为不可交互。"""

        if nodeid not in self.loc_status:
            return None
        return int(self.loc_status[nodeid])

    def is_active(self, nodeid: int) -> bool:
        status = self.status_of(nodeid)
        return status in (LOC_STATUS_ACTIVE, LOC_STATUS_OPEN)

    def active_node_ids(self) -> tuple[int, ...]:
        return tuple(
            nodeid
            for nodeid, status in self.loc_status.items()
            if int(status) in (LOC_STATUS_ACTIVE, LOC_STATUS_OPEN)
        )

    def with_loc_updates(self, updates: Mapping[int, int]) -> "AreaSession":
        merged = dict(self.loc_status)
        for nodeid, status in updates.items():
            merged[int(nodeid)] = int(status)
        return replace(self, loc_status=merged)


@dataclass(frozen=True)
class FarmProgress:
    area_id: int
    area_name: str
    target_hearth: int
    hearth_gained: int
    hearth_total: int
    keys_total: int
    key_item_id: int
    key_item_name: str
    monsters_killed: int
    small_chests_opened: int
    big_chests_opened: int
    open_times: int
    phase: str = FARM_STEP_IDLE
    current_node_id: int = 0
    current_node_kind: str = ""
    last_reward_item_id: int = 0
    last_reward_delta: int = 0
    settled_monsters: int = 0
    no_key_monsters: int = 0
    missing_hearth_chests: int = 0
    last_transition: str = ""
    last_reset_reason: str = ""

    @property
    def completed(self) -> bool:
        return self.hearth_gained >= self.target_hearth

    @property
    def phase_label(self) -> str:
        return FARM_STEP_LABELS.get(self.phase, self.phase)


@dataclass(frozen=True)
class ProcessLocResult:
    ret: int
    loc_updates: Mapping[int, int]
    flag: int
    items: tuple[ItemChange, ...]
    reward_item_id: int = 0
    reward_delta: int = 0
    battle_won: bool | None = None


@dataclass(frozen=True)
class BattleEndResult:
    """The minimum ``Battle_S2C_end`` result needed by treasure farming."""

    win: bool
    result_code: int | None = None
    round_number: int | None = None


# Client enum ``BATTLE_END_RESULT_*`` (decrypted-js/main.js).
BATTLE_END_RESULT_NONE = 0
BATTLE_END_RESULT_LOSE = 1
BATTLE_END_RESULT_WIN = 2
BATTLE_END_RESULT_RETREAT = 3
BATTLE_END_RESULT_ABORT = 4
BATTLE_END_RESULT_TIMEOUT = 5
BATTLE_END_RESULT_LABELS = {
    BATTLE_END_RESULT_NONE: "无结果",
    BATTLE_END_RESULT_LOSE: "战败",
    BATTLE_END_RESULT_WIN: "胜利",
    BATTLE_END_RESULT_RETREAT: "撤退",
    BATTLE_END_RESULT_ABORT: "作废(ABORT)",
    BATTLE_END_RESULT_TIMEOUT: "超时",
}


def battle_end_result_label(result_code: int | None) -> str:
    if result_code is None:
        return "未知"
    return BATTLE_END_RESULT_LABELS.get(result_code, f"未登记({result_code})")


def decode_treasure_battle_end(data: bytes) -> BattleEndResult:
    """Decode the victory signal used by a monster node.

    The server normally supplies both ``win`` (field 2) and ``result``
    (field 10).  Result codes follow ``BATTLE_END_RESULT_*``:
    1=LOSE, 2=WIN, 3=RETREAT, 4=ABORT, 5=TIMEOUT.
    """

    win_flag: int | None = None
    result_code: int | None = None
    round_number: int | None = None
    for field_number, wire_type, value in ProtoReader(data).fields():
        if wire_type != 0:
            continue
        if field_number == 1:
            round_number = decode_int32(int(value))
        elif field_number == 2:
            win_flag = int(value)
        elif field_number == 10:
            result_code = decode_int32(int(value))
    if result_code == BATTLE_END_RESULT_WIN:
        win = True
    elif result_code in (
        BATTLE_END_RESULT_LOSE,
        BATTLE_END_RESULT_RETREAT,
        BATTLE_END_RESULT_ABORT,
        BATTLE_END_RESULT_TIMEOUT,
    ):
        win = False
    elif win_flag is not None:
        win = bool(win_flag)
    else:
        win = False
    return BattleEndResult(
        win=win,
        result_code=result_code,
        round_number=round_number,
    )


def format_battle_not_won_error(outcome: BattleEndResult) -> str:
    """Human-readable failure for a non-winning ``Battle_S2C_end``."""

    label = battle_end_result_label(outcome.result_code)
    if outcome.result_code == BATTLE_END_RESULT_ABORT:
        return (
            "怪物战斗被服务端作废（ABORT/结果 4），未确认钥匙奖励。"
            "常见原因：开战包 eteam 回传了未展开的坐标 -1/-1"
            "（官方会先按坑位 seat 填真实格子）。"
            "请更新后重试；若仍失败请退出手机端游戏避免顶号。"
        )
    if outcome.result_code == BATTLE_END_RESULT_RETREAT:
        return "怪物战斗撤退结束，未确认钥匙奖励（结果 3）"
    if outcome.result_code == BATTLE_END_RESULT_TIMEOUT:
        return "怪物战斗超时结束，未确认钥匙奖励（结果 5）"
    if outcome.result_code == BATTLE_END_RESULT_LOSE:
        return "怪物战斗战败，未确认钥匙奖励（结果 1）"
    return (
        f"怪物战斗结束但未胜利，未确认钥匙奖励"
        f"（{label}"
        + (
            f"，结果 {outcome.result_code}"
            if outcome.result_code is not None
            else ""
        )
        + "）"
    )


@dataclass(frozen=True)
class EventFuncAction:
    """客户端 ``Event_func_action`` 中与继续事件有关的最小字段集。"""

    wait: int = 0
    wait_action_id: int = 0
    event_id: int = 0
    dialog_id: int = 0
    location_id: int = 0

    @property
    def label(self) -> str:
        if self.wait in EVENT_WAIT_LABELS:
            return EVENT_WAIT_LABELS[self.wait]
        if self.wait_action_id in EVENT_WAIT_ACTION_LABELS:
            return EVENT_WAIT_ACTION_LABELS[self.wait_action_id]
        if self.wait or self.wait_action_id:
            return "未知事件等待动作"
        return "事件继续动作"

    @property
    def auto_confirmable(self) -> bool:
        return self.wait in EVENT_WAIT_LABELS or (
            self.wait_action_id == EVENT_WAIT_ACTION_ITEM_INTERACTION
        )


@dataclass(frozen=True)
class EventOptionEntry:
    """``Event_start.option`` 中客户端展示的一条选项。

    聚宝宝箱交互后常见选项会带 ``use`` / ``useNum``（地图钥匙与消耗数量），
    玩家点确认后客户端发送 ``Event_option{opt:optidx}``。
    """

    title: str = ""
    optidx: int = 0
    use: int = 0
    use_num: int = 0
    color: int = 0
    exit: int = 0
    icon: int = 0
    icommit: int = 0

    @property
    def is_item_cost_option(self) -> bool:
        """是否为消耗物品的确认选项（如用钥匙开箱）。"""

        return self.exit == 0 and self.use > 0 and self.use_num > 0

    @property
    def is_native_auto_index(self) -> bool:
        """``optidx >= 100``：与 EventModule autoidx 自动分支同一编号段。"""

        return self.exit == 0 and self.optidx >= 100

    @property
    def is_open_chest_title(self) -> bool:
        text = self.title or ""
        return (
            "打开宝箱" in text
            or "开启宝箱" in text
            or ("宝箱" in text and ("打开" in text or "开启" in text))
            or text.strip() == "拾取"
        )

    @property
    def is_take_key_title(self) -> bool:
        """战后「带走钥匙」确认按钮。"""

        text = self.title or ""
        return "带走钥匙" in text or ("带走" in text and "钥匙" in text)

    @property
    def is_continue_forward_title(self) -> bool:
        """战斗结算后的无消耗「继续前进」确认按钮。"""

        return (self.title or "").strip() == "继续前进"

    @property
    def is_touch_magic_circle_title(self) -> bool:
        """战后地图事件的无消耗「触碰法阵」确认按钮。"""

        return (self.title or "").strip() == "触碰法阵"


@dataclass(frozen=True)
class EventStart:
    """客户端 ``Event_start`` 的自动选项与可选分支。"""

    auto_option: int = 0
    event_id: int = 0
    dialog_id: int = 0
    location_id: int = 0
    options: tuple[EventOptionEntry, ...] = ()

    @property
    def auto_confirmable(self) -> bool:
        # EventModule 对 autoidx >= 100 的事件直接发送 Event_option，
        # 不弹出可供玩家选择的分支。
        if self.auto_option >= 100:
            return True
        # 未知上下文中只显示客户端可识别的「带走钥匙」自动项。开箱选项必须
        # 由当前节点上下文和目标地图钥匙共同确认，不能在状态查询中泛化标记。
        return self.choose_take_key_option() is not None

    def choose_take_key_option(self) -> EventOptionEntry | None:
        """战后胜利「带走钥匙」按钮（实测 autoidx=0, optidx=100, title=带走钥匙）。"""

        titled = [
            opt
            for opt in self.options
            if opt.exit == 0 and opt.is_take_key_title
        ]
        if titled:
            return titled[0]
        return None

    def choose_battle_continue_option(self) -> EventOptionEntry | None:
        """战后唯一的无消耗「继续前进」按钮。

        该分支只由怪物节点的实时上下文调用。要求服务端仅给出一个原生
        自动编号段选项，避免把同名的普通剧情或带提交数据的分支误确认。
        """

        if len(self.options) != 1:
            return None
        option = self.options[0]
        if (
            option.is_continue_forward_title
            and option.is_native_auto_index
            and option.use == 0
            and option.use_num == 0
            and option.icommit == 0
        ):
            return option
        return None

    def choose_touch_magic_circle_option(self) -> EventOptionEntry | None:
        """选择当前怪物事件中明确指定的「触碰法阵」分支。

        该事件通常会同时给出「暂时离开」，因此不以选项数量或原生自动编号
        作为门禁；只接受唯一的精确标题、非退出、无消耗且无提交数据的分支。
        调用方仍必须限制在当前怪物节点上下文。
        """

        matches = [
            option
            for option in self.options
            if (
                option.exit == 0
                and option.is_touch_magic_circle_title
                and option.use == 0
                and option.use_num == 0
                and option.icommit == 0
            )
        ]
        return matches[0] if len(matches) == 1 else None

    def choose_open_chest_option(
        self,
        *,
        preferred_item_ids: frozenset[int] | set[int] | None = None,
        item_totals: Mapping[int, int] | None = None,
        min_keys: int = SMALL_CHEST_KEY_COST,
    ) -> EventOptionEntry | None:
        """选择「打开宝箱」分支（日志常见：打开宝箱 / 暂时离开 二选一，use 常为 0）。

        必须排除离开/取消；有背包快照时钥匙不足返回 None。
        """

        leave_markers = ("离开", "取消", "关闭", "稍后", "暂时")
        preferred_ids = preferred_item_ids or frozenset()
        open_opts: list[EventOptionEntry] = []
        for opt in self.options:
            if opt.exit != 0:
                continue
            title = opt.title or ""
            if any(marker in title for marker in leave_markers):
                continue
            matching_key_cost = (
                opt.is_item_cost_option
                and bool(preferred_ids)
                and opt.use in preferred_ids
            )
            # 标题是兼容旧事件表的受控回退；若同时给出了消耗物品，必须也是
            # 当前聚宝地图的钥匙，不能替用户确认其它道具消耗。
            title_matches = opt.is_open_chest_title and (
                not opt.is_item_cost_option
                or not preferred_ids
                or opt.use in preferred_ids
            )
            if title_matches or matching_key_cost:
                open_opts.append(opt)
        if not open_opts:
            return None
        titled = [opt for opt in open_opts if opt.is_open_chest_title]
        pool = titled or open_opts
        chosen = pool[0]
        if item_totals is None:
            return chosen
        if chosen.is_item_cost_option:
            if int(item_totals.get(chosen.use, 0)) < int(chosen.use_num):
                return None
            return chosen
        if preferred_ids:
            need = max(1, int(min_keys))
            if not any(
                int(item_totals.get(int(item_id), 0)) >= need
                for item_id in preferred_ids
            ):
                return None
        return chosen

    def choose_item_cost_option(
        self,
        *,
        preferred_item_ids: frozenset[int] | set[int] | None = None,
        item_totals: Mapping[int, int] | None = None,
    ) -> EventOptionEntry | None:
        """Pick a safe auto option that spends map keys / items to continue.

        Preference order:
        1. Explicit open-chest branch (title / cost);
        2. Non-exit options whose ``use`` is in ``preferred_item_ids`` (map keys);
        3. Any non-exit option with a positive item cost;
        4. **Must afford** when ``item_totals`` is provided — 钥匙不足时绝不自动确认开箱。
        """

        open_chest = self.choose_open_chest_option(
            preferred_item_ids=preferred_item_ids,
            item_totals=item_totals,
        )
        if open_chest is not None:
            return open_chest

        preferred_ids = preferred_item_ids or frozenset()
        if not preferred_ids:
            return None
        pool = [
            opt
            for opt in self.options
            if opt.is_item_cost_option and opt.use in preferred_ids
        ]
        if not pool:
            return None
        if item_totals is not None:
            affordable = [
                opt
                for opt in pool
                if int(item_totals.get(opt.use, 0)) >= opt.use_num
            ]
            # 有背包快照时：钥匙/道具不够就不要点「打开宝箱」
            return affordable[0] if affordable else None
        # 无背包快照（仅用于 auto_confirmable 探测）：允许识别消耗项；
        # 真实刷取路径总会带 _item_totals，由上面分支做钥匙校验。
        return pool[0]

    def choose_confirmable_option(
        self,
        *,
        preferred_item_ids: frozenset[int] | set[int] | None = None,
        item_totals: Mapping[int, int] | None = None,
    ) -> EventOptionEntry | None:
        """Pick any option the farm script may safely auto-confirm."""

        take_key = self.choose_take_key_option()
        if take_key is not None:
            return take_key
        return self.choose_item_cost_option(
            preferred_item_ids=preferred_item_ids,
            item_totals=item_totals,
        )


def _load_json_rows(path: Path) -> list[dict[str, object]]:
    if not path.is_file():
        return []
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        return []
    return [row for row in raw if isinstance(row, dict)]


@lru_cache(maxsize=1)
def list_treasure_map_catalog() -> tuple[TreasureMapCatalogEntry, ...]:
    """所有 ``worldid==2`` 的聚宝地图及对应钥匙物品。"""

    rows = _load_json_rows(NATIVE_APP_ROOT / "decrypted-data" / "mapareas.json")
    entries: list[TreasureMapCatalogEntry] = []
    for row in rows:
        if row.get("worldid") != 2:
            continue
        area_id = row.get("id")
        mapgroup = row.get("mapgroup")
        if not isinstance(area_id, int) or isinstance(area_id, bool) or area_id <= 0:
            continue
        if not isinstance(mapgroup, int) or isinstance(mapgroup, bool) or mapgroup <= 0:
            continue
        key_raw = row.get("iconshow")
        key_item_id = 0
        if isinstance(key_raw, int) and not isinstance(key_raw, bool) and key_raw > 0:
            key_item_id = key_raw
        elif isinstance(key_raw, str) and key_raw.strip().isdigit():
            key_item_id = int(key_raw.strip())
        entries.append(
            TreasureMapCatalogEntry(
                area_id=area_id,
                name=treasure_area_name(area_id),
                mapgroup=mapgroup,
                key_item_id=key_item_id,
                key_item_name=item_name(key_item_id) if key_item_id else "未知钥匙",
            )
        )
    entries.sort(key=lambda entry: entry.area_id)
    return tuple(entries)


@lru_cache(maxsize=1)
def _treasure_catalog_by_id() -> Mapping[int, TreasureMapCatalogEntry]:
    return {entry.area_id: entry for entry in list_treasure_map_catalog()}


def is_treasure_map_area(area_id: int) -> bool:
    """是否为聚宝之地可刷取地图（worldid==2 目录内）。

    潮汐之门(9004)、流放者之岛等 worldid==1 区域会作为进图中转/剧情图出现在
    ``curarea``，不得当聚宝目标解析。
    """

    if not isinstance(area_id, int) or isinstance(area_id, bool) or area_id <= 0:
        return False
    return area_id in _treasure_catalog_by_id()


def get_treasure_map_entry(area_id: int) -> TreasureMapCatalogEntry:
    entry = _treasure_catalog_by_id().get(area_id)
    if entry is None:
        raise TreasureFarmError(f"未知聚宝地图：{treasure_area_name(area_id)}")
    return entry


def _classify_maplocation_kind(
    *,
    notes: str,
    mapshow: object,
    monster_id: int,
    icon: str = "",
) -> str | None:
    """将 maplocations 行分类为刷取节点类型；旅店等非战斗事件返回 None。

    各聚宝图均有 ``notes=boss`` / ``icon_battle`` 的 Boss 座（如石化森林 27），
    旧逻辑只认 notes=战斗 / mapshow=1 / monsterid>0，会漏掉 Boss，导致清图后
    仍卡在 Boss 节点却误报地图已清空。
    """

    notes_norm = notes.strip().lower()
    icon_norm = icon.strip().lower()
    if notes == "大宝箱" or mapshow == 11:
        return NODE_KIND_BIG_CHEST
    if notes == "小宝箱" or mapshow == 2:
        return NODE_KIND_SMALL_CHEST
    # Boss：表内 monsterid 常为 0、mapshow 为 0，只能靠 notes / 战斗图标识别
    if (
        notes_norm == "boss"
        or notes == "战斗"
        or monster_id > 0
        or mapshow == 1
        or icon_norm == "icon_battle"
    ):
        return NODE_KIND_MONSTER
    return None


@lru_cache(maxsize=32)
def load_area_nodes(area_id: int) -> tuple[MapNodeSpec, ...]:
    """从 maplocations 表加载指定聚宝地图的怪物/宝箱节点。"""

    rows = _load_json_rows(
        NATIVE_APP_ROOT / "decrypted-data" / "tables" / "maplocations.json"
    )
    nodes: list[MapNodeSpec] = []
    for row in rows:
        if row.get("areaid") != area_id:
            continue
        nodeid = row.get("nodeid")
        if not isinstance(nodeid, int) or isinstance(nodeid, bool) or nodeid <= 0:
            continue
        notes = str(row.get("notes") or "")
        mapshow = row.get("mapshow")
        monsterid = row.get("monsterid")
        monster_id = (
            int(monsterid)
            if isinstance(monsterid, int)
            and not isinstance(monsterid, bool)
            and monsterid > 0
            else 0
        )
        kind = _classify_maplocation_kind(
            notes=notes,
            mapshow=mapshow,
            monster_id=monster_id,
            icon=str(row.get("icon") or ""),
        )
        if kind is None:
            continue
        nodes.append(
            MapNodeSpec(
                nodeid=nodeid,
                kind=kind,
                monsterid=monster_id,
                notes=notes,
            )
        )
    nodes.sort(key=lambda node: node.nodeid)
    return tuple(nodes)


def encode_enter_treasure_request(group: int, enterway: int = ENTERWAY_NORMAL) -> bytes:
    if not isinstance(group, int) or isinstance(group, bool) or group <= 0:
        raise ValueError("聚宝地图组 ID 必须是正整数")
    if enterway not in (ENTERWAY_NORMAL, ENTERWAY_RESET, 2):
        raise ValueError("enterway 无效")
    payload = encode_int_field(1, group)
    if enterway != 0:
        payload += encode_int_field(2, enterway)
    return payload


def encode_processloc_request(nodeid: int, area_id: int) -> bytes:
    if not isinstance(nodeid, int) or isinstance(nodeid, bool) or nodeid <= 0:
        raise ValueError("节点 ID 必须是正整数")
    if not isinstance(area_id, int) or isinstance(area_id, bool) or area_id <= 0:
        raise ValueError("聚宝地图 ID 必须是正整数")
    return encode_int_field(1, nodeid) + encode_int_field(2, area_id)


def encode_map_move(path: list[tuple[int, int]] | tuple[tuple[int, int], ...]) -> bytes:
    """Encode ``Map_move`` path：repeated field1 ``{x,y}``（与客户端 ReportPath 一致）。"""

    if not path:
        raise ValueError("移动路径不能为空")
    payload = b""
    for x, y in path:
        if not isinstance(x, int) or not isinstance(y, int):
            raise ValueError("路径坐标必须是整数")
        point = encode_int_field(1, x) + encode_int_field(2, y)
        payload += encode_bytes_field(1, point)
    return payload


@lru_cache(maxsize=32)
def load_area_seat_positions(area_id: int) -> Mapping[int, tuple[int, int]]:
    """zone-layout 中 seat(nodeid) → 格子坐标 (x, y)。"""

    path = NATIVE_APP_ROOT / "decrypted-data" / "zone-layouts" / f"{area_id}.json"
    if not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    data = raw.get("data") if isinstance(raw, dict) else None
    if not isinstance(data, dict):
        return {}
    seats: dict[int, tuple[int, int]] = {}
    for tile in data.values():
        if not isinstance(tile, dict):
            continue
        seat = tile.get("seat")
        x = tile.get("x")
        y = tile.get("y")
        if (
            isinstance(seat, int)
            and not isinstance(seat, bool)
            and seat > 0
            and isinstance(x, int)
            and isinstance(y, int)
        ):
            seats[seat] = (x, y)
    return seats


def build_move_path(
    start: tuple[int, int],
    end: tuple[int, int],
    *,
    max_steps: int = 8,
) -> list[tuple[int, int]]:
    """生成简单折线路径（先横后纵），供 Map_move 使用。"""

    sx, sy = start
    ex, ey = end
    if (sx, sy) == (ex, ey):
        return [(ex, ey)]
    path: list[tuple[int, int]] = []
    x, y = sx, sy
    # 分步逼近，避免单包路径过长
    while (x, y) != (ex, ey) and len(path) < max_steps:
        if x != ex:
            step = 1 if ex > x else -1
            dist = min(abs(ex - x), 3)
            x += step * dist
        elif y != ey:
            step = 1 if ey > y else -1
            dist = min(abs(ey - y), 3)
            y += step * dist
        path.append((x, y))
    if not path or path[-1] != (ex, ey):
        path.append((ex, ey))
    return path


def decode_enter_treasure_response(data: bytes) -> int:
    ret = 0
    for field_number, wire_type, value in ProtoReader(data).fields():
        if field_number == 1 and wire_type == 0:
            ret = decode_int32(int(value))
    return ret


def _decode_protobuf_int_map(data: bytes, *, entry_field: int = 1) -> dict[int, int]:
    """Decode a protobuf map<int32,int32> encoded as repeated ``{key,value}`` messages."""

    result: dict[int, int] = {}
    for field_number, wire_type, value in ProtoReader(data).fields():
        if field_number != entry_field or wire_type != 2:
            continue
        key = 0
        val = 0
        for nested_field, nested_wire, nested_value in ProtoReader(bytes(value)).fields():
            if nested_wire != 0:
                continue
            if nested_field == 1:
                key = decode_int32(int(nested_value))
            elif nested_field == 2:
                val = decode_int32(int(nested_value))
        # status 可为 0（ACTIVE），必须按 key 写入
        if key != 0:
            result[key] = val
    return result


def decode_area_detail_locs(data: bytes) -> dict[int, int]:
    """Decode ``AreaDetail.locs`` map (seat/nodeid -> status).

    ``AreaDetail`` 字段 1 即为 ``map<int32,int32> locs`` 的 repeated 条目。
    """

    return _decode_protobuf_int_map(data, entry_field=1)


def decode_area_detail_rands(data: bytes) -> dict[int, tuple[int, int]]:
    """Decode ``AreaDetail.rands``：seat → 本局随机格子坐标 (x,y)。

    聚宝地图每局会把地标刷到 rands 位置；移动必须用 rands 而非静态 layout。
    """

    rands: dict[int, tuple[int, int]] = {}
    for field_number, wire_type, value in ProtoReader(data).fields():
        if field_number != 2 or wire_type != 2:
            continue
        key = 0
        x = 0
        y = 0
        for nested_field, nested_wire, nested_value in ProtoReader(bytes(value)).fields():
            if nested_field == 1 and nested_wire == 0:
                key = decode_int32(int(nested_value))
            elif nested_field == 2 and nested_wire == 2:
                for p_fn, p_wt, p_val in ProtoReader(bytes(nested_value)).fields():
                    if p_fn == 1 and p_wt == 0:
                        x = decode_int32(int(p_val))
                    elif p_fn == 2 and p_wt == 0:
                        y = decode_int32(int(p_val))
        if key != 0:
            rands[key] = (x, y)
    return rands


@dataclass(frozen=True)
class MoveTriggerState:
    """客户端 ``AreaDetail.mtdata`` 的移动触发器运行态。"""

    max: int = 0
    remain: int = 0
    area: int = 0
    triggernum: int = 0

    @property
    def active(self) -> bool:
        return self.max > 0


def decode_move_trigger_state(data: bytes) -> MoveTriggerState:
    """Decode ``MoveTriggerData``: ``max, remain, area, triggernum``."""

    values: dict[int, int] = {}
    for field_number, wire_type, value in ProtoReader(data).fields():
        if wire_type == 0 and field_number in (1, 2, 3, 4):
            values[field_number] = decode_int32(int(value))
    return MoveTriggerState(
        max=values.get(1, 0),
        remain=values.get(2, 0),
        area=values.get(3, 0),
        triggernum=values.get(4, 0),
    )


def decode_area_detail_move_trigger(data: bytes) -> MoveTriggerState | None:
    """Decode optional ``AreaDetail.field7`` move-trigger data."""

    for field_number, wire_type, value in ProtoReader(data).fields():
        if field_number == 7 and wire_type == 2:
            return decode_move_trigger_state(bytes(value))
    return None


def decode_area_detail(data: bytes) -> tuple[dict[int, int], dict[int, tuple[int, int]]]:
    """Return ``(locs, rands)`` from one AreaDetail blob."""

    return decode_area_detail_locs(data), decode_area_detail_rands(data)


def decode_enter_area_response(data: bytes) -> tuple[int, int, dict[int, int]]:
    """Return ``(ret, area_id, loc_status)`` from ``Map_enter_area``.

    客户端 ``Jc``：field1 ret、field2 id、field3 locs(AreaDetail)、
    field4 area、field5 pos、field6 curid、field7 fromflag。
    """

    ret = 0
    area_id = 0
    locs: dict[int, int] = {}
    for field_number, wire_type, value in ProtoReader(data).fields():
        if field_number == 1 and wire_type == 0:
            ret = decode_int32(int(value))
        elif field_number == 2 and wire_type == 0:
            area_id = decode_int32(int(value))
        elif field_number == 3 and wire_type == 2:
            # AreaDetail（locs + rands）
            locs, _rands = decode_area_detail(bytes(value))
            # rands 由调用方通过 decode_enter_area_full 获取；此处保持兼容只回 locs
        elif field_number == 4 and wire_type == 2:
            # area 摘要；id 可能在此；勿把 area 当 locs
            nested_id = 0
            for nested_field, nested_wire, nested_value in ProtoReader(bytes(value)).fields():
                if nested_field == 1 and nested_wire == 0:
                    nested_id = decode_int32(int(nested_value))
            if area_id <= 0 and nested_id > 0:
                area_id = nested_id
        elif field_number == 6 and wire_type == 0:
            if area_id <= 0:
                area_id = decode_int32(int(value))
    return ret, area_id, locs


def decode_enter_area_full(
    data: bytes,
) -> tuple[int, int, dict[int, int], dict[int, tuple[int, int]]]:
    """``(ret, area_id, locs, rands)`` from ``Map_enter_area``。"""

    ret = 0
    area_id = 0
    locs: dict[int, int] = {}
    rands: dict[int, tuple[int, int]] = {}
    for field_number, wire_type, value in ProtoReader(data).fields():
        if field_number == 1 and wire_type == 0:
            ret = decode_int32(int(value))
        elif field_number == 2 and wire_type == 0:
            area_id = decode_int32(int(value))
        elif field_number == 3 and wire_type == 2:
            locs, rands = decode_area_detail(bytes(value))
        elif field_number == 4 and wire_type == 2:
            nested_id = 0
            for nested_field, nested_wire, nested_value in ProtoReader(bytes(value)).fields():
                if nested_field == 1 and nested_wire == 0:
                    nested_id = decode_int32(int(nested_value))
            if area_id <= 0 and nested_id > 0:
                area_id = nested_id
        elif field_number == 6 and wire_type == 0:
            if area_id <= 0:
                area_id = decode_int32(int(value))
    return ret, area_id, locs, rands


def decode_processloc_response(data: bytes) -> ProcessLocResult:
    ret = 0
    loc_updates: dict[int, int] = {}
    flag = 0
    for field_number, wire_type, value in ProtoReader(data).fields():
        if field_number == 1 and wire_type == 0:
            ret = decode_int32(int(value))
        elif field_number == 2 and wire_type == 2:
            # locchanges.locs：map 条目在字段 1
            loc_updates = _decode_protobuf_int_map(bytes(value), entry_field=1)
        elif field_number == 4 and wire_type == 0:
            flag = decode_int32(int(value))
    return ProcessLocResult(ret=ret, loc_updates=loc_updates, flag=flag, items=())


def _decode_event_info(data: bytes) -> tuple[int, int, int]:
    """Decode ``EventInfo`` fields used to correlate an event with a map node."""

    event_id = 0
    dialog_id = 0
    location_id = 0
    for field_number, wire_type, value in ProtoReader(data).fields():
        if wire_type != 0:
            continue
        if field_number == 1:
            event_id = decode_int32(int(value))
        elif field_number == 4:
            dialog_id = decode_int32(int(value))
        elif field_number == 7:
            location_id = decode_int32(int(value))
    return event_id, dialog_id, location_id


def decode_event_option_entry(data: bytes) -> EventOptionEntry:
    """Decode one ``Event_start.option`` entry (client codec ``Zt``)."""

    title = ""
    optidx = 0
    use = 0
    use_num = 0
    color = 0
    exit_flag = 0
    icon = 0
    icommit = 0
    for field_number, wire_type, value in ProtoReader(data).fields():
        if field_number == 1 and wire_type == 2:
            try:
                title = bytes(value).decode("utf-8")
            except UnicodeDecodeError:
                title = ""
            continue
        if wire_type != 0:
            continue
        decoded = decode_int32(int(value))
        if field_number == 2:
            optidx = decoded
        elif field_number == 3:
            color = decoded
        elif field_number == 4:
            use = decoded
        elif field_number == 5:
            use_num = decoded
        elif field_number == 6:
            exit_flag = decoded
        elif field_number == 7:
            icon = decoded
        elif field_number == 9:
            icommit = decoded
    return EventOptionEntry(
        title=title,
        optidx=optidx,
        use=use,
        use_num=use_num,
        color=color,
        exit=exit_flag,
        icon=icon,
        icommit=icommit,
    )


def decode_event_start(data: bytes) -> EventStart:
    """Decode ``Event_start`` (13300): auto option and player-facing options."""

    auto_option = 0
    event_id = 0
    dialog_id = 0
    location_id = 0
    options: list[EventOptionEntry] = []
    for field_number, wire_type, value in ProtoReader(data).fields():
        if field_number == 1 and wire_type == 2:
            event_id, dialog_id, location_id = _decode_event_info(bytes(value))
        elif field_number == 3 and wire_type == 2:
            options.append(decode_event_option_entry(bytes(value)))
        elif field_number == 4 and wire_type == 0:
            auto_option = decode_int32(int(value))
    return EventStart(
        auto_option=auto_option,
        event_id=event_id,
        dialog_id=dialog_id,
        location_id=location_id,
        options=tuple(options),
    )


def decode_event_func_action(data: bytes) -> EventFuncAction:
    """Decode ``Event_func_action`` (13320) from the generated client codec."""

    wait = 0
    wait_action_id = 0
    event_id = 0
    dialog_id = 0
    location_id = 0
    for field_number, wire_type, value in ProtoReader(data).fields():
        if field_number == 1 and wire_type == 2:
            event_id, dialog_id, location_id = _decode_event_info(bytes(value))
        elif field_number == 2 and wire_type == 0:
            wait = decode_int32(int(value))
        elif field_number == 3 and wire_type == 2:
            for nested_field, nested_wire, nested_value in ProtoReader(
                bytes(value)
            ).fields():
                if nested_field == 1 and nested_wire == 0:
                    wait_action_id = decode_int32(int(nested_value))
                    break
    return EventFuncAction(
        wait=wait,
        wait_action_id=wait_action_id,
        event_id=event_id,
        dialog_id=dialog_id,
        location_id=location_id,
    )


def encode_event_option(
    option: int,
    dialog_id: int = 0,
    event_id: int = 0,
    *,
    itemcommit: int = 0,
    commits: tuple[int, ...] | list[int] = (),
) -> bytes:
    """Encode native ``Event_option`` after the player (or auto) picks a branch.

    Fields match the generated client codec ``GvY`` / ``Gt``:
    ``opt``, ``dialog``, ``evt``, repeated ``commits``, ``itemcommit``.
    """

    payload = b""
    if option:
        payload += encode_int_field(1, option)
    if dialog_id:
        payload += encode_int_field(2, dialog_id)
    if event_id:
        payload += encode_int_field(3, event_id)
    for commit in commits:
        if int(commit):
            payload += encode_int_field(4, int(commit))
    if itemcommit:
        payload += encode_int_field(5, int(itemcommit))
    return payload


def encode_event_func_next(*, skip: int = 0, pass_event: int = 0) -> bytes:
    """Encode native ``Event_func_next`` after an event display has completed."""

    payload = b""
    if skip:
        payload += encode_int_field(1, skip)
    if pass_event:
        payload += encode_int_field(2, pass_event)
    return payload


def decode_map_reset_response(data: bytes) -> tuple[int, int, dict[int, int]]:
    """Decode ``Map_reset_area`` (ed)：``ret, areaid, areadetail.locs``。

    注意：聚宝之地客户端重置主要走 ``Map_enter_treasure(enterway=Reset)``；
    ``Map_reset_area`` 多用于魂域等，此处仅作兼容解析。
    """

    ret = 0
    area_id = 0
    locs: dict[int, int] = {}
    for field_number, wire_type, value in ProtoReader(data).fields():
        if field_number == 1 and wire_type == 0:
            ret = decode_int32(int(value))
        elif field_number == 2 and wire_type == 0:
            area_id = decode_int32(int(value))
        elif field_number == 3 and wire_type == 2:
            locs = decode_area_detail_locs(bytes(value))
    return ret, area_id, locs


def decode_treasure_open_times(data: bytes) -> int:
    """``TreasureData.times``：今日已开大宝箱次数。"""

    open_times = 0
    for field_number, wire_type, value in ProtoReader(data).fields():
        if field_number == 1 and wire_type == 0:
            open_times = decode_int32(int(value))
            break
    return open_times


def _decode_game_data_map_blob(data: bytes) -> bytes | None:
    for field_number, wire_type, value in ProtoReader(data).fields():
        if field_number == 11 and wire_type == 2:
            return bytes(value)
    return None


@dataclass(frozen=True)
class MapLoginSnapshot:
    """登录 ``Game_data.map`` 可读快照（不发起业务动作）。"""

    dead: bool
    needsoul: bool
    curarea: int
    pos_x: int
    pos_y: int
    events: int
    loc_status: Mapping[int, int]
    seat_rands: Mapping[int, tuple[int, int]]
    game_data_has_battle_blob: bool
    # Game_data.field35.battle. The official client forwards these exact values
    # in its client_login_state event after Client_data_get has returned.
    battle_state: int = 0
    battle_type: int = 0
    # AreaInfo/AreaData are client-visible map runtime flags.  They are kept
    # separate from the server-only processloc ret=2 interlock.
    area_state: int = 0
    area_flag: int = 0
    area_locked: int = 0
    area_remains: int = 0
    move_trigger: MoveTriggerState | None = None


LOC_STATUS_LABELS = {
    LOC_STATUS_ACTIVE: "可交互(ACTIVE)",
    LOC_STATUS_OPEN: "开启(OPEN)",
    LOC_STATUS_PASSED: "已完成(PASSED)",
    -1: "关闭(CLOSED)",
}

BATTLE_TYPE_LABELS = {
    0: "PVE",
    1: "TEAM",
    2: "地图LOC",
    3: "墓穴GRAVE",
    4: "地下城DUNG",
    5: "PVP",
    6: "NPC",
    7: "地图事件LOCEVT",
    8: "全图事件ALLEVT",
    9: "本地TEAM",
    10: "龙痕竞技场",
    11: "无序迷境",
    12: "攻城",
    13: "世界BOSS",
}


def decode_game_data_map_snapshot(data: bytes) -> MapLoginSnapshot:
    """解析登录快照中的地图状态（死亡/坐标/当前图/节点状态等）。"""

    map_data = _decode_game_data_map_blob(data)
    dead = False
    needsoul = False
    events = 0
    pos_x = 0
    pos_y = 0
    curarea = 0
    locs: dict[int, int] = {}
    rands: dict[int, tuple[int, int]] = {}
    area_records: dict[int, tuple[int, int, int]] = {}
    area_remains = 0
    move_trigger: MoveTriggerState | None = None
    if map_data is not None:
        for field_number, wire_type, value in ProtoReader(map_data).fields():
            if field_number == 4 and wire_type == 0:
                events = decode_int32(int(value))
            elif field_number == 6 and wire_type == 2:
                area_info = bytes(value)
                for a_fn, a_wt, a_val in ProtoReader(area_info).fields():
                    if a_fn == 1 and a_wt == 2:
                        area_id = 0
                        state = 0
                        flag = 0
                        locked = 0
                        for e_fn, e_wt, e_val in ProtoReader(bytes(a_val)).fields():
                            if e_fn == 1 and e_wt == 0:
                                area_id = decode_int32(int(e_val))
                            elif e_fn == 2 and e_wt == 2:
                                for r_fn, r_wt, r_val in ProtoReader(
                                    bytes(e_val)
                                ).fields():
                                    if r_wt != 0:
                                        continue
                                    if r_fn == 2:
                                        state = decode_int32(int(r_val))
                                    elif r_fn == 3:
                                        flag = decode_int32(int(r_val))
                                    elif r_fn == 6:
                                        locked = decode_int32(int(r_val))
                        if area_id > 0:
                            area_records[area_id] = (state, flag, locked)
                    elif a_fn == 2 and a_wt == 0:
                        curarea = decode_int32(int(a_val))
                    elif a_fn == 3 and a_wt == 2:
                        area_detail = bytes(a_val)
                        locs, rands = decode_area_detail(area_detail)
                        move_trigger = decode_area_detail_move_trigger(area_detail)
                    elif a_fn == 4 and a_wt == 0:
                        area_remains = decode_int32(int(a_val))
            elif field_number == 7 and wire_type == 0:
                dead = bool(value)
            elif field_number == 8 and wire_type == 2:
                for p_fn, p_wt, p_val in ProtoReader(bytes(value)).fields():
                    if p_fn == 1 and p_wt == 0:
                        pos_x = decode_int32(int(p_val))
                    elif p_fn == 2 and p_wt == 0:
                        pos_y = decode_int32(int(p_val))
            elif field_number == 10 and wire_type == 0:
                needsoul = bool(value)

    # Game_data.field35 is the login-time battle marker consumed by
    # clientDataModule: {type: battleType, state: battleState}.
    battle_blob_len = 0
    battle_state = 0
    battle_type = 0
    for field_number, wire_type, value in ProtoReader(data).fields():
        if field_number == 35 and wire_type == 2:
            battle_blob = bytes(value)
            battle_blob_len = len(battle_blob)
            for battle_fn, battle_wt, battle_val in ProtoReader(battle_blob).fields():
                if battle_fn == 3 and battle_wt == 0:
                    battle_state = decode_int32(int(battle_val))
                elif battle_fn == 4 and battle_wt == 0:
                    battle_type = decode_int32(int(battle_val))
            break

    area_state, area_flag, area_locked = area_records.get(curarea, (0, 0, 0))
    return MapLoginSnapshot(
        dead=dead,
        needsoul=needsoul,
        curarea=curarea,
        pos_x=pos_x,
        pos_y=pos_y,
        events=events,
        loc_status=locs,
        seat_rands=rands,
        game_data_has_battle_blob=battle_blob_len > 0,
        battle_state=battle_state,
        battle_type=battle_type,
        area_state=area_state,
        area_flag=area_flag,
        area_locked=area_locked,
        area_remains=area_remains,
        move_trigger=move_trigger,
    )


def encode_evt_script_trigger(trigger_type: int = 2, varid: int = 0) -> bytes:
    """``Evt_script_trigger``：Stage 就绪时客户端发 type=2。"""

    payload = b""
    if trigger_type:
        payload += encode_int_field(1, trigger_type)
    if varid:
        payload += encode_int_field(2, varid)
    return payload


def encode_client_data_get(keys: str = "*") -> bytes:
    """Encode the official post-``Game_data`` ``Client_data_get`` request.

    Client ``clientDataModule.onGameData`` sends ``{keys: "*"}`` with the
    generated ``k6D`` codec before it emits ``client_login_state``.  The
    protobuf has one string field, so the canonical request is ``0a 01 2a``.
    """

    return encode_string_field(1, keys)


def encode_string_field(field_number: int, value: str) -> bytes:
    raw = value.encode("utf-8")
    return encode_bytes_field(field_number, raw)


def encode_double_field(field_number: int, value: float) -> bytes:
    """Encode a protobuf fixed64 ``double`` field for the local Struct codec."""

    return bytes(((field_number << 3) | 1,)) + struct.pack("<d", value)


def encode_client_talog(
    op: str,
    *,
    int_params: Mapping[str, int] | None = None,
) -> bytes:
    """``Client_talog`` (10531)：客户端埋点/场景上报。

    对照 main.js ``ReportEnterMap`` / ``SendSceneChangeReport``：
    ``{ op, params: { map_id, x, y } }``。
    field1 是 string op；field2 是 ``google.protobuf.Value``，其中的
    ``structValue`` 才承载 ``google.protobuf.Struct``。数值必须以
    ``Value.number_value``（field 2 / fixed64 double）编码，才能与客户端
    ``X2P`` codec 的字节序列一致。
    """

    payload = encode_string_field(1, op)
    if int_params:
        # Struct.fields: repeated { key: string, value: google.protobuf.Value }.
        # The generated client codec first wraps params as Value.structValue.
        parts = []
        for key, val in int_params.items():
            value_msg = encode_double_field(2, float(val))
            entry = encode_string_field(1, key) + encode_bytes_field(2, value_msg)
            parts.append(encode_bytes_field(1, entry))
        if parts:
            struct_body = b"".join(parts)
            value_msg = encode_bytes_field(5, struct_body)
            payload += encode_bytes_field(2, value_msg)
    return payload


def decode_game_data_curarea(data: bytes) -> int:
    """从 ``Game_data.map.areainfo.curarea`` 读取当前区域。"""

    return decode_game_data_map_snapshot(data).curarea


def decode_game_data_map_dead(data: bytes) -> bool:
    """``Game_data.map.Dead``：审判官阵亡时无法进入聚宝之地。"""

    return decode_game_data_map_snapshot(data).dead


def decode_game_data_area_detail(
    data: bytes,
) -> tuple[int, dict[int, int]]:
    """Return ``(curarea, loc_status)`` from login Game_data when already in map."""

    snap = decode_game_data_map_snapshot(data)
    return snap.curarea, dict(snap.loc_status)


def choose_next_action(
    session: AreaSession,
    nodes: tuple[MapNodeSpec, ...],
    *,
    keys: int,
    prefer_big_chest: bool = True,
    allow_big_chest: bool = True,
) -> MapNodeSpec | None:
    """选择下一步：钥匙足够才开箱，否则只打怪取钥匙。

    规则：
    - 普通宝箱需 ``keys >= 1``，大宝箱需 ``keys >= 5``；
    - 钥匙不足时 **绝不** 返回宝箱节点，只返回可击杀小怪/Boss；
    - 钥匙够时优先开箱（可 ``prefer_big_chest``），再打怪。
    - 钥匙严格大于 ``CHEST_ONLY_KEY_THRESHOLD`` 时，只返回可开启的宝箱；
      当前没有可开启宝箱则返回 ``None``，由刷取循环回主城后重新进图。

    仅选择服务端 ``locs`` 中明确为 ACTIVE/OPEN 的节点，避免对未激活
    地标发 ``Map_processloc``（会触发 ret=2「已有地标激活」或空结算）。

    ``allow_big_chest=False``：本轮已确认日限或服务端拒开大宝箱，不再选大宝箱。
    """

    if not session.loc_status:
        return None
    try:
        key_count = max(0, int(keys))
    except (TypeError, ValueError):
        key_count = 0
    active = [node for node in nodes if session.is_active(node.nodeid)]
    if not active:
        return None

    small = [n for n in active if n.kind == NODE_KIND_SMALL_CHEST]
    big = [n for n in active if n.kind == NODE_KIND_BIG_CHEST]
    monsters = [n for n in active if n.kind == NODE_KIND_MONSTER]

    can_big = (
        allow_big_chest
        and key_count >= BIG_CHEST_KEY_COST
        and session.open_times < DAILY_BIG_CHEST_OPEN_LIMIT
        and bool(big)
    )
    can_small = key_count >= SMALL_CHEST_KEY_COST and bool(small)
    chest_only_mode = key_count > CHEST_ONLY_KEY_THRESHOLD

    # 钥匙足够：优先开箱拿炉温
    if prefer_big_chest and can_big:
        return big[0]
    if can_small:
        return small[0]
    if can_big:
        return big[0]
    if chest_only_mode:
        return None
    # 钥匙不足或无可开宝箱：只能先击杀小怪/Boss 拿钥匙
    if monsters:
        return monsters[0]
    return None


def progress_payload(progress: FarmProgress) -> dict[str, object]:
    return {
        "area_id": progress.area_id,
        "area_name": progress.area_name,
        "target_hearth": progress.target_hearth,
        "hearth_gained": progress.hearth_gained,
        "hearth_total": progress.hearth_total,
        "hearth_item_name": item_name(HEARTH_ITEM_ID),
        "keys_total": progress.keys_total,
        "key_item_id": progress.key_item_id,
        "key_item_name": progress.key_item_name,
        "monsters_killed": progress.monsters_killed,
        "settled_monsters": progress.settled_monsters,
        "no_key_monsters": progress.no_key_monsters,
        "missing_hearth_chests": progress.missing_hearth_chests,
        "small_chests_opened": progress.small_chests_opened,
        "big_chests_opened": progress.big_chests_opened,
        "open_times": progress.open_times,
        "completed": progress.completed,
        "phase": progress.phase,
        "phase_label": progress.phase_label,
        "current_node_id": progress.current_node_id,
        "current_node_name": map_node_name(
            progress.current_node_id, progress.current_node_kind
        ),
        "current_node_kind": progress.current_node_kind,
        "last_reward_item_id": progress.last_reward_item_id,
        "last_reward_item_name": (
            item_name(progress.last_reward_item_id)
            if progress.last_reward_item_id > 0
            else ""
        ),
        "last_reward_delta": progress.last_reward_delta,
        "last_transition": progress.last_transition,
        "last_reset_reason": progress.last_reset_reason,
    }


class TreasureFarmClient:
    """聚宝刷取会话；可注入共享 GameSession。"""

    def __init__(
        self,
        endpoint: GameEndpoint,
        timeout: float = 20.0,
        *,
        session: object | None = None,
        battle_timeout: float = DEFAULT_BATTLE_TIMEOUT,
        socket_factory: Callable[[str, float], NativeWebSocket] = NativeWebSocket.connect,
        websocket_log: Path | bool | None = True,
    ) -> None:
        from game_session import bind_shared_session

        self.endpoint = endpoint
        self.timeout = timeout
        self.battle_timeout = battle_timeout
        self.socket_factory = socket_factory
        self.socket: NativeWebSocket | None = None
        self.password: str | None = None
        bind_shared_session(
            self,
            session,  # type: ignore[arg-type]
            error_cls=TreasureFarmError,
            task="treasure_farm",
            websocket_log=websocket_log,
        )
        self._item_totals: dict[int, int] = {}
        self._battle_context: GameDataBattleContext | None = None
        self._open_times = 0
        self._curarea = 0
        self._initial_locs: dict[int, int] = {}
        self._map_dead = False
        self._map_snapshot: MapLoginSnapshot | None = None
        self._pending_battle_info: BattleInfo | None = None
        self._raw_game_data: bytes | None = None
        # GameSession keeps the login-time Game_data available for all feature
        # clients.  Its map snapshot becomes stale once Map_enter_area arrives,
        # so remember which shared snapshot has completed initialization.
        self._shared_game_data_ref: object | None = None
        self._shared_login_ready_ref: object | None = None
        self._login_battle_message_ids: list[int] = []
        self._saw_battle_s2c_start = False
        self._saw_battle_frames = False
        self._battle_started_by_us = False
        self._battle_ended = False
        self._battle_won: bool | None = None
        self._battle_result_code: int | None = None
        self.state_probe_timeout = 4.0
        self._pos_x = 0
        self._pos_y = 0
        self._seat_rands: dict[int, tuple[int, int]] = {}
        self._move_trigger: MoveTriggerState | None = None
        self._last_processloc_ret: int | None = None
        self._client_data_requested = False
        self._client_data_ready = False
        self._auto_event_progress = True
        self._pending_event_action: EventFuncAction | None = None
        self._pending_event_start: EventStart | None = None
        self._last_event_action: EventFuncAction | None = None
        self._last_event_start: EventStart | None = None
        self._event_chain_active = False
        self._event_end_seen = False
        self._event_progress_notes: list[str] = []
        # Set when an Event_start item-cost option (key open chest) was confirmed.
        self._last_confirmed_key_option: EventOptionEntry | None = None
        # Only a currently selected farm node may auto-confirm a title/cost option.
        self._active_event_context: dict[str, object] | None = None
        # Set only by the shared-session recovery coordinator.  A previous
        # process can leave a post-battle map event without its local node
        # context, so this mode accepts only the same no-cost continuation
        # buttons the normal monster flow already recognizes.
        self._login_recovery_mode = False
        self._preferred_event_item_ids: frozenset[int] = frozenset(
            entry.key_item_id
            for entry in list_treasure_map_catalog()
            if entry.key_item_id > 0
        )

    def close(self) -> None:
        from game_session import shared_close

        if not shared_close(self):
            return
        if self.socket is not None:
            self.socket.close()
            self.socket = None

    def _send_message(self, message_id: int, data: bytes = b"", *, encrypted: bool) -> None:
        from game_session import session_send_message

        if session_send_message(self, message_id, data, encrypted=encrypted):
            return
        if self.socket is None:
            raise TreasureFarmError("WebSocket 尚未连接")
        packet = encode_message_header(message_id, data)
        if encrypted:
            if not self.password:
                raise TreasureFarmError("游戏服尚未下发会话密码")
            self.socket.send_text(pack1_encode(packet, self.password))
        else:
            self.socket.send_binary(packet)

    def _decode_frame(self, opcode: int, payload: bytes) -> MessageHeader:
        if self.password is not None:
            if opcode not in (0x1, 0x2):
                raise TreasureFarmError(f"加密游戏报文 opcode 异常：{opcode}")
            payload = pack1_decode(payload, self.password)
        return decode_message_header(payload)

    def _receive_header(self, deadline: float, context: str) -> MessageHeader:
        from game_session import try_session_receive_header

        handled, header = try_session_receive_header(self, deadline, context)
        if handled:
            assert header is not None
            return header
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TreasureFarmError(f"等待{context}超时")
        try:
            assert self.socket is not None
            opcode, payload = self.socket.recv_message(remaining)
        except socket.timeout as exc:
            raise TreasureFarmError(f"等待{context}超时") from exc
        except OSError as exc:
            raise TreasureFarmError(f"读取{context}报文失败：{exc}") from exc
        return self._decode_frame(opcode, payload)

    def _note_event_progress(self, message: str) -> None:
        self._event_progress_notes.append(message)

    def drain_event_progress_notes(self) -> tuple[str, ...]:
        """Return event-state transitions produced since the previous drain."""

        notes = tuple(self._event_progress_notes)
        self._event_progress_notes.clear()
        return notes

    def _handle_event_message(self, header: MessageHeader) -> bool:
        """Advance only event actions the official client advances without a choice.

        ``Event_func_next`` has no event identifier in its normal payload; it is
        valid only after the matching server ``Event_func_action``.  This method
        therefore never sends it speculatively, and keeps unknown wait actions
        pending for diagnosis rather than selecting a branch.
        """

        if header.message_id == EVENT_START_MESSAGE_ID:
            start = decode_event_start(header.data)
            self._last_event_start = start
            self._pending_event_start = start
            self._event_chain_active = True
            self._event_end_seen = False
            if start.auto_option >= 100 and self._auto_event_progress:
                self._send_message(
                    EVENT_OPTION_MESSAGE_ID,
                    encode_event_option(
                        start.auto_option,
                        start.dialog_id,
                        start.event_id,
                    ),
                    encrypted=True,
                )
                self._pending_event_start = None
                self._note_event_progress("地图事件自动选项已确认")
            elif start.auto_option >= 100:
                self._note_event_progress("检测到地图事件自动选项（状态查询未确认）")
            else:
                context = getattr(self, "_active_event_context", None)
                node_kind = (
                    str(context.get("node_kind") or "")
                    if isinstance(context, dict)
                    else ""
                )
                key_item_id = (
                    int(context.get("key_item_id") or 0)
                    if isinstance(context, dict)
                    else 0
                )
                if node_kind in (NODE_KIND_SMALL_CHEST, NODE_KIND_BIG_CHEST):
                    chosen = start.choose_open_chest_option(
                        preferred_item_ids=(
                            frozenset({key_item_id}) if key_item_id > 0 else frozenset()
                        ),
                        item_totals=getattr(self, "_item_totals", None),
                        min_keys=chest_key_cost(node_kind),
                    )
                elif node_kind == NODE_KIND_MONSTER:
                    chosen = (
                        start.choose_take_key_option()
                        or start.choose_touch_magic_circle_option()
                        or start.choose_battle_continue_option()
                    )
                elif self._login_recovery_mode and self._curarea > 0:
                    chosen = (
                        start.choose_take_key_option()
                        or start.choose_touch_magic_circle_option()
                        or start.choose_battle_continue_option()
                    )
                else:
                    # Outside a live node action, never infer a spend/branch
                    # from a title or arbitrary item cost.
                    chosen = None
                if chosen is not None and self._auto_event_progress:
                    self._send_message(
                        EVENT_OPTION_MESSAGE_ID,
                        encode_event_option(
                            chosen.optidx,
                            start.dialog_id,
                            start.event_id,
                            itemcommit=chosen.icommit,
                        ),
                        encrypted=True,
                    )
                    self._pending_event_start = None
                    if chosen.is_take_key_title:
                        self._note_event_progress(
                            "带走钥匙已确认"
                            f"（title={chosen.title!r}，opt={chosen.optidx}）"
                        )
                    elif chosen.is_continue_forward_title:
                        self._note_event_progress(
                            "战后继续前进已确认"
                            f"（title={chosen.title!r}，opt={chosen.optidx}）"
                        )
                    elif chosen.is_touch_magic_circle_title:
                        self._note_event_progress(
                            "触碰法阵已确认"
                            f"（title={chosen.title!r}，opt={chosen.optidx}）"
                        )
                    elif node_kind in (NODE_KIND_SMALL_CHEST, NODE_KIND_BIG_CHEST):
                        self._last_confirmed_key_option = chosen
                        cost_name = item_name(chosen.use) if chosen.use else "道具"
                        self._note_event_progress(
                            "确定用钥匙打开宝箱已确认"
                            f"（{cost_name}×{chosen.use_num}，opt={chosen.optidx}）"
                        )
                    else:
                        self._note_event_progress(
                            f"地图事件选项已确认（title={chosen.title!r}，"
                            f"opt={chosen.optidx}）"
                        )
                elif chosen is not None:
                    self._note_event_progress(
                        "检测到可确认地图事件选项（状态查询未确认）"
                    )
                else:
                    self._note_event_progress(
                        "检测到需选择的地图事件，保留等待状态"
                        f"（event={start.event_id}，dialog={start.dialog_id}，"
                        f"options={len(start.options)}）"
                    )
            return True

        if header.message_id == EVENT_FUNC_ACTION_MESSAGE_ID:
            action = decode_event_func_action(header.data)
            self._last_event_action = action
            self._pending_event_action = action
            self._event_chain_active = True
            self._event_end_seen = False
            if action.auto_confirmable and self._auto_event_progress:
                self._send_message(
                    EVENT_FUNC_NEXT_MESSAGE_ID,
                    encode_event_func_next(),
                    encrypted=True,
                )
                self._pending_event_action = None
                self._note_event_progress(f"地图事件{action.label}已确认")
            elif action.auto_confirmable:
                self._note_event_progress(
                    f"检测到地图事件{action.label}（状态查询未确认）"
                )
            else:
                self._note_event_progress("检测到未知地图事件等待动作，保留等待状态")
            return True

        if header.message_id == EVENT_OPTION_FAILED_MESSAGE_ID:
            # 官方客户端会把该事件重新交给对话 UI；脚本不猜测后续选项。
            previous_start = getattr(self, "_last_event_start", None)
            if isinstance(previous_start, EventStart):
                self._pending_event_start = previous_start
            self._note_event_progress("地图事件自动选项未通过，保留等待状态")
            return True

        if header.message_id == EVENT_END_MESSAGE_ID:
            self._pending_event_action = None
            self._pending_event_start = None
            self._event_chain_active = False
            self._event_end_seen = True
            self._note_event_progress("地图事件已结束")
            return True

        return False

    def _handle_common_message(self, header: MessageHeader) -> bool:
        if header.message_id == HEARTBEAT_MESSAGE_ID:
            self._send_message(
                HEARTBEAT_RET_MESSAGE_ID,
                encrypted=self.password is not None,
            )
            return True
        if header.message_id == LOGIN_FAIL_MESSAGE_ID:
            raise TreasureFarmError("游戏服 Login 失败")
        if header.message_id == KICKOUT_MESSAGE_ID:
            ret = 0
            message = ""
            for field_number, wire_type, value in ProtoReader(header.data).fields():
                if field_number == 1 and wire_type == 0:
                    ret = decode_int32(int(value))
                elif field_number == 2 and wire_type == 2:
                    try:
                        message = bytes(value).decode("utf-8")
                    except UnicodeDecodeError:
                        message = ""
            raise TreasureFarmKickout(ret, message)
        if header.message_id == STORAGE_ITEM_CHANGE_MESSAGE_ID:
            notice = decode_item_change_notify(header.data)
            for change in notice.items:
                if change.item_id > 0:
                    self._item_totals[change.item_id] = change.total
            return True
        if header.message_id == MAP_TREASURE_INFO_MESSAGE_ID:
            self._open_times = decode_treasure_open_times(header.data)
            return True
        if self._handle_event_message(header):
            return True
        return False

    def _apply_game_data(self, data: bytes, *, request_client_data: bool) -> None:
        """Update login state and issue the native client's client-data request."""

        self._raw_game_data = data
        self._item_totals = decode_game_data_item_totals(data)
        self._battle_context = decode_game_data_battle_context(data)
        snap = decode_game_data_map_snapshot(data)
        self._map_snapshot = snap
        self._curarea = snap.curarea
        self._initial_locs = dict(snap.loc_status)
        self._map_dead = snap.dead
        self._pos_x = snap.pos_x
        self._pos_y = snap.pos_y
        self._seat_rands = dict(snap.seat_rands)
        self._move_trigger = snap.move_trigger
        if request_client_data and not self._client_data_requested:
            self._send_message(
                CLIENT_DATA_GET_MESSAGE_ID,
                encode_client_data_get(),
                encrypted=True,
            )
            self._client_data_requested = True

    def _record_client_data_ready(self, header: MessageHeader) -> bool:
        if header.message_id != CLIENT_DATA_GET_MESSAGE_ID:
            return False
        self._client_data_ready = True
        return True

    def login(self) -> None:
        from game_session import try_session_ensure_ready

        if try_session_ensure_ready(self, self.endpoint):
            try:
                game_data = getattr(self._session, "game_data", None)
                # A shared GameSession exposes the Game_data it received while
                # logging in.  Do not replay that cached snapshot after a
                # Map_enter_area has already moved this client into a new map:
                # doing so restores the pre-entry curarea and makes the entry
                # loop wait for a response it has already consumed.
                if game_data is not None and game_data is self._shared_login_ready_ref:
                    return
                if game_data:
                    if game_data is not self._shared_game_data_ref:
                        self._shared_game_data_ref = game_data
                        self._client_data_requested = False
                        self._client_data_ready = False
                    self._apply_game_data(bytes(game_data), request_client_data=True)
                self._collect_post_login_battle_signals(
                    min(self.state_probe_timeout, 2.0)
                )
                if self._curarea > 0:
                    self._signal_stage_ready()
                    self._collect_post_login_battle_signals(
                        max(self.state_probe_timeout, 6.0)
                    )
                    if self._pending_battle_info is None:
                        self._signal_stage_ready()
                        self._collect_post_login_battle_signals(8.0)
                else:
                    self._collect_post_login_battle_signals(self.state_probe_timeout)
                self._refresh_treasure_info()
                self._shared_login_ready_ref = game_data
            except Exception:
                self.close()
                raise
            return

        if self.socket is not None:
            return
        self.socket = self.socket_factory(self.endpoint.url, self.timeout)
        try:
            self._send_message(
                LOGIN_MESSAGE_ID,
                encode_login_payload(self.endpoint.game_token),
                encrypted=False,
            )
            deadline = time.monotonic() + self.timeout
            saw_reunique = False
            while True:
                header = self._receive_header(deadline, "游戏服登录")
                if header.message_id == PACK_PASSWORD_MESSAGE_ID:
                    encrypted_password = decode_pack_password(header.data)
                    try:
                        self.password = pack1_decode(
                            encrypted_password, SOCKET_PACK_KEY
                        ).decode("utf-8")
                    except UnicodeDecodeError as exc:
                        raise TreasureFarmError("游戏服会话密码不是 UTF-8 文本") from exc
                    continue
                if self._handle_common_message(header):
                    continue
                if header.message_id == GAME_DATA_MESSAGE_ID:
                    self._apply_game_data(header.data, request_client_data=True)
                    continue
                if self._record_client_data_ready(header):
                    continue
                if header.message_id in BATTLE_ACTIVE_MESSAGE_IDS:
                    self._note_battle_message(header)
                    continue
                if header.message_id == LOGIN_REUNIQUE_MESSAGE_ID:
                    saw_reunique = True
                    break
            if not saw_reunique:
                raise TreasureFarmError("未完成游戏服登录")
            # 客户端须在 Client_data_get 返回后才会发 client_login_state；其参数
            # 直接来自 Game_data.field35.battleState/battleType。先等该响应，再
            # 对齐由该事件驱动的 Stage 恢复序列。
            self._collect_post_login_battle_signals(min(self.state_probe_timeout, 2.0))
            # 登录后：若已在图内，先对齐客户端进 Stage 序列，再听 Battle_info。
            if self._curarea > 0:
                self._signal_stage_ready()
                self._collect_post_login_battle_signals(
                    max(self.state_probe_timeout, 6.0)
                )
                if self._pending_battle_info is None:
                    # 第二轮：再发就绪信号并拉长窗口
                    self._signal_stage_ready()
                    self._collect_post_login_battle_signals(8.0)
            else:
                self._collect_post_login_battle_signals(self.state_probe_timeout)
            self._refresh_treasure_info()
        except Exception:
            self.close()
            raise

    def _note_battle_message(self, header: MessageHeader) -> None:
        if not hasattr(self, "_login_battle_message_ids"):
            self._login_battle_message_ids = []
        self._login_battle_message_ids.append(header.message_id)
        if header.message_id == BATTLE_INFO_MESSAGE_ID:
            try:
                self._pending_battle_info = decode_battle_info(header.data)
                # 新战斗握手：清掉上一场结束标记
                if self._pending_battle_info.ret == 0:
                    self._battle_ended = False
                    self._battle_won = None
                    self._battle_result_code = None
                    self._battle_started_by_us = False
                    self._saw_battle_s2c_start = False
                    self._saw_battle_frames = False
            except Exception:
                pass
        elif header.message_id == BATTLE_S2C_START_MESSAGE_ID:
            self._saw_battle_s2c_start = True
            self._battle_started_by_us = True
        elif header.message_id == BATTLE_S2C_END_MESSAGE_ID:
            outcome = decode_treasure_battle_end(header.data)
            self._battle_ended = True
            self._battle_won = outcome.win
            self._battle_result_code = outcome.result_code
            self._pending_battle_info = None
        elif header.message_id in (
            BATTLE_S2C_FRAME_BROADCAST_MESSAGE_ID,
            BATTLE_S2C_FRAME_HASH_VERIFY_MESSAGE_ID,
        ):
            self._saw_battle_frames = True

    def _collect_post_login_battle_signals(self, seconds: float) -> None:
        deadline = time.monotonic() + max(0.2, seconds)
        while time.monotonic() < deadline:
            try:
                header = self._receive_header(deadline, "登录后战斗信号窗口")
            except TreasureFarmError:
                break
            if self._handle_common_message(header):
                continue
            if header.message_id in BATTLE_ACTIVE_MESSAGE_IDS:
                self._note_battle_message(header)
                continue
            if header.message_id == GAME_DATA_MESSAGE_ID:
                self._apply_game_data(header.data, request_client_data=True)
                continue
            if self._record_client_data_ready(header):
                continue

    def _signal_stage_ready(self) -> None:
        """尽量复现客户端进 Stage 后的就绪序列，促使补发挂起 ``Battle_info``。

        静态客户端顺序为 ``PopSceneToStage`` 先上报 ``scene_change``，Stage
        加载完成后 ``ReportEnterMap``，随后 ``Evt_script_trigger(type=2)``。
        ``type=0`` 属于 ``PopSceneToMainCity``，不应混入登录恢复。
        """

        try:
            # 1) PopSceneToStage 的切场景上报（Stage 尚不存在时 old=0）。
            if self._curarea > 0:
                self._send_message(
                    CLIENT_TALOG_MESSAGE_ID,
                    encode_client_talog(
                        "scene_change",
                        int_params={
                            "scene_id_old": 0,
                            "scene_id_new": int(self._curarea),
                        },
                    ),
                    encrypted=True,
                )
                # 2) Stage.ReportEnterMap 在 Stage 初始化完成后发送。
                self._send_message(
                    CLIENT_TALOG_MESSAGE_ID,
                    encode_client_talog(
                        "enter_map",
                        int_params={
                            "map_id": int(self._curarea),
                            "x": int(self._pos_x),
                            "y": int(self._pos_y),
                        },
                    ),
                    encrypted=True,
                )
            # 3) PopSceneToStage callback：Stage ready only emits type=2.
            self._send_message(
                EVT_SCRIPT_TRIGGER_MESSAGE_ID,
                encode_evt_script_trigger(2, 0),
                encrypted=True,
            )
        except TreasureFarmError:
            return

    def _return_to_map_start(self, *, timeout: float = 3.0) -> bool:
        """Request the native client's map-position recovery path.

        ``StageActor.CheckCannotMove`` sends an empty ``Map_return_start`` after
        repeated blocked movement.  This is distinct from battle recovery and is
        only useful when a stale landmark lock has no battle marker or handshake.
        The server may answer with ``Map_move`` or only follow-up state messages,
        so callers must still verify with a fresh ``Map_processloc`` probe.
        """

        if self._curarea <= 0:
            return False
        self._send_message(MAP_RETURN_START_MESSAGE_ID, encrypted=True)
        deadline = time.monotonic() + max(0.2, timeout)
        while time.monotonic() < deadline:
            try:
                header = self._receive_header(deadline, "Map_return_start")
            except TreasureFarmError:
                break
            if self._handle_common_message(header):
                continue
            if header.message_id == GAME_DATA_MESSAGE_ID:
                self._apply_game_data(header.data, request_client_data=True)
                continue
            if self._record_client_data_ready(header):
                continue
            if header.message_id in BATTLE_ACTIVE_MESSAGE_IDS:
                self._note_battle_message(header)
                return True
            if header.message_id in (MAP_RETURN_START_MESSAGE_ID, MAP_MOVE_MESSAGE_ID):
                return True
        return False

    def _refresh_treasure_info(self) -> None:
        self._send_message(MAP_TREASURE_INFO_MESSAGE_ID, encrypted=True)
        deadline = time.monotonic() + self.timeout
        while True:
            header = self._receive_header(deadline, "Map_treasure_info")
            if self._record_client_data_ready(header):
                continue
            if header.message_id == GAME_DATA_MESSAGE_ID:
                self._apply_game_data(header.data, request_client_data=True)
                continue
            if header.message_id in BATTLE_ACTIVE_MESSAGE_IDS:
                self._note_battle_message(header)
                continue
            if self._handle_common_message(header):
                if header.message_id == MAP_TREASURE_INFO_MESSAGE_ID:
                    return
                continue
            if header.message_id == MAP_TREASURE_INFO_MESSAGE_ID:
                self._open_times = decode_treasure_open_times(header.data)
                return

    def item_total(self, item_id: int) -> int:
        return int(self._item_totals.get(item_id, 0))

    def open_times(self) -> int:
        return self._open_times

    def battle_subphase(self) -> str:
        """细分战斗相关子阶段。"""

        if self._saw_battle_s2c_start or self._saw_battle_frames:
            if self._battle_ended:
                return PHASE_MAP_IDLE
            return PHASE_BATTLE_RUNNING
        pending = self._pending_battle_info
        if pending is not None and pending.ret == 0:
            # skipTeam=true：客户端直接开战；false：布阵/准备界面
            if pending.skip_team or self._battle_started_by_us:
                return PHASE_BATTLE_RUNNING
            return PHASE_BATTLE_PREPARE
        return PHASE_MAP_IDLE

    def classify_phase(
        self,
        *,
        processloc_ret: int | None = None,
        dead: bool | None = None,
        area_id: int | None = None,
    ) -> str:
        """根据死亡/区域/战斗协议/最近 processloc 归类会话阶段。"""

        if dead is None:
            dead = self._map_dead
        if area_id is None:
            area_id = self._curarea
        if processloc_ret is None:
            processloc_ret = self._last_processloc_ret

        if dead:
            return PHASE_DEAD
        battle = self.battle_subphase()
        if battle == PHASE_BATTLE_PREPARE:
            return PHASE_BATTLE_PREPARE
        if battle == PHASE_BATTLE_RUNNING:
            return PHASE_BATTLE_RUNNING
        snapshot = getattr(self, "_map_snapshot", None)
        if snapshot is not None and snapshot.battle_state != 0:
            return PHASE_BATTLE_RECOVERY
        if area_id <= 0:
            return PHASE_CITY
        if processloc_ret == 2:
            return PHASE_LANDMARK_LOCKED
        if processloc_ret == 0:
            return PHASE_ACTIONABLE
        if processloc_ret is not None and processloc_ret != 0:
            return PHASE_INTERACT_BLOCKED
        return PHASE_MAP_IDLE

    def finish_pending_battle(
        self,
        *,
        timeout: float | None = None,
        stop_requested: Callable[[], bool] | None = None,
    ) -> bool:
        """推进战斗准备→开战→结束。返回是否处理过战斗。

        支持两种入口：
        - 已有 ``Battle_info``（布阵/准备界面）→ 发 ``Battle_C2S_start``；
        - 已有 S2C_start/战斗帧（进行中）→ 只等到 ``Battle_S2C_end`` / loc 结算。
        """

        stop_requested = stop_requested or (lambda: False)
        if stop_requested():
            raise TreasureFarmCancelled("已请求停止，未继续处理挂起战斗")
        self.login()
        info = self._pending_battle_info
        # 尝试再捞一次 Battle_info（官方客户端进 Stage 后才常推送）
        if info is None or info.ret != 0:
            if not (self._saw_battle_s2c_start or self._saw_battle_frames):
                self._signal_stage_ready()
                self._collect_post_login_battle_signals(2.5)
                info = self._pending_battle_info

        already_running = bool(
            self._saw_battle_s2c_start or self._saw_battle_frames
        ) and not self._battle_ended
        if (info is None or info.ret != 0) and not already_running:
            return False

        # 战斗准备界面：客户端等玩家点开战；自动化直接用当前编队开战
        if (
            info is not None
            and info.ret == 0
            and not self._saw_battle_s2c_start
            and not already_running
        ):
            self._start_battle(info)
            self._configure_battle()
            self._battle_started_by_us = True

        deadline = time.monotonic() + (
            timeout if timeout is not None else self.battle_timeout
        )
        while time.monotonic() < deadline:
            if stop_requested():
                raise TreasureFarmCancelled("已请求停止，结束当前战斗等待")
            try:
                header = self._receive_header(deadline, "结束挂起战斗")
            except TreasureFarmError:
                break
            if self._handle_common_message(header):
                continue
            if header.message_id in BATTLE_ACTIVE_MESSAGE_IDS or header.message_id in (
                BATTLE_S2C_END_MESSAGE_ID,
                BATTLE_C2S_START_MESSAGE_ID,
            ):
                self._note_battle_message(header)
                if header.message_id == BATTLE_S2C_START_MESSAGE_ID:
                    start = decode_battle_start_response(header.data)
                    if start.ret != 0:
                        raise TreasureFarmError(f"战斗开始失败：ret={start.ret}")
                    if not self._battle_started_by_us:
                        self._configure_battle()
                elif header.message_id == BATTLE_INFO_MESSAGE_ID:
                    # 链式战斗 / 迟到的握手
                    more = self._pending_battle_info
                    if (
                        more is not None
                        and more.ret == 0
                        and not self._saw_battle_s2c_start
                    ):
                        self._start_battle(more)
                        self._configure_battle()
                        self._battle_started_by_us = True
                elif header.message_id == BATTLE_S2C_END_MESSAGE_ID:
                    # 结束后再短等 loc 结算
                    deadline = max(deadline, time.monotonic() + 3.0)
                continue
            if header.message_id == MAP_PROCESSLOC_MESSAGE_ID:
                result = decode_processloc_response(header.data)
                self._last_processloc_ret = result.ret
                if result.loc_updates:
                    self._initial_locs = {
                        **self._initial_locs,
                        **result.loc_updates,
                    }
                if self._battle_ended or result.loc_updates:
                    break
                continue
            if header.message_id == MAP_EXIT_AREA_MESSAGE_ID:
                ret = 0
                for field_number, wire_type, value in ProtoReader(header.data).fields():
                    if field_number == 1 and wire_type == 0:
                        ret = decode_int32(int(value))
                if ret == 0:
                    self._curarea = 0
                    self._initial_locs = {}
                    self._seat_rands = {}
                break
            if header.message_id == MAP_ENTER_AREA_MESSAGE_ID:
                ret, entered_id, locs, rands = decode_enter_area_full(header.data)
                if ret == 0 and entered_id > 0:
                    self._curarea = entered_id
                    if locs:
                        self._initial_locs = dict(locs)
                    if rands:
                        self._seat_rands = dict(rands)
                break

        # 仅在战斗真正结束或地标已释放时算成功；误报 True 会导致继续开箱踩 ret=2
        if self._battle_ended:
            self._pending_battle_info = None
            if self._last_processloc_ret == 2:
                self._last_processloc_ret = None
            return True
        if self._last_processloc_ret == 0:
            self._pending_battle_info = None
            return True
        if (
            self._last_processloc_ret is not None
            and self._last_processloc_ret != 2
            and not (
                self._pending_battle_info is not None
                and self._pending_battle_info.ret == 0
            )
        ):
            return True
        return False

    def recover_from_landmark_lock(
        self,
        *,
        timeout: float = 45.0,
        emit: Callable[[str, str, dict[str, object]], None] | None = None,
        stop_requested: Callable[[], bool] | None = None,
    ) -> bool:
        """地标占用 / 疑似战斗中：Stage 就绪 → 捞 Battle_info → 打完。

        ``Map_processloc`` 返回 ret=2 时，先完成登录恢复并等待完整战斗握手。
        没有 ``Battle_info`` 就没有 battle_id/敌方编队，不能构造开战包。
        """

        def _emit(level: str, message: str, data: dict[str, object] | None = None) -> None:
            if emit is not None:
                emit(level, message, data or {})

        stop_requested = stop_requested or (lambda: False)
        if stop_requested():
            raise TreasureFarmCancelled("已请求停止，未开始地标恢复")
        self.login()
        deadline = time.monotonic() + max(8.0, timeout)
        marker = self._map_snapshot
        return_start_acknowledged: bool | None = None
        marker_state = marker.battle_state if marker else 0
        marker_type = marker.battle_type if marker else 0
        marker_label = (
            f"battleState={marker_state}, battleType={marker_type}"
            if marker and marker.game_data_has_battle_blob
            else "Game_data 未携带 battle 标识"
        )
        _emit(
            "info",
            "检测到地标占用/疑似战斗，先结算挂起战斗再开箱"
            f"（{marker_label}，Client_data_get="
            f"{'已响应' if self._client_data_ready else '待响应'}）",
            {
                "curarea": self._curarea,
                "battle_state": marker_state,
                "battle_type": marker_type,
                "client_data_ready": self._client_data_ready,
            },
        )

        # 多轮模拟 Stage 就绪，促使服务端补发 Battle_info
        for attempt in range(4):
            if stop_requested():
                raise TreasureFarmCancelled("已请求停止，结束地标恢复")
            if time.monotonic() >= deadline:
                break
            self._signal_stage_ready()
            self._collect_post_login_battle_signals(2.0)
            if (
                self._pending_battle_info is not None
                and self._pending_battle_info.ret == 0
            ) or self._saw_battle_s2c_start or self._saw_battle_frames:
                _emit(
                    "info",
                    f"已收到战斗协议信号（第 {attempt + 1} 轮 Stage 就绪）",
                    {
                        "has_battle_info": self._pending_battle_info is not None,
                        "s2c_start": self._saw_battle_s2c_start,
                        "frames": self._saw_battle_frames,
                    },
                )
                break

        no_battle_signal = (
            self._pending_battle_info is None
            and not self._saw_battle_s2c_start
            and not self._saw_battle_frames
        )
        # Native StageActor sends Map_return_start after repeated blocked
        # movement.  A ret=2 without a nonzero login marker is that recovery
        # family, not a battle that can be started without Battle_info.
        if (
            no_battle_signal
            and marker_state == 0
            and self._curarea > 0
            and time.monotonic() < deadline
        ):
            _emit(
                "info",
                "未发现可恢复战斗标识或 Battle_info，尝试 Map_return_start 回到地图起点",
                {"battle_state": marker_state, "battle_type": marker_type},
            )
            return_start_acknowledged = self._return_to_map_start(
                timeout=min(3.0, max(0.2, deadline - time.monotonic()))
            )
            no_battle_signal = (
                self._pending_battle_info is None
                and not self._saw_battle_s2c_start
                and not self._saw_battle_frames
            )
            if no_battle_signal:
                _emit(
                    "info",
                    "Map_return_start 已发送；不再用 Map_processloc 重探，"
                    "下一次明确的节点动作将承担确认",
                    {"return_start_acknowledged": return_start_acknowledged},
                )

        if self.finish_pending_battle(
            timeout=max(5.0, deadline - time.monotonic()),
            stop_requested=stop_requested,
        ):
            _emit("info", "挂起战斗已尝试结算", {})
            if (
                self._pending_battle_info is not None
                or self.battle_subphase()
                in (PHASE_BATTLE_PREPARE, PHASE_BATTLE_RUNNING)
            ):
                # 链式战继续沿完整战斗协议结算。
                self.finish_pending_battle(
                    timeout=max(5.0, deadline - time.monotonic()),
                    stop_requested=stop_requested,
                )
            if self.battle_subphase() not in (
                PHASE_BATTLE_PREPARE,
                PHASE_BATTLE_RUNNING,
            ):
                # 只清理已完成战斗留下的历史 ret；下一次实际节点动作会重新
                # 获得服务端结果，不在这里发额外 processloc。
                self._last_processloc_ret = None
                _emit("success", "挂起战斗已结算，等待下一次节点动作确认地图状态", {})
                return True

        # 被动清理窗口（仍可能迟到 Battle_info）
        remaining = deadline - time.monotonic()
        if remaining > 1.0:
            self.clear_pending_map_activity(timeout=min(12.0, remaining))
            if self.finish_pending_battle(
                timeout=max(3.0, deadline - time.monotonic()),
                stop_requested=stop_requested,
            ):
                self._last_processloc_ret = None
                return True

        locked = self._last_processloc_ret == 2 or self.classify_phase() in (
            PHASE_BATTLE_RECOVERY,
            PHASE_LANDMARK_LOCKED,
            PHASE_BATTLE_PREPARE,
            PHASE_BATTLE_RUNNING,
        )
        if locked:
            _emit(
                "warning",
                "纯脚本恢复未解除地标占用："
                f"{marker_label}，Client_data_get="
                f"{'已响应' if self._client_data_ready else '待响应'}，"
                + (
                    "Map_return_start=已响应，"
                    if return_start_acknowledged is True
                    else (
                        "Map_return_start=未确认响应，"
                        if return_start_acknowledged is False
                        else ""
                    )
                )
                +
                "未收到 Battle_info；停止后续开箱",
                {
                    "processloc_ret": self._last_processloc_ret,
                    "phase": self.classify_phase(),
                    "battle_state": marker_state,
                    "battle_type": marker_type,
                    "return_start_acknowledged": return_start_acknowledged,
                },
            )
            return False
        return True

    def ensure_actionable(
        self,
        *,
        preferred_area_id: int = 0,
        emit: Callable[[str, str, dict[str, object]], None] | None = None,
        max_rounds: int = 8,
    ) -> dict[str, object]:
        """切图后/刷取前：只用登录状态推进已存在的战斗。

        处理顺序：
        1. 阵亡 → 报错
        2. 战斗准备 → 自动开战
        3. 战斗中 → 等到结束
        4. 地图空闲 → 返回可执行

        此方法不得发送 ``Map_processloc``。该消息是正式的点地标动作，
        对 ACTIVE 怪物发送会启动战斗，不能作为预检或状态探针。
        """

        def _emit(level: str, message: str, data: dict[str, object] | None = None) -> None:
            if emit is not None:
                emit(level, message, data or {})

        self.login()
        last_status: dict[str, object] = {}
        for round_i in range(max_rounds):
            # 先根据已知协议信号分类
            phase = self.classify_phase()
            area_id = self._curarea
            area_name = treasure_area_name(area_id) if area_id else "主城/非区域"

            if phase == PHASE_DEAD:
                raise TreasureFarmError("审判官已阵亡，无法继续（请先复活）")

            if phase == PHASE_BATTLE_RECOVERY:
                _emit(
                    "info",
                    f"检测到登录期战斗标识，重放恢复序列（{area_name}）",
                    {
                        "phase": phase,
                        "battle_state": self._map_snapshot.battle_state
                        if self._map_snapshot
                        else 0,
                        "battle_type": self._map_snapshot.battle_type
                        if self._map_snapshot
                        else 0,
                        "round": round_i,
                    },
                )
                recovered = self.recover_from_landmark_lock(
                    timeout=min(self.battle_timeout, 40.0),
                    emit=emit,
                )
                if not recovered:
                    return {
                        "phase": phase,
                        "phase_label": PHASE_LABELS[phase],
                        "area_id": area_id,
                        "area_name": area_name,
                        "battle_state": self._map_snapshot.battle_state
                        if self._map_snapshot
                        else 0,
                        "battle_type": self._map_snapshot.battle_type
                        if self._map_snapshot
                        else 0,
                        "round": round_i,
                    }
                continue

            if phase == PHASE_BATTLE_PREPARE:
                _emit(
                    "info",
                    f"检测到战斗准备界面，自动开战（{area_name}）",
                    {"phase": phase, "round": round_i},
                )
                self.finish_pending_battle()
                continue

            if phase == PHASE_BATTLE_RUNNING:
                _emit(
                    "info",
                    f"检测到战斗中，等待战斗结束（{area_name}）",
                    {"phase": phase, "round": round_i},
                )
                # 若还没发过开战且仍有 Battle_info，补开战
                if (
                    self._pending_battle_info is not None
                    and self._pending_battle_info.ret == 0
                    and not self._saw_battle_s2c_start
                ):
                    self.finish_pending_battle()
                else:
                    # 只等结束
                    deadline = time.monotonic() + min(self.battle_timeout, 120.0)
                    while time.monotonic() < deadline and not self._battle_ended:
                        try:
                            header = self._receive_header(deadline, "等待战斗结束")
                        except TreasureFarmError:
                            break
                        if self._handle_common_message(header):
                            continue
                        if header.message_id in BATTLE_ACTIVE_MESSAGE_IDS or (
                            header.message_id == BATTLE_S2C_END_MESSAGE_ID
                        ):
                            self._note_battle_message(header)
                        elif header.message_id == MAP_PROCESSLOC_MESSAGE_ID:
                            result = decode_processloc_response(header.data)
                            self._last_processloc_ret = result.ret
                            if result.loc_updates:
                                self._initial_locs = {
                                    **self._initial_locs,
                                    **result.loc_updates,
                                }
                                break
                continue

            # 非战斗：仅用登录快照与已收消息判断。未知的 ret=2 会由第一
            # 个实际击怪/开箱动作返回并在 process_node 内原位处理。
            phase = self.classify_phase()
            last_status = {
                "phase": phase,
                "phase_label": PHASE_LABELS.get(phase, phase),
                "area_id": area_id,
                "area_name": area_name,
                "battle_subphase": self.battle_subphase(),
                "round": round_i,
                "non_mutating_preflight": True,
            }
            _emit(
                "info",
                f"状态检测：{PHASE_LABELS.get(phase, phase)}（纯状态预检）",
                last_status,
            )

            if phase in (PHASE_ACTIONABLE, PHASE_MAP_IDLE):
                last_status["phase"] = PHASE_ACTIONABLE
                last_status["phase_label"] = PHASE_LABELS[PHASE_ACTIONABLE]
                _emit("success", "已到达可执行状态", last_status)
                return last_status

            if phase == PHASE_INTERACT_BLOCKED:
                _emit(
                    "warning",
                    "登录期交互状态异常，尝试 Stage 就绪信号后重试",
                    last_status,
                )
                self._signal_stage_ready()
                self._collect_post_login_battle_signals(2.0)
                if self._pending_battle_info is not None:
                    continue
                # 仍阻塞则返回当前状态（由上层决定是否重进图）
                if round_i >= max_rounds - 1:
                    return last_status
                time.sleep(0.5)
                continue

            if phase == PHASE_CITY:
                if preferred_area_id > 0:
                    _emit(
                        "info",
                        f"当前在主城，准备进入 {treasure_area_name(preferred_area_id)}",
                        last_status,
                    )
                    return last_status
                return last_status

        return last_status or {
            "phase": self.classify_phase(),
            "phase_label": PHASE_LABELS.get(self.classify_phase(), ""),
            "area_id": self._curarea,
        }

    def verify_can_interact(self, area_id: int) -> int | None:
        """返回已知交互结果，不发送新的 ``Map_processloc``。

        ``Map_processloc`` 不是查询接口。只允许 ``process_node`` 在已有明确
        目标（击怪或开箱）时发送它；预检保留上一真实动作的 ret，未知返回
        ``None``，由下一实际动作获得服务端结果。
        """

        self.login()
        if area_id <= 0:
            area_id = self._curarea
        if area_id <= 0:
            return None

        # 挂起战斗优先：官方进 Stage 会先 tryEnterBattle，再点地标
        if self.battle_subphase() in (PHASE_BATTLE_PREPARE, PHASE_BATTLE_RUNNING):
            self.finish_pending_battle(timeout=min(self.battle_timeout, 90.0))

        return self._last_processloc_ret

    def inspect_status(self, *, probe: bool = False) -> dict[str, object]:
        """读取当前会话状态，不触发地图节点交互。

        可调用的协议来源：
        - 登录 ``Game_data.map``：Dead、pos、curarea、locs
        - ``Map_treasure_info``：大宝箱开启次数
        - 登录窗口 ``Battle_info``：是否有未完成战斗

        ``Map_processloc`` 与 ``Map_exit_area`` 都会改变服务端状态或创建战斗，
        状态查询不支持把它们作为探针。
        """

        if probe:
            raise ValueError("状态查询不支持交互探针：Map_processloc 是正式节点动作")

        # 状态页需要看见 EventModule 的挂起动作，但不能代替玩家关闭奖励/对话。
        # 刷取流程会启用该开关，并仅确认客户端无需选择的官方事件分支。
        previous_auto_event_progress = getattr(self, "_auto_event_progress", True)
        self._auto_event_progress = False
        try:
            return self._inspect_status_read_only()
        finally:
            self._auto_event_progress = previous_auto_event_progress

    def _inspect_status_read_only(self) -> dict[str, object]:
        """实现 ``inspect_status`` 的收包部分；调用方负责关闭事件确认。"""

        self.login()
        snap = self._map_snapshot or MapLoginSnapshot(
            dead=self._map_dead,
            needsoul=False,
            curarea=self._curarea,
            pos_x=self._pos_x,
            pos_y=self._pos_y,
            events=0,
            loc_status=dict(self._initial_locs),
            seat_rands=dict(self._seat_rands),
            game_data_has_battle_blob=False,
        )
        area_id = snap.curarea
        area_label = treasure_area_name(area_id) if area_id else "主城/非区域"
        is_treasure = False
        key_item_id = 0
        key_item_name = ""
        mapgroup = 0
        if area_id > 0:
            try:
                entry = get_treasure_map_entry(area_id)
                is_treasure = True
                key_item_id = entry.key_item_id
                key_item_name = entry.key_item_name
                mapgroup = entry.mapgroup
            except TreasureFarmError:
                is_treasure = False

        loc_counts: dict[str, int] = {}
        for status in snap.loc_status.values():
            label = LOC_STATUS_LABELS.get(int(status), f"状态{status}")
            loc_counts[label] = loc_counts.get(label, 0) + 1

        nodes = load_area_nodes(area_id) if is_treasure else ()
        active_monsters = 0
        active_small = 0
        active_big = 0
        for node in nodes:
            st = snap.loc_status.get(node.nodeid)
            if st not in (LOC_STATUS_ACTIVE, LOC_STATUS_OPEN):
                continue
            if node.kind == NODE_KIND_MONSTER:
                active_monsters += 1
            elif node.kind == NODE_KIND_SMALL_CHEST:
                active_small += 1
            elif node.kind == NODE_KIND_BIG_CHEST:
                active_big += 1

        # 再听一轮战斗包（调用 status 时可能刚好迟到）
        self._collect_post_login_battle_signals(1.0)
        if self._curarea > 0 and self._pending_battle_info is None:
            self._signal_stage_ready()
            self._collect_post_login_battle_signals(2.5)

        pending = self._pending_battle_info
        battle_marker = {
            "present": snap.game_data_has_battle_blob,
            "battle_state": snap.battle_state,
            "battle_type": snap.battle_type,
            "battle_type_name": BATTLE_TYPE_LABELS.get(
                snap.battle_type, f"类型{snap.battle_type}"
            ),
            "active": snap.battle_state != 0,
            "client_data_ready": self._client_data_ready,
            "note": (
                "客户端收到 Client_data_get 响应后，会把此值作为 "
                "client_login_state.battleState 处理"
            ),
        }
        pending_battle: dict[str, object] | None = None
        if pending is not None:
            pending_battle = {
                "battle_id": pending.battle_id,
                "ret": pending.ret,
                "battle_type": pending.battle_type,
                "battle_type_name": BATTLE_TYPE_LABELS.get(
                    pending.battle_type, f"类型{pending.battle_type}"
                ),
                "location_id": pending.location_id,
                "player_units": pending.player_units,
                "enemy_units": pending.enemy_units,
                "skip_team": pending.skip_team,
                "detected_via": "Battle_info",
            }
        battle_signal_names = [
            MESSAGE_NAMES.get(mid, str(mid)) for mid in self._login_battle_message_ids
        ]
        pending_event: dict[str, object] | None = None
        event_action = getattr(self, "_pending_event_action", None)
        event_start = getattr(self, "_pending_event_start", None)
        if isinstance(event_action, EventFuncAction):
            pending_event = {
                "kind": "func_action",
                "kind_name": "地图事件展示/等待动作",
                "label": event_action.label,
                "auto_confirmable": event_action.auto_confirmable,
            }
        elif isinstance(event_start, EventStart):
            pending_event = {
                "kind": "start",
                "kind_name": "地图事件开始",
                "label": (
                    "官方自动选项"
                    if event_start.auto_confirmable
                    else "需玩家选择的事件分支"
                ),
                "auto_confirmable": event_start.auto_confirmable,
            }

        processloc_probe: dict[str, object] | None = None
        exit_probe: dict[str, object] | None = None
        phase_code = self.classify_phase(
            dead=snap.dead,
            area_id=area_id,
        )
        # 细分准备界面
        if (
            phase_code == PHASE_BATTLE_RUNNING
            and pending_battle
            and not pending_battle.get("skip_team")
            and not self._saw_battle_s2c_start
        ):
            phase_code = PHASE_BATTLE_PREPARE
        if (
            pending_battle
            and pending_battle.get("ret") == 0
            and not self._saw_battle_s2c_start
            and not self._saw_battle_frames
        ):
            phase_code = PHASE_BATTLE_PREPARE

        phase = PHASE_LABELS.get(phase_code, phase_code)
        landmark_locked = phase_code == PHASE_LANDMARK_LOCKED
        in_battle_confirmed = phase_code in (
            PHASE_BATTLE_PREPARE,
            PHASE_BATTLE_RUNNING,
        )
        actionable = phase_code == PHASE_ACTIONABLE

        if isinstance(pending_battle, dict):
            pending_battle = {
                **pending_battle,
                "ui_hint": (
                    "战斗准备/布阵界面（skipTeam=false，客户端等开战）"
                    if phase_code == PHASE_BATTLE_PREPARE
                    else (
                        "战斗进行中"
                        if phase_code == PHASE_BATTLE_RUNNING
                        else "已收到Battle_info"
                    )
                ),
            }

        detection_notes = [
            "战斗准备：收到 Battle_info 且尚未 S2C_start（客户端多为布阵界面）",
            "战斗中：已 S2C_start 或收到战斗帧",
            "状态查询只读取登录快照，不发送 Map_processloc 或 Map_exit_area",
            "进图预检只收敛已存在的战斗；首个明确节点动作返回实际交互结果",
        ]
        if battle_marker["active"]:
            detection_notes.insert(
                0,
                "Game_data.field35.battleState 非零：服务端登录快照标记了战斗恢复状态",
            )
        else:
            detection_notes.insert(
                0,
                "Game_data.field35 没有活动战斗标识：ret=2 按地图地标残留路径恢复",
            )
        if landmark_locked and not battle_marker["active"]:
            trigger = snap.move_trigger
            if trigger is None or not trigger.active:
                detection_notes.insert(
                    1,
                    "AreaDetail.mtdata 未激活：ret=2 不是客户端 MoveTrigger 路径",
                )
            if snap.area_locked == 0:
                detection_notes.insert(
                    2,
                    "AreaData.locked=0：ret=2 不对应客户端可见的区域锁字段",
                )
        if pending_event is not None:
            detection_notes.insert(
                0,
                f"地图事件等待：{pending_event['label']}；状态查询仅展示，不发送事件确认",
            )

        return {
            "phase": phase,
            "phase_code": phase_code,
            "actionable": actionable,
            "dead": snap.dead,
            "needsoul": snap.needsoul,
            "in_area": area_id > 0,
            "area_id": area_id,
            "area_name": area_label,
            "is_treasure_map": is_treasure,
            "mapgroup": mapgroup,
            "pos": {"x": snap.pos_x, "y": snap.pos_y},
            "events_counter": snap.events,
            "map_runtime": {
                "events": snap.events,
                "area_state": snap.area_state,
                "area_flag": snap.area_flag,
                "area_locked": snap.area_locked,
                "area_remains": snap.area_remains,
                "move_trigger": (
                    {
                        "max": snap.move_trigger.max,
                        "remain": snap.move_trigger.remain,
                        "area": snap.move_trigger.area,
                        "triggernum": snap.move_trigger.triggernum,
                    }
                    if snap.move_trigger is not None
                    else None
                ),
            },
            "loc_total": len(snap.loc_status),
            "loc_status_counts": loc_counts,
            "active_monsters": active_monsters,
            "active_small_chests": active_small,
            "active_big_chests": active_big,
            "treasure_big_chest_open_times": self._open_times,
            "items": {
                "hearth": {
                    "id": HEARTH_ITEM_ID,
                    "name": item_name(HEARTH_ITEM_ID),
                    "total": self.item_total(HEARTH_ITEM_ID),
                },
                "ticket": {
                    "id": TREASURE_TICKET_ITEM_ID,
                    "name": item_name(TREASURE_TICKET_ITEM_ID),
                    "total": self.item_total(TREASURE_TICKET_ITEM_ID),
                },
                "map_key": {
                    "id": key_item_id,
                    "name": key_item_name or "—",
                    "total": self.item_total(key_item_id) if key_item_id else 0,
                },
            },
            "in_battle_confirmed": in_battle_confirmed,
            "login_battle_marked": phase_code == PHASE_BATTLE_RECOVERY,
            "landmark_locked": landmark_locked,
            "pending_battle": pending_battle,
            "pending_event": pending_event,
            "login_battle_messages": battle_signal_names,
            "game_data_battle_marker": battle_marker,
            "processloc_probe": processloc_probe,
            "exit_probe": exit_probe,
            "has_team": self._battle_context is not None
            and bool(getattr(self._battle_context, "team", ())),
            "detection_notes": detection_notes,
        }

    def _enter_preflight(self) -> None:
        """进图前仅检查阵亡；客户端进聚宝不校验门票（mapareas.costid 为空）。"""

        if self._map_dead:
            raise TreasureFarmError("审判官已阵亡，无法进入聚宝之地（请先复活）")

    def _same_mapgroup(self, area_id: int, other_area_id: int) -> bool:
        if area_id == other_area_id:
            return True
        if other_area_id <= 0:
            return False
        # 主城/剧情图（潮汐之门、流放者之岛等）不是聚宝图，不可与聚宝 mapgroup 比较
        if not is_treasure_map_area(area_id) or not is_treasure_map_area(other_area_id):
            return False
        try:
            return (
                get_treasure_map_entry(area_id).mapgroup
                == get_treasure_map_entry(other_area_id).mapgroup
            )
        except TreasureFarmError:
            return False

    def _leave_non_treasure_area(self, target_area_id: int) -> None:
        """当前不在目标聚宝图时：尽量退回主城，再由调用方重新进图。

        登录落在潮汐之门(9004)、流放者之岛等非聚宝区域时，不得把 curarea 当
        作刷取目标；先 ``Map_exit_area``（失败则清空本地区域态），再进目标图。
        """

        if self._curarea <= 0 or self._curarea == target_area_id:
            return
        if is_treasure_map_area(self._curarea) and self._same_mapgroup(
            target_area_id, self._curarea
        ):
            return
        try:
            self.exit_area()
        except (TreasureFarmError, TreasureFarmRejected):
            # 已在主城或服务端拒绝退出：清空本地态，后续直接 Map_enter_treasure
            self._curarea = 0
            self._initial_locs = {}
            self._seat_rands = {}

    def enter_treasure(self, area_id: int, *, reset: bool = False) -> AreaSession:
        """进入指定聚宝地图；成功后返回区域会话。"""

        self.login()
        entry = get_treasure_map_entry(area_id)
        enterway = ENTERWAY_RESET if reset else ENTERWAY_NORMAL

        # 已在目标图且未要求重置：用登录快照，但必须先走 Stage 就绪/战斗收敛
        # （对照客户端：curarea>0 → PopSceneToStage → SendEvtTriggerMsg → 可能 tryEnterBattle）
        if (
            not reset
            and self._curarea == area_id
            and self._initial_locs
        ):
            self._signal_stage_ready()
            self._collect_post_login_battle_signals(3.0)
            if self.battle_subphase() in (
                PHASE_BATTLE_PREPARE,
                PHASE_BATTLE_RUNNING,
            ):
                self.finish_pending_battle()
            else:
                self.clear_pending_map_activity(timeout=4.0)
            return AreaSession(
                area_id=area_id,
                loc_status=dict(self._initial_locs),
                open_times=self._open_times,
            )
        # 已在同组聚宝图：优先用现有 locs；需要刷新时再重置。
        if (
            not reset
            and self._curarea > 0
            and is_treasure_map_area(self._curarea)
            and self._same_mapgroup(area_id, self._curarea)
        ):
            if self._initial_locs and self._curarea == area_id:
                self._signal_stage_ready()
                self._collect_post_login_battle_signals(3.0)
                if self.battle_subphase() in (
                    PHASE_BATTLE_PREPARE,
                    PHASE_BATTLE_RUNNING,
                ):
                    self.finish_pending_battle()
                return AreaSession(
                    area_id=area_id,
                    loc_status=dict(self._initial_locs),
                    open_times=self._open_times,
                )
            return self.reset_area(area_id)

        # 已在目标图但 locs 为空：强制重置以拿到节点状态。
        if not reset and self._curarea == area_id and not self._initial_locs:
            reset = True
            enterway = ENTERWAY_RESET

        # 当前在非聚宝图（潮汐之门/剧情图）或其它聚宝图：先退回主城再进目标。
        if not reset and self._curarea > 0 and self._curarea != area_id:
            if not is_treasure_map_area(self._curarea):
                self._leave_non_treasure_area(area_id)
            elif not self._same_mapgroup(area_id, self._curarea):
                try:
                    self.exit_area()
                except (TreasureFarmError, TreasureFarmRejected):
                    # 退出失败时仍尝试直接 Map_enter_treasure，由服务端切换
                    pass

        self._enter_preflight()

        self._send_message(
            MAP_ENTER_TREASURE_MESSAGE_ID,
            encode_enter_treasure_request(entry.mapgroup, enterway),
            encrypted=True,
        )
        deadline = time.monotonic() + self.timeout
        enter_ret: int | None = None
        while True:
            header = self._receive_header(deadline, "进入聚宝之地")
            if self._handle_common_message(header):
                continue
            if header.message_id == MAP_ENTER_TREASURE_MESSAGE_ID:
                enter_ret = decode_enter_treasure_response(header.data)
                # 已在当前组：ret=7，改用重置刷新（不耗门票）。
                if enter_ret == 7 and not reset:
                    return self.enter_treasure(area_id, reset=True)
                if enter_ret not in (0, 7):
                    detail = ENTER_TREASURE_RET_LABELS.get(enter_ret, "")
                    if enter_ret == 5:
                        detail = (
                            f"{detail}；也可能是地图未解锁（需完成对应章节解锁条件）。"
                            f"当前{item_name(TREASURE_TICKET_ITEM_ID)} "
                            f"{self.item_total(TREASURE_TICKET_ITEM_ID)}"
                        )
                    raise TreasureFarmRejected("进入", enter_ret, detail=detail)
                continue
            if header.message_id == MAP_ENTER_AREA_MESSAGE_ID:
                ret, entered_id, locs, rands = decode_enter_area_full(header.data)
                if ret != 0:
                    # Reset 时服务端常先推一条失败的 enter_area，再回
                    # Map_enter_treasure.ret；以 treasure 响应为准，勿抢先抛错。
                    if reset or enter_ret is None:
                        continue
                    detail = ENTER_AREA_RET_LABELS.get(ret, "")
                    if ret == 5:
                        detail = (
                            f"{detail}；请确认地图已在大地图解锁（unlockticket 条件）。"
                            f"当前{item_name(TREASURE_TICKET_ITEM_ID)} "
                            f"{self.item_total(TREASURE_TICKET_ITEM_ID)}"
                        )
                    raise TreasureFarmRejected("进入区域", ret, detail=detail)
                if entered_id <= 0:
                    entered_id = area_id
                # 进主城/剧情图等非目标区域：只更新本地 curarea，继续等目标聚宝图
                if entered_id != area_id and not self._same_mapgroup(
                    area_id, entered_id
                ):
                    if not is_treasure_map_area(entered_id):
                        # 潮汐之门(9004) 等中转包：记录位置但不当作刷取成功
                        if entered_id > 0:
                            self._curarea = entered_id
                            if locs:
                                self._initial_locs = dict(locs)
                            if rands:
                                self._seat_rands = dict(rands)
                        continue
                    # 其它聚宝图（不同 mapgroup）：也跳过，等目标组
                    continue
                if not locs:
                    raise TreasureFarmError(
                        f"进入 {treasure_area_name(entered_id or area_id)} 成功，"
                        "但服务端未返回节点状态，无法刷取"
                    )
                self._curarea = entered_id
                self._initial_locs = dict(locs)
                self._seat_rands = dict(rands)
                self._map_dead = False
                # 进图后可能立刻进入战斗准备/战斗中：先收敛到可执行
                self.ensure_actionable(preferred_area_id=entered_id)
                # ensure 过程中可能被推到主城：若已离开目标图则继续等/重进
                if self._curarea > 0 and self._curarea != entered_id:
                    if not is_treasure_map_area(self._curarea) or not self._same_mapgroup(
                        area_id, self._curarea
                    ):
                        continue
                return AreaSession(
                    area_id=entered_id
                    if is_treasure_map_area(entered_id)
                    else (self._curarea if is_treasure_map_area(self._curarea) else area_id),
                    loc_status=dict(self._initial_locs or locs),
                    open_times=self._open_times,
                )
            # 进图途中可能夹杂 Battle_info（切图进准备界面）
            if header.message_id in BATTLE_ACTIVE_MESSAGE_IDS or (
                header.message_id == BATTLE_S2C_END_MESSAGE_ID
            ):
                self._note_battle_message(header)
                continue
            # 忽略其它推送，继续等待进入结果。

    def reset_area(self, area_id: int | None = None) -> AreaSession:
        """刷新聚宝节点：回主城后再正常进图。

        不使用 ``Map_enter_treasure(enterway=Reset)``（易 ret=3/6）或
        ``Map_reset_area``（魂域协议，易误报「道具扣除数量异常」）。
        与客户端「退出关卡会重置聚宝之地」一致：``Map_exit_area`` →
        ``Map_enter_treasure(enterway=Normal)``。

        ``area_id`` 为刷取目标；当 ``_curarea`` 已被推到潮汐之门等非聚宝图时，
        必须用调用方传入的目标 ID，不能把主城 ID 当成聚宝地图。
        """

        self.login()
        target_id = int(area_id or 0)
        if target_id <= 0:
            target_id = int(self._curarea or 0)
        if not is_treasure_map_area(target_id):
            raise TreasureFarmError(
                "当前不在聚宝地图中，无法重置"
                + (
                    f"（所在：{treasure_area_name(self._curarea)}）"
                    if self._curarea > 0
                    else ""
                )
            )
        # 先尽量结束挂起战斗 / 事件，否则 exit 常被拒绝
        if self._curarea > 0 and is_treasure_map_area(self._curarea):
            self.clear_pending_map_activity(timeout=8.0)
            self.finish_pending_battle(timeout=min(self.battle_timeout, 60.0))
            try:
                self.exit_area()
            except TreasureFarmRejected as exc:
                raise TreasureFarmRejected(
                    "重置",
                    exc.ret,
                    detail=(
                        f"无法退出回主城（ret {exc.ret}）："
                        f"{exc.detail or '退出聚宝失败'}。"
                        "请确认图内战斗/事件已结束；或完全退出手机端后重试。"
                    ),
                ) from exc
            except TreasureFarmError as exc:
                raise TreasureFarmError(
                    f"无法退出回主城：{exc}。请确认图内战斗/事件已结束。"
                ) from exc
        elif self._curarea > 0 and not is_treasure_map_area(self._curarea):
            # 已在主城/剧情图：尽量再 exit 一次清状态，失败也直接重进目标
            self._leave_non_treasure_area(target_id)
        try:
            return self.enter_treasure(target_id, reset=False)
        except TreasureFarmRejected as exc:
            detail = ENTER_TREASURE_RET_LABELS.get(exc.ret, exc.detail or "")
            raise TreasureFarmRejected(
                "重置",
                exc.ret,
                detail=(
                    f"已回主城，但再次进入失败：{detail or exc}。"
                    "请确认地图已解锁且无其它客户端占用会话。"
                ),
            ) from exc

    def exit_area(self) -> None:
        self.login()
        self._send_message(MAP_EXIT_AREA_MESSAGE_ID, encrypted=True)
        deadline = time.monotonic() + self.timeout
        while True:
            header = self._receive_header(deadline, "退出区域")
            if self._handle_common_message(header):
                continue
            if header.message_id == MAP_EXIT_AREA_MESSAGE_ID:
                ret = 0
                for field_number, wire_type, value in ProtoReader(header.data).fields():
                    if field_number == 1 and wire_type == 0:
                        ret = decode_int32(int(value))
                        break
                if ret != 0:
                    raise TreasureFarmRejected(
                        "退出区域",
                        ret,
                        detail="退出聚宝失败（可能地图内仍有未结束战斗/地标）",
                    )
                self._curarea = 0
                self._initial_locs = {}
                return
            if header.message_id == MAP_ENTER_AREA_MESSAGE_ID:
                # 退出后可能推送主城 enter
                self._curarea = 0
                self._initial_locs = {}
                return

    def _configure_battle(self) -> None:
        self._send_message(
            BATTLE_C2S_SET_TIMESCALE_MESSAGE_ID,
            encode_battle_timescale(BATTLE_TIMESCALE_X3),
            encrypted=True,
        )
        self._send_message(
            BATTLE_C2S_AUTO_UNIQUE_SKILL_MESSAGE_ID,
            encode_battle_auto(True),
            encrypted=True,
        )
        self._send_message(
            BATTLE_C2S_AUTO_ARTIFACT_SKILL_MESSAGE_ID,
            encode_battle_auto(True),
            encrypted=True,
        )

    def _start_battle(self, battle: BattleInfo) -> None:
        if self._battle_context is None:
            raise TreasureFarmError("缺少 Game_data 编队，无法开始战斗")
        payload = encode_battle_c2s_start(battle, self._battle_context.team)
        self._send_message(BATTLE_C2S_START_MESSAGE_ID, payload, encrypted=True)

    def clear_pending_map_activity(self, *, timeout: float = 12.0) -> bool:
        """消化登录后挂起的战斗 / processloc 结算，解除「已有地标激活」。

        返回是否处理过任何挂起活动。先发 Stage 就绪信号再听包（与客户端进
        Stage 后才推 ``Battle_info`` 一致）。
        """

        self.login()
        if self._curarea > 0:
            self._signal_stage_ready()
        deadline = time.monotonic() + timeout
        battle_started = False
        battle_configured = False
        saw_activity = False
        while time.monotonic() < deadline:
            try:
                header = self._receive_header(
                    min(deadline, time.monotonic() + 1.5),
                    "清理挂起地图活动",
                )
            except TreasureFarmError:
                break
            if self._handle_common_message(header):
                continue
            if header.message_id in BATTLE_ACTIVE_MESSAGE_IDS or (
                header.message_id == BATTLE_S2C_END_MESSAGE_ID
            ):
                self._note_battle_message(header)
                saw_activity = True
                if header.message_id == BATTLE_INFO_MESSAGE_ID:
                    info = self._pending_battle_info
                    if (
                        info is not None
                        and info.ret == 0
                        and not battle_started
                        and not self._saw_battle_s2c_start
                    ):
                        self._start_battle(info)
                        battle_started = True
                        self._configure_battle()
                        battle_configured = True
                elif header.message_id == BATTLE_S2C_START_MESSAGE_ID:
                    start = decode_battle_start_response(header.data)
                    if start.ret == 0 and not battle_configured:
                        self._configure_battle()
                        battle_configured = True
                elif header.message_id == BATTLE_S2C_END_MESSAGE_ID:
                    deadline = max(deadline, time.monotonic() + 3.0)
                continue
            if header.message_id == MAP_PROCESSLOC_MESSAGE_ID:
                result = decode_processloc_response(header.data)
                self._last_processloc_ret = result.ret
                if result.ret == 0 and result.loc_updates:
                    saw_activity = True
                    self._initial_locs = {
                        **self._initial_locs,
                        **result.loc_updates,
                    }
                    deadline = max(deadline, time.monotonic() + 1.0)
                continue
            # 其它业务消息：不中断清理，继续短等
        # 若窗口内已开战，再尽量等到结束
        if battle_started or self._saw_battle_frames or self._saw_battle_s2c_start:
            if not self._battle_ended:
                self.finish_pending_battle(timeout=min(self.battle_timeout, 60.0))
                saw_activity = True
        return saw_activity

    def resume_login_recovery(self, *, timeout: float = 45.0) -> bool:
        """Resume a map-owned login residue before another feature starts.

        This intentionally does not probe a new landmark.  It only consumes
        packets already restored by the server, sends the native stage-ready
        sequence when needed, and advances known no-choice event continuations.
        A remaining dialog is left visible to the shared recovery coordinator.
        """

        self._login_recovery_mode = True
        try:
            self.login()
            phase = self.classify_phase()
            progressed = False
            if phase in (
                PHASE_BATTLE_RECOVERY,
                PHASE_BATTLE_PREPARE,
                PHASE_BATTLE_RUNNING,
                PHASE_LANDMARK_LOCKED,
            ):
                progressed = self.recover_from_landmark_lock(timeout=timeout)
            if self._event_chain_active or self._pending_event_start or self._pending_event_action:
                progressed = (
                    self.clear_pending_map_activity(timeout=min(12.0, timeout))
                    or progressed
                )
            return progressed
        finally:
            self._login_recovery_mode = False

    def has_pending_login_event(self) -> bool:
        """Whether an event still requires a non-automatic player choice."""

        return bool(self._pending_event_start or self._pending_event_action)

    def _move_trigger_ready(self) -> bool:
        trigger = self._move_trigger
        return bool(trigger is not None and trigger.active and trigger.remain == 0)

    def _activate_move_trigger(self) -> bool:
        """Activate a server-advertised move trigger exactly when it is ready."""

        if not self._move_trigger_ready():
            return False
        self._send_message(MAP_MOVETRIGGER_ACTIVE_MESSAGE_ID, encrypted=True)
        return True

    def _move_trigger_state_label(self) -> str:
        trigger = self._move_trigger
        if trigger is None:
            return "mtdata=未下发"
        return (
            f"mtdata(max={trigger.max}, remain={trigger.remain}, "
            f"area={trigger.area}, triggernum={trigger.triggernum})"
        )

    def move_toward_node(self, area_id: int, nodeid: int) -> bool:
        """先 ``Map_move`` 走到节点附近，再交互。成功更新本地坐标。

        优先使用本局 ``rands`` 随机坐标，其次静态 zone-layout。
        """

        target = self._seat_rands.get(nodeid) or load_area_seat_positions(area_id).get(
            nodeid
        )
        if target is None:
            return False
        start = (self._pos_x, self._pos_y)
        if start == (0, 0):
            start = target
        path = build_move_path(start, target, max_steps=16)
        # 逐步发送短路径，提高服务端接受率
        chunks: list[list[tuple[int, int]]] = []
        buf: list[tuple[int, int]] = []
        for pt in path:
            buf.append(pt)
            if len(buf) >= 4:
                chunks.append(buf)
                buf = []
        if buf:
            chunks.append(buf)
        if not chunks:
            chunks = [[target]]

        move_ok = False
        for chunk in chunks:
            self._send_message(
                MAP_MOVE_MESSAGE_ID, encode_map_move(chunk), encrypted=True
            )
            deadline = time.monotonic() + min(self.timeout, 5.0)
            while time.monotonic() < deadline:
                try:
                    header = self._receive_header(deadline, "Map_move")
                except TreasureFarmError:
                    break
                if self._handle_common_message(header):
                    continue
                if header.message_id == MAP_MOVE_MESSAGE_ID:
                    ret = 0
                    px = self._pos_x
                    py = self._pos_y
                    for field_number, wire_type, value in ProtoReader(header.data).fields():
                        if field_number == 1 and wire_type == 0:
                            ret = decode_int32(int(value))
                        elif field_number == 2 and wire_type == 2:
                            for p_fn, p_wt, p_val in ProtoReader(bytes(value)).fields():
                                if p_fn == 1 and p_wt == 0:
                                    px = decode_int32(int(p_val))
                                elif p_fn == 2 and p_wt == 0:
                                    py = decode_int32(int(p_val))
                        elif field_number == 4 and wire_type == 2:
                            self._move_trigger = decode_move_trigger_state(bytes(value))
                    self._pos_x, self._pos_y = px, py
                    if ret == 0:
                        move_ok = True
                    break
                if header.message_id in BATTLE_ACTIVE_MESSAGE_IDS:
                    self._note_battle_message(header)
                    continue
            time.sleep(0.05)

        # 逻辑上落到目标（服务端可能 teleport）
        self._pos_x, self._pos_y = target
        # Native StageActor only sends this after its MoveTriggerData says the
        # route has reached a trigger boundary.  Do not add unrelated 15574
        # packets to a ret=2 landmark-lock recovery.
        if self._activate_move_trigger():
            try:
                drain_deadline = time.monotonic() + 0.6
                while time.monotonic() < drain_deadline:
                    try:
                        header = self._receive_header(drain_deadline, "movetrigger")
                    except TreasureFarmError:
                        break
                    if self._handle_common_message(header):
                        continue
                    if header.message_id in BATTLE_ACTIVE_MESSAGE_IDS:
                        self._note_battle_message(header)
                        continue
                    if header.message_id == MAP_MOVETRIGGER_ACTIVE_MESSAGE_ID:
                        for field_number, wire_type, value in ProtoReader(
                            header.data
                        ).fields():
                            if field_number == 2 and wire_type == 2:
                                self._move_trigger = decode_move_trigger_state(
                                    bytes(value)
                                )
                        break
            except TreasureFarmError:
                pass
        time.sleep(0.1)
        return move_ok or True

    def _emit_workflow_step(
        self,
        emit: Callable[[str, str, dict[str, object]], None] | None,
        step: str,
        message: str | None = None,
        data: Mapping[str, object] | None = None,
    ) -> None:
        if emit is None:
            return
        payload: dict[str, object] = {
            "workflow_step": step,
            "workflow_label": FARM_STEP_LABELS.get(step, step),
        }
        if data:
            payload.update(data)
            raw_nodeid = data.get("nodeid")
            if isinstance(raw_nodeid, int) and not isinstance(raw_nodeid, bool):
                payload.setdefault(
                    "node_name",
                    map_node_name(raw_nodeid, str(data.get("kind") or "")),
                )
        emit(
            "info",
            message or FARM_STEP_LABELS.get(step, step),
            payload,
        )

    def _item_changes_since(
        self,
        before_items: Mapping[int, int],
    ) -> tuple[ItemChange, ...]:
        """Build a stable item delta snapshot from storage notifications."""

        items: list[ItemChange] = []
        current_items = getattr(self, "_item_totals", {})
        for item_id, total in current_items.items():
            prev = before_items.get(item_id, 0)
            if total != prev:
                items.append(ItemChange(item_id=item_id, delta=total - prev, total=total))
        for item_id, prev in before_items.items():
            if item_id not in current_items and prev != 0:
                items.append(ItemChange(item_id=item_id, delta=-prev, total=0))
        return tuple(items)

    def _wait_for_item_reward(
        self,
        item_id: int,
        before_total: int,
        *,
        timeout: float,
        stop_requested: Callable[[], bool] | None = None,
    ) -> int:
        """Wait for a positive inventory delta and finish any reward event.

        ``timeout`` 为 0 时只做一次即时检查，不再空等。
        """

        stop_requested = stop_requested or (lambda: False)
        if stop_requested():
            raise TreasureFarmCancelled("已请求停止，未继续等待奖励")
        if timeout <= 0:
            return max(0, int(self.item_total(item_id)) - int(before_total))
        deadline = time.monotonic() + timeout
        # 短超时用更细的 poll，避免 0.3s 预算被一次 0.5s recv 吃光后还多等
        poll = min(0.2, max(0.05, timeout * 0.5))
        while time.monotonic() < deadline:
            if stop_requested():
                raise TreasureFarmCancelled("已请求停止，结束奖励等待")
            current = self.item_total(item_id)
            event_active = bool(getattr(self, "_event_chain_active", False))
            if current > before_total and not event_active:
                return current - before_total
            # 事件已结束且超时预算将尽：允许无掉落立刻返回（小怪不掉钥匙）
            if not event_active and time.monotonic() + poll >= deadline:
                if current > before_total:
                    return current - before_total
                # 再读一轮可能迟到的包，然后退出
            try:
                header = self._receive_header(
                    min(deadline, time.monotonic() + poll),
                    f"等待{item_name(item_id)}奖励",
                )
            except TreasureFarmCancelled:
                raise
            except TreasureFarmError:
                # A reward notification may already have updated totals while
                # the short socket read timed out.  Re-check before returning.
                current = self.item_total(item_id)
                if current > before_total and not getattr(
                    self, "_event_chain_active", False
                ):
                    return current - before_total
                continue

            if self._handle_common_message(header):
                continue
            if header.message_id == GAME_DATA_MESSAGE_ID:
                self._apply_game_data(header.data, request_client_data=False)
                continue
            if self._record_client_data_ready(header):
                continue
            if header.message_id in BATTLE_ACTIVE_MESSAGE_IDS or (
                header.message_id == BATTLE_S2C_END_MESSAGE_ID
            ):
                self._note_battle_message(header)
                continue
            if header.message_id == MAP_PROCESSLOC_MESSAGE_ID:
                result = decode_processloc_response(header.data)
                self._last_processloc_ret = result.ret
                if result.loc_updates:
                    self._initial_locs = {
                        **getattr(self, "_initial_locs", {}),
                        **result.loc_updates,
                    }
                continue

        return max(0, self.item_total(item_id) - before_total)

    def process_farm_node(
        self,
        area_id: int,
        nodeid: int,
        node_kind: str,
        *,
        emit: Callable[[str, str, dict[str, object]], None] | None = None,
        stop_requested: Callable[[], bool] | None = None,
    ) -> ProcessLocResult:
        """Run one typed farm action and wait for its business reward."""

        if node_kind == NODE_KIND_MONSTER:
            reward_item_id = get_treasure_map_entry(area_id).key_item_id
            if reward_item_id <= 0:
                raise TreasureFarmError("目标聚宝地图未配置地图钥匙物品")
        elif node_kind in (NODE_KIND_SMALL_CHEST, NODE_KIND_BIG_CHEST):
            reward_item_id = HEARTH_ITEM_ID
            self._ensure_chest_keys(area_id, node_kind)
        else:
            raise TreasureFarmError(f"未知聚宝节点类型：{node_kind}")
        return self.process_node(
            area_id,
            nodeid,
            node_kind=node_kind,
            expected_reward_item_id=reward_item_id,
            emit=emit,
            stop_requested=stop_requested,
        )

    def _ensure_chest_keys(self, area_id: int, node_kind: str) -> None:
        """开箱前校验地图钥匙；不足则拒绝，迫使刷取循环先去打怪。"""

        cost = chest_key_cost(node_kind)
        if cost <= 0:
            return
        entry = get_treasure_map_entry(area_id)
        have = int(self.item_total(entry.key_item_id))
        if have < cost:
            kind_label = (
                "大宝箱" if node_kind == NODE_KIND_BIG_CHEST else "普通宝箱"
            )
            raise TreasureFarmError(
                f"钥匙不足，无法开启{kind_label}"
                f"（需要 {cost}，当前 {have} {entry.key_item_name}）；"
                "请先击杀小怪获得钥匙"
            )

    def process_node(
        self,
        area_id: int,
        nodeid: int,
        *,
        node_kind: str | None = None,
        expected_reward_item_id: int = 0,
        emit: Callable[[str, str, dict[str, object]], None] | None = None,
        stop_requested: Callable[[], bool] | None = None,
    ) -> ProcessLocResult:
        """处理地图节点（战斗或开箱），并收集期间物品变动。

        服务端语义（与客户端 ``OnMapProcessloc`` 一致）：
        - ``ret=0`` 且 ``locchanges`` 非空：节点真正结算完成；
        - ``ret=0`` 且 ``locchanges`` 为空：可能只是中间态，战斗尚未结束，
          必须继续等待，不能当作成功；
        - ``ret=2``：已有地标激活（通常上一战未结束），战斗中应继续等，
          未开战则视为拒绝。

        直接 ``Map_processloc``，不发 ``Map_move``（服务端按 nodeid/areaid 结算，
        不强制要求客户端先走到格子）。
        """

        stop_requested = stop_requested or (lambda: False)
        if stop_requested():
            raise TreasureFarmCancelled("已请求停止，未开始节点交互")
        self.login()
        if expected_reward_item_id <= 0:
            if node_kind == NODE_KIND_MONSTER:
                expected_reward_item_id = get_treasure_map_entry(area_id).key_item_id
                if expected_reward_item_id <= 0:
                    raise TreasureFarmError("目标聚宝地图未配置地图钥匙物品")
            elif node_kind in (NODE_KIND_SMALL_CHEST, NODE_KIND_BIG_CHEST):
                expected_reward_item_id = HEARTH_ITEM_ID
        node_name = map_node_name(nodeid, node_kind or "")
        before_items = dict(self._item_totals)
        active_key_item_id = 0
        if node_kind in (NODE_KIND_MONSTER, NODE_KIND_SMALL_CHEST, NODE_KIND_BIG_CHEST):
            active_key_item_id = get_treasure_map_entry(area_id).key_item_id
        self._active_event_context = {
            "area_id": area_id,
            "nodeid": nodeid,
            "node_kind": node_kind or "",
            "key_item_id": active_key_item_id,
        }
        if node_kind == NODE_KIND_MONSTER:
            # Do not carry the outcome of a previous node into an instant
            # (flag=1) victory that has no Battle_info handshake.
            self._battle_won = None
            self._battle_result_code = None
        if node_kind == NODE_KIND_MONSTER:
            self._emit_workflow_step(
                emit,
                FARM_STEP_MONSTER_INTERACT,
                "怪物交互",
                {
                    "nodeid": nodeid,
                    "area_id": area_id,
                    "area_name": treasure_area_name(area_id),
                    "kind": node_kind,
                },
            )
        elif node_kind in (NODE_KIND_SMALL_CHEST, NODE_KIND_BIG_CHEST):
            # 双保险：process_farm_node 已检；直接调 process_node 时同样拦截
            self._ensure_chest_keys(area_id, node_kind)
            self._emit_workflow_step(
                emit,
                FARM_STEP_CHEST_INTERACT,
                "宝箱交互",
                {
                    "nodeid": nodeid,
                    "area_id": area_id,
                    "area_name": treasure_area_name(area_id),
                    "kind": node_kind,
                },
            )
        # 直接 processloc 开箱/点怪，不先 Map_move
        resent_after_clear = False
        move_trigger_retries = 0
        self._send_message(
            MAP_PROCESSLOC_MESSAGE_ID,
            encode_processloc_request(nodeid, area_id),
            encrypted=True,
        )
        is_chest = node_kind in (NODE_KIND_SMALL_CHEST, NODE_KIND_BIG_CHEST)
        # 开箱不走战斗，不应用 180s 战斗超时；避免事件未确认时空等到整轮超时
        node_timeout = (
            min(float(self.battle_timeout), 45.0)
            if is_chest
            else float(self.battle_timeout)
        )
        deadline = time.monotonic() + node_timeout
        last_progress_at = time.monotonic()
        chest_stall_limit = 12.0
        battle_started = False
        battle_configured = False
        battle_ended = False
        victory_emitted = False
        enter_emitted = False
        final_result: ProcessLocResult | None = None
        saw_empty_processloc = False

        def _mark_progress() -> None:
            nonlocal last_progress_at
            last_progress_at = time.monotonic()

        def _try_confirm_pending_chest() -> None:
            """开箱 Event_start 未确认时主动再选「打开宝箱」，钥匙不足则快速失败。"""

            if not is_chest or not self._auto_event_progress:
                return
            pending = getattr(self, "_pending_event_start", None)
            if not isinstance(pending, EventStart):
                return
            if pending.auto_option >= 100:
                return
            map_entry = get_treasure_map_entry(area_id)
            min_keys = chest_key_cost(node_kind or NODE_KIND_SMALL_CHEST) or SMALL_CHEST_KEY_COST
            chosen = pending.choose_open_chest_option(
                preferred_item_ids=getattr(
                    self, "_preferred_event_item_ids", frozenset()
                )
                | frozenset(
                    {map_entry.key_item_id} if map_entry.key_item_id > 0 else set()
                ),
                item_totals=getattr(self, "_item_totals", {}),
                min_keys=min_keys,
            )
            if chosen is None:
                # 有明确开箱/离开选项但钥匙不够：勿空等 45s
                openish = [
                    opt
                    for opt in pending.options
                    if opt.exit == 0
                    and (
                        opt.is_open_chest_title
                        or opt.is_item_cost_option
                    )
                ]
                if openish:
                    have = int(self.item_total(map_entry.key_item_id))
                    raise TreasureFarmError(
                        f"钥匙不足，无法确认打开宝箱"
                        f"（需要 {min_keys}，当前 {have} {map_entry.key_item_name}）；"
                        "请先击杀小怪获得钥匙"
                    )
                return
            self._send_message(
                EVENT_OPTION_MESSAGE_ID,
                encode_event_option(
                    chosen.optidx,
                    pending.dialog_id,
                    pending.event_id,
                    itemcommit=chosen.icommit,
                ),
                encrypted=True,
            )
            self._pending_event_start = None
            self._last_confirmed_key_option = chosen
            cost_name = (
                item_name(chosen.use)
                if chosen.use
                else map_entry.key_item_name
            )
            self._note_event_progress(
                "确定用钥匙打开宝箱已确认"
                f"（{cost_name}×{chosen.use_num or min_keys}，opt={chosen.optidx}）"
            )
            _mark_progress()

        while True:
            if stop_requested():
                raise TreasureFarmCancelled("已请求停止，结束节点等待")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                pending = getattr(self, "_pending_event_start", None)
                pending_hint = ""
                if isinstance(pending, EventStart):
                    titles = "、".join(
                        opt.title or f"opt{opt.optidx}" for opt in pending.options
                    ) or "无选项"
                    pending_hint = f"；挂起事件选项=[{titles}]"
                raise TreasureFarmError(
                    f"处理{node_name}超时（{treasure_area_name(area_id)}）"
                    f"：战斗已开始={battle_started}，战斗已结束={battle_ended}"
                    f"，已收到空结算={saw_empty_processloc}"
                    f"，事件链={bool(getattr(self, '_event_chain_active', False))}"
                    f"{pending_hint}"
                )
            # 开箱：超过 stall 仍无 locchanges，主动诊断/再确认，避免空转
            if (
                is_chest
                and final_result is None
                and time.monotonic() - last_progress_at >= chest_stall_limit
            ):
                _try_confirm_pending_chest()
                if final_result is None and time.monotonic() - last_progress_at >= chest_stall_limit:
                    pending = getattr(self, "_pending_event_start", None)
                    titles = ""
                    if isinstance(pending, EventStart):
                        titles = "、".join(
                            opt.title or f"opt{opt.optidx}" for opt in pending.options
                        )
                    raise TreasureFarmError(
                        f"处理{node_name}卡住（{treasure_area_name(area_id)}）："
                        f"{chest_stall_limit:.0f}s 内未完成开箱结算"
                        f"（空结算={saw_empty_processloc}，事件={titles or '无'}）。"
                        "常见原因：未确认「打开宝箱」、钥匙不足或地标仍被占用"
                    )
            try:
                header = self._receive_header(
                    time.monotonic() + min(remaining, 5.0),
                    f"处理{node_name}",
                )
            except TreasureFarmError:
                # A monster may report its location change before the battle
                # end packet.  Keep listening in that case; otherwise a
                # short socket timeout would turn an in-flight win into a
                # false "未确认战斗胜利" failure.
                waiting_for_battle_end = (
                    node_kind == NODE_KIND_MONSTER
                    and battle_started
                    and not battle_ended
                    and final_result is not None
                    and bool(final_result.loc_updates)
                )
                # After victory the server still sends「带走钥匙」Event_start and
                # then locchanges / item rewards.  Do not treat idle sockets as
                # failure while that chain is active.
                waiting_after_monster_victory = (
                    node_kind == NODE_KIND_MONSTER
                    and battle_ended
                    and (
                        final_result is None
                        or not final_result.loc_updates
                        or getattr(self, "_event_chain_active", False)
                        or getattr(self, "_pending_event_start", None) is not None
                    )
                )
                waiting_for_key_confirm = (
                    is_chest
                    and getattr(self, "_event_chain_active", False)
                    and final_result is None
                )
                if waiting_for_key_confirm:
                    _try_confirm_pending_chest()
                if (
                    final_result is not None
                    and final_result.loc_updates
                    and not waiting_for_battle_end
                    and not waiting_after_monster_victory
                ):
                    break
                if (
                    waiting_for_battle_end
                    or waiting_for_key_confirm
                    or waiting_after_monster_victory
                ):
                    continue
                pending_start = getattr(self, "_pending_event_start", None)
                if (
                    isinstance(pending_start, EventStart)
                    and not pending_start.auto_confirmable
                ):
                    titles = "、".join(
                        opt.title or f"opt{opt.optidx}" for opt in pending_start.options
                    ) or "无选项"
                    raise TreasureFarmError(
                        f"{node_name}卡在事件确认：服务端下发了需手动选择的地图事件"
                        f"（{titles}），脚本无法安全猜测分支"
                    ) from None
                raise
            if self._handle_common_message(header):
                _mark_progress()
                # Emit chest key-confirm workflow when open-chest cost option fired.
                confirmed = getattr(self, "_last_confirmed_key_option", None)
                if (
                    confirmed is not None
                    and node_kind in (NODE_KIND_SMALL_CHEST, NODE_KIND_BIG_CHEST)
                ):
                    self._last_confirmed_key_option = None
                    self._emit_workflow_step(
                        emit,
                        FARM_STEP_CHEST_KEY_CONFIRM,
                        "确定用钥匙打开宝箱",
                        {
                            "nodeid": nodeid,
                            "kind": node_kind or "",
                            "item_id": confirmed.use,
                            "item_name": item_name(confirmed.use)
                            if confirmed.use
                            else "",
                            "cost": confirmed.use_num,
                            "optidx": confirmed.optidx,
                            "title": confirmed.title,
                        },
                    )
                # 开箱：common 处理后若仍挂起 Event_start，立即再尝试确认
                if is_chest and final_result is None:
                    _try_confirm_pending_chest()
                continue
            if header.message_id == BATTLE_INFO_MESSAGE_ID:
                self._note_battle_message(header)
                info = decode_battle_info(header.data)
                if info.ret != 0:
                    raise TreasureFarmError(f"战斗信息失败：ret={info.ret}")
                if not info.skip_team and not battle_started:
                    self._emit_workflow_step(
                        emit,
                        FARM_STEP_BATTLE_PREPARE,
                        "战斗准备",
                        {
                            "nodeid": nodeid,
                            "kind": node_kind or "",
                            "automatic": False,
                        },
                    )
                if not battle_started:
                    self._start_battle(info)
                    battle_started = True
                    self._emit_workflow_step(
                        emit,
                        FARM_STEP_BATTLE_ENTER,
                        "进入战斗",
                        {
                            "nodeid": nodeid,
                            "kind": node_kind or "",
                            "automatic": bool(info.skip_team),
                        },
                    )
                    enter_emitted = True
                continue
            if header.message_id == BATTLE_S2C_START_MESSAGE_ID:
                self._note_battle_message(header)
                start = decode_battle_start_response(header.data)
                if start.ret != 0:
                    raise TreasureFarmError(f"战斗开始失败：ret={start.ret}")
                if not enter_emitted:
                    self._emit_workflow_step(
                        emit,
                        FARM_STEP_BATTLE_ENTER,
                        "进入战斗",
                        {
                            "nodeid": nodeid,
                            "kind": node_kind or "",
                            "automatic": True,
                        },
                    )
                    enter_emitted = True
                battle_started = True
                if not battle_configured:
                    self._configure_battle()
                    battle_configured = True
                continue
            if header.message_id == GAME_DATA_MESSAGE_ID:
                self._apply_game_data(header.data, request_client_data=False)
                continue
            if header.message_id == BATTLE_S2C_END_MESSAGE_ID:
                self._note_battle_message(header)
                outcome = decode_treasure_battle_end(header.data)
                if not outcome.win:
                    raise TreasureFarmError(format_battle_not_won_error(outcome))
                battle_ended = True
                if node_kind == NODE_KIND_MONSTER and not victory_emitted:
                    victory_emitted = True
                    self._emit_workflow_step(
                        emit,
                        FARM_STEP_BATTLE_VICTORY,
                        "战斗胜利",
                        {
                            "nodeid": nodeid,
                            "kind": node_kind or "",
                            "result_code": outcome.result_code,
                            "round": outcome.round_number,
                        },
                    )
                continue
            if header.message_id in (
                BATTLE_S2C_FRAME_BROADCAST_MESSAGE_ID,
                BATTLE_S2C_FRAME_HASH_VERIFY_MESSAGE_ID,
                BATTLE_C2S_START_MESSAGE_ID,
            ):
                continue
            if header.message_id == MAP_PROCESSLOC_MESSAGE_ID:
                process_result = decode_processloc_response(header.data)
                self._last_processloc_ret = process_result.ret
                _mark_progress()
                if process_result.ret == 2:
                    # 已有地标激活：多为未结算地图战（客户端进游戏直接战斗 UI）。
                    if battle_started or battle_ended:
                        continue
                    if not resent_after_clear:
                        # 先续战，禁止立刻再点宝箱；把恢复进度打到 UI，避免卡在「怪物交互」无反馈
                        if emit is not None:
                            emit(
                                "warning",
                                (
                                    f"{node_name} processloc ret=2（已有地标激活），"
                                    "先尝试结算挂起战斗/事件"
                                ),
                                {
                                    "nodeid": nodeid,
                                    "area_id": area_id,
                                    "kind": node_kind or "",
                                    "processloc_ret": 2,
                                },
                            )
                        self.recover_from_landmark_lock(
                            timeout=30.0,
                            emit=emit,
                            stop_requested=stop_requested,
                        )
                        if self.finish_pending_battle(
                            timeout=min(self.battle_timeout, 90.0),
                            stop_requested=stop_requested,
                        ):
                            battle_started = True
                        resent_after_clear = True
                        self._send_message(
                            MAP_PROCESSLOC_MESSAGE_ID,
                            encode_processloc_request(nodeid, area_id),
                            encrypted=True,
                        )
                        deadline = time.monotonic() + node_timeout
                        _mark_progress()
                        continue
                    raise TreasureFarmRejected("处理节点", process_result.ret)
                if process_result.ret == 60:
                    # 仅在服务端下发且已就绪的 mtdata 上激活一次，避免把 15574
                    # 当成 ret=60/ret=2 的通用恢复包。
                    if move_trigger_retries >= MAX_MOVETRIGGER_RETRIES_PER_ACTION:
                        raise TreasureFarmError(
                            f"{node_name}处理节点连续 ret=60，移动触发重试已达上限"
                        )
                    if not self._activate_move_trigger():
                        raise TreasureFarmError(
                            f"{node_name}处理节点 ret=60，但无可激活移动触发器"
                            f"（{self._move_trigger_state_label()}）"
                        )
                    move_trigger_retries += 1
                    time.sleep(0.2)
                    self._send_message(
                        MAP_PROCESSLOC_MESSAGE_ID,
                        encode_processloc_request(nodeid, area_id),
                        encrypted=True,
                    )
                    _mark_progress()
                    continue
                if process_result.ret != 0:
                    raise TreasureFarmRejected("处理节点", process_result.ret)
                # 开箱常见：先一条 ret=0 空 locchanges，再 Event 确认后才有 loc 结算
                if not process_result.loc_updates:
                    saw_empty_processloc = True
                    if is_chest:
                        _try_confirm_pending_chest()
                    continue
                # 只有 locchanges 非空才算节点真正完成（客户端同样判断）。
                if process_result.loc_updates:
                    final_result = process_result
                    if node_kind in (NODE_KIND_SMALL_CHEST, NODE_KIND_BIG_CHEST):
                        self._emit_workflow_step(
                            emit,
                            FARM_STEP_CHEST_OPEN,
                            "打开宝箱",
                            {"nodeid": nodeid, "kind": node_kind or ""},
                        )
                    # 秒杀或已结束战斗：可再等极短窗口收物品通知。
                    if process_result.flag == 1 or battle_ended or not battle_started:
                        # 非战斗开箱 / 秒杀：再捞一次可能迟到的物品包（不宜超过 0.3s）
                        drain_deadline = time.monotonic() + LOC_UPDATE_DRAIN_S
                        while time.monotonic() < drain_deadline:
                            if stop_requested():
                                raise TreasureFarmCancelled(
                                    "已请求停止，结束掉落收尾等待"
                                )
                            try:
                                extra = self._receive_header(
                                    time.monotonic() + min(0.15, LOC_UPDATE_DRAIN_S),
                                    f"{node_name}掉落收尾",
                                )
                            except TreasureFarmError:
                                break
                            if self._handle_common_message(extra):
                                continue
                            if extra.message_id == MAP_PROCESSLOC_MESSAGE_ID:
                                more = decode_processloc_response(extra.data)
                                if more.ret == 0 and more.loc_updates:
                                    merged = dict(final_result.loc_updates)
                                    merged.update(more.loc_updates)
                                    final_result = ProcessLocResult(
                                        ret=0,
                                        loc_updates=merged,
                                        flag=more.flag or final_result.flag,
                                        items=(),
                                    )
                                continue
                            if extra.message_id == BATTLE_INFO_MESSAGE_ID:
                                # 链式战斗：继续主循环
                                self._note_battle_message(extra)
                                info = decode_battle_info(extra.data)
                                if info.ret == 0 and not battle_started:
                                    self._start_battle(info)
                                    battle_started = True
                                    final_result = None
                                break
                            if extra.message_id == BATTLE_S2C_END_MESSAGE_ID:
                                self._note_battle_message(extra)
                                outcome = decode_treasure_battle_end(extra.data)
                                if not outcome.win:
                                    raise TreasureFarmError(
                                        format_battle_not_won_error(outcome)
                                    )
                                battle_ended = True
                                if node_kind == NODE_KIND_MONSTER and not victory_emitted:
                                    victory_emitted = True
                                    self._emit_workflow_step(
                                        emit,
                                        FARM_STEP_BATTLE_VICTORY,
                                        "战斗胜利",
                                        {
                                            "nodeid": nodeid,
                                            "kind": node_kind or "",
                                            "result_code": outcome.result_code,
                                            "round": outcome.round_number,
                                        },
                                    )
                                continue
                            if extra.message_id in BATTLE_ACTIVE_MESSAGE_IDS:
                                self._note_battle_message(extra)
                                continue
                            # 其它消息：放回不了，尽量用 common 已处理
                        if final_result is not None:
                            break
                    # 战斗已开但未结束：继续等，直到 battle end + 有 loc 更新
                    continue
                # ret=0 空 locchanges：中间态，继续等 Battle_info / 最终结算
                continue
            # 其它消息忽略

        if final_result is None or not final_result.loc_updates:
            raise TreasureFarmError(
                f"{node_name}未收到有效结算（locchanges 为空）"
            )

        battle_won: bool | None = None
        if node_kind == NODE_KIND_MONSTER:
            battle_won = getattr(self, "_battle_won", None)
            if battle_won is None:
                battle_won = final_result.battle_won
            if battle_won is None and final_result.flag == 1:
                # ``flag=1`` is the server's instant-victory path.
                battle_won = True
            if battle_won is not True:
                raise TreasureFarmError("怪物节点已结算，但未确认战斗胜利")
            if not victory_emitted:
                victory_emitted = True
                self._emit_workflow_step(
                    emit,
                    FARM_STEP_BATTLE_VICTORY,
                    "战斗胜利",
                    {
                        "nodeid": nodeid,
                        "kind": node_kind or "",
                        "automatic": final_result.flag == 1,
                    },
                )
            self._emit_workflow_step(
                emit,
                FARM_STEP_KEY_TAKE,
                "带走钥匙",
                {"nodeid": nodeid, "kind": node_kind or ""},
            )

        # 汇总本节点期间物品变化（相对 before）。
        items = self._item_changes_since(before_items)
        reward_delta = sum(
            change.delta for change in items if change.item_id == expected_reward_item_id
        )
        event_active = bool(getattr(self, "_event_chain_active", False))
        if expected_reward_item_id > 0 and (reward_delta <= 0 or event_active):
            wait_s = reward_wait_timeout(
                node_kind=node_kind,
                reward_delta=reward_delta,
                event_active=event_active,
                battle_won=battle_won,
                has_loc_updates=bool(final_result.loc_updates),
            )
            if wait_s > 0:
                waited_reward_delta = self._wait_for_item_reward(
                    expected_reward_item_id,
                    before_items.get(expected_reward_item_id, 0),
                    timeout=wait_s,
                    stop_requested=stop_requested,
                )
                reward_delta = max(reward_delta, waited_reward_delta)
            items = self._item_changes_since(before_items)
            if reward_delta <= 0:
                reward_delta = sum(
                    change.delta
                    for change in items
                    if change.item_id == expected_reward_item_id
                )
            event_active = bool(getattr(self, "_event_chain_active", False))
            # Monster LOCEVT scripts run「判断是否获得钥匙」; the server may
            # skip「带走钥匙」and grant nothing.  Node is still PASSED — do
            # not hard-fail so the farm can fight the next monster.
            monster_settled_without_key = (
                node_kind == NODE_KIND_MONSTER
                and battle_won is True
                and bool(final_result.loc_updates)
                and reward_delta <= 0
                and not event_active
            )
            if reward_delta <= 0 and not monster_settled_without_key:
                raise TreasureFarmError(
                    f"{node_name}已结算，但未收到{item_name(expected_reward_item_id)}奖励"
                )
            if event_active and reward_delta > 0:
                # 掉落已到但事件未结束：再短等 Event_end，避免整轮卡死 8s
                self._wait_for_item_reward(
                    expected_reward_item_id,
                    before_items.get(expected_reward_item_id, 0),
                    timeout=REWARD_WAIT_EVENT_ACTIVE_S,
                    stop_requested=stop_requested,
                )
                event_active = bool(getattr(self, "_event_chain_active", False))
                if event_active:
                    raise TreasureFarmError(
                        f"{node_name}已收到{item_name(expected_reward_item_id)}，"
                        "但奖励事件尚未结束"
                    )
            if reward_delta > 0 and node_kind == NODE_KIND_MONSTER:
                self._emit_workflow_step(
                    emit,
                    FARM_STEP_KEY_REWARD,
                    "获得钥匙奖励",
                    {
                        "nodeid": nodeid,
                        "kind": node_kind or "",
                        "item_id": expected_reward_item_id,
                        "item_name": item_name(expected_reward_item_id),
                        "delta": reward_delta,
                    },
                )
            elif reward_delta > 0 and node_kind in (
                NODE_KIND_SMALL_CHEST,
                NODE_KIND_BIG_CHEST,
            ):
                self._emit_workflow_step(
                    emit,
                    FARM_STEP_CHEST_REWARD,
                    "获得炉温奖励",
                    {
                        "nodeid": nodeid,
                        "kind": node_kind or "",
                        "item_id": expected_reward_item_id,
                        "item_name": item_name(expected_reward_item_id),
                        "delta": reward_delta,
                    },
                )
            elif monster_settled_without_key:
                self._note_event_progress(
                    "判断是否获得钥匙：本场未掉落钥匙（节点已结算）"
                )
        elif expected_reward_item_id > 0:
            if node_kind == NODE_KIND_MONSTER:
                self._emit_workflow_step(
                    emit,
                    FARM_STEP_KEY_REWARD,
                    "获得钥匙奖励",
                    {
                        "nodeid": nodeid,
                        "kind": node_kind or "",
                        "item_id": expected_reward_item_id,
                        "item_name": item_name(expected_reward_item_id),
                        "delta": reward_delta,
                    },
                )
            elif node_kind in (NODE_KIND_SMALL_CHEST, NODE_KIND_BIG_CHEST):
                self._emit_workflow_step(
                    emit,
                    FARM_STEP_CHEST_REWARD,
                    "获得炉温奖励",
                    {
                        "nodeid": nodeid,
                        "kind": node_kind or "",
                        "item_id": expected_reward_item_id,
                        "item_name": item_name(expected_reward_item_id),
                        "delta": reward_delta,
                    },
                )

        result = ProcessLocResult(
            ret=final_result.ret,
            loc_updates=final_result.loc_updates,
            flag=final_result.flag,
            items=tuple(items),
            reward_item_id=expected_reward_item_id,
            reward_delta=reward_delta,
            battle_won=battle_won if node_kind == NODE_KIND_MONSTER else None,
        )
        self._active_event_context = None
        return result


def run_treasure_farm(
    client: TreasureFarmClient,
    area_id: int,
    target_hearth: int,
    *,
    emit: Callable[[str, str, dict[str, object]], None] | None = None,
    stop_requested: Callable[[], bool] | None = None,
) -> FarmProgress:
    """在指定聚宝地图刷取宝箱，直到炉温增量达到目标或无法继续。"""

    if not isinstance(target_hearth, int) or isinstance(target_hearth, bool) or target_hearth <= 0:
        raise TreasureFarmError("目标炉温必须是正整数")
    if target_hearth > 100_000:
        raise TreasureFarmError("目标炉温过大")

    target_area_id = area_id
    entry = get_treasure_map_entry(area_id)
    nodes = load_area_nodes(area_id)
    if not nodes:
        raise TreasureFarmError(f"{entry.name} 缺少地图节点配置")

    client.login()
    hearth_before = client.item_total(HEARTH_ITEM_ID)
    keys = client.item_total(entry.key_item_id)
    # Keep a small logical inventory mirror.  Real sessions update it through
    # STORAGE_ITEM_CHANGE; test/delayed sessions may only expose the delta on
    # ProcessLocResult.items, which is still enough to drive the next action.
    tracked_totals: dict[int, int] = {}
    last_actual_totals: dict[int, int] = {}
    runtime_state: dict[str, str] = {
        "last_transition": "准备进入聚宝地图",
        "last_reset_reason": "",
    }

    emit = emit or (lambda _level, _message, _data: None)
    stop_requested = stop_requested or (lambda: False)

    def supports_stop_requested(callback: Callable[..., object]) -> bool:
        """Keep lightweight test clients compatible with cooperative stopping."""

        try:
            parameters = inspect.signature(callback).parameters
        except (TypeError, ValueError):
            return False
        return "stop_requested" in parameters or any(
            parameter.kind == inspect.Parameter.VAR_KEYWORD
            for parameter in parameters.values()
        )

    def emit_event_progress() -> None:
        """把 EventModule 的无选择确认链写入任务日志。"""

        drain = getattr(client, "drain_event_progress_notes", None)
        if not callable(drain):
            return
        for message in drain():
            emit("info", str(message), {"event": {"state": str(message)}})

    def bind_session(next_session: AreaSession, *, reason: str = "") -> AreaSession:
        """采纳服务端区域：仅接受聚宝图；中转/剧情图则回主城重进目标。"""

        nonlocal area_id, entry, nodes, keys
        sid = int(next_session.area_id or 0)
        if is_treasure_map_area(sid):
            runtime_state["last_transition"] = reason or "同步聚宝地图状态"
            if sid != area_id:
                area_id = sid
                entry = get_treasure_map_entry(area_id)
                nodes = load_area_nodes(area_id)
                keys = client.item_total(entry.key_item_id)
                tracked_totals[entry.key_item_id] = keys
                last_actual_totals[entry.key_item_id] = keys
            return next_session
        where = treasure_area_name(sid) if sid > 0 else "主城/未知区域"
        runtime_state["last_transition"] = (
            f"落入非聚宝图：{where}"
            + (f"（{reason}）" if reason else "")
        )
        emit(
            "warning",
            (
                f"当前位于非聚宝地图「{where}」"
                + (f"（{reason}）" if reason else "")
                + f"，返回主城后重新进入{entry.name}"
            ),
            {
                "current_area_id": sid,
                "target_area_id": target_area_id,
            },
        )
        reentered = client.enter_treasure(target_area_id)
        area_id = (
            reentered.area_id
            if is_treasure_map_area(reentered.area_id)
            else target_area_id
        )
        entry = get_treasure_map_entry(area_id)
        nodes = load_area_nodes(area_id)
        keys = client.item_total(entry.key_item_id)
        tracked_totals[entry.key_item_id] = keys
        last_actual_totals[entry.key_item_id] = keys
        if not is_treasure_map_area(reentered.area_id):
            raise TreasureFarmError(
                f"重进{entry.name}失败，仍停留在{treasure_area_name(reentered.area_id)}"
            )
        runtime_state["last_transition"] = f"已重进{entry.name}"
        return reentered

    emit_event_progress()

    session = bind_session(client.enter_treasure(target_area_id), reason="进图")
    emit_event_progress()

    if not session.loc_status:
        raise TreasureFarmError(
            f"{entry.name} 未返回可交互节点状态，请退出地图后重试"
        )

    # ── 非交互预检 ───────────────────────────────────────────────────
    # Map_processloc 是正式点地标动作。进图前只收敛已知 Battle_info，
    # 不再向 ACTIVE 怪物发送所谓“门禁探针”。
    ensure = getattr(client, "ensure_actionable", None)
    if callable(ensure):
        ready = ensure(
            preferred_area_id=area_id,
            emit=lambda level, message, data: emit(level, message, data),
        )
        emit(
            "info",
            f"进图后状态：{ready.get('phase_label', ready.get('phase', ''))}",
            {"session": ready, "farm": {}},
        )
        emit_event_progress()
        cur = int(getattr(client, "_curarea", session.area_id) or 0)
        initial = getattr(client, "_initial_locs", None)
        if cur > 0 and not is_treasure_map_area(cur):
            session = bind_session(
                AreaSession(
                    area_id=cur,
                    loc_status=dict(initial or {}),
                    open_times=client.open_times(),
                ),
                reason="预检后落到中转/剧情图",
            )
        elif cur != session.area_id and cur != area_id and cur > 0:
            emit("info", "战斗结算后离开目标图，重新进入", {})
            session = bind_session(
                client.enter_treasure(target_area_id),
                reason="预检后重进",
            )
        elif initial:
            adopt_id = cur if is_treasure_map_area(cur) else area_id
            session = AreaSession(
                area_id=adopt_id,
                loc_status=dict(initial),
                open_times=client.open_times(),
            )
            if adopt_id != area_id and is_treasure_map_area(adopt_id):
                session = bind_session(session, reason="预检同步区域")

    clear_fn = getattr(client, "clear_pending_map_activity", None)
    if callable(clear_fn) and clear_fn(timeout=4.0):
        emit("info", "已清理服务端挂起的战斗/地标结算", {})
    emit_event_progress()

    monsters_killed = 0
    settled_monsters = 0
    no_key_monsters = 0
    missing_hearth_chests = 0
    small_opened = 0
    big_opened = 0
    stalled_rounds = 0
    workflow_state: dict[str, object] = {
        "phase": FARM_STEP_IDLE,
        "nodeid": 0,
        "kind": "",
        "reward_item_id": 0,
        "reward_delta": 0,
    }

    def inventory_total(item_id: int) -> int:
        actual = int(client.item_total(item_id))
        previous_actual = last_actual_totals.get(item_id)
        if previous_actual is None or actual != previous_actual:
            last_actual_totals[item_id] = actual
            tracked_totals[item_id] = actual
        return tracked_totals.get(item_id, actual)

    def bind_key_inventory() -> None:
        current = int(client.item_total(entry.key_item_id))
        tracked_totals[entry.key_item_id] = current
        last_actual_totals[entry.key_item_id] = current

    def reset_session(reason: str) -> AreaSession:
        """Return to town and re-enter the target map with a visible reason."""

        runtime_state["last_reset_reason"] = reason
        runtime_state["last_transition"] = reason
        next_session = bind_session(
            client.reset_area(target_area_id),
            reason=reason,
        )
        return next_session

    tracked_totals[HEARTH_ITEM_ID] = hearth_before
    last_actual_totals[HEARTH_ITEM_ID] = hearth_before
    tracked_totals[entry.key_item_id] = keys
    last_actual_totals[entry.key_item_id] = keys
    # 今日大宝箱已满则整轮不再选大宝箱（服务端拒开也会置 False）
    allow_big_chest = client.open_times() < DAILY_BIG_CHEST_OPEN_LIMIT
    if not allow_big_chest:
        emit(
            "info",
            (
                f"今日大宝箱已达上限（{client.open_times()}/"
                f"{DAILY_BIG_CHEST_OPEN_LIMIT}），本轮只开普通宝箱/打怪"
            ),
            {"open_times": client.open_times()},
        )

    def _disable_big_chest(reason: str) -> None:
        nonlocal allow_big_chest
        if not allow_big_chest:
            return
        allow_big_chest = False
        # 本地次数钳到上限，避免 choose_next_action 仍认为可开
        current = int(client.open_times())
        if current < DAILY_BIG_CHEST_OPEN_LIMIT and hasattr(client, "_open_times"):
            client._open_times = DAILY_BIG_CHEST_OPEN_LIMIT  # noqa: SLF001
        emit(
            "warning",
            (
                f"{reason}：今日大宝箱不再开启"
                f"（{client.open_times()}/{DAILY_BIG_CHEST_OPEN_LIMIT}）"
            ),
            {
                "open_times": client.open_times(),
                "daily_limit": DAILY_BIG_CHEST_OPEN_LIMIT,
            },
        )

    def current_progress() -> FarmProgress:
        hearth_total = inventory_total(HEARTH_ITEM_ID)
        return FarmProgress(
            area_id=area_id,
            area_name=entry.name,
            target_hearth=target_hearth,
            hearth_gained=max(0, hearth_total - hearth_before),
            hearth_total=hearth_total,
            keys_total=inventory_total(entry.key_item_id),
            key_item_id=entry.key_item_id,
            key_item_name=entry.key_item_name,
            monsters_killed=monsters_killed,
            settled_monsters=settled_monsters,
            no_key_monsters=no_key_monsters,
            missing_hearth_chests=missing_hearth_chests,
            small_chests_opened=small_opened,
            big_chests_opened=big_opened,
            open_times=client.open_times(),
            phase=str(workflow_state["phase"]),
            current_node_id=int(workflow_state["nodeid"]),
            current_node_kind=str(workflow_state["kind"]),
            last_reward_item_id=int(workflow_state["reward_item_id"]),
            last_reward_delta=int(workflow_state["reward_delta"]),
            last_transition=runtime_state["last_transition"],
            last_reset_reason=runtime_state["last_reset_reason"],
        )

    def set_workflow_step(
        step: str,
        message: str,
        *,
        node: MapNodeSpec | None = None,
        reward_item_id: int = 0,
        reward_delta: int = 0,
        level: str = "info",
        extra: Mapping[str, object] | None = None,
    ) -> None:
        workflow_state.update(
            {
                "phase": step,
                "nodeid": node.nodeid if node is not None else workflow_state["nodeid"],
                "kind": node.kind if node is not None else workflow_state["kind"],
                "reward_item_id": reward_item_id,
                "reward_delta": reward_delta,
            }
        )
        workflow: dict[str, object] = {
            "step": step,
            "label": FARM_STEP_LABELS.get(step, step),
            "nodeid": workflow_state["nodeid"],
            "node_name": map_node_name(
                int(workflow_state["nodeid"]), str(workflow_state["kind"])
            ),
            "kind": workflow_state["kind"],
        }
        if reward_item_id > 0:
            workflow.update(
                {
                    "item_id": reward_item_id,
                    "item_name": item_name(reward_item_id),
                    "delta": reward_delta,
                }
            )
        payload: dict[str, object] = {
            "farm": progress_payload(current_progress()),
            "workflow": workflow,
        }
        if extra:
            payload.update(extra)
        emit(level, message, payload)

    def process_step_callback(
        step: str,
        message: str,
        data: dict[str, object],
    ) -> None:
        """Adapt protocol-level callbacks to the farm progress projection."""

        workflow_state.update(
            {
                "phase": step,
                "nodeid": data.get("nodeid", workflow_state["nodeid"]),
                "kind": data.get("kind", workflow_state["kind"]),
                "reward_item_id": data.get(
                    "item_id", workflow_state["reward_item_id"]
                ),
                "reward_delta": data.get(
                    "delta", workflow_state["reward_delta"]
                ),
            }
        )
        event_data: dict[str, object] = {
            "farm": progress_payload(current_progress()),
            "workflow": {
                "step": step,
                "label": FARM_STEP_LABELS.get(step, step),
                "node_name": map_node_name(
                    int(data.get("nodeid", 0)),
                    str(data.get("kind") or workflow_state["kind"]),
                ),
                **data,
            },
        }
        emit("info", message, event_data)

    progress = current_progress()
    active_count = len(session.active_node_ids())
    emit(
        "info",
        (
            f"开始刷取 {entry.name}：目标炉温 +{target_hearth}，"
            f"当前{item_name(HEARTH_ITEM_ID)} {progress.hearth_total}，"
            f"{entry.key_item_name} {progress.keys_total}，"
            f"可交互节点 {active_count} 个"
        ),
        {
            "farm": progress_payload(progress),
            "active_nodes": list(session.active_node_ids()),
            "active_node_details": [
                {
                    "id": node.nodeid,
                    "name": map_node_name(node.nodeid, node.kind),
                    "kind": node.kind,
                }
                for node in nodes
                if session.is_active(node.nodeid)
            ],
        },
    )

    while True:
        if stop_requested():
            runtime_state["last_transition"] = "已请求停止"
            progress = current_progress()
            emit("warning", "已请求停止聚宝刷取", {"farm": progress_payload(progress)})
            return progress

        progress = current_progress()
        if progress.completed:
            emit(
                "success",
                (
                    f"{entry.name} 刷取完成：炉温 +{progress.hearth_gained}/"
                    f"{target_hearth}（当前 {progress.hearth_total}）"
                ),
                {"farm": progress_payload(progress)},
            )
            try:
                client.exit_area()
            except TreasureFarmError:
                pass
            return progress

        session = replace(session, open_times=client.open_times())
        if allow_big_chest and session.open_times >= DAILY_BIG_CHEST_OPEN_LIMIT:
            _disable_big_chest("Map_treasure_info 显示大宝箱次数已满")
        # 每轮选点前：若上次 processloc 留下 ret=2，先续战/解卡，禁止直接选点。
        last_ret = getattr(client, "_last_processloc_ret", None)
        if last_ret == 2:
            emit(
                "warning",
                "检测到地标占用(ret=2)，暂停选点，先结算挂起战斗",
                {"farm": progress_payload(progress)},
            )
            recover = getattr(client, "recover_from_landmark_lock", None)
            recovered = False
            try:
                if callable(recover):
                    recover_kwargs: dict[str, object] = {
                        "timeout": 30.0,
                        "emit": lambda level, message, data: emit(level, message, data),
                    }
                    if supports_stop_requested(recover):
                        recover_kwargs["stop_requested"] = stop_requested
                    recovered = bool(recover(**recover_kwargs))
            except TreasureFarmCancelled:
                if hasattr(client, "_active_event_context"):
                    client._active_event_context = None  # type: ignore[attr-defined]  # noqa: SLF001
                runtime_state["last_transition"] = "已请求停止"
                progress = current_progress()
                emit("warning", "已请求停止聚宝刷取", {"farm": progress_payload(progress)})
                return progress
            emit_event_progress()
            if not recovered:
                runtime_state["last_transition"] = "地标交互仍未结算"
                raise TreasureFarmError(
                    f"{entry.name} 当前地标交互仍未结算（ret=2）。"
                    "已停止刷取，避免重复触发；请先在游戏内完成当前交互/战斗"
                    "并领取结算后重新开始。"
                )
            # 仅在恢复成功后，ret=2 才是历史诊断；下一次节点动作将重新确认。
            if hasattr(client, "_last_processloc_ret"):
                client._last_processloc_ret = None  # type: ignore[attr-defined]  # noqa: SLF001
            stalled_rounds = 0
            initial = getattr(client, "_initial_locs", None)
            cur_after = int(
                getattr(client, "_curarea", session.area_id) or session.area_id or 0
            )
            if initial is not None:
                if is_treasure_map_area(cur_after):
                    session = AreaSession(
                        area_id=cur_after,
                        loc_status=dict(initial),
                        open_times=client.open_times(),
                    )
                    if cur_after != area_id:
                        session = bind_session(session, reason="解卡后区域变化")
                else:
                    session = bind_session(
                        AreaSession(
                            area_id=cur_after,
                            loc_status=dict(initial),
                            open_times=client.open_times(),
                        ),
                        reason="解卡后落到非聚宝图",
                    )
            continue
        # Use the reconciled mirror here.  A reward notification can arrive
        # after ProcessLocResult, so reading the raw client total would make
        # the next iteration miss a newly acquired key.
        # 每轮确认仍在聚宝图；被顶到潮汐之门等中转图时回城重进目标。
        live_cur = int(getattr(client, "_curarea", session.area_id) or 0)
        if live_cur > 0 and not is_treasure_map_area(live_cur):
            session = bind_session(
                AreaSession(
                    area_id=live_cur,
                    loc_status=dict(getattr(client, "_initial_locs", {}) or {}),
                    open_times=client.open_times(),
                ),
                reason="刷取中离开聚宝图",
            )
            stalled_rounds = 0
            continue
        # 选点前以服务端实时钥匙数为准，避免镜像偏高误开箱
        live_keys = int(client.item_total(entry.key_item_id))
        tracked_totals[entry.key_item_id] = live_keys
        last_actual_totals[entry.key_item_id] = live_keys
        keys = live_keys
        chest_only_mode = keys > CHEST_ONLY_KEY_THRESHOLD
        action = choose_next_action(
            session,
            nodes,
            keys=keys,
            allow_big_chest=allow_big_chest,
        )
        # 二次确认：宝箱节点必须钥匙够，否则改打怪
        if action is not None and action.kind in (
            NODE_KIND_SMALL_CHEST,
            NODE_KIND_BIG_CHEST,
        ):
            need = chest_key_cost(action.kind)
            if keys < need:
                emit(
                    "warning",
                    (
                        f"钥匙不足（{keys}/{need} {entry.key_item_name}），"
                        "跳过开箱，先击杀小怪"
                    ),
                    {
                        "farm": progress_payload(progress),
                        "keys": keys,
                        "required": need,
                    },
                )
                monsters_only = tuple(
                    n for n in nodes if n.kind == NODE_KIND_MONSTER
                )
                action = choose_next_action(
                    session,
                    monsters_only if monsters_only else nodes,
                    keys=0,
                    allow_big_chest=False,
                )
        if action is None:
            active_configured = [
                node for node in nodes if session.is_active(node.nodeid)
            ]
            known_ids = {node.nodeid for node in nodes}
            leftover_server_ids = tuple(
                sorted(
                    nodeid
                    for nodeid, status in session.loc_status.items()
                    if int(status) in (LOC_STATUS_ACTIVE, LOC_STATUS_OPEN)
                    and int(nodeid) not in known_ids
                )
            )
            if chest_only_mode:
                reset_reason = "钥匙超过20且无可开宝箱，返回主城重进"
                emit(
                    "info",
                    (
                        f"{entry.name} 钥匙 {keys} 超过 "
                        f"{CHEST_ONLY_KEY_THRESHOLD}，当前无可开宝箱，"
                        "返回主城重新进入地图"
                    ),
                    {
                        "farm": progress_payload(progress),
                        "keys": keys,
                        "chest_only_mode": True,
                    },
                )
            elif leftover_server_ids:
                emit(
                    "warning",
                    (
                        f"{entry.name} 服务端仍有未识别节点 "
                        f"{list(leftover_server_ids)}，将重置地图"
                    ),
                    {
                        "farm": progress_payload(progress),
                        "leftover_node_ids": list(leftover_server_ids),
                    },
                )
            elif active_configured:
                emit(
                    "info",
                    (
                        f"{entry.name} 剩余节点钥匙不足（钥匙 {keys}），"
                        "重置地图继续刷取"
                    ),
                    {"farm": progress_payload(progress)},
                )
            else:
                emit(
                    "info",
                    f"{entry.name} 当前节点已耗尽，重置地图继续刷取",
                    {"farm": progress_payload(progress)},
                )
            session = reset_session(
                reset_reason if chest_only_mode else "节点耗尽后重置"
            )
            bind_key_inventory()
            stalled_rounds = 0
            continue

        kind_label = {
            NODE_KIND_MONSTER: "击杀怪物",
            NODE_KIND_SMALL_CHEST: "开启普通宝箱",
            NODE_KIND_BIG_CHEST: "开启大宝箱",
        }.get(action.kind, "处理节点")
        if str(action.notes or "").strip().lower() == "boss":
            kind_label = "击杀Boss"
            node_label = f"Boss地标（ID {action.nodeid}）"
        else:
            node_label = map_node_name(action.nodeid, action.kind)
        emit(
            "info",
            f"{kind_label}：{node_label}，钥匙 {keys}",
            {
                "farm": progress_payload(progress),
                "nodeid": action.nodeid,
                "node_name": node_label,
                "kind": action.kind,
                "notes": action.notes,
            },
        )

        reward_item_id = (
            entry.key_item_id
            if action.kind == NODE_KIND_MONSTER
            else HEARTH_ITEM_ID
        )
        before_actual_totals = {
            HEARTH_ITEM_ID: int(client.item_total(HEARTH_ITEM_ID)),
            entry.key_item_id: int(client.item_total(entry.key_item_id)),
        }
        before_tracked_totals = {
            HEARTH_ITEM_ID: inventory_total(HEARTH_ITEM_ID),
            entry.key_item_id: inventory_total(entry.key_item_id),
        }

        def _fallback_step(step: str, message: str) -> None:
            """Project the typed flow for lightweight/fake clients."""

            set_workflow_step(step, message, node=action)

        fallback_flow = False
        try:
            farm_process = getattr(client, "process_farm_node", None)
            if callable(farm_process):
                # Keep compatibility with small test/dry-run clients that
                # implement the typed method without the optional callback.
                try:
                    parameters = inspect.signature(farm_process).parameters
                except (TypeError, ValueError):
                    parameters = {}
                accepts_kwargs = any(
                    parameter.kind == inspect.Parameter.VAR_KEYWORD
                    for parameter in parameters.values()
                )
                callback_kwargs: dict[str, object] = {}
                if "emit" in parameters or accepts_kwargs:
                    callback_kwargs["emit"] = process_step_callback
                if supports_stop_requested(farm_process):
                    callback_kwargs["stop_requested"] = stop_requested
                result = farm_process(
                    session.area_id,
                    action.nodeid,
                    action.kind,
                    **callback_kwargs,
                )
            else:
                # Legacy clients still get the same business ordering in the
                # task stream; the live client uses process_farm_node above
                # for protocol-level checkpoints.
                fallback_flow = True
                if action.kind == NODE_KIND_MONSTER:
                    _fallback_step(FARM_STEP_MONSTER_INTERACT, "怪物交互")
                    _fallback_step(FARM_STEP_BATTLE_PREPARE, "战斗准备")
                    _fallback_step(FARM_STEP_BATTLE_ENTER, "进入战斗")
                else:
                    _fallback_step(FARM_STEP_CHEST_INTERACT, "宝箱交互")
                process_kwargs: dict[str, object] = {}
                process_node = client.process_node
                if supports_stop_requested(process_node):
                    process_kwargs["stop_requested"] = stop_requested
                result = process_node(
                    session.area_id,
                    action.nodeid,
                    **process_kwargs,
                )
        except TreasureFarmCancelled:
            if hasattr(client, "_active_event_context"):
                client._active_event_context = None  # type: ignore[attr-defined]  # noqa: SLF001
            runtime_state["last_transition"] = "已请求停止"
            progress = current_progress()
            emit("warning", "已请求停止聚宝刷取", {"farm": progress_payload(progress)})
            return progress
        except TreasureFarmRejected as exc:
            if hasattr(client, "_active_event_context"):
                client._active_event_context = None  # type: ignore[attr-defined]  # noqa: SLF001
            emit_event_progress()
            # 大宝箱：若服务端返回日限 / times 已满，本轮永久跳过大宝箱
            if action.kind == NODE_KIND_BIG_CHEST:
                refresh = getattr(client, "_refresh_treasure_info", None)
                if callable(refresh):
                    try:
                        refresh()
                    except TreasureFarmError:
                        pass
                detail = f"{exc.detail} {exc}"
                if is_big_chest_daily_limit(
                    ret=exc.ret,
                    open_times=client.open_times(),
                    detail=detail,
                ):
                    _disable_big_chest(
                        f"开启大宝箱被拒 ret={exc.ret}（当日已开启上限）"
                    )
                    stalled_rounds = 0
                    continue
                # 未识别为日限但仍失败：刷新后若 times 已满同样停开
                if client.open_times() >= DAILY_BIG_CHEST_OPEN_LIMIT:
                    _disable_big_chest("开启大宝箱失败且次数已满")
                    stalled_rounds = 0
                    continue

            stalled_rounds += 1
            if exc.ret == 2:
                emit(
                    "warning",
                    (
                        f"{kind_label}被拒：已有地标激活 ret=2（服务端交互互斥）。"
                        "先检查 Battle_info/运行态，禁止连点开箱。"
                    ),
                    {"farm": progress_payload(progress), "ret": exc.ret},
                )
                recover = getattr(client, "recover_from_landmark_lock", None)
                if callable(recover):
                    recover_kwargs: dict[str, object] = {
                        "timeout": 40.0,
                        "emit": lambda level, message, data: emit(level, message, data),
                    }
                    if supports_stop_requested(recover):
                        recover_kwargs["stop_requested"] = stop_requested
                    recovered = recover(**recover_kwargs)
                    emit_event_progress()
                    if recovered:
                        stalled_rounds = 0
                        initial = getattr(client, "_initial_locs", None)
                        cur_rec = int(
                            getattr(client, "_curarea", session.area_id)
                            or session.area_id
                            or 0
                        )
                        if initial is not None:
                            if is_treasure_map_area(cur_rec):
                                session = AreaSession(
                                    area_id=cur_rec,
                                    loc_status=dict(initial),
                                    open_times=client.open_times(),
                                )
                                if cur_rec != area_id:
                                    session = bind_session(
                                        session, reason="ret=2 恢复后区域变化"
                                    )
                            else:
                                session = bind_session(
                                    AreaSession(
                                        area_id=cur_rec,
                                        loc_status=dict(initial),
                                        open_times=client.open_times(),
                                    ),
                                    reason="ret=2 恢复后落到非聚宝图",
                                )
                        continue
                finish = getattr(client, "finish_pending_battle", None)
                finish_kwargs: dict[str, object] = {
                    "timeout": getattr(client, "battle_timeout", 180.0)
                }
                if callable(finish) and supports_stop_requested(finish):
                    finish_kwargs["stop_requested"] = stop_requested
                if callable(finish) and finish(**finish_kwargs):
                    emit_event_progress()
                    stalled_rounds = 0
                    continue
            if stalled_rounds >= 3:
                # ret=2 时重置地图几乎必然失败；保留恢复标识并停止本轮。
                if exc.ret == 2:
                    raise TreasureFarmError(
                        f"{kind_label}连续 ret=2：当前会话卡在未结算战斗/地标占用。"
                        "已停止连点与重置；请检查本次状态报告的 battleState、"
                        "battleType 和 Battle_info 收包记录。"
                    ) from exc
                emit(
                    "info",
                    f"{entry.name} 连续失败，返回主城并重新加载地图",
                    {"farm": progress_payload(progress)},
                )
                session = reset_session("连续失败后重置")
                bind_key_inventory()
                stalled_rounds = 0
                continue
            time.sleep(0.15)
            continue
        except TreasureFarmError as exc:
            if hasattr(client, "_active_event_context"):
                client._active_event_context = None  # type: ignore[attr-defined]  # noqa: SLF001
            emit_event_progress()
            # 开箱前/确认时钥匙不足：同步钥匙后改打怪，不中断整轮刷取
            if "钥匙不足" in str(exc):
                live_keys = int(client.item_total(entry.key_item_id))
                tracked_totals[entry.key_item_id] = live_keys
                last_actual_totals[entry.key_item_id] = live_keys
                emit(
                    "warning",
                    str(exc),
                    {
                        "farm": progress_payload(progress),
                        "keys": live_keys,
                        "nodeid": action.nodeid,
                    },
                )
                stalled_rounds = 0
                continue
            # 开箱卡住：刷新会话节点态后重试/改打怪，避免整任务直接失败
            if action.kind in (NODE_KIND_SMALL_CHEST, NODE_KIND_BIG_CHEST) and (
                "超时" in str(exc) or "卡住" in str(exc)
            ):
                emit(
                    "warning",
                    str(exc),
                    {
                        "farm": progress_payload(progress),
                        "nodeid": action.nodeid,
                        "kind": action.kind,
                    },
                )
                stalled_rounds += 1
                if stalled_rounds >= 3:
                    session = reset_session("开箱卡住后重置")
                    bind_key_inventory()
                    stalled_rounds = 0
                    continue
                time.sleep(0.15)
                continue
            raise

        emit_event_progress()
        if not result.loc_updates:
            stalled_rounds += 1
            emit(
                "warning",
                f"{kind_label}未产生节点结算，暂不计数并重试",
                {
                    "farm": progress_payload(progress),
                    "nodeid": action.nodeid,
                    "node_name": map_node_name(action.nodeid, action.kind),
                },
            )
            if stalled_rounds >= 3:
                raise TreasureFarmError(
                    f"{kind_label}连续未收到节点结算，已停止以等待奖励确认"
                )
            continue

        # Validate the reward belonging to this exact node.  Prefer the live
        # inventory change; if it has not arrived yet, use the typed result
        # delta and keep it in the logical mirror until the next notification.
        result_deltas: dict[int, int] = {}
        result_totals: dict[int, int] = {}
        for change in result.items:
            result_deltas[change.item_id] = (
                result_deltas.get(change.item_id, 0) + change.delta
            )
            result_totals[change.item_id] = change.total
        if result.reward_item_id > 0 and result.reward_delta > 0:
            result_deltas[result.reward_item_id] = max(
                result_deltas.get(result.reward_item_id, 0),
                result.reward_delta,
            )
        observed_deltas: dict[int, int] = {}
        for item_id in (HEARTH_ITEM_ID, entry.key_item_id):
            actual_after = int(client.item_total(item_id))
            actual_delta = actual_after - before_actual_totals[item_id]
            result_delta = result_deltas.get(item_id, 0)
            if actual_delta != 0:
                delta = actual_delta
            else:
                result_total = result_totals.get(item_id)
                # A delayed storage notification may have reached the client
                # before the result object.  If its authoritative total is
                # already equal to both snapshots, do not apply the same
                # result delta a second time.  Zero totals with a positive
                # delta are treated as omitted totals for legacy adapters.
                if (
                    result_total is not None
                    and result_total == actual_after == before_tracked_totals[item_id]
                    and (result_delta < 0 or result_total > 0)
                ):
                    delta = 0
                elif (
                    result_total is not None
                    and result_total == actual_after
                    and (result_delta < 0 or result_total > 0)
                ):
                    delta = result_total - before_tracked_totals[item_id]
                else:
                    delta = result_delta
            observed_deltas[item_id] = delta
            tracked_totals[item_id] = before_tracked_totals[item_id] + delta
            last_actual_totals[item_id] = actual_after

        reward_delta = observed_deltas[reward_item_id]
        monster_no_key = (
            action.kind == NODE_KIND_MONSTER
            and result.battle_won is True
            and bool(result.loc_updates)
            and reward_delta <= 0
        )
        if reward_delta <= 0 and not monster_no_key:
            if action.kind in (NODE_KIND_SMALL_CHEST, NODE_KIND_BIG_CHEST):
                missing_hearth_chests += 1
                runtime_state["last_transition"] = "宝箱已结算但缺少炉温奖励"
                emit(
                    "error",
                    f"{kind_label}已结算但未收到{item_name(reward_item_id)}奖励",
                    {
                        "farm": progress_payload(current_progress()),
                        "nodeid": action.nodeid,
                        "kind": action.kind,
                        "reward_item_id": reward_item_id,
                    },
                )
            raise TreasureFarmError(
                f"{kind_label}已完成节点交互，但未收到"
                f"{item_name(reward_item_id)}奖励；已停止，避免重复点击"
            )
        if action.kind == NODE_KIND_MONSTER and result.battle_won is False:
            raise TreasureFarmError("怪物战斗未胜利，未带走钥匙")

        # The legacy adapter cannot observe protocol-level battle messages.
        # Publish its remaining checkpoints only after the matching reward has
        # been verified, so a missing reward never looks like a completed node.
        if fallback_flow:
            if action.kind == NODE_KIND_MONSTER:
                _fallback_step(FARM_STEP_BATTLE_VICTORY, "战斗胜利")
                _fallback_step(FARM_STEP_KEY_TAKE, "带走钥匙")
            else:
                _fallback_step(FARM_STEP_CHEST_OPEN, "打开宝箱")

        if action.kind == NODE_KIND_MONSTER:
            if monster_no_key:
                # Server event「判断是否获得钥匙」can end without「带走钥匙」.
                set_workflow_step(
                    FARM_STEP_KEY_TAKE,
                    "本场未掉落钥匙（节点已结算，继续刷怪）",
                    node=action,
                    reward_item_id=reward_item_id,
                    reward_delta=0,
                    level="warning",
                )
            elif workflow_state["phase"] != FARM_STEP_KEY_REWARD:
                set_workflow_step(
                    FARM_STEP_KEY_REWARD,
                    "获得钥匙奖励",
                    node=action,
                    reward_item_id=reward_item_id,
                    reward_delta=reward_delta,
                )
        else:
            if workflow_state["phase"] != FARM_STEP_CHEST_REWARD:
                set_workflow_step(
                    FARM_STEP_CHEST_REWARD,
                    "获得炉温奖励",
                    node=action,
                    reward_item_id=reward_item_id,
                    reward_delta=reward_delta,
                )

        stalled_rounds = 0
        session = session.with_loc_updates(result.loc_updates)

        if action.kind == NODE_KIND_MONSTER:
            # Count only kills that actually yielded map keys.
            settled_monsters += 1
            if reward_delta > 0:
                monsters_killed += 1
                runtime_state["last_transition"] = "怪物已结算并获得钥匙"
            else:
                no_key_monsters += 1
                runtime_state["last_transition"] = "怪物已结算但未掉钥匙"
        elif action.kind == NODE_KIND_SMALL_CHEST:
            small_opened += 1
            runtime_state["last_transition"] = "普通宝箱已结算并获得炉温"
        elif action.kind == NODE_KIND_BIG_CHEST:
            big_opened += 1
            runtime_state["last_transition"] = "大宝箱已结算并获得炉温"
            # 本地乐观 +1；服务端 Map_treasure_info 也会刷新
            client._open_times = min(  # noqa: SLF001 — 会话内部计数
                DAILY_BIG_CHEST_OPEN_LIMIT, client.open_times() + 1
            )
            if client.open_times() >= DAILY_BIG_CHEST_OPEN_LIMIT:
                _disable_big_chest("大宝箱开启成功后次数已满")

        workflow_state.update(
            {
                "phase": FARM_STEP_COMPLETE,
                "nodeid": action.nodeid,
                "kind": action.kind,
                "reward_item_id": reward_item_id,
                "reward_delta": reward_delta,
            }
        )
        progress = current_progress()
        loot_bits: list[str] = []
        for change in result.items:
            if change.delta == 0:
                continue
            sign = "+" if change.delta > 0 else ""
            loot_bits.append(f"{item_name(change.item_id)} {sign}{change.delta}")
        loot_text = "、".join(loot_bits) if loot_bits else "无物品变动"
        flag_note = "秒杀" if result.flag == 1 else "战斗"
        emit(
            "info",
            (
                f"{kind_label}完成（{flag_note}）· {loot_text} · "
                f"炉温 +{progress.hearth_gained}/{target_hearth}"
            ),
            {
                "farm": progress_payload(progress),
                "loc_updates": dict(result.loc_updates),
                "loc_update_nodes": [
                    {
                        "id": updated_node_id,
                        "name": map_node_name(
                            updated_node_id,
                            action.kind if updated_node_id == action.nodeid else "",
                        ),
                        "status": status,
                    }
                    for updated_node_id, status in result.loc_updates.items()
                ],
            },
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="聚宝之地自动刷取宝箱（击杀小怪取钥匙、开箱获炉温）"
    )
    parser.add_argument("--token-file", type=Path, default=PROJECT_ROOT / "tokens.json")
    parser.add_argument("--account-url", default="http://dixcbdlogin.gamelunar.com:8101/api")
    parser.add_argument("--post-format", choices=("json", "form"), default="json")
    parser.add_argument("--zone-id", default="4101", help="区服 ID，默认 4101")
    parser.add_argument(
        "--area-id",
        type=int,
        default=530101,
        help="聚宝地图 ID，默认沉默之城 530101",
    )
    parser.add_argument(
        "--target-hearth",
        type=int,
        default=1,
        help="目标炉温增量；测试可用 1，验证拿到炉温即成功",
    )
    parser.add_argument(
        "--status",
        action="store_true",
        help="只查询当前地图/死亡/战斗/地标状态，不刷取",
    )
    parser.add_argument(
        "--status-no-probe",
        action="store_true",
        help="兼容旧命令；--status 现在始终仅读取登录快照",
    )
    parser.add_argument("--http-timeout", type=float, default=15.0)
    parser.add_argument("--timeout", type=float, default=20.0, help="WebSocket 常规超时")
    parser.add_argument("--battle-timeout", type=float, default=180.0, help="单次战斗超时")
    parser.add_argument("--channel-name", default="taojin_android_zhuyue")
    parser.add_argument("--channel-id", default="110001")
    parser.add_argument("--media", default="M521957")
    parser.add_argument("--device-id", default="2c54fe7b2fe5f0fe")
    parser.add_argument("--device-model", default="HONOR REP-AN00")
    parser.add_argument("--system-version", default="Android")
    parser.add_argument("--terminal-info", default="HONOR REP-AN00")
    parser.add_argument("--client-ip", default="112.10.204.243")
    parser.add_argument("--imei", default="i am imei")
    parser.add_argument("--mac", default="i am mac")
    parser.add_argument("--oaid", default="")
    parser.add_argument("--android-id", default="")
    parser.add_argument("--device-extend", default="{}")
    parser.add_argument("--build-version", default="1.4.1.30")
    parser.add_argument("--update-version", default="1.4.1")
    return parser


def format_status_report(status: Mapping[str, object]) -> str:
    """把 inspect_status 结果格式化为中文报告。"""

    items = status.get("items") if isinstance(status.get("items"), dict) else {}
    hearth = items.get("hearth") if isinstance(items.get("hearth"), dict) else {}
    ticket = items.get("ticket") if isinstance(items.get("ticket"), dict) else {}
    mkey = items.get("map_key") if isinstance(items.get("map_key"), dict) else {}
    pos = status.get("pos") if isinstance(status.get("pos"), dict) else {}
    lines = [
        f"阶段：{status.get('phase')}",
        f"阵亡：{'是' if status.get('dead') else '否'}"
        + (f"（需魂域：{'是' if status.get('needsoul') else '否'}）"),
        f"所在：{status.get('area_name')}（ID {status.get('area_id')}）"
        + (" · 聚宝图" if status.get("is_treasure_map") else ""),
        f"坐标：({pos.get('x')}, {pos.get('y')})",
        f"节点：共 {status.get('loc_total')} · {status.get('loc_status_counts')}",
        (
            f"可交互：怪 {status.get('active_monsters')} / "
            f"小宝箱 {status.get('active_small_chests')} / "
            f"大宝箱 {status.get('active_big_chests')}"
        ),
        f"今日大宝箱开启次数：{status.get('treasure_big_chest_open_times')}",
        (
            f"物品：{hearth.get('name')} {hearth.get('total')} · "
            f"{ticket.get('name')} {ticket.get('total')} · "
            f"{mkey.get('name')} {mkey.get('total')}"
        ),
        f"编队：{'有' if status.get('has_team') else '无'}",
    ]
    pending = status.get("pending_battle")
    marker = status.get("game_data_battle_marker")
    if isinstance(marker, dict):
        marker_state = marker.get("battle_state")
        marker_type_value = marker.get("battle_type")
        marker_type = marker.get("battle_type_name")
        marker_status = "已发现" if marker.get("active") else "未激活"
        lines.append(
            f"登录战斗标识：{marker_status} · "
            f"battleState={marker_state} · "
            f"battleType={marker_type_value}（{marker_type}）· "
            f"Client_data_get={'已响应' if marker.get('client_data_ready') else '待响应'}"
        )
    runtime = status.get("map_runtime")
    if isinstance(runtime, dict):
        trigger = runtime.get("move_trigger")
        trigger_text = "未激活"
        if isinstance(trigger, dict):
            trigger_text = (
                f"max={trigger.get('max')} / remain={trigger.get('remain')} / "
                f"area={trigger.get('area')} / triggernum={trigger.get('triggernum')}"
            )
        lines.append(
            "地图运行态："
            f"events={runtime.get('events')} · "
            f"area(state={runtime.get('area_state')}, flag={runtime.get('area_flag')}, "
            f"locked={runtime.get('area_locked')}, remains={runtime.get('area_remains')}) · "
            f"mtdata={trigger_text}"
        )
    if isinstance(pending, dict):
        lines.append(
            "战斗协议：已确认 · "
            f"类型 {pending.get('battle_type_name')} · "
            f"敌方 {pending.get('enemy_units')} · "
            f"location {pending.get('location_id')} · "
            f"skipTeam={pending.get('skip_team')} · "
            f"{pending.get('ui_hint', '')}"
        )
    else:
        lines.append(
            "战斗协议：未收到 Battle_info/战斗帧"
        )
    pending_event = status.get("pending_event")
    if isinstance(pending_event, dict):
        mode = "可由刷取流程自动确认" if pending_event.get("auto_confirmable") else "保留等待选择"
        lines.append(f"地图事件：{pending_event.get('label')} · {mode}")
    if status.get("actionable"):
        lines.append("可执行：是（可尝试点怪/开箱）")
    else:
        lines.append("可执行：否（需 ensure_actionable 推进状态）")
    if status.get("phase_code"):
        lines.append(f"阶段码：{status.get('phase_code')}")
    if status.get("landmark_locked"):
        lines.append(
            "地标锁定：是（processloc ret=2 → 服务端交互互斥；需完整战斗握手才可结算）"
        )
    msgs = status.get("login_battle_messages")
    if isinstance(msgs, list) and msgs:
        lines.append("登录期战斗相关消息：" + "、".join(str(m) for m in msgs))
    notes = status.get("detection_notes")
    if isinstance(notes, list):
        for note in notes:
            lines.append(f"说明：{note}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    def emit(level: str, message: str, data: dict) -> None:
        print(f"[{level}] {message}", flush=True)

    try:
        tokens = load_tokens(args.token_file)
        endpoint = resolve_game_endpoint(tokens, args)
        client = TreasureFarmClient(
            endpoint,
            timeout=args.timeout,
            battle_timeout=args.battle_timeout,
        )
        try:
            if args.status:
                print(
                    f"区服 {endpoint.zone_name or endpoint.zone_id} · 状态查询",
                    flush=True,
                )
                status = client.inspect_status()
                print(format_status_report(status), flush=True)
                return 0

            print(
                f"已连接区服 {endpoint.zone_name or endpoint.zone_id}，"
                f"地图={treasure_area_name(args.area_id)}，"
                f"目标{item_name(HEARTH_ITEM_ID)} +{args.target_hearth}",
                flush=True,
            )
            progress = run_treasure_farm(
                client,
                args.area_id,
                args.target_hearth,
                emit=emit,
                stop_requested=lambda: False,
            )
        finally:
            client.close()
    except (TreasureFarmError, HarvestError, OSError, ValueError) as exc:
        print(f"刷取失败：{exc}", file=sys.stderr, flush=True)
        return 1

    print(
        f"结果：{progress.area_name} · "
        f"{item_name(HEARTH_ITEM_ID)} +{progress.hearth_gained}/{progress.target_hearth} "
        f"（当前 {progress.hearth_total}）· "
        f"击杀 {progress.monsters_killed} · "
        f"普通宝箱 {progress.small_chests_opened} · "
        f"大宝箱 {progress.big_chests_opened}",
        flush=True,
    )
    # 成功标准：本次确实获得炉温（炉火）
    if progress.hearth_gained > 0:
        print(
            f"成功：已获取{item_name(HEARTH_ITEM_ID)} +{progress.hearth_gained}",
            flush=True,
        )
        return 0
    print(
        f"未成功：本次{item_name(HEARTH_ITEM_ID)}增量为 0，不能记为成功",
        file=sys.stderr,
        flush=True,
    )
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
