#!/usr/bin/env bash
# Read-only preflight for the Linux/Xubuntu deployment. It never sends a
# message and never prints secrets. Run it from the project root on the VM.
set -uo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."

ENV_FILE="${WECHAT_ENV_FILE:-.env}"
NETWORK=0
if [[ "${1:-}" == "--network" ]]; then
  NETWORK=1
elif [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  cat <<'EOF'
用法: bash scripts/check-linux-ready.sh [--network]

检查系统、X11/D-Bus、AT-SPI、微信进程与数据目录、依赖和 .env。
--network 额外测试 LLM_BASE_URL 的 TCP 可达性。
不启动微信、不扫描密钥、不发送消息。
EOF
  exit 0
fi

if [[ -t 1 ]]; then
  C_OK=$'\033[32m'; C_WARN=$'\033[33m'; C_BAD=$'\033[31m'; C_DIM=$'\033[2m'; C_END=$'\033[0m'
else
  C_OK=""; C_WARN=""; C_BAD=""; C_DIM=""; C_END=""
fi
PASS=0; WARN=0; FAIL=0
ok()   { printf '%s[ OK ]%s %s\n' "$C_OK" "$C_END" "$*"; PASS=$((PASS + 1)); }
warn() { printf '%s[WARN]%s %s\n' "$C_WARN" "$C_END" "$*"; WARN=$((WARN + 1)); }
bad()  { printf '%s[FAIL]%s %s\n' "$C_BAD" "$C_END" "$*"; FAIL=$((FAIL + 1)); }
section() { printf '\n%s== %s ==%s\n' "$C_DIM" "$*" "$C_END"; }

PY="${WECHAT_PYTHON:-}"
if [[ -z "$PY" ]]; then
  for candidate in "$HOME/.venvs/wechat-agent-linux/bin/python" ".venv/bin/python"; do
    if [[ -x "$candidate" ]]; then PY="$candidate"; break; fi
  done
fi
[[ -n "$PY" ]] || PY=python3

env_get() {
  local key="$1"
  [[ -f "$ENV_FILE" ]] || return 0
  if "$PY" -c 'import dotenv' >/dev/null 2>&1; then
    "$PY" - "$ENV_FILE" "$key" <<'PY'
import sys
from dotenv import dotenv_values
print(dotenv_values(sys.argv[1]).get(sys.argv[2], "") or "")
PY
  else
    sed -n "s/^[[:space:]]*\(export[[:space:]]\+\)\?${key}[[:space:]]*=[[:space:]]*//p" "$ENV_FILE" \
      | tail -n 1 \
      | sed -e 's/^"//' -e 's/"$//' -e "s/^'//" -e "s/'$//"
  fi
}

# The agent itself sources this file; do the same so checks work from SSH too.
if [[ -z "${DISPLAY:-}" || -z "${DBUS_SESSION_BUS_ADDRESS:-}" ]]; then
  # shellcheck source=/dev/null
  source scripts/linux-session.sh
fi

section "系统"
if [[ -r /etc/os-release ]]; then
  # shellcheck source=/dev/null
  . /etc/os-release
  case "${ID:-}" in
    ubuntu) ok "系统: ${PRETTY_NAME:-${ID:-unknown}}" ;;
    *) warn "系统 ${PRETTY_NAME:-${ID:-unknown}} 不是 Ubuntu/Xubuntu；脚本按 Ubuntu 24.04 验证" ;;
  esac
else
  bad "无法读取 /etc/os-release"
fi
if [[ "$(uname -m)" == "x86_64" ]]; then
  ok "架构: x86_64"
else
  bad "架构 $(uname -m) 不是 x86_64；微信 Linux 版与适配器按 x86_64 验证"
fi
if [[ $EUID -ne 0 ]]; then
  ok "运行用户: ${USER:-$(id -un)}（非 root）"
else
  bad "不要用 root 运行微信或 Agent；请用桌面普通用户"
fi

