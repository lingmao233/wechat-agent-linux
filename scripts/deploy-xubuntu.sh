#!/usr/bin/env bash
# Prepare a Xubuntu/Ubuntu 24.04 (X11) VM for wechat-agent-linux.
# This script installs packages, creates the venv and points .env at the
# local WeChat account. It never starts WeChat, scans keys or sends messages.
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."

ENV_FROM=""
DATA_DIR=""
MIGRATE_HOME=""
WECHAT_DEB=""
VENV_DIR="${WECHAT_VENV_DIR:-$HOME/.venvs/wechat-agent-linux}"
SKIP_APT=0
CONFIGURE=1

usage() {
  cat <<'EOF'
用法: bash scripts/deploy-xubuntu.sh [选项]

在 Xubuntu/Ubuntu 24.04 (X11) 上准备运行环境。不会启动微信、扫描密钥或发送消息。

选项:
  --env-from PATH       导入现有 .env（写入 .env，旧文件自动备份）
  --data-dir PATH       指定 ~/xwechat_files/<账号目录>；省略时自动发现唯一目录
  --migrate-home PATH   旧 Linux 主目录（如 /home/wx），把 .env 中该前缀改为当前 $HOME
  --wechat-deb PATH     安装官方微信 Linux .deb
  --venv PATH           虚拟环境目录（默认 ~/.venvs/wechat-agent-linux）
  --skip-apt            跳过 apt 安装（依赖已就绪时）
  --no-configure        只装环境，不改 .env
  -h, --help
EOF
}

