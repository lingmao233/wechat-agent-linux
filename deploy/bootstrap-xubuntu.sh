#!/usr/bin/env bash
# wechat-agent-linux · Xubuntu/Ubuntu 24.04 (X11) 虚拟机部署编排器
#
# 把 docs/DEPLOY-XUBUNTU-VM.md 的手工步骤收敛成 5 条幂等命令。
# 需要人参与的只有两件：扫码登录微信、最后打开发送开关。
#
#   bash deploy/bootstrap-xubuntu.sh --phase base      装依赖 / 装微信 deb / venv / 导入 .env
#   bash deploy/bootstrap-xubuntu.sh --phase agent     微信登录后：识别账号目录 / 初始化库
#   bash deploy/bootstrap-xubuntu.sh --phase key       扫数据库密钥（必要时自动 sudo 一次）
#   bash deploy/bootstrap-xubuntu.sh --phase check     预检 + ready + 群 ID 映射
#   bash deploy/bootstrap-xubuntu.sh --phase autostart 可选：XFCE 自启动（24h 运行）
#
# 本脚本不会启动微信、不会发送消息；导入配置时一律把 WECHAT_ALLOW_SEND 置为 false。
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd -- "$SCRIPT_DIR/.." && pwd)"
cd -- "$ROOT"

PHASES="base"
ENV_FROM=""
WECHAT_DEB=""
DATA_DIR=""
MIGRATE_HOME=""
PIP_INDEX="${PIP_INDEX:-}"
SKIP_APT=0
ENV_FILE=".env"
VENV_DIR="${WECHAT_VENV_DIR:-$HOME/.venvs/wechat-agent-linux}"
PY="$VENV_DIR/bin/python"
STATE_DIR="${WECHAT_STATE_DIR:-${XDG_STATE_HOME:-$HOME/.local/state}/wechat-agent-linux}"
SESSION_FILE="$STATE_DIR/graphical-session.env"

bold() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
info() { printf '  %s\n' "$*"; }
ok()   { printf '  [ OK ] %s\n' "$*"; }
todo() { printf '  [ -- ] %s\n' "$*"; }
warn() { printf '  [提示] %s\n' "$*" >&2; }
die()  { printf '\n  [错误] %s\n' "$*" >&2; exit 1; }