MEM_MB=$(awk '/MemTotal/ {print int($2 / 1024)}' /proc/meminfo 2>/dev/null || echo 0)
if (( MEM_MB >= 3500 )); then
  ok "内存: ${MEM_MB} MB"
else
  warn "内存 ${MEM_MB} MB；微信 + 浏览器/控制台建议至少 4 GB，推荐 8 GB"
fi
DISK_AVAIL_KB=$(df -Pk . 2>/dev/null | awk 'NR == 2 {print $4}')
if [[ -n "${DISK_AVAIL_KB:-}" ]] && (( DISK_AVAIL_KB > 5 * 1024 * 1024 )); then
  ok "项目所在磁盘剩余: $(( DISK_AVAIL_KB / 1024 / 1024 )) GB"
else
  warn "项目所在磁盘剩余不足 5 GB；微信数据和缓存会持续增长"
fi

section "图形会话 (X11 + D-Bus)"
if [[ "${XDG_SESSION_TYPE:-}" == "x11" ]]; then
  ok "会话类型: X11"
elif [[ "${XDG_SESSION_TYPE:-}" == "wayland" ]]; then
  bad "当前是 Wayland；xdotool/AT-SPI 写路径不可用，请在 LightDM 选择 Xubuntu X11 会话"
else
  warn "XDG_SESSION_TYPE=${XDG_SESSION_TYPE:-未设置}；无法确认是否为 X11"
fi
if [[ -n "${DISPLAY:-}" ]]; then
  ok "DISPLAY=$DISPLAY"
else
  bad "DISPLAY 未设置；必须在已登录的图形会话中运行，或先 source scripts/linux-session.sh"
fi
if [[ -n "${DBUS_SESSION_BUS_ADDRESS:-}" ]]; then
  ok "D-Bus 会话已连接"
else
  bad "DBUS_SESSION_BUS_ADDRESS 未设置；运行 bash scripts/launch-wechat.sh 后 source scripts/linux-session.sh"
fi
if [[ -n "${XAUTHORITY:-}" && -r "${XAUTHORITY}" ]]; then
  ok "XAUTHORITY=$XAUTHORITY"
elif [[ -r "$HOME/.Xauthority" ]]; then
  warn "未设置 XAUTHORITY；Xlib 默认会使用 $HOME/.Xauthority，若截图/点击失败请显式 export"
else
  warn "找不到 Xauthority；SSH 会话可能无法访问 :0"
fi
if command -v pgrep >/dev/null && pgrep -x xfce4-screensaver >/dev/null 2>&1; then
  warn "xfce4-screensaver 正在运行；请关闭锁屏，否则窗口可能不可见/不可点击"
fi
if command -v xset >/dev/null && DISPLAY="${DISPLAY:-}" xset q >/dev/null 2>&1; then
  if xset q | grep -q 'DPMS is Enabled'; then
    warn "X11 DPMS 已启用；长期运行请执行 xset s off -dpms 并关闭电源管理休眠"
  else
    ok "X11 DPMS 已关闭"
  fi
fi

section "系统依赖"
for cmd in python3 xdotool xclip xwininfo import ffmpeg gdbus gsettings pgrep flock; do
  if command -v "$cmd" >/dev/null 2>&1; then
    ok "命令: $cmd"
  else
    bad "缺少命令 $cmd；运行 bash scripts/deploy-xubuntu.sh"
  fi
done
if "$PY" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null; then
  ok "Python: $("$PY" -c 'import platform; print(platform.python_version())')"
else
  bad "Python $("$PY" -c 'import platform; print(platform.python_version())' 2>/dev/null) 低于 3.11；使用 Xubuntu 24.04 或设置 WECHAT_PYTHON"
fi
if "$PY" -c 'import pyatspi' >/dev/null 2>&1; then
  ok "python3-pyatspi 可导入"
else
  bad "pyatspi 不可导入；安装 python3-pyatspi，并用 --system-site-packages 创建 venv"
fi
if "$PY" -c 'import gi; gi.require_version("Gtk", "3.0"); from gi.repository import Gtk' >/dev/null 2>&1; then
  ok "GTK 图片剪贴板可导入"
