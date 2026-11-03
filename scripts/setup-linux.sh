#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."
PYTHON_BIN="${WECHAT_PYTHON:-python3}"
if ! "$PYTHON_BIN" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)'; then
  printf '%s\n' '需要 Python 3.11+；Ubuntu 22.04 请先准备对应解释器，并设置 WECHAT_PYTHON。' >&2
  exit 2
fi
if [[ "${WECHAT_SKIP_APT:-0}" != "1" ]]; then
  # pilk publishes Windows wheels; Linux builds its SILK codec locally.
  wechat_build_packages=(build-essential)
  if ! "$PYTHON_BIN" -c 'from pathlib import Path; import sysconfig; raise SystemExit(0 if (Path(sysconfig.get_paths()["include"]) / "Python.h").is_file() else 1)'; then
    wechat_build_packages+=("$("$PYTHON_BIN" -c 'import sys; print("python%d.%d-dev" % sys.version_info[:2])')")
  fi
  sudo apt-get install -y python3-venv python3-pip python3-pyatspi python3-gi gir1.2-gtk-3.0 at-spi2-core \
    dbus-x11 libglib2.0-bin xdotool xclip x11-utils imagemagick ffmpeg pulseaudio-utils "${wechat_build_packages[@]}"
fi
wechat_venv_dir="${WECHAT_VENV_DIR:-.venv}"
if [[ -e "$wechat_venv_dir" && ! -x "$wechat_venv_dir/bin/python" ]]; then
  printf '%s\n' '目标虚拟环境不是 Linux venv；保留原目录，请设置 WECHAT_VENV_DIR 为新的 Linux 路径。' >&2
  exit 2
fi
"$PYTHON_BIN" -m venv --system-site-packages "$wechat_venv_dir"
"$wechat_venv_dir/bin/python" -m pip install -e '.[test]'
mkdir -p state
chmod 700 state
if [[ ! -e .env ]]; then
  cp .env.linux.example .env
  chmod 600 .env
fi
if [[ "${WECHAT_SKIP_INIT_DB:-0}" != "1" ]]; then
  "$wechat_venv_dir/bin/python" -m wechat_agent init-db
fi
printf '%s\n' '填写 .env 的账号目录、群映射和模型配置，然后运行 bash scripts/enable-a11y.sh。'
