#!/usr/bin/env bash
# Push and start frida-server on a connected Android device/emulator.
# Host frida version: 17.16.3 (must match frida-server binary).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
VER="17.16.3"
BIN_DIR="${ROOT}/frida-server"
DEST="/data/local/tmp/frida-server"

if ! command -v adb >/dev/null 2>&1; then
  echo "error: adb not found" >&2
  exit 1
fi

if ! adb get-state >/dev/null 2>&1; then
  echo "error: no adb device. Start an emulator or connect a rooted device first." >&2
  adb devices
  exit 1
fi

ARCH="$(adb shell getprop ro.product.cpu.abi | tr -d '\r')"
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

echo "device abi: ${ARCH} -> ${SRC##*/}"
adb push "${SRC}" "${DEST}"
adb shell "chmod 755 ${DEST}"

# Stop any previous frida-server, then start as root if possible.
adb shell "pkill -f frida-server || true" >/dev/null 2>&1 || true
if adb shell "su -c 'id'" 2>/dev/null | grep -q "uid=0"; then
  adb shell "su -c '${DEST} -D &'"
  echo "frida-server started as root"
else
  # Some emulators run adbd as root already.
  adb shell "${DEST} -D &" || {
    echo "error: failed to start frida-server (need root / adbd root)" >&2
    exit 1
  }
  echo "frida-server started (no su; hope adbd is root)"
fi

sleep 1
echo "verify with:  $(dirname "$0")/../../.venv/bin/frida-ps -U"
