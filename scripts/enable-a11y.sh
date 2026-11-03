#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."
source scripts/linux-session.sh
: "${DBUS_SESSION_BUS_ADDRESS:?请先在图形登录会话中启动 D-Bus，再运行本脚本}"
gsettings set org.gnome.desktop.interface toolkit-accessibility true
gdbus call --session --dest org.a11y.Bus --object-path /org/a11y/bus \
  --method org.freedesktop.DBus.Properties.Set org.a11y.Status IsEnabled '<true>'
gdbus call --session --dest org.a11y.Bus --object-path /org/a11y/bus \
  --method org.freedesktop.DBus.Properties.Set org.a11y.Status ScreenReaderEnabled '<true>'
printf '%s\n' '无障碍已开启；从当前会话重新启动微信，使 QT 无障碍环境生效。'
