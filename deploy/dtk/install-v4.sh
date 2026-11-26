#!/usr/bin/env bash
# Install DTK v4 (single container) on a small server.
#
# v4 is used here because the Tencent host has ~3.6 GB RAM and no Docker
# Compose stack for PostgreSQL/Redis.  The API is bound to 127.0.0.1 only;
# the WeChat VM reaches it through an SSH tunnel.
set -euo pipefail

ROOT="${DTK_DIR:-$HOME/dtk-v4}"
PORT="${DTK_PORT:-8080}"

if ! command -v docker >/dev/null 2>&1; then
  echo "docker is not installed. Install Docker first, then re-run." >&2
  exit 1
fi

mkdir -p "$ROOT"
cd "$ROOT"

if [ ! -f douyin-web.yaml ]; then
  echo "Pulling the v4 image to extract its config template..."
  docker pull evil0ctal/douyin_tiktok_download_api:V4.1.2
  docker rm -f dtk-v4 >/dev/null 2>&1 || true
  docker create --name dtk-v4 evil0ctal/douyin_tiktok_download_api:V4.1.2 >/dev/null
  docker cp dtk-v4:/app/crawlers/douyin/web/config.yaml ./douyin-web.yaml
  docker rm -f dtk-v4 >/dev/null
  echo
  echo "Edit $ROOT/douyin-web.yaml and replace the Cookie line with a"
  echo "logged-in Douyin browser cookie, then run this script again."
  exit 0
fi

cookie_line="$(grep -iE '^[[:space:]]*Cookie:' douyin-web.yaml | head -1 || true)"
cookie_value="$(printf '%s' "${cookie_line#*:}" | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//' -e 's/^"//' -e 's/"$//')"
if [ -z "$cookie_value" ]; then
  echo "Cookie is empty in $ROOT/douyin-web.yaml; fill it before starting." >&2
  exit 1
fi

# Compose will create a directory instead of a file if the bind source is
# missing, so keep the file check above.
docker rm -f dtk-v4 >/dev/null 2>&1 || true
docker run -d --name dtk-v4 --restart unless-stopped \
  -p "127.0.0.1:${PORT}:80" \
  -e TZ=Asia/Shanghai \
  -v "$ROOT/douyin-web.yaml:/app/crawlers/douyin/web/config.yaml:ro" \
  evil0ctal/douyin_tiktok_download_api:V4.1.2 >/dev/null

echo "DTK v4 started on 127.0.0.1:${PORT} (loopback only)."
echo "Health: curl -fsS http://127.0.0.1:${PORT}/docs >/dev/null && echo ok"
echo "From the WeChat VM, tunnel it:"
echo "  ssh -N -L ${PORT}:127.0.0.1:${PORT} $(whoami)@<this-host>"