usage() {
  cat <<'EOF'
用法: bash deploy/bootstrap-xubuntu.sh [选项]

阶段（可逗号组合，如 --phase base,agent）:
  base       系统依赖 + 微信 .deb + Linux venv + 导入/生成 .env（默认）
  agent      从已登录微信推导账号目录，写回 .env，初始化 Agent 数据库
  key        扫描 SQLCipher 密钥；遇到 PTRACE_DENIED 自动 sudo 重试一次并修复属主
  check      预检 + ready + 缺失时输出群 ID 映射
  autostart  写 XFCE 自启动项（launch-wechat + agent 守护 + 关屏保/DPMS）
  report     只打印当前进度，不改任何东西

选项:
  --env-from PATH     导入现有 .env（默认自动找 ~/.env.from-windows）
  --wechat-deb PATH   安装官方微信 Linux x86_64 .deb
  --data-dir PATH     明确指定 ~/xwechat_files/<账号目录>（跳过自动发现）
  --migrate-home DIR  旧主目录（如 /home/wx），自动把配置里的该前缀换成 $HOME
  --pip-index URL     pip 镜像，如 https://pypi.tuna.tsinghua.edu.cn/simple
  --skip-apt          跳过 apt 安装（依赖已就绪）
  -h, --help

示例:
  bash deploy/bootstrap-xubuntu.sh --phase base \
      --env-from ~/.env.from-windows \
      --wechat-deb ~/Downloads/WeChatLinux_x86_64.deb \
      --pip-index https://pypi.tuna.tsinghua.edu.cn/simple
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --phase)        [[ $# -ge 2 ]] || die "--phase 缺少参数"; PHASES="$2"; shift 2 ;;
    --env-from)     [[ $# -ge 2 ]] || die "--env-from 缺少参数"; ENV_FROM="$2"; shift 2 ;;
    --wechat-deb)   [[ $# -ge 2 ]] || die "--wechat-deb 缺少参数"; WECHAT_DEB="$2"; shift 2 ;;
    --data-dir)     [[ $# -ge 2 ]] || die "--data-dir 缺少参数"; DATA_DIR="$2"; shift 2 ;;
    --migrate-home) [[ $# -ge 2 ]] || die "--migrate-home 缺少参数"; MIGRATE_HOME="$2"; shift 2 ;;
    --pip-index)    [[ $# -ge 2 ]] || die "--pip-index 缺少参数"; PIP_INDEX="$2"; shift 2 ;;
    --skip-apt)     SKIP_APT=1; shift ;;
    -h|--help)      usage; exit 0 ;;
    *) die "未知参数: $1（--help 查看用法）" ;;
  esac
done

# ---------------------------------------------------------------- 工具函数

env_get() {
  local key="$1" value=""
  [[ -f "$ENV_FILE" ]] || return 0
  if [[ -x "$PY" ]]; then
    value="$("$PY" - "$ENV_FILE" "$key" <<'PY' 2>/dev/null || true
import sys
from dotenv import dotenv_values
print(dotenv_values(sys.argv[1]).get(sys.argv[2], "") or "")
PY
)"
  fi
  if [[ -z "$value" ]]; then
    value="$(sed -n "s/^[[:space:]]*\(export[[:space:]]\+\)\?${key}[[:space:]]*=[[:space:]]*//p" "$ENV_FILE" \
      | tail -n 1 | sed -e 's/^"//' -e 's/"$//' -e "s/^'//" -e "s/'$//")"
  fi
  printf '%s' "$value"
}

expand_home() {
  local p="$1"
  printf '%s' "${p/#\~/$HOME}"
}

key_path() {
  local p
  p="$(env_get WECHAT_LINUX_KEY_FILE)"
  p="${p:-$STATE_DIR/linux_keys.json}"
  expand_home "$p"
}

wechat_running() {
  pgrep -x wechat >/dev/null 2>&1 || pgrep -x weixin >/dev/null 2>&1
}

# 现有配置里的路径是 /home/wx/...；当前用户不是 wx 时自动迁移，避免去建同名用户。
detect_migrate_home() {
  if [[ -n "$MIGRATE_HOME" || "$HOME" == "/home/wx" ]]; then return 0; fi
  local probe=""
  if [[ -f "$ENV_FILE" ]]; then probe="$ENV_FILE"; fi
  if [[ -z "$probe" && -n "$ENV_FROM" && -f "$ENV_FROM" ]]; then probe="$ENV_FROM"; fi
  if [[ -z "$probe" ]]; then return 0; fi
  if grep -Fq '/home/wx/' "$probe"; then
    MIGRATE_HOME="/home/wx"
    info "配置里有 /home/wx 路径，自动加 --migrate-home /home/wx（迁移到 $HOME）"
  fi
  return 0
}

# ---------------------------------------------------------------- 阶段实现

phase_preflight() {
  bold "预检：系统 / 架构 / Python / 图形会话"
  [[ $EUID -ne 0 ]] || die "不要用 root 运行；请用桌面普通用户，脚本内部按需调用 sudo"
  ok "用户: $(id -un)  主目录: $HOME"

  [[ "$(uname -m)" == "x86_64" ]] || die "架构 $(uname -m)；微信 Linux 版与适配器按 x86_64 验证"
  ok "架构: x86_64"

  if [[ -r /etc/os-release ]]; then
    # shellcheck source=/dev/null
    . /etc/os-release
    if [[ "${ID:-}" == "ubuntu" ]]; then
      ok "系统: ${PRETTY_NAME:-Ubuntu}"
    else
      warn "系统 ${PRETTY_NAME:-${ID:-未知}} 不是 Ubuntu/Xubuntu；脚本按 Ubuntu 24.04 验证"
    fi
    major="${VERSION_ID:-0}"; major="${major%%.*}"
    if [[ "${major:-0}" -lt 24 ]]; then
      warn "Ubuntu ${VERSION_ID:-未知} 自带 Python 可能低于 3.11；推荐 24.04 LTS"
    fi
  fi

  command -v python3 >/dev/null || die "缺少 python3"
  python3 -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)' \
    || die "python3 $(python3 -c 'import platform;print(platform.python_version())') 低于 3.11"
  ok "python3: $(python3 -c 'import platform; print(platform.python_version())')"

  local mem_mb avail_kb
  mem_mb="$(awk '/MemTotal/ {print int($2/1024)}' /proc/meminfo 2>/dev/null || echo 0)"
  if [[ "${mem_mb:-0}" -ge 3500 ]]; then ok "内存: ${mem_mb} MB"; else warn "内存 ${mem_mb} MB；建议 4 GB 起，推荐 8 GB"; fi
  avail_kb="$(df -Pk . | awk 'NR==2 {print $4}')"
  if [[ -n "${avail_kb:-}" ]]; then
    if [[ "$avail_kb" -gt $((5*1024*1024)) ]]; then ok "磁盘剩余: $((avail_kb/1024/1024)) GB"
    else warn "磁盘剩余不足 5 GB；微信数据、图片缓存和 Agent 库会持续增长"; fi
  fi

  case "${XDG_SESSION_TYPE:-}" in
    x11)     ok "桌面会话: X11（写路径可用的前提）" ;;
    wayland) warn "当前是 Wayland；发送需要 X11，请在登录界面选 Xubuntu Session 后重新登录" ;;
    *)       warn "XDG_SESSION_TYPE 未设置（SSH 会话？）；扫码登录和 launch-wechat.sh 必须在 VM 桌面终端里做" ;;
  esac
  if [[ -n "${DISPLAY:-}" ]]; then ok "DISPLAY=$DISPLAY"; else warn "DISPLAY 未设置；图形步骤请在桌面终端执行"; fi
}

