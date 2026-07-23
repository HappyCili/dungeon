#!/usr/bin/env bash
# 附加到已运行的地下城堡4，抓取 Battle_info / 地图协议收发。
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
REPO="$(cd "${ROOT}/.." && pwd)"
FRIDA="${REPO}/venv/bin/frida"
BRIDGE="${ROOT}/node_modules/frida-il2cpp-bridge/dist/index.js"
HOOK="${ROOT}/tools/frida_hook_battle_info.js"
OUT="${FRIDA_BATTLE_LOG:-/tmp/dungeon4_battle_frida.jsonl}"

if [[ ! -x "${FRIDA}" ]]; then
  FRIDA="${REPO}/.venv/bin/frida"
fi
if [[ ! -x "${FRIDA}" ]]; then
  echo "error: frida not found in venv" >&2
  exit 1
fi
if [[ ! -f "${BRIDGE}" ]]; then
  echo "error: missing ${BRIDGE} (npm i frida-il2cpp-bridge in ui_app)" >&2
  exit 1
fi

if ! adb get-state >/dev/null 2>&1; then
  echo "error: no adb device" >&2
  exit 1
fi

PID="$(adb shell pidof com.zygames.dungeon4 2>/dev/null | tr -d '\r' | awk '{print $1}')"
if [[ -z "${PID}" ]]; then
  echo "error: game not running. Start com.zygames.dungeon4 first." >&2
  exit 1
fi

echo "attach pid=${PID} package=com.zygames.dungeon4"
echo "jsonl => ${OUT}"
echo "In game: enter treasure map / start fight / re-login into battle."
echo "Ctrl+C to stop."
: >"${OUT}"

# frida message -> jsonl
export PYTHONUNBUFFERED=1
"${FRIDA}" -U -p "${PID}" \
  -l "${BRIDGE}" \
  -l "${HOOK}" \
  2> >(tee /tmp/dungeon4_battle_frida_stderr.log >&2) |
  while IFS= read -r line; do
    echo "${line}"
    # 终端 [battle] 行也落一份
    if [[ "${line}" == \[battle\]* ]] || [[ "${line}" == message:* ]]; then
      printf '%s\n' "{\"raw\":$(python3 -c 'import json,sys; print(json.dumps(sys.argv[1]))' "${line}")}" >>"${OUT}" || true
    fi
  done
