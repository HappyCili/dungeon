#!/usr/bin/env python3
"""日常/周常任务状态、积分和活跃奖励的游戏服客户端。

该模块封装任务共性协议：读取 ``Game_data`` 中的日常/周常与任务进度，
主动查询 ``Dailyquest_info``，以及领取两组已完成任务的积分和已达到的活跃奖励。
具体玩法动作由 ``daily_actions.py`` 调用各自现有客户端完成。

用法：
    .venv/bin/python daily_quest.py status
    .venv/bin/python daily_quest.py claim
    .venv/bin/python daily_quest.py --self-test
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Mapping

from harvest_fief import (
    HEARTBEAT_MESSAGE_ID,
    HEARTBEAT_RET_MESSAGE_ID,
    LOGIN_FAIL_MESSAGE_ID,
    LOGIN_MESSAGE_ID,
    LOGIN_REUNIQUE_MESSAGE_ID,
    PACK_PASSWORD_MESSAGE_ID,
    SOCKET_PACK_KEY,
    GameEndpoint,
    HarvestError,
    MessageHeader,
    NativeWebSocket,
    ProtoReader,
    decode_int32,
    decode_message_header,
    decode_pack_password,
    encode_bytes_field,
    encode_int_field,
    encode_login_payload,
    encode_message_header,
    encode_varint,
    load_tokens,
    pack1_decode,
    pack1_encode,
    resolve_game_endpoint,
)
from ws_traffic_log import bind_traffic_logging
from harvest_fief import build_parser as build_base_parser
from logging_store import MANAGED_DESTINATION, LogPersistenceError, write_standard_log
from id_descriptions import activity_reward_name, daily_task_name, quest_name
from project_paths import NATIVE_APP_ROOT


DEFAULT_DAILY_QUEST_PATH = NATIVE_APP_ROOT / "decrypted-task-data" / "daily_quest.json"
DEFAULT_ACTIVITY_REWARD_PATH = NATIVE_APP_ROOT / "decrypted-task-data" / "activityreward.json"
DEFAULT_RESULT_LOG = MANAGED_DESTINATION

GAME_DATA_MESSAGE_ID = 10490
KICKOUT_MESSAGE_ID = 10030
DAILYQUEST_INFO_MESSAGE_ID = 19700
DAILYQUEST_GET_QUEST_REWARD_MESSAGE_ID = 19702
DAILYQUEST_GET_SCORE_REWARD_MESSAGE_ID = 19704
DAILY_GROUP_ID = 1
WEEKLY_GROUP_ID = 2


class DailyQuestError(HarvestError):
    """日常/周常任务协议状态不满足执行前提。"""


class GameSessionKickout(HarvestError):
    """游戏服在日常任务会话登录阶段中止连接。"""

    def __init__(self, ret: int, message: str = "") -> None:
        self.ret = ret
        self.message = message
        detail = f"，消息={message}" if message else ""
        super().__init__(f"游戏服终止日常任务会话：ret={ret}{detail}")


@dataclass(frozen=True)
class DailyTaskState:
    daily_id: int
    finished: bool
    score_claimed: bool


@dataclass(frozen=True)
class DailyQuestStatus:
    daily_remaining_seconds: int
    daily_reset_seconds: int
    daily_reward_ids: tuple[int, ...]
    tasks: Mapping[int, DailyTaskState]
    quest_progress: Mapping[int, int]
    weekly_remaining_seconds: int = 0
    weekly_reset_seconds: int = 0
    weekly_reward_ids: tuple[int, ...] = ()

    def task(self, daily_id: int) -> DailyTaskState | None:
        return self.tasks.get(daily_id)

    def progress_for(self, quest_id: int) -> int | None:
        return self.quest_progress.get(quest_id)

    def reward_ids_for_group(self, group_id: int) -> tuple[int, ...]:
        if group_id == DAILY_GROUP_ID:
            return self.daily_reward_ids
        if group_id == WEEKLY_GROUP_ID:
            return self.weekly_reward_ids
        raise ValueError(f"不支持的任务组：{group_id}")


@dataclass(frozen=True)
class DailyTaskConfig:
    daily_id: int
    quest_id: int
    group_id: int
    activity_score: int


@dataclass(frozen=True)
class ActivityRewardConfig:
    reward_id: int
    group_id: int
    score: int


@dataclass(frozen=True)
class DailyCatalog:
    tasks: Mapping[int, DailyTaskConfig]
    activity_rewards: tuple[ActivityRewardConfig, ...]
    weekly_tasks: Mapping[int, DailyTaskConfig] = field(default_factory=dict)
    weekly_activity_rewards: tuple[ActivityRewardConfig, ...] = ()

    def tasks_for_group(self, group_id: int) -> Mapping[int, DailyTaskConfig]:
        if group_id == DAILY_GROUP_ID:
            return self.tasks
        if group_id == WEEKLY_GROUP_ID:
            return self.weekly_tasks
        raise ValueError(f"不支持的任务组：{group_id}")

    def rewards_for_group(
        self, group_id: int
    ) -> tuple[ActivityRewardConfig, ...]:
        if group_id == DAILY_GROUP_ID:
            return self.activity_rewards
        if group_id == WEEKLY_GROUP_ID:
            return self.weekly_activity_rewards
        raise ValueError(f"不支持的任务组：{group_id}")


@dataclass(frozen=True)
class DailyQuestRewardResponse:
    ret: int
    daily_id: int
    group_id: int
    daily_ids: tuple[int, ...]


@dataclass(frozen=True)
class DailyScoreRewardResponse:
    ret: int
    group_id: int
    reward_id: int
    reward_ids: tuple[int, ...]


@dataclass(frozen=True)
class DailyClaimResult:
    claimed_task_ids: tuple[int, ...]
    claimed_reward_ids: tuple[int, ...]
    status: DailyQuestStatus
    claimed_daily_task_ids: tuple[int, ...] = ()
    claimed_weekly_task_ids: tuple[int, ...] = ()
    claimed_daily_reward_ids: tuple[int, ...] = ()
    claimed_weekly_reward_ids: tuple[int, ...] = ()


def _read_json_array(path: Path, label: str) -> list[object]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise DailyQuestError(f"读取{label}失败：{path}：{exc}") from exc
    if not isinstance(payload, list):
        raise DailyQuestError(f"{label}顶层必须是数组：{path}")
    return payload


def _required_int(row: object, key: str, label: str) -> int:
    if not isinstance(row, dict) or not isinstance(row.get(key), int):
        raise DailyQuestError(f"{label}字段 {key} 必须是整数")
    return row[key]


def load_daily_catalog(
    daily_quest_path: Path = DEFAULT_DAILY_QUEST_PATH,
    activity_reward_path: Path = DEFAULT_ACTIVITY_REWARD_PATH,
) -> DailyCatalog:
    """加载本地日常与周常任务、活跃奖励配置。"""

    tasks_by_group: dict[int, dict[int, DailyTaskConfig]] = {
        DAILY_GROUP_ID: {},
        WEEKLY_GROUP_ID: {},
    }
    score_field_by_group = {
        DAILY_GROUP_ID: "scoreday",
        WEEKLY_GROUP_ID: "scoreweek",
    }
    for row in _read_json_array(daily_quest_path, "daily_quest 配置"):
        group_id = _required_int(row, "groupId", "daily_quest")
        if group_id not in tasks_by_group:
            continue
        tasks = tasks_by_group[group_id]
        daily_id = _required_int(row, "id", "daily_quest")
        if daily_id in tasks:
            raise DailyQuestError(
                f"daily_quest 任务组 {group_id} 存在重复任务 ID：{daily_id}"
            )
        tasks[daily_id] = DailyTaskConfig(
            daily_id=daily_id,
            quest_id=_required_int(row, "questid", "daily_quest"),
            group_id=group_id,
            activity_score=_required_int(
                row, score_field_by_group[group_id], "daily_quest"
            ),
        )

    rewards_by_group: dict[int, list[ActivityRewardConfig]] = {
        DAILY_GROUP_ID: [],
        WEEKLY_GROUP_ID: [],
    }
    for row in _read_json_array(activity_reward_path, "activityreward 配置"):
        group_id = _required_int(row, "groupId", "activityreward")
        if group_id not in rewards_by_group:
            continue
        rewards_by_group[group_id].append(
            ActivityRewardConfig(
                reward_id=_required_int(row, "id", "activityreward"),
                group_id=group_id,
                score=_required_int(row, "score", "activityreward"),
            )
        )
    for rewards in rewards_by_group.values():
        rewards.sort(key=lambda reward: reward.score)
    if any(
        not tasks_by_group[group_id] or not rewards_by_group[group_id]
        for group_id in (DAILY_GROUP_ID, WEEKLY_GROUP_ID)
    ):
        raise DailyQuestError("日常/周常任务或活跃奖励配置为空")
    return DailyCatalog(
        tasks=tasks_by_group[DAILY_GROUP_ID],
        activity_rewards=tuple(rewards_by_group[DAILY_GROUP_ID]),
        weekly_tasks=tasks_by_group[WEEKLY_GROUP_ID],
        weekly_activity_rewards=tuple(rewards_by_group[WEEKLY_GROUP_ID]),
    )


def _decode_daily_task_state(data: bytes) -> DailyTaskState:
    daily_id = 0
    finished = False
    score_claimed = False
    for field_number, wire_type, value in ProtoReader(data).fields():
        if wire_type != 0:
            continue
        if field_number == 1:
            daily_id = decode_int32(int(value))
        elif field_number == 2:
            finished = bool(value)
        elif field_number == 3:
            score_claimed = bool(value)
    if daily_id <= 0:
        raise DailyQuestError("Dailyquest_info 包含无效日常 ID")
    return DailyTaskState(daily_id, finished, score_claimed)


def decode_dailyquest_status(
    data: bytes, *, quest_progress: Mapping[int, int] | None = None
) -> DailyQuestStatus:
    """解码 ``DailyQuestInfo``，字段布局来自本地客户端 protobuf 定义。"""

    daily_remaining_seconds = 0
    daily_reset_seconds = 0
    daily_reward_ids: list[int] = []
    weekly_remaining_seconds = 0
    weekly_reset_seconds = 0
    weekly_reward_ids: list[int] = []
    tasks: dict[int, DailyTaskState] = {}
    for field_number, wire_type, value in ProtoReader(data).fields():
        if field_number == 1 and wire_type == 0:
            daily_remaining_seconds = int(value)
        elif field_number == 2 and wire_type == 0:
            daily_reset_seconds = int(value)
        elif field_number == 3 and wire_type == 0:
            daily_reward_ids.append(decode_int32(int(value)))
        elif field_number == 3 and wire_type == 2:
            daily_reward_ids.extend(_decode_packed_int32(bytes(value)))
        elif field_number == 4 and wire_type == 0:
            weekly_remaining_seconds = int(value)
        elif field_number == 5 and wire_type == 0:
            weekly_reset_seconds = int(value)
        elif field_number == 6 and wire_type == 0:
            weekly_reward_ids.append(decode_int32(int(value)))
        elif field_number == 6 and wire_type == 2:
            weekly_reward_ids.extend(_decode_packed_int32(bytes(value)))
        elif field_number == 7 and wire_type == 2:
            task = _decode_daily_task_state(bytes(value))
            if task.daily_id in tasks:
                raise DailyQuestError(f"Dailyquest_info 包含重复日常 ID：{task.daily_id}")
            tasks[task.daily_id] = task
    return DailyQuestStatus(
        daily_remaining_seconds=daily_remaining_seconds,
        daily_reset_seconds=daily_reset_seconds,
        daily_reward_ids=tuple(daily_reward_ids),
        tasks=tasks,
        quest_progress=dict(quest_progress or {}),
        weekly_remaining_seconds=weekly_remaining_seconds,
        weekly_reset_seconds=weekly_reset_seconds,
        weekly_reward_ids=tuple(weekly_reward_ids),
    )


def _decode_progress_value(data: bytes) -> int:
    for field_number, wire_type, value in ProtoReader(data).fields():
        if field_number == 1 and wire_type == 0:
            return decode_int32(int(value))
    return 0


def _decode_quest_progress_entry(data: bytes) -> tuple[int, int] | None:
    condition_id = 0
    progress_payload: bytes | None = None
    for field_number, wire_type, value in ProtoReader(data).fields():
        if field_number == 1 and wire_type == 0:
            condition_id = decode_int32(int(value))
        elif field_number == 2 and wire_type == 2:
            progress_payload = bytes(value)
    if condition_id <= 0 or progress_payload is None:
        return None
    return condition_id, _decode_progress_value(progress_payload)


def _decode_quest_progress(data: bytes) -> tuple[int, dict[int, int]]:
    quest_id = 0
    progress: dict[int, int] = {}
    for field_number, wire_type, value in ProtoReader(data).fields():
        if field_number == 1 and wire_type == 0:
            quest_id = decode_int32(int(value))
        elif field_number == 6 and wire_type == 2:
            entry = _decode_quest_progress_entry(bytes(value))
            if entry is not None:
                condition_id, current = entry
                progress[condition_id] = current
    return quest_id, progress


def decode_game_data_quest_progress(data: bytes) -> dict[int, int]:
    """返回按任务 ID 索引的第一条件进度。

    正式日常配置中的任务 ID 与唯一条件 ID 相同。若客户端返回多条件任务，
    只在条件 ID 与任务 ID 一致时记录，以避免把无关条件误作日常次数。
    """

    quest_data: bytes | None = None
    for field_number, wire_type, value in ProtoReader(data).fields():
        if field_number == 12 and wire_type == 2:
            quest_data = bytes(value)
            break
    if quest_data is None:
        return {}

    quest_list: bytes | None = None
    for field_number, wire_type, value in ProtoReader(quest_data).fields():
        if field_number == 1 and wire_type == 2:
            quest_list = bytes(value)
            break
    if quest_list is None:
        return {}

    progress_by_quest: dict[int, int] = {}
    for field_number, wire_type, value in ProtoReader(quest_list).fields():
        if field_number != 1 or wire_type != 2:
            continue
        quest_id, progress = _decode_quest_progress(bytes(value))
        if quest_id > 0 and quest_id in progress:
            progress_by_quest[quest_id] = progress[quest_id]
    return progress_by_quest


def decode_game_data_daily_status(data: bytes) -> DailyQuestStatus:
    """从 ``Game_data`` 中提取日常状态和当前任务进度。"""

    daily_payload: bytes | None = None
    for field_number, wire_type, value in ProtoReader(data).fields():
        if field_number == 19 and wire_type == 2:
            daily_payload = bytes(value)
            break
    if daily_payload is None:
        raise DailyQuestError("Game_data 缺少 dailyquest 状态")
    return decode_dailyquest_status(
        daily_payload,
        quest_progress=decode_game_data_quest_progress(data),
    )


def encode_daily_quest_reward_request(daily_id: int, group_id: int = DAILY_GROUP_ID) -> bytes:
    return encode_int_field(1, daily_id) + encode_int_field(2, group_id)


def encode_daily_score_reward_request(reward_id: int, group_id: int = DAILY_GROUP_ID) -> bytes:
    return encode_int_field(1, group_id) + encode_int_field(2, reward_id)


def _decode_packed_int32(data: bytes) -> tuple[int, ...]:
    reader = ProtoReader(data)
    values: list[int] = []
    while reader.position < len(data):
        values.append(decode_int32(reader.read_varint()))
    return tuple(values)


def decode_daily_quest_reward_response(data: bytes) -> DailyQuestRewardResponse:
    ret = 0
    daily_id = 0
    group_id = 0
    daily_ids: list[int] = []
    for field_number, wire_type, value in ProtoReader(data).fields():
        if field_number == 1 and wire_type == 0:
            ret = decode_int32(int(value))
        elif field_number == 2 and wire_type == 0:
            daily_id = decode_int32(int(value))
        elif field_number == 3 and wire_type == 0:
            group_id = decode_int32(int(value))
        elif field_number == 4 and wire_type == 0:
            daily_ids.append(decode_int32(int(value)))
        elif field_number == 4 and wire_type == 2:
            daily_ids.extend(_decode_packed_int32(bytes(value)))
    return DailyQuestRewardResponse(ret, daily_id, group_id, tuple(daily_ids))


def decode_daily_score_reward_response(data: bytes) -> DailyScoreRewardResponse:
    ret = 0
    group_id = 0
    reward_id = 0
    reward_ids: list[int] = []
    for field_number, wire_type, value in ProtoReader(data).fields():
        if field_number == 1 and wire_type == 0:
            ret = decode_int32(int(value))
        elif field_number == 2 and wire_type == 0:
            group_id = decode_int32(int(value))
        elif field_number == 3 and wire_type == 0:
            reward_id = decode_int32(int(value))
        elif field_number == 4 and wire_type == 0:
            reward_ids.append(decode_int32(int(value)))
        elif field_number == 4 and wire_type == 2:
            reward_ids.extend(_decode_packed_int32(bytes(value)))
    return DailyScoreRewardResponse(ret, group_id, reward_id, tuple(reward_ids))


class DailyQuestClient:
    """单个游戏服会话中的日常状态查询与领取操作。"""

    def __init__(
        self,
        endpoint: GameEndpoint,
        timeout: float,
        *,
        socket_factory: Callable[[str, float], NativeWebSocket] = NativeWebSocket.connect,
        websocket_log: Path | bool | None = True,
    ) -> None:
        self.endpoint = endpoint
        self.timeout = timeout
        self.socket_factory = socket_factory
        self.socket: NativeWebSocket | None = None
        self.password: str | None = None
        bind_traffic_logging(
            self,
            task="daily_quest",
            path=websocket_log,
            error_cls=DailyQuestError,
        )
        self._game_data_status: DailyQuestStatus | None = None

    def __enter__(self) -> "DailyQuestClient":
        self.login()
        return self

    def __exit__(self, _exc_type: object, _exc: object, _traceback: object) -> None:
        self.close()

    def close(self) -> None:
        if self.socket is not None:
            self.socket.close()
            self.socket = None

    def _send_message(self, message_id: int, data: bytes = b"", *, encrypted: bool) -> None:
        if self.socket is None:
            raise DailyQuestError("WebSocket 尚未连接")
        packet = encode_message_header(message_id, data)
        if encrypted:
            if not self.password:
                raise DailyQuestError("游戏服尚未下发会话密码")
            self.socket.send_text(pack1_encode(packet, self.password))
        else:
            self.socket.send_binary(packet)

    def _decode_frame(self, opcode: int, payload: bytes) -> MessageHeader:
        if self.password is not None:
            if opcode not in (0x1, 0x2):
                raise DailyQuestError(f"加密游戏报文 opcode 异常：{opcode}")
            payload = pack1_decode(payload, self.password)
        return decode_message_header(payload)

    def _receive_header(self, deadline: float, context: str) -> MessageHeader:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise DailyQuestError(f"等待{context}超时")
        try:
            assert self.socket is not None
            opcode, payload = self.socket.recv_message(remaining)
        except socket.timeout as exc:
            raise DailyQuestError(f"等待{context}超时") from exc
        except OSError as exc:
            raise DailyQuestError(f"读取{context}报文失败：{exc}") from exc
        return self._decode_frame(opcode, payload)

    def _handle_common_message(self, header: MessageHeader) -> bool:
        if header.message_id == HEARTBEAT_MESSAGE_ID:
            self._send_message(
                HEARTBEAT_RET_MESSAGE_ID,
                encrypted=self.password is not None,
            )
            return True
        if header.message_id == LOGIN_FAIL_MESSAGE_ID:
            raise DailyQuestError("游戏服 Login 失败")
        if header.message_id == KICKOUT_MESSAGE_ID:
            ret = 0
            message = ""
            for field_number, wire_type, value in ProtoReader(header.data).fields():
                if field_number == 1 and wire_type == 0:
                    ret = decode_int32(int(value))
                elif field_number == 2 and wire_type == 2:
                    message = bytes(value).decode("utf-8", errors="replace")
            raise GameSessionKickout(ret, message)
        return False

    def login(self) -> DailyQuestStatus:
        if self.socket is not None:
            if self._game_data_status is None:
                raise DailyQuestError("日常任务会话缺少 Game_data")
            return self._game_data_status

        self.socket = self.socket_factory(self.endpoint.url, self.timeout)
        try:
            self._send_message(
                LOGIN_MESSAGE_ID,
                encode_login_payload(self.endpoint.game_token),
                encrypted=False,
            )
            login_complete = False
            game_data_status: DailyQuestStatus | None = None
            deadline = time.monotonic() + self.timeout
            while not (login_complete and game_data_status is not None):
                header = self._receive_header(deadline, "游戏服登录及 Game_data")
                if header.message_id == PACK_PASSWORD_MESSAGE_ID:
                    encrypted_password = decode_pack_password(header.data)
                    try:
                        self.password = pack1_decode(
                            encrypted_password, SOCKET_PACK_KEY
                        ).decode("utf-8")
                    except UnicodeDecodeError as exc:
                        raise DailyQuestError("游戏服会话密码不是 UTF-8 文本") from exc
                    continue
                if self._handle_common_message(header):
                    continue
                if header.message_id == GAME_DATA_MESSAGE_ID:
                    game_data_status = decode_game_data_daily_status(header.data)
                elif header.message_id == LOGIN_REUNIQUE_MESSAGE_ID:
                    login_complete = True
            self._game_data_status = game_data_status
            # 与客户端 SocketManager 的缓存业务消息调度顺序保持一致。
            time.sleep(0.1)
            return game_data_status
        except Exception:
            self.close()
            raise

    def get_status(self) -> DailyQuestStatus:
        self.login()
        self._send_message(DAILYQUEST_INFO_MESSAGE_ID, encrypted=True)
        deadline = time.monotonic() + self.timeout
        while True:
            header = self._receive_header(deadline, "Dailyquest_info 响应")
            if self._handle_common_message(header):
                continue
            if header.message_id == DAILYQUEST_INFO_MESSAGE_ID:
                assert self._game_data_status is not None
                status = decode_dailyquest_status(
                    header.data,
                    quest_progress=self._game_data_status.quest_progress,
                )
                self._game_data_status = status
                return status

    def claim_task_reward(
        self, daily_id: int, group_id: int = DAILY_GROUP_ID
    ) -> DailyQuestRewardResponse:
        self.login()
        self._send_message(
            DAILYQUEST_GET_QUEST_REWARD_MESSAGE_ID,
            encode_daily_quest_reward_request(daily_id, group_id),
            encrypted=True,
        )
        deadline = time.monotonic() + self.timeout
        while True:
            header = self._receive_header(deadline, "Dailyquest_get_questreward 响应")
            if self._handle_common_message(header):
                continue
            if header.message_id == DAILYQUEST_GET_QUEST_REWARD_MESSAGE_ID:
                return decode_daily_quest_reward_response(header.data)

    def claim_score_reward(
        self, reward_id: int, group_id: int = DAILY_GROUP_ID
    ) -> DailyScoreRewardResponse:
        self.login()
        self._send_message(
            DAILYQUEST_GET_SCORE_REWARD_MESSAGE_ID,
            encode_daily_score_reward_request(reward_id, group_id),
            encrypted=True,
        )
        deadline = time.monotonic() + self.timeout
        while True:
            header = self._receive_header(deadline, "Dailyquest_get_scorereward 响应")
            if self._handle_common_message(header):
                continue
            if header.message_id == DAILYQUEST_GET_SCORE_REWARD_MESSAGE_ID:
                return decode_daily_score_reward_response(header.data)

    def claim_available(self, catalog: DailyCatalog) -> DailyClaimResult:
        """领取服务端明确可领取的日常/周常积分与活跃奖励。"""

        initial = self.get_status()
        group_ids = (DAILY_GROUP_ID, WEEKLY_GROUP_ID)
        group_labels = {DAILY_GROUP_ID: "日常", WEEKLY_GROUP_ID: "周常"}
        candidate_task_ids_by_group: dict[int, list[int]] = {}
        for group_id in group_ids:
            group_tasks = catalog.tasks_for_group(group_id)
            candidate_task_ids = [
                task_id
                for task_id in group_tasks
                if (task := initial.task(task_id)) is not None
                and task.finished
                and not task.score_claimed
            ]
            candidate_task_ids_by_group[group_id] = sorted(candidate_task_ids)
            for task_id in candidate_task_ids_by_group[group_id]:
                response = self.claim_task_reward(task_id, group_id)
                if response.ret != 0:
                    raise DailyQuestError(
                        f"领取{group_labels[group_id]}任务 {task_id} 积分失败："
                        f"ret={response.ret}"
                    )

        after_task_claims = self.get_status()
        claimed_task_ids_by_group: dict[int, tuple[int, ...]] = {}
        candidate_reward_ids_by_group: dict[int, list[int]] = {}
        for group_id in group_ids:
            group_tasks = catalog.tasks_for_group(group_id)
            claimed_task_ids_by_group[group_id] = tuple(
                task_id
                for task_id in candidate_task_ids_by_group[group_id]
                if (task := after_task_claims.task(task_id)) is not None
                and task.score_claimed
            )
            activity_score = sum(
                config.activity_score
                for task_id, config in group_tasks.items()
                if (task := after_task_claims.task(task_id)) is not None
                and task.score_claimed
            )
            claimed_reward_ids = after_task_claims.reward_ids_for_group(group_id)
            candidate_reward_ids = [
                reward.reward_id
                for reward in catalog.rewards_for_group(group_id)
                if reward.score <= activity_score
                and reward.reward_id not in claimed_reward_ids
            ]
            candidate_reward_ids_by_group[group_id] = candidate_reward_ids
            for reward_id in candidate_reward_ids:
                response = self.claim_score_reward(reward_id, group_id)
                if response.ret != 0:
                    raise DailyQuestError(
                        f"领取{group_labels[group_id]}活跃奖励 {reward_id} 失败："
                        f"ret={response.ret}"
                    )

        final_status = self.get_status()
        claimed_reward_ids_by_group = {
            group_id: tuple(
                reward_id
                for reward_id in candidate_reward_ids_by_group[group_id]
                if reward_id in final_status.reward_ids_for_group(group_id)
            )
            for group_id in group_ids
        }
        claimed_task_ids = tuple(
            task_id
            for group_id in group_ids
            for task_id in claimed_task_ids_by_group[group_id]
        )
        claimed_reward_ids = tuple(
            reward_id
            for group_id in group_ids
            for reward_id in claimed_reward_ids_by_group[group_id]
        )
        return DailyClaimResult(
            claimed_task_ids=claimed_task_ids,
            claimed_reward_ids=claimed_reward_ids,
            status=final_status,
            claimed_daily_task_ids=claimed_task_ids_by_group[DAILY_GROUP_ID],
            claimed_weekly_task_ids=claimed_task_ids_by_group[WEEKLY_GROUP_ID],
            claimed_daily_reward_ids=claimed_reward_ids_by_group[DAILY_GROUP_ID],
            claimed_weekly_reward_ids=claimed_reward_ids_by_group[WEEKLY_GROUP_ID],
        )


def build_daily_status_payload(
    status: DailyQuestStatus, catalog: DailyCatalog
) -> dict[str, Any]:
    """Render all configured daily tasks without exposing session information."""

    tasks: list[dict[str, Any]] = []
    for config in sorted(catalog.tasks.values(), key=lambda item: item.daily_id):
        state = status.task(config.daily_id)
        progress = status.progress_for(config.quest_id)
        tasks.append(
            {
                "daily_id": config.daily_id,
                "daily_name": daily_task_name(config.daily_id),
                "quest_id": config.quest_id,
                "quest_name": quest_name(config.quest_id),
                "finished": state.finished if state is not None else False,
                "getscore": state.score_claimed if state is not None else False,
                "progress": progress if progress is not None else 0,
                "activity_score": config.activity_score,
                "reported_by_server": state is not None,
            }
        )

    return {
        "daily_remaining_seconds": status.daily_remaining_seconds,
        "daily_reset_seconds": status.daily_reset_seconds,
        "weekly_remaining_seconds": status.weekly_remaining_seconds,
        "weekly_reset_seconds": status.weekly_reset_seconds,
        "claimed_reward_ids": list(status.daily_reward_ids),
        "claimed_reward_names": [
            activity_reward_name(reward_id) for reward_id in status.daily_reward_ids
        ],
        "claimed_daily_reward_ids": list(status.daily_reward_ids),
        "claimed_daily_reward_names": [
            activity_reward_name(reward_id) for reward_id in status.daily_reward_ids
        ],
        "claimed_weekly_reward_ids": list(status.weekly_reward_ids),
        "claimed_weekly_reward_names": [
            activity_reward_name(reward_id) for reward_id in status.weekly_reward_ids
        ],
        "tasks": tasks,
    }


def build_daily_result_log_record(
    endpoint: GameEndpoint,
    operation: str,
    status: DailyQuestStatus,
    catalog: DailyCatalog,
    *,
    claimed_task_ids: tuple[int, ...] = (),
    claimed_reward_ids: tuple[int, ...] = (),
    claimed_daily_task_ids: tuple[int, ...] = (),
    claimed_weekly_task_ids: tuple[int, ...] = (),
    claimed_daily_reward_ids: tuple[int, ...] = (),
    claimed_weekly_reward_ids: tuple[int, ...] = (),
    timestamp: str | None = None,
) -> dict[str, Any]:
    """Build a JSONL record that omits URLs, tokens, passwords, and packets."""

    return {
        "timestamp": timestamp
        or datetime.now().astimezone().isoformat(timespec="seconds"),
        "event": "daily_quest",
        "operation": operation,
        "zone": {"id": endpoint.zone_id, "name": endpoint.zone_name},
        "claimed_task_ids": list(claimed_task_ids),
        "claimed_task_names": [daily_task_name(task_id) for task_id in claimed_task_ids],
        "claimed_reward_ids": list(claimed_reward_ids),
        "claimed_reward_names": [
            activity_reward_name(reward_id) for reward_id in claimed_reward_ids
        ],
        "claimed_daily_task_ids": list(claimed_daily_task_ids),
        "claimed_daily_task_names": [
            daily_task_name(task_id) for task_id in claimed_daily_task_ids
        ],
        "claimed_weekly_task_ids": list(claimed_weekly_task_ids),
        "claimed_weekly_task_names": [
            daily_task_name(task_id) for task_id in claimed_weekly_task_ids
        ],
        "claimed_daily_reward_ids": list(claimed_daily_reward_ids),
        "claimed_daily_reward_names": [
            activity_reward_name(reward_id)
            for reward_id in claimed_daily_reward_ids
        ],
        "claimed_weekly_reward_ids": list(claimed_weekly_reward_ids),
        "claimed_weekly_reward_names": [
            activity_reward_name(reward_id)
            for reward_id in claimed_weekly_reward_ids
        ],
        "status": build_daily_status_payload(status, catalog),
    }


def append_daily_result_log(
    path: Path | object | None,
    endpoint: GameEndpoint,
    operation: str,
    status: DailyQuestStatus,
    catalog: DailyCatalog,
    *,
    claimed_task_ids: tuple[int, ...] = (),
    claimed_reward_ids: tuple[int, ...] = (),
    claimed_daily_task_ids: tuple[int, ...] = (),
    claimed_weekly_task_ids: tuple[int, ...] = (),
    claimed_daily_reward_ids: tuple[int, ...] = (),
    claimed_weekly_reward_ids: tuple[int, ...] = (),
    timestamp: str | None = None,
) -> dict[str, Any]:
    """Append a complete redacted daily result and return its logged record."""

    record = build_daily_result_log_record(
        endpoint,
        operation,
        status,
        catalog,
        claimed_task_ids=claimed_task_ids,
        claimed_reward_ids=claimed_reward_ids,
        claimed_daily_task_ids=claimed_daily_task_ids,
        claimed_weekly_task_ids=claimed_weekly_task_ids,
        claimed_daily_reward_ids=claimed_daily_reward_ids,
        claimed_weekly_reward_ids=claimed_weekly_reward_ids,
        timestamp=timestamp,
    )
    details = {key: value for key, value in record.items() if key not in {"timestamp", "event", "operation", "zone"}}
    try:
        result = write_standard_log(
            event="daily_quest", operation=operation, zone=record["zone"], details=details,
            destination=path, timestamp=record["timestamp"],
        )
    except LogPersistenceError as exc:
        raise DailyQuestError(f"写入日常结果日志失败：{exc}") from exc
    return result.record if result is not None else None


def build_parser() -> argparse.ArgumentParser:
    parser = build_base_parser()
    parser.prog = "daily_quest.py"
    parser.description = __doc__
    parser.add_argument(
        "--result-log",
        type=Path,
        default=DEFAULT_RESULT_LOG,
        help="脱敏 JSONL 结果日志路径。",
    )
    parser.add_argument(
        "command",
        choices=("status", "claim"),
        nargs="?",
        help="status 查询日常/周常状态；claim 领取服务端明确可领取的两组奖励。",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.self_test:
        run_self_tests()
        print("日常任务本地协议自检通过")
        return 0
    if args.command is None:
        parser.error("需要指定 status 或 claim")

    try:
        catalog = load_daily_catalog()
        tokens = load_tokens(args.token_file)
        endpoint = resolve_game_endpoint(tokens, args)
        with DailyQuestClient(endpoint, args.timeout) as client:
            if args.command == "status":
                status = client.get_status()
                claimed_task_ids: tuple[int, ...] = ()
                claimed_reward_ids: tuple[int, ...] = ()
                claimed_daily_task_ids: tuple[int, ...] = ()
                claimed_weekly_task_ids: tuple[int, ...] = ()
                claimed_daily_reward_ids: tuple[int, ...] = ()
                claimed_weekly_reward_ids: tuple[int, ...] = ()
            else:
                claims = client.claim_available(catalog)
                status = claims.status
                claimed_task_ids = claims.claimed_task_ids
                claimed_reward_ids = claims.claimed_reward_ids
                claimed_daily_task_ids = claims.claimed_daily_task_ids
                claimed_weekly_task_ids = claims.claimed_weekly_task_ids
                claimed_daily_reward_ids = claims.claimed_daily_reward_ids
                claimed_weekly_reward_ids = claims.claimed_weekly_reward_ids
        record = append_daily_result_log(
            args.result_log,
            endpoint,
            args.command,
            status,
            catalog,
            claimed_task_ids=claimed_task_ids,
            claimed_reward_ids=claimed_reward_ids,
            claimed_daily_task_ids=claimed_daily_task_ids,
            claimed_weekly_task_ids=claimed_weekly_task_ids,
            claimed_daily_reward_ids=claimed_daily_reward_ids,
            claimed_weekly_reward_ids=claimed_weekly_reward_ids,
        )
    except HarvestError as exc:
        print(f"日常状态或奖励操作失败：{exc}", file=sys.stderr)
        return 1

    print(json.dumps(record, ensure_ascii=False, indent=2))
    if args.result_log is MANAGED_DESTINATION:
        print("结果日志：logs/daily_quest/<日期>.jsonl", file=sys.stderr)
    else:
        print(f"结果日志：{args.result_log.expanduser().resolve()}", file=sys.stderr)
    return 0


def _encode_daily_task_state(
    daily_id: int, *, finished: bool, score_claimed: bool
) -> bytes:
    payload = encode_int_field(1, daily_id)
    if finished:
        payload += encode_int_field(2, 1)
    if score_claimed:
        payload += encode_int_field(3, 1)
    return payload


def _encode_game_data_for_test(
    *,
    daily_state: bytes,
    quest_id: int,
    progress: int,
) -> bytes:
    progress_value = encode_int_field(1, progress)
    progress_entry = encode_int_field(1, quest_id) + encode_bytes_field(2, progress_value)
    quest = encode_int_field(1, quest_id) + encode_bytes_field(6, progress_entry)
    quest_list = encode_bytes_field(1, quest)
    quest_data = encode_bytes_field(1, quest_list)
    return encode_bytes_field(12, quest_data) + encode_bytes_field(19, daily_state)


def run_self_tests() -> None:
    daily_state = encode_bytes_field(
        7, _encode_daily_task_state(101, finished=False, score_claimed=False)
    )
    daily_state += encode_bytes_field(
        7, _encode_daily_task_state(104, finished=True, score_claimed=True)
    )
    daily_state += encode_int_field(1, 3600)
    daily_state += encode_int_field(2, 7200)
    daily_state += encode_int_field(3, 101)
    daily_state += encode_int_field(4, 3 * 24 * 3600)
    daily_state += encode_int_field(5, 7 * 24 * 3600)
    daily_state += encode_int_field(6, 201)
    game_data = _encode_game_data_for_test(
        daily_state=daily_state,
        quest_id=50001,
        progress=3,
    )
    status = decode_game_data_daily_status(game_data)
    assert status.task(101) == DailyTaskState(101, False, False)
    assert status.task(104) == DailyTaskState(104, True, True)
    assert status.progress_for(50001) == 3
    assert status.daily_reward_ids == (101,)
    assert status.weekly_remaining_seconds == 3 * 24 * 3600
    assert status.weekly_reset_seconds == 7 * 24 * 3600
    assert status.weekly_reward_ids == (201,)
    assert encode_daily_quest_reward_request(101) == b"\x08e\x10\x01"
    assert encode_daily_score_reward_request(102) == b"\x08\x01\x10f"
    assert encode_daily_quest_reward_request(201, WEEKLY_GROUP_ID) == b"\x08\xc9\x01\x10\x02"
    assert encode_daily_score_reward_request(202, WEEKLY_GROUP_ID) == b"\x08\x02\x10\xca\x01"

    task_reward = decode_daily_quest_reward_response(
        encode_int_field(1, 0)
        + encode_int_field(2, 101)
        + encode_int_field(3, 1)
        + encode_bytes_field(4, encode_varint(101))
    )
    assert task_reward == DailyQuestRewardResponse(0, 101, 1, (101,))
    score_reward = decode_daily_score_reward_response(
        encode_int_field(1, 0)
        + encode_int_field(2, 1)
        + encode_int_field(3, 102)
        + encode_bytes_field(4, encode_varint(102))
    )
    assert score_reward == DailyScoreRewardResponse(0, 1, 102, (102,))

    class TestSocket:
        def __init__(self, frames: list[tuple[int, bytes]]) -> None:
            self.frames = frames
            self.binary_frames: list[bytes] = []
            self.text_frames: list[str] = []
            self.closed = False

        def send_binary(self, payload: bytes) -> None:
            self.binary_frames.append(payload)

        def send_text(self, payload: str) -> None:
            self.text_frames.append(payload)

        def recv_message(self, timeout: float) -> tuple[int, bytes]:
            if not self.frames:
                raise socket.timeout()
            return self.frames.pop(0)

        def close(self) -> None:
            self.closed = True

    session_password = "87654321"
    password_payload = encode_bytes_field(
        1, pack1_encode(session_password.encode("utf-8"), SOCKET_PACK_KEY).encode("utf-8")
    )

    def encrypted(message_id: int, payload: bytes = b"") -> tuple[int, bytes]:
        packet = encode_message_header(message_id, payload)
        return 0x2, pack1_encode(packet, session_password).encode("utf-8")

    socket_state = TestSocket(
        [
            (0x2, encode_message_header(PACK_PASSWORD_MESSAGE_ID, password_payload)),
            encrypted(GAME_DATA_MESSAGE_ID, game_data),
            encrypted(LOGIN_REUNIQUE_MESSAGE_ID),
            encrypted(DAILYQUEST_INFO_MESSAGE_ID, daily_state),
        ]
    )
    endpoint = GameEndpoint("ws://test.invalid", "game-token", "1", "test")
    client = DailyQuestClient(
        endpoint,
        1.0,
        socket_factory=lambda _url, _timeout: socket_state,
    )
    try:
        queried = client.get_status()
    finally:
        client.close()
    assert queried.progress_for(50001) == 3
    assert socket_state.closed
    assert decode_message_header(socket_state.binary_frames[0]).message_id == LOGIN_MESSAGE_ID
    query_packet = decode_message_header(
        pack1_decode(socket_state.text_frames[0], session_password)
    )
    assert query_packet == MessageHeader(DAILYQUEST_INFO_MESSAGE_ID, 0, b"")


if __name__ == "__main__":
    raise SystemExit(main())