die() { printf '错误: %s\n' "$*" >&2; exit 1; }
warn() { printf '提示: %s\n' "$*" >&2; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --env-from) [[ $# -ge 2 ]] || die "--env-from 缺少参数"; ENV_FROM="$2"; shift 2 ;;
    --data-dir) [[ $# -ge 2 ]] || die "--data-dir 缺少参数"; DATA_DIR="$2"; shift 2 ;;
    --migrate-home) [[ $# -ge 2 ]] || die "--migrate-home 缺少参数"; MIGRATE_HOME="$2"; shift 2 ;;
    --wechat-deb) [[ $# -ge 2 ]] || die "--wechat-deb 缺少参数"; WECHAT_DEB="$2"; shift 2 ;;
    --venv) [[ $# -ge 2 ]] || die "--venv 缺少参数"; VENV_DIR="$2"; shift 2 ;;
    --skip-apt) SKIP_APT=1; shift ;;
    --no-configure) CONFIGURE=0; shift ;;
    -h|--help) usage; exit 0 ;;
    *) die "未知参数: $1（--help 查看用法）" ;;
  esac
done

[[ $EUID -ne 0 ]] || die "不要用 root 运行；请用桌面普通用户，脚本内部按需调用 sudo"
[[ "$(uname -m)" == "x86_64" ]] || die "当前架构 $(uname -m)；微信 Linux 版与适配器按 x86_64 验证"

if [[ -r /etc/os-release ]]; then
  # shellcheck source=/dev/null
  . /etc/os-release
  if [[ "${ID:-}" != "ubuntu" ]]; then
    warn "当前系统 ${PRETTY_NAME:-${ID:-unknown}} 不是 Ubuntu/Xubuntu，脚本按 Ubuntu 24.04 验证"
  fi
  major="${VERSION_ID:-0}"
  major="${major%%.*}"
  if [[ "${major:-0}" -lt 24 ]]; then
    warn "Ubuntu ${VERSION_ID:-未知} 的 python3 可能低于 3.11；推荐 Xubuntu 24.04 LTS"
  fi
else
  warn "无法读取 /etc/os-release"
fi

SYSTEM_PYTHON="${WECHAT_PYTHON:-python3}"
"$SYSTEM_PYTHON" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)' \
  || die "$("$SYSTEM_PYTHON" -c 'import platform; print(platform.python_version())' 2>/dev/null) 低于 3.11；使用 24.04 或设置 WECHAT_PYTHON"

sudo -v || die "需要 sudo 安装系统依赖"

if [[ $SKIP_APT -eq 0 ]]; then
  sudo apt-get update
  # python3-dev + build-essential：pilk（微信语音 SILK 编解码）只有源码包，
  # pip 要现场编译，缺 Python.h 会整仓安装失败。
  sudo apt-get install -y python3-venv python3-pip python3-dev build-essential python3-pyatspi python3-gi gir1.2-gtk-3.0 at-spi2-core \
    dbus-x11 libglib2.0-bin xdotool xclip x11-utils imagemagick ffmpeg pulseaudio-utils curl
fi

if [[ ! -x /opt/wechat/wechat ]]; then
  if [[ -n "$WECHAT_DEB" ]]; then
    [[ -f "$WECHAT_DEB" ]] || die "找不到 .deb: $WECHAT_DEB"
    sudo apt-get install -y "$(realpath "$WECHAT_DEB")"
  else
    warn "未找到 /opt/wechat/wechat。请从 https://linux.weixin.qq.com/ 下载 x86_64 .deb，然后运行："
    warn "  bash scripts/deploy-xubuntu.sh --wechat-deb ~/Downloads/WeChatLinux_x86_64.deb"
  fi
fi

if [[ -n "$ENV_FROM" ]]; then
  [[ -f "$ENV_FROM" ]] || die "找不到配置: $ENV_FROM"
  source_path="$(realpath "$ENV_FROM")"
  target_path="$(realpath -m .env)"
  if [[ "$source_path" == "$target_path" ]]; then
    printf '使用当前 .env（--env-from 指向同一文件）\n'
  else
    if [[ -f .env ]]; then
      backup=".env.bak.$(date +%Y%m%d-%H%M%S)"
      cp -p .env "$backup"
      chmod 600 "$backup"
      printf '已备份 .env -> %s\n' "$backup"
    fi
    install -m 600 "$source_path" .env
    printf '已导入 %s -> .env\n' "$source_path"
  fi
fi

if [[ ! -f .env ]]; then
  cp .env.linux.example .env
  chmod 600 .env
  printf '已从 .env.linux.example 创建 .env\n'
fi

# Fail closed even if the data-directory auto-detection below cannot run yet.
if grep -Eq '^[[:space:]]*(export[[:space:]]+)?WECHAT_ALLOW_SEND[[:space:]]*=' .env; then
  sed -i -E 's|^([[:space:]]*(export[[:space:]]+)?WECHAT_ALLOW_SEND[[:space:]]*=).*$|\1false|' .env
else
  printf '\nWECHAT_ALLOW_SEND=false\n' >> .env
fi
chmod 600 .env

if [[ "$HOME" != "/home/wx" ]] && grep -Fq '/home/wx/' .env && [[ -z "$MIGRATE_HOME" ]]; then
  die ".env 仍使用 /home/wx 路径。请加 --migrate-home /home/wx 重跑，或创建用户 wx 后部署"
fi

WECHAT_VENV_DIR="$VENV_DIR" \
WECHAT_SKIP_APT=1 \
WECHAT_SKIP_INIT_DB=1 \
WECHAT_PYTHON="$SYSTEM_PYTHON" \
  bash scripts/setup-linux.sh

if [[ $CONFIGURE -eq 1 ]]; then
  configure=(scripts/configure-linux-env.py --env .env --disable-send)
  if [[ -n "$MIGRATE_HOME" ]]; then
    configure+=(--migrate-home "$MIGRATE_HOME")
  fi
  if [[ -n "$DATA_DIR" ]]; then
    configure+=(--data-dir "$DATA_DIR")
  else
    configure+=(--discover-data-dir)
  fi
  if ! "$VENV_DIR/bin/python" "${configure[@]}"; then
    warn "未能自动配置微信数据目录；登录微信后可重跑："
    warn "  \"$VENV_DIR/bin/python\" scripts/configure-linux-env.py --env .env --discover-data-dir --disable-send"
  fi
fi

if ! "$VENV_DIR/bin/python" -m wechat_agent init-db; then
  warn "init-db 失败；先修正 .env 的 DATABASE_URL/路径，再运行："
  warn "  \"$VENV_DIR/bin/python\" -m wechat_agent init-db --config .env"
fi

cat <<EOF

部署脚本已完成基础环境。
虚拟环境: $VENV_DIR
配置文件: $(pwd)/.env

还需要在图形桌面完成（顺序不要颠倒）:
  1. 登录微信 Linux 版，加入目标群；退出微信
  2. 启动独立无障碍会话: bash scripts/launch-wechat.sh
  3. 另一个终端: source scripts/linux-session.sh
  4. 扫描密钥: "\$WECHAT_PYTHON" -m wechat_agent keyscan --config .env
     （若 PTRACE_DENIED，见 docs/DEPLOY-XUBUNTU-VM.md 第 8 节）
  5. 预检: bash scripts/check-linux-ready.sh
  6. 自检: "\$WECHAT_PYTHON" -m wechat_agent ready --config .env
  7. 只读试运行: bash scripts/start-linux.sh desktop --config .env

当前 WECHAT_ALLOW_SEND 已设为 false；确认读路径和群映射后再打开发送。
EOF