phase_base() {
  bold "阶段 base：系统依赖 + 微信客户端 + venv + 导入配置"

  if [[ -z "$ENV_FROM" ]]; then
    for candidate in "$HOME/.env.from-windows" "$HOME/env.from-windows" "$SCRIPT_DIR/env.from-windows"; do
      if [[ -f "$candidate" ]]; then ENV_FROM="$candidate"; break; fi
    done
  fi

  local args=()
  if [[ -n "$ENV_FROM" ]]; then
    [[ -f "$ENV_FROM" ]] || die "找不到配置 $ENV_FROM"
    info "配置来源: $ENV_FROM"
    args+=(--env-from "$ENV_FROM")
  else
    warn "没有找到 .env.from-windows；将以 .env.linux.example 为模板，模型密钥需要手工填"
  fi

  if [[ -n "$WECHAT_DEB" ]]; then
    [[ -f "$WECHAT_DEB" ]] || die "找不到 .deb: $WECHAT_DEB"
    args+=(--wechat-deb "$WECHAT_DEB")
  fi
  if [[ -n "$DATA_DIR" ]]; then args+=(--data-dir "$DATA_DIR"); fi

  detect_migrate_home
  if [[ -n "$MIGRATE_HOME" ]]; then args+=(--migrate-home "$MIGRATE_HOME"); fi
  if (( SKIP_APT )); then args+=(--skip-apt); fi

  if [[ -n "$PIP_INDEX" ]]; then
    export PIP_INDEX_URL="$PIP_INDEX"
    info "pip 镜像: $PIP_INDEX"
  fi

  WECHAT_VENV_DIR="$VENV_DIR" bash scripts/deploy-xubuntu.sh ${args[@]+"${args[@]}"}

  # configure-linux-env.py 在微信尚未登录时会提前退出，/home/wx -> $HOME 的迁移就
  # 没落盘，DATABASE_URL 等键仍指向旧主目录，init-db 必然失败。这里补一次确定性迁移。
  if [[ "$HOME" != "/home/wx" && -f "$ENV_FILE" ]] && grep -Fq '/home/wx/' "$ENV_FILE"; then
    sed -i "s|/home/wx/|$HOME/|g" "$ENV_FILE"
    info "已把 .env 里的 /home/wx/ 统一迁移为 $HOME/"
  fi
  if ! "$PY" -m wechat_agent init-db --config "$ENV_FILE"; then
    warn "init-db 仍失败；检查 .env 的 DATABASE_URL 和磁盘权限后重跑："
    warn "  \"$PY\" -m wechat_agent init-db --config $ENV_FILE"
  fi

  ok "基础环境完成（venv: $VENV_DIR）"
  info "配置已强制 WECHAT_ALLOW_SEND=false；打开发送是最后一步，见 docs/DEPLOY-XUBUNTU-VM.md 第 11 节"
}

phase_agent() {
  bold "阶段 agent：识别微信账号目录 + 初始化 Agent 数据库"
  [[ -x "$PY" ]] || die "venv 不存在；先运行 --phase base"

  local args=(--env "$ENV_FILE" --disable-send)
  detect_migrate_home
  if [[ -n "$MIGRATE_HOME" ]]; then args+=(--migrate-home "$MIGRATE_HOME"); fi
  if [[ -n "$DATA_DIR" ]]; then
    args+=(--data-dir "$DATA_DIR")
  else
    args+=(--discover-data-dir)
  fi

  if ! "$PY" scripts/configure-linux-env.py "${args[@]}"; then
    die "无法识别微信数据目录。确认微信已扫码登录并打开过目标群；或显式指定:
    $PY scripts/configure-linux-env.py --env $ENV_FILE --data-dir \"\$HOME/xwechat_files/wxid_xxx_abcd\" --disable-send"
  fi

  "$PY" -m wechat_agent init-db --config "$ENV_FILE"
  ok "agent 阶段完成"
}

