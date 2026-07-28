#!/usr/bin/env bash
# Push and start frida-server on a connected Android device/emulator.
# Host frida version: 17.16.3 (must match frida-server binary).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
VER="17.16.3"
BIN_DIR="${ROOT}/frida-server"
DEST="/data/local/tmp/frida-server"
DEVICE="${ANDROID_SERIAL:-emulator-5554}"
ADB=(adb -s "${DEVICE}")

if ! command -v adb >/dev/null 2>&1; then
  echo "error: adb not found" >&2
  exit 1
fi

if ! "${ADB[@]}" get-state >/dev/null 2>&1; then
  echo "error: emulator ${DEVICE} is unavailable. Start it or set ANDROID_SERIAL." >&2
  adb devices
  exit 1
fi

ARCH="$("${ADB[@]}" shell getprop ro.product.cpu.abi | tr -d '\r')"
case "${ARCH}" in
  arm64-v8a)  SUFFIX="android-arm64" ;;
  armeabi-v7a|armeabi) SUFFIX="android-arm" ;;
  x86_64)     SUFFIX="android-x86_64" ;;
  x86)        SUFFIX="android-x86" ;;
  *)
    echo "error: unsupported abi: ${ARCH}" >&2
    exit 1
    ;;
esac

SRC="${BIN_DIR}/frida-server-${VER}-${SUFFIX}"
if [[ ! -x "${SRC}" ]]; then
  echo "error: missing binary: ${SRC}" >&2
  exit 1
fi

echo "device: ${DEVICE}, abi: ${ARCH} -> ${SRC##*/}"
"${ADB[@]}" push "${SRC}" "${DEST}"
"${ADB[@]}" shell "chmod 755 ${DEST}"

# An Android app process cannot be instrumented by a shell-user server. Standard
# AVDs permit adb root; fall back to su for rooted production-like images.
if ! "${ADB[@]}" shell id 2>/dev/null | grep -q "uid=0"; then
  "${ADB[@]}" root >/dev/null 2>&1 || true
  "${ADB[@]}" wait-for-device
fi

"${ADB[@]}" shell "pkill -f frida-server || true" >/dev/null 2>&1 || true
if "${ADB[@]}" shell id 2>/dev/null | grep -q "uid=0"; then
  "${ADB[@]}" shell "${DEST} -D &"
elif "${ADB[@]}" shell "su -c 'id'" 2>/dev/null | grep -q "uid=0"; then
  "${ADB[@]}" shell "su -c '${DEST} -D &'"
else
  echo "error: frida-server needs root; use a root-enabled Android emulator" >&2
  exit 1
fi

echo "frida-server started as root"

sleep 1
echo "verify with:  ANDROID_SERIAL=${DEVICE} $(dirname "$0")/../../.venv/bin/frida-ps -U"