else
  bad "缺少 GTK 图片剪贴板依赖；安装 python3-gi gir1.2-gtk-3.0，并用 --system-site-packages 创建 venv"
fi

section "微信与配置"
WECHAT_BIN=$(env_get WECHAT_LINUX_BINARY)
WECHAT_BIN=${WECHAT_BIN:-/opt/wechat/wechat}
WECHAT_BIN=${WECHAT_BIN/#\~/$HOME}
if [[ -x "$WECHAT_BIN" ]]; then
  ok "微信可执行文件: $WECHAT_BIN"
else
  bad "找不到可执行文件 $WECHAT_BIN；从 https://linux.weixin.qq.com/ 安装 x86_64 .deb"
fi
if pgrep -x wechat >/dev/null 2>&1 || pgrep -x weixin >/dev/null 2>&1; then
  ok "微信进程正在运行"
else
  bad "微信未运行；先登录，再运行 bash scripts/launch-wechat.sh"
fi

if [[ ! -f "$ENV_FILE" ]]; then
  bad "缺少 $ENV_FILE；运行 bash scripts/deploy-xubuntu.sh 或用 --env-from 导入"
else
  ok "配置文件: $ENV_FILE"
  DATA_DIR=$(env_get WECHAT_LINUX_DATA_DIR)
  DATA_DIR=${DATA_DIR:-}
  DATA_DIR=${DATA_DIR/#\~/$HOME}
  if [[ -n "$DATA_DIR" && -d "$DATA_DIR/db_storage" ]]; then
    ok "微信数据目录: $DATA_DIR"
    CONFIG_ACCOUNT=$(env_get WECHAT_ACCOUNT_ID)
    DIR_ACCOUNT=$(basename "$DATA_DIR")
    DIR_ACCOUNT=${DIR_ACCOUNT%_*}
    if [[ -n "$CONFIG_ACCOUNT" && "$CONFIG_ACCOUNT" == "$DIR_ACCOUNT" ]]; then
      ok "账号 ID 与数据目录一致: $CONFIG_ACCOUNT"
    else
      bad "WECHAT_ACCOUNT_ID=${CONFIG_ACCOUNT:-未设置} 与目录推导账号 ${DIR_ACCOUNT:-未知} 不一致"
    fi
  else
    bad "WECHAT_LINUX_DATA_DIR 未配置或不存在: ${DATA_DIR:-未设置}"
  fi

  GROUPS=$(env_get WECHAT_GROUP_NAMES)
  if [[ -n "$GROUPS" ]]; then
    ok "监听群: $GROUPS"
  else
    bad "WECHAT_GROUP_NAMES 为空"
  fi
  CHAT_IDS=$(env_get WECHAT_LINUX_CHAT_IDS)
  if [[ -n "$CHAT_IDS" && "$CHAT_IDS" != "{}" ]]; then
    ok "WECHAT_LINUX_CHAT_IDS 已配置"
  else
    warn "WECHAT_LINUX_CHAT_IDS 为空；群显示名不唯一时 ready 会报 CHAT_NOT_FOUND"
  fi

  KEY_FILE=$(env_get WECHAT_LINUX_KEY_FILE)
  KEY_FILE=${KEY_FILE:-state/linux_keys.json}
  KEY_FILE=${KEY_FILE/#\~/$HOME}
  if [[ -f "$KEY_FILE" ]]; then
    KEY_PERM=$(stat -c '%a' "$KEY_FILE" 2>/dev/null || echo '?')
    if [[ "$KEY_PERM" == "600" ]]; then
      ok "密钥缓存: $KEY_FILE (0600)"
    else
      warn "密钥缓存权限为 $KEY_PERM；执行 chmod 600 $KEY_FILE"
    fi
  else
    bad "密钥缓存不存在: $KEY_FILE；运行 keyscan"
  fi
fi

section "AT-SPI 控件树"
if [[ -n "${DISPLAY:-}" && -n "${DBUS_SESSION_BUS_ADDRESS:-}" ]]; then
  ATSPI_OUT=$("$PY" - <<'PY' 2>&1
import sys
try:
    import pyatspi
except Exception as exc:
    print(f"IMPORT {exc}")
    sys.exit(2)
try:
    desktop = pyatspi.Registry.getDesktop(0)
    names = [str(getattr(app, "name", "") or "") for app in desktop]
except Exception as exc:
    print(f"BUS {exc}")
    sys.exit(3)
print("APPS " + " | ".join(names[:30]))
names_folded = {name.casefold() for name in names}
sys.exit(0 if names_folded & {"wechat", "weixin", "微信"} else 4)
PY
)
  ATSPI_RC=$?
  case $ATSPI_RC in
    0) ok "$ATSPI_OUT" ;;
    2) bad "pyatspi 导入失败: $ATSPI_OUT" ;;
    3) bad "无法访问 AT-SPI 总线: $ATSPI_OUT；检查 $DBUS_SESSION_BUS_ADDRESS 是否与微信一致" ;;
    4) bad "AT-SPI 中没有微信: $ATSPI_OUT；用 launch-wechat.sh 启动并开启 accessibility" ;;
    *) bad "AT-SPI 检查异常 (rc=$ATSPI_RC): $ATSPI_OUT" ;;
  esac
