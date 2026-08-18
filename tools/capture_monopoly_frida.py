#!/usr/bin/env python3
"""Attach a read-only Monopoly WebSocket hook and save matching packets."""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import frida


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DEVICE = os.environ.get("ANDROID_SERIAL", "emulator-5554")
DEFAULT_PACKAGE = "com.zygames.dungeon4"
DEFAULT_OUT = Path("/tmp/dungeon4_monopoly_frida.jsonl")
DEFAULT_LOCAL_PORT = int(os.environ.get("FRIDA_LOCAL_PORT", "27042"))


def run_adb(device: str, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["adb", "-s", device, *args],
        check=check,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def require_device(device: str) -> None:
    state = run_adb(device, "get-state", check=False)
    if state.returncode != 0 or state.stdout.strip() != "device":
        detail = state.stderr.strip() or state.stdout.strip() or "unknown ADB error"
        raise RuntimeError(f"真机未就绪：{detail}")


def running_pid(device: str, package: str) -> int:
    result = run_adb(device, "shell", "pidof", package, check=False)
    values = result.stdout.strip().split()
    if not values:
        raise RuntimeError("游戏进程未运行")
    return int(values[0])


def load_script_source() -> str:
    paths = (
        ROOT / "node_modules" / "frida-il2cpp-bridge" / "dist" / "index.js",
        ROOT / "tools" / "frida_hook_monopoly.js",
    )
    missing = [str(path) for path in paths if not path.is_file()]
    if missing:
        raise RuntimeError("缺少 Frida 脚本：" + ", ".join(missing))
    return "\n\n".join(path.read_text(encoding="utf-8") for path in paths)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--device", default=DEFAULT_DEVICE)
    parser.add_argument("--package", default=DEFAULT_PACKAGE)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--local-port", type=int, default=DEFAULT_LOCAL_PORT)
    parser.add_argument("--duration", type=float, default=90.0)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    require_device(args.device)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    # ADB already exposes the USB Frida transport. Going through a second
    # TCP forward can hang before a script is loaded on this device.
    device = frida.get_usb_device(timeout=5)
    if device.id != args.device:
        raise RuntimeError(f"Frida 设备不匹配：期望 {args.device}，实际 {device.id}")
    session = device.attach(running_pid(args.device, args.package))
    script = session.create_script(load_script_source())
    stop = False

    def request_stop(_signal: int, _frame: object) -> None:
        nonlocal stop
        stop = True

    def on_message(message: dict[str, Any], _data: bytes | None) -> None:
        payload = message.get("payload") if message.get("type") == "send" else None
        if isinstance(payload, dict) and payload.get("type") == "monopoly_ws":
            record = dict(payload)
            record["captured_at"] = int(time.time() * 1000)
            output.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
            output.flush()
            print(
                f"{record['direction']} {record['message_id']} "
                f"{record['message_name']} {record['payload_hex']}",
                flush=True,
            )
        elif isinstance(payload, dict):
            print("[frida] " + json.dumps(payload, ensure_ascii=False), flush=True)
        else:
            print("[frida] " + str(message), file=sys.stderr, flush=True)

    script.on("message", on_message)
    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)
    with args.out.open("w", encoding="utf-8") as output:
        script.load()
        print(f"已附加 {args.device}；只读采集 {args.duration:g} 秒：{args.out}", flush=True)
        deadline = time.monotonic() + args.duration
        while not stop and time.monotonic() < deadline:
            time.sleep(0.2)
    script.unload()
    session.detach()
    print(f"采集结束：{args.out}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(f"error: {error}", file=sys.stderr)
        raise SystemExit(1)
