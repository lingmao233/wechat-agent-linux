#!/usr/bin/env bash
# Restart the desktop console from a shell without a terminal of its own,
# for example over SSH from Windows.  The private graphical session created
# by scripts/launch-wechat.sh is reused so AT-SPI keeps working.
set -uo pipefail

usage() {
  cat <<'EOF'
用法: bash scripts/restart-desktop.sh [选项]

  --start-agent   控制台就绪后自动调用一次 /api/start，Agent 立即开始工作
  --port N        控制台端口，默认 8765（也可用环境变量 WECHAT_DESKTOP_PORT）
  -h, --help      显示本帮助

前提: 微信运行在 launch-wechat.sh 打开的私有会话里，且 graphical-session.env 仍有效。
日志: ~/.local/state/wechat-agent-linux/desktop.log
EOF
}

PORT="${WECHAT_DESKTOP_PORT:-8765}"
start_agent=0
while (($#)); do
  case "$1" in
    --start-agent) start_agent=1; shift ;;
    --port)
      [[ -n "${2:-}" ]] || { printf '选项 --port 需要参数\n' >&2; exit 2; }
      PORT="$2"; shift 2 ;;
    -h | --help) usage; exit 0 ;;
    *) printf '未知参数: %s\n' "$1" >&2; usage >&2; exit 2 ;;
  esac
done

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT" || { printf '无法进入项目目录\n' >&2; exit 1; }

ENV_FILE="${WECHAT_ENV_FILE:-$ROOT/.env}"
STATE_DIR="${WECHAT_STATE_DIR:-${XDG_STATE_HOME:-$HOME/.local/state}/wechat-agent-linux}"
SESSION_FILE="$STATE_DIR/graphical-session.env"
PY="${WECHAT_PYTHON:-$HOME/.venvs/wechat-agent-linux/bin/python}"
LOG="$STATE_DIR/desktop.log"

fail() { printf '[失败] %s\n' "$1" >&2; exit "${2:-1}"; }

[[ -x "$PY" ]] || fail "找不到 venv 解释器: $PY（先运行 deploy/bootstrap-xubuntu.sh --phase base）"
[[ -f "$ENV_FILE" ]] || fail "找不到配置文件: $ENV_FILE"
[[ -f "$SESSION_FILE" ]] ||
  fail "缺少图形会话文件 $SESSION_FILE；先在桌面终端运行 bash scripts/launch-wechat.sh 并保持该终端"
pgrep -u "$(id -u)" -x wechat >/dev/null ||
  fail "微信没有运行；先运行 bash scripts/launch-wechat.sh 并保持该终端"

if pgrep -u "$(id -u)" -f 'wechat_agent desktop' >/dev/null; then
  printf '停止旧控制台…\n'
  pkill -u "$(id -u)" -f 'wechat_agent desktop' 2>/dev/null
  for _ in $(seq 1 40); do
    pgrep -u "$(id -u)" -f 'wechat_agent desktop' >/dev/null || break
    sleep 0.25
  done
  if pgrep -u "$(id -u)" -f 'wechat_agent desktop' >/dev/null; then
    pkill -9 -u "$(id -u)" -f 'wechat_agent desktop' 2>/dev/null
    sleep 1
  fi
fi

# shellcheck disable=SC1090
set -a
source "$SESSION_FILE"
set +a

[[ -n "${DISPLAY:-}" ]] || fail "graphical-session.env 里没有 DISPLAY"
[[ -n "${DBUS_SESSION_BUS_ADDRESS:-}" ]] || fail "graphical-session.env 里没有 DBUS_SESSION_BUS_ADDRESS"

if command -v gdbus >/dev/null && [[ -n "${AT_SPI_BUS_ADDRESS:-}" ]]; then
  gdbus call --address "$AT_SPI_BUS_ADDRESS" --dest org.a11y.atspi.Registry \
    --object-path /org/a11y/atspi/registry \
    --method org.freedesktop.DBus.Peer.Ping >/dev/null 2>&1 ||
    fail "无障碍总线已失效（微信重启过？）；重新运行 bash scripts/launch-wechat.sh 后再试"
fi

mkdir -p -- "$STATE_DIR"
: >"$LOG"
printf '启动控制台: %s -m wechat_agent desktop --port %s\n' "$PY" "$PORT"
setsid nohup "$PY" -m wechat_agent desktop --config "$ENV_FILE" --port "$PORT" >>"$LOG" 2>&1 &
disown $! 2>/dev/null || true

port_open() {
  "$PY" - "$PORT" <<'PY' 2>/dev/null
import socket, sys
sock = socket.socket()
sock.settimeout(0.5)
try:
    sock.connect(("127.0.0.1", int(sys.argv[1])))
except OSError:
    sys.exit(1)
finally:
    sock.close()
PY
}

up=0
for _ in $(seq 1 60); do
  if port_open; then up=1; break; fi
  sleep 0.5
done
if ((up == 0)); then
  printf '控制台没有起来，最近日志：\n' >&2
  tail -n 20 "$LOG" >&2
  fail "端口 $PORT 未就绪" 3
fi

if ((start_agent == 1)); then
  printf '调用 /api/start 启动 Agent…\n'
  "$PY" - "$PORT" "$ENV_FILE" <<'PY'
import sys, urllib.request
from wechat_agent.config import Settings
token = Settings(_env_file=sys.argv[2]).desktop_token
request = urllib.request.Request(
    f"http://127.0.0.1:{sys.argv[1]}/api/start", data=b"", method="POST",
    headers={"Authorization": "Bearer " + token} if token else {},
)
try:
    body = urllib.request.urlopen(request, timeout=60).read().decode("utf-8", "replace")
except Exception as exc:  # noqa: BLE001 - report and keep the console alive
    print(f"[警告] /api/start 失败: {exc}")
else:
    print(body[:400])
PY
fi

printf '\n控制台已就绪: http://127.0.0.1:%s\n' "$PORT"
printf '日志: %s\n' "$LOG"