else
  warn "DISPLAY/D-Bus 不完整，跳过 AT-SPI 检查"
fi

section "模型与服务"
LLM_BASE=$(env_get LLM_BASE_URL)
LLM_KEY=$(env_get LLM_API_KEY)
LLM_MODEL=$(env_get LLM_MODEL)
if [[ -n "$LLM_BASE" && -n "$LLM_KEY" && -n "$LLM_MODEL" ]]; then
  ok "LLM 已配置: $LLM_BASE / $LLM_MODEL"
else
  bad "LLM_BASE_URL / LLM_API_KEY / LLM_MODEL 未配置完整"
fi
VISION_BASE=$(env_get VISION_BASE_URL)
VISION_KEY=$(env_get VISION_API_KEY)
VISION_MODEL=$(env_get VISION_MODEL)
if [[ -n "$VISION_BASE" || -n "$VISION_KEY" || -n "$VISION_MODEL" ]]; then
  ok "使用独立视觉端点"
else
  ok "视觉调用回退到同一 LLM"
fi
if [[ "$(env_get OPENVIKING_ENABLED)" == "true" ]]; then
  if [[ -n "$(env_get OPENVIKING_URL)" ]]; then
    ok "OpenViking 已启用"
  else
    warn "OPENVIKING_ENABLED=true 但 OPENVIKING_URL 为空"
  fi
fi

if [[ $NETWORK -eq 1 && -n "$LLM_BASE" ]]; then
  read -r LLM_HOST LLM_PORT < <("$PY" - "$LLM_BASE" <<'PY'
import sys
from urllib.parse import urlparse
parsed = urlparse(sys.argv[1])
print(parsed.hostname or "", parsed.port or (443 if parsed.scheme == "https" else 80))
PY
)
  if [[ -n "${LLM_HOST:-}" ]] && timeout 5 bash -c "cat < /dev/null > /dev/tcp/$LLM_HOST/$LLM_PORT" 2>/dev/null; then
    ok "LLM TCP 可达: $LLM_HOST:$LLM_PORT"
  else
    warn "LLM TCP 不可达: ${LLM_HOST:-未知}:${LLM_PORT:-未知}；检查 VM 网络/代理/防火墙"
  fi
fi

printf '\n%s结果: %d 通过, %d 警告, %d 失败%s\n' "$C_DIM" "$PASS" "$WARN" "$FAIL" "$C_END"
if (( FAIL > 0 )); then
  printf '先修复 FAIL；然后运行：\n  source scripts/linux-session.sh\n  "$WECHAT_PYTHON" -m wechat_agent ready --config .env\n'
  exit 1
fi
printf '环境检查通过。下一步：\n  source scripts/linux-session.sh\n  "$WECHAT_PYTHON" -m wechat_agent ready --config .env\n'
exit 0
