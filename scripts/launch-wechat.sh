#!/usr/bin/env bash
# Keep the client, accessibility registry and agent on one graphical session.
set -euo pipefail
if [[ "${1:-}" == --help ]]; then
  printf '%s\n' 'Usage: bash scripts/launch-wechat.sh [WeChat arguments]' \
    'Run as the desktop user. An existing WeChat process is never replaced.' \
    'Then source scripts/linux-session.sh in the terminal used for the agent.'
  exit 0
fi
if [[ $(id -u) == 0 ]]; then
  printf '%s\n' '请使用运行微信的普通用户启动，不能以 root 运行整个客户端。' >&2
  exit 2
fi
umask 077
export WECHAT_STATE_DIR="${WECHAT_STATE_DIR:-${XDG_STATE_HOME:-$HOME/.local/state}/wechat-agent-linux}"
mkdir -p -- "$WECHAT_STATE_DIR/desktop"
chmod 700 "$WECHAT_STATE_DIR/desktop"
if [[ "${1:-}" != --session ]]; then
  if pgrep -u "$(id -u)" -x wechat >/dev/null || pgrep -u "$(id -u)" -x weixin >/dev/null; then
    printf '%s\n' '微信已在运行；本命令不会替换现有窗口。先正常退出微信后再启动独立无障碍会话。' >&2
    exit 3
  fi
  exec 9>"$WECHAT_STATE_DIR/desktop/session.lock"
  flock -n 9 || { printf '%s\n' '微信图形会话已由本脚本管理。' >&2; exit 3; }
  # Accessibility uses a private runtime; audio stays on the desktop user's
  # existing PipeWire/Pulse server instead of looking inside that empty runtime.
  wechat_audio_runtime="${PIPEWIRE_RUNTIME_DIR:-${XDG_RUNTIME_DIR:-/run/user/$(id -u)}}"
  if [[ -z "${PULSE_SERVER:-}" && -S "$wechat_audio_runtime/pulse/native" ]]; then
    export PULSE_SERVER="unix:$wechat_audio_runtime/pulse/native"
  fi
  if [[ -z "${PIPEWIRE_RUNTIME_DIR:-}" && -S "$wechat_audio_runtime/pipewire-0" ]]; then
    export PIPEWIRE_RUNTIME_DIR="$wechat_audio_runtime"
  fi
  export XDG_RUNTIME_DIR
  XDG_RUNTIME_DIR="$(mktemp -d "$WECHAT_STATE_DIR/desktop/runtime.XXXXXXXX")"
  unset DBUS_SESSION_BUS_ADDRESS AT_SPI_BUS_ADDRESS
  exec dbus-run-session -- bash "$0" --session "$@"
fi
shift
export DISPLAY="${DISPLAY:-:0}"
export QT_QPA_PLATFORM=xcb
export QT_ACCESSIBILITY=1 QT_LINUX_ACCESSIBILITY_ALWAYS_ON=1 NO_AT_BRIDGE=0
gdbus call --session --dest org.a11y.Bus --object-path /org/a11y/bus \
  --method org.freedesktop.DBus.Properties.Set org.a11y.Status IsEnabled '<true>' >/dev/null
gdbus call --session --dest org.a11y.Bus --object-path /org/a11y/bus \
  --method org.freedesktop.DBus.Properties.Set org.a11y.Status ScreenReaderEnabled '<true>' >/dev/null
wechat_bus_response="$(gdbus call --session --dest org.a11y.Bus --object-path /org/a11y/bus --method org.a11y.Bus.GetAddress)"
export AT_SPI_BUS_ADDRESS
AT_SPI_BUS_ADDRESS="$(printf '%s' "$wechat_bus_response" | python3 -c 'import ast,sys; print(ast.literal_eval(sys.stdin.read())[0])')"
gdbus call --address "$AT_SPI_BUS_ADDRESS" --dest org.a11y.atspi.Registry \
  --object-path /org/a11y/atspi/registry --method org.freedesktop.DBus.Peer.Ping >/dev/null
python3 - <<'PY'
import os
from pathlib import Path
import shlex
target = Path(os.environ['WECHAT_STATE_DIR']) / 'graphical-session.env'
keys = ('DISPLAY', 'WAYLAND_DISPLAY', 'PULSE_SERVER', 'PIPEWIRE_RUNTIME_DIR', 'QT_QPA_PLATFORM',
        'DBUS_SESSION_BUS_ADDRESS', 'AT_SPI_BUS_ADDRESS', 'XDG_RUNTIME_DIR',
        'QT_ACCESSIBILITY', 'QT_LINUX_ACCESSIBILITY_ALWAYS_ON', 'NO_AT_BRIDGE')
temporary = target.with_suffix('.tmp')
temporary.write_text('\n'.join('export ' + key + '=' + shlex.quote(os.environ[key])
                               for key in keys if key in os.environ) + '\n')
os.chmod(temporary, 0o600)
temporary.replace(target)
PY
printf '%s\n' '独立无障碍会话已就绪；请保留此启动进程，Agent 使用 graphical-session.env 中的同一会话。'
exec "${WECHAT_EXECUTABLE:-/opt/wechat/wechat}" --force-renderer-accessibility "$@"