phase_key() {
  bold "阶段 key：扫描微信数据库密钥"
  [[ -x "$PY" ]] || die "venv 不存在；先运行 --phase base"
  wechat_running || die "微信没有运行。先在图形终端执行 bash scripts/launch-wechat.sh 并保持该终端"

  local log rc=0
  log="$(mktemp)"
  "$PY" -m wechat_agent keyscan --config "$ENV_FILE" 2>&1 | tee "$log" || rc=$?

  if (( rc == 0 )); then
    ok "密钥扫描成功"
  elif grep -q 'PTRACE_DENIED' "$log"; then
    warn "同用户读 /proc/<pid>/mem 被拒（Ubuntu 默认 kernel.yama.ptrace_scope=1）"
    warn "只对这条扫描命令提权一次，不改全局 ptrace_scope"
    sudo -v || die "需要 sudo 权限"
    sudo env HOME="$HOME" "$PY" -m wechat_agent keyscan --config "$ENV_FILE" \
      || die "sudo 下仍然失败；见 docs/DEPLOY-XUBUNTU-VM.md 第 8 节"
    local kf=""
    kf="$(key_path)"
    if [[ -z "$kf" ]]; then kf="$STATE_DIR/linux_keys.json"; fi
    if [[ -f "$kf" ]]; then
      sudo chown "$(id -u):$(id -g)" "$kf"
      chmod 600 "$kf"
      ok "密钥文件属主已恢复为 $(id -un): $(kf)"
    else
      warn "扫描成功但没找到 $kf；核对 .env 的 WECHAT_LINUX_KEY_FILE"
    fi
  else
    die "扫描失败，错误码见上面输出。常见原因：账号目录不对、微信多开（用 WECHAT_LINUX_PID 指定）、版本漂移"
  fi

  rm -f "$log"
  "$PY" -m wechat_agent ready --config "$ENV_FILE" || warn "ready 还有其它错误，继续跑 --phase check 看完整清单"
}

phase_check() {
  bold "阶段 check：环境预检 + ready"
  [[ -x "$PY" ]] || die "venv 不存在；先运行 --phase base"

  # shellcheck source=/dev/null
  source scripts/linux-session.sh
  WECHAT_PYTHON="$PY" bash scripts/check-linux-ready.sh --network \
    || warn "预检有 FAIL；先按上面每一条 [FAIL] 修，再继续"

  local rc=0
  "$PY" -m wechat_agent ready --config "$ENV_FILE" || rc=$?
  if (( rc == 0 )); then
    ok "ready 通过：只读路径就绪（这只代表能读，不等于发送已验证）"
  else
    warn "ready 失败（退出码 $rc），错误码对照 docs/DEPLOY-XUBUNTU-VM.md 9.2 表"
  fi

  local chat_ids
  chat_ids="$(env_get WECHAT_LINUX_CHAT_IDS)"
  if [[ -z "$chat_ids" || "$chat_ids" == "{}" ]]; then
    bold "群 ID 映射为空，下面输出可直接粘到 WECHAT_LINUX_CHAT_IDS"
    "$PY" scripts/list-linux-chats.py --config "$ENV_FILE" --mapping \
      || warn "列不出会话；通常是密钥或数据目录还没就绪"
  fi

  return "$rc"
}

phase_autostart() {
  bold "阶段 autostart：写 XFCE 自启动（可选，24 小时运行用）"
  [[ -x "$PY" ]] || die "venv 不存在；先运行 --phase base"
  case "$ROOT" in
    *" "*) die "项目路径含空格，自启动项需手工写；见 docs/DEPLOY-XUBUNTU-VM.md 第 12 节" ;;
  esac

  mkdir -p "$HOME/.config/autostart"

  cat > "$HOME/.config/autostart/wechat-a11y.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=WeChat accessibility session
Comment=wechat-agent-linux: 私有 D-Bus + AT-SPI 会话启动微信
Exec=bash -lc 'cd $ROOT && exec bash scripts/launch-wechat.sh'
Terminal=false
X-GNOME-Autostart-enabled=true
EOF

  cat > "$HOME/.config/autostart/wechat-agent.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=wechat-agent
