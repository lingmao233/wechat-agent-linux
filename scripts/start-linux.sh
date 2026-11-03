#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."
source scripts/linux-session.sh
export PYTHONUNBUFFERED=1
export PYTHONPATH="$PWD/src${PYTHONPATH:+:$PYTHONPATH}"
umask 077
exec "${WECHAT_PYTHON:-.venv/bin/python}" -m wechat_agent "${1:-desktop}" "${@:2}"
