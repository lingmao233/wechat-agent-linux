#!/usr/bin/env bash
# Source from the SAME graphical login that owns WeChat.
export QT_LINUX_ACCESSIBILITY_ALWAYS_ON=1
export QT_ACCESSIBILITY=1
export NO_AT_BRIDGE=0
wechat_session_file="${WECHAT_STATE_DIR:-${XDG_STATE_HOME:-$HOME/.local/state}/wechat-agent-linux}/graphical-session.env"
if [[ -z "${DBUS_SESSION_BUS_ADDRESS:-}" && -r "$wechat_session_file" && -O "$wechat_session_file" ]]; then
  source "$wechat_session_file"
fi
if [[ -z "${DISPLAY:-}" && -S /tmp/.X11-unix/X0 ]]; then
  export DISPLAY=:0
fi
if [[ -z "${XDG_RUNTIME_DIR:-}" && -d /run/user/$(id -u) ]]; then
  export XDG_RUNTIME_DIR="/run/user/$(id -u)"
fi
unset wechat_session_file
