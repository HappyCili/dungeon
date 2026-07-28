#!/usr/bin/env bash
# Capture one legion-war daily run from the selected Android emulator.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
REPO="$(cd "${ROOT}/.." && pwd)"
PYTHON="${REPO}/.venv/bin/python"
DEVICE="${ANDROID_SERIAL:-emulator-5554}"
OUT="${FRIDA_LEGION_WAR_LOG:-/tmp/dungeon4_legion_war_frida.jsonl}"
PORT="${FRIDA_LOCAL_PORT:-27042}"

if [[ ! -x "${PYTHON}" ]]; then
  PYTHON="${REPO}/venv/bin/python"
fi
if [[ ! -x "${PYTHON}" ]]; then
  echo "error: Python venv not found under ${REPO}" >&2
  exit 1
fi

cd "${ROOT}"
"${PYTHON}" tools/capture_legion_war_frida.py --device "${DEVICE}" --local-port "${PORT}" --out "${OUT}"
"${PYTHON}" tools/validate_legion_war_capture.py "${OUT}"
