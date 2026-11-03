#!/usr/bin/env bash
# Try local recovery first; elevate only a one-off memory scan if necessary.
set -euo pipefail
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
if [[ "$EUID" -eq 0 ]]; then
  echo 'Run this script as your normal desktop user; it elevates only the scan.' >&2
  exit 2
fi
PY="${WECHAT_PYTHON:-$HOME/.venvs/wechat-agent-linux/bin/python}"
if [[ ! -x "$PY" ]]; then
  PY="$ROOT/.venv/bin/python"
fi
if [[ ! -x "$PY" ]]; then
  echo 'Set WECHAT_PYTHON to the project Python executable.' >&2
  exit 2
fi
if report="$(PYTHONPATH="$ROOT/src" "$PY" -m wechat_agent keyscan --images-only --local-only --config "$ROOT/.env")"; then
  printf '%s\n' "$report"
  exit 0
fi
if [[ "$report" != *'"IMAGE_KEY_NOT_FOUND"'* ]]; then
  printf '%s\n' "$report"
  exit 2
fi
sudo env HOME="$HOME" PYTHONPATH="$ROOT/src" "$PY" -m wechat_agent keyscan --images-only --config "$ROOT/.env"
