#!/usr/bin/env python3
"""Attach to the Android emulator and save observed legion-war packets as JSONL."""

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
DEFAULT_OUT = Path("/tmp/dungeon4_legion_war_frida.jsonl")
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
        raise RuntimeError(f"模拟器 {device} 未就绪：{detail}")


def running_pid(device: str, package: str) -> int:
    result = run_adb(device, "shell", "pidof", package, check=False)
    pid_text = result.stdout.strip().split()
    if not pid_text:
        raise RuntimeError(f"模拟器中未运行 {package}；请先启动并登录游戏")
    return int(pid_text[0])


def load_script_source() -> str:
    bridge = ROOT / "node_modules" / "frida-il2cpp-bridge" / "dist" / "index.js"
    hook = ROOT / "tools" / "frida_hook_legion_war.js"
    missing = [str(path) for path in (bridge, hook) if not path.is_file()]
    if missing:
        raise RuntimeError("缺少 Frida 脚本：" + ", ".join(missing))
    return bridge.read_text(encoding="utf-8") + "\n\n" + hook.read_text(encoding="utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--device", default=DEFAULT_DEVICE, help="ADB 模拟器序列号")
    parser.add_argument("--package", default=DEFAULT_PACKAGE, help="Android 包名")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT, help="JSONL 输出路径")
    parser.add_argument(
        "--spawn",
        action="store_true",
        help="由 Frida 重启并 spawn 应用，适用于附加现有进程超时的真机",
    )
    parser.add_argument(
        "--local-port",
        type=int,
        default=DEFAULT_LOCAL_PORT,
        help="主机转发端口；同一台主机连接多个设备时分别设置",
    )
    parser.add_argument(
        "--duration",
        type=float,
        default=0,
        help="采集秒数；0 表示持续至 Ctrl+C",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    require_device(args.device)
    if not 1024 <= args.local_port <= 65535:
        raise ValueError("--local-port 必须在 1024 到 65535 之间")
    args.out.parent.mkdir(parents=True, exist_ok=True)

    # ADB forwarding makes the Frida target unambiguously the selected emulator.
    run_adb(
        args.device,
        "forward",
        f"tcp:{args.local_port}",
        "tcp:27042",
    )
    manager = frida.get_device_manager()
    device = manager.add_remote_device(f"127.0.0.1:{args.local_port}")
    try:
        device.enumerate_processes()
    except frida.TransportError as error:
        raise RuntimeError(
            "无法连接模拟器中的 frida-server；先运行 "
            f"ANDROID_SERIAL={args.device} ./tools/start_frida_server.sh"
        ) from error

    spawned = False
    if args.spawn:
        run_adb(args.device, "shell", "am", "force-stop", args.package)
        pid = device.spawn([args.package])
        spawned = True
    else:
        pid = running_pid(args.device, args.package)
    session = device.attach(pid)
    script = session.create_script(load_script_source())
    stop = False

    def request_stop(_signal: int, _frame: object) -> None:
        nonlocal stop
        stop = True

    def on_message(message: dict[str, Any], _data: bytes | None) -> None:
        payload = message.get("payload") if message.get("type") == "send" else None
        if isinstance(payload, dict) and payload.get("type") == "legion_ws":
            record = dict(payload)
            record["captured_at"] = int(time.time() * 1000)
            output.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
            output.flush()
            print(
                f"{record['direction']} {record['message_id']} "
                f"{record['message_name']} payload={len(record['payload_hex']) // 2}",
                flush=True,
            )
            return
        if isinstance(payload, dict):
            print("[frida] " + json.dumps(payload, ensure_ascii=False), file=sys.stderr)
            return
        print("[frida] " + str(message), file=sys.stderr)

    script.on("message", on_message)
    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)
    with args.out.open("w", encoding="utf-8") as output:
        script.load()
        if spawned:
            device.resume(pid)
        print(
            f"已附加 {args.device} pid={pid}{'（spawn）' if spawned else ''}；"
            "执行一次军团战日常，Ctrl+C 停止。\n"
            f"日志：{args.out}",
            flush=True,
        )
        deadline = time.monotonic() + args.duration if args.duration > 0 else None
        while not stop and (deadline is None or time.monotonic() < deadline):
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