Comment=wechat-agent-linux: 等微信会话就绪后启动控制台（含网页 UI）并守护
Exec=bash -lc 'cd $ROOT && for i in \$(seq 1 90); do pgrep -x wechat >/dev/null 2>&1 && [ -f "$SESSION_FILE" ] && break; sleep 2; done; while true; do if ! pgrep -u "\$(id -u)" -f "wechat_agent desktop" >/dev/null 2>&1; then bash scripts/restart-desktop.sh --start-agent; fi; sleep 60; done'
Terminal=false
X-GNOME-Autostart-enabled=true
EOF

  cat > "$HOME/.config/autostart/wechat-no-dpms.desktop" <<'EOF'
[Desktop Entry]
Type=Application
Name=disable screen blanking
Comment=发送依赖窗口可见；关闭屏保和 DPMS
Exec=bash -lc 'xset s off; xset s noblank; xset -dpms; command -v xfce4-screensaver-command >/dev/null 2>&1 && xfce4-screensaver-command -d || true'
Terminal=false
X-GNOME-Autostart-enabled=true
EOF

  ok "已写入 3 个自启动项: ~/.config/autostart/wechat-{a11y,agent,no-dpms}.desktop"
  info "仍需在 设置→电源管理器 里关掉挂起/休眠，并在 设置→会话和启动 里确认没有别的锁屏程序"
}

phase_report() { :; }  # 只读：main 末尾统一打印 show_progress / next_hint

show_progress() {
  bold "当前进度"
  if [[ -x "$PY" ]]; then ok "venv: $VENV_DIR"; else todo "venv 未创建（--phase base）"; fi
  if [[ -f "$ENV_FILE" ]]; then
    ok "配置: $ROOT/$ENV_FILE  platform=$(env_get WECHAT_PLATFORM)  allow_send=$(env_get WECHAT_ALLOW_SEND)"
  else
    todo "缺少 .env（--phase base --env-from ~/.env.from-windows）"
  fi
  local data
  data="$(expand_home "$(env_get WECHAT_LINUX_DATA_DIR)")"
  if [[ -n "$data" && -d "$data/db_storage" ]]; then ok "微信数据目录: $data"
  else todo "微信数据目录未识别（微信登录后 --phase agent）"; fi
  if [[ -x "${WECHAT_LINUX_BINARY:-/opt/wechat/wechat}" ]]; then ok "微信客户端: ${WECHAT_LINUX_BINARY:-/opt/wechat/wechat}"
  else todo "微信客户端未安装（--wechat-deb，或从 https://linux.weixin.qq.com/ 下载）"; fi
  if wechat_running; then ok "微信进程运行中"; else todo "微信未运行（bash scripts/launch-wechat.sh）"; fi
  if [[ -f "$SESSION_FILE" ]]; then ok "图形会话文件: $SESSION_FILE"; else todo "尚无 graphical-session.env（launch-wechat.sh 会生成）"; fi
  local kf; kf="$(key_path)"
  if [[ -f "$kf" ]]; then ok "密钥缓存: $kf"; else todo "密钥未扫描（--phase key）"; fi
}

next_hint() {
  local kf
  kf="$(key_path)"
  printf '\n'
  if [[ ! -x "$PY" ]]; then
    printf '下一步: bash deploy/bootstrap-xubuntu.sh --phase base'
    [[ -n "$ENV_FROM" ]] && printf ' --env-from %s' "$ENV_FROM"
    printf '\n'
  elif ! wechat_running; then
    printf '下一步（在 VM 桌面终端，保持该终端）: bash scripts/launch-wechat.sh\n'
  elif [[ ! -f "$kf" ]]; then
    printf '下一步: bash deploy/bootstrap-xubuntu.sh --phase key\n'
  else
    printf '下一步: bash deploy/bootstrap-xubuntu.sh --phase check\n'
  fi
  printf '导出环境: export WECHAT_PYTHON="%s" WECHAT_VENV_DIR="%s"\n' "$PY" "$VENV_DIR"
}

run_phase() {
  case "$1" in
    preflight) phase_preflight ;;
    base)      phase_preflight; phase_base ;;
    agent)     phase_agent ;;
    key)       phase_key ;;
    check)     phase_check ;;
    autostart) phase_autostart ;;
    report)    phase_report ;;
    all)       phase_preflight; phase_base; phase_report ;;
    *) die "未知阶段: $1（可选 base|agent|key|check|autostart|report|all）" ;;
  esac
}

main() {
  local rc=0 phase
  IFS=',' read -r -a requested <<< "$PHASES"
  for phase in "${requested[@]}"; do
    phase="${phase// /}"
    [[ -n "$phase" ]] || continue
    run_phase "$phase" || rc=$?
    if (( rc != 0 )); then break; fi
  done
  show_progress
  next_hint
  exit "$rc"
}

main
