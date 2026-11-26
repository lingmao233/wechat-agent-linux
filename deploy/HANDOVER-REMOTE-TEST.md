# 交给另一个 AI / 另一台机器测试：接入手册

本文件写给"要接手测试的人或 AI"。所有命令默认在**另一台机器**上执行，
除非明确标注「VM 内」。

> 如果对方只是想用起来、不想敲命令：直接用 DSH 插件「微信」标签页 →「部署」子页的一键连接／一键部署／一键配置，见 [DSH 插件说明](../docs/DSH-PLUGIN.md)。本文保留给需要手工排查、看图形会话或做验收的人。

---

## 0. 先确认对方是哪种接入

| 对方能做什么 | 走哪种方式 | 需要开放 |
|---|---|---|
| 能跑 shell / SSH | **方式 A：SSH**（首选，覆盖 90% 需求） | TCP 22 |
| 只能看网页 | 方式 C：HTTP API + 隧道 | 无（走 SSH 隧道） |
| 需要看真实图形界面 | 方式 B：截图 / VNC | 无（走 SSH 隧道） |

微信自动化依赖 **X11 图形会话 + AT-SPI**，所以纯 HTTP 无法替代 SSH：
读消息可以不看界面，发送必须有可见窗口。

---

## 方式 A：SSH（推荐）

### VM 内一次性准备

```bash
sudo apt install -y openssh-server
sudo systemctl enable --now ssh
```

确认监听：

```bash
ss -tlnp | grep ':22'
```

### 对方机器上验证

```bash
ssh user@example.com "echo ok"
```

首次会问 `yes/no` 和密码；配好密钥后不再询问（见下）。

### 免密钥（可选）

对方机器上：

```bash
ssh-keygen -t ed25519 -f ~/.ssh/id_ed25519 -N ""
ssh-copy-id -i ~/.ssh/id_ed25519.pub user@example.com
```

Windows（cmd）：

```cmd
ssh-keygen -t ed25519 -f "%USERPROFILE%\.ssh\id_ed25519" -N ""
type "%USERPROFILE%\.ssh\id_ed25519.pub" | ssh user@example.com "mkdir -p ~/.ssh && cat >> ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys"
```

### 关键：SSH 会话要带上图形环境

Agent 的读写全部依赖 `launch-wechat.sh` 建立的私有会话。**任何**需要 Agent
能力的 SSH 命令，第一句都必须是：

```bash
cd ~/wechat-agent-linux
source ~/.local/state/wechat-agent-linux/graphical-session.env
export WECHAT_PYTHON="$HOME/.venvs/wechat-agent-linux/bin/python"
```

非交互式写法（一条命令搞定）：

```bash
ssh user@example.com 'cd ~/wechat-agent-linux && \
  source ~/.local/state/wechat-agent-linux/graphical-session.env && \
  export WECHAT_PYTHON="$HOME/.venvs/wechat-agent-linux/bin/python" && \
  "$WECHAT_PYTHON" -m wechat_agent ready --config .env'
```

`ready` 返回 `"ready": true` 才算链路完整。

---

## 方式 B：看屏幕 / 操作鼠标键盘

### B1. 单张截图（最轻）

对方机器上：

```bash
ssh user@example.com 'DISPLAY=:0.0 import -window root /tmp/screen.png'
scp user@example.com:/tmp/screen.png .
```

`import` 来自 imagemagick，VM 上已装（`check` 阶段验证过）。

### B2. 远程点击/输入

```bash
ssh user@example.com 'DISPLAY=:0.0 xdotool search --name wechat windowactivate'
ssh user@example.com 'DISPLAY=:0.0 xdotool mousemove 400 300 click 1'
```

`xdotool` 已装。注意：**不要用它去点"退出微信"**，那会杀掉整个任务。

### B3. 实时图形（VNC）

VM 内：

```bash
sudo apt install -y x11vnc
x11vnc -display :0 -localhost -rfbport 5900 -nopw -forever -shared
```

对方机器上建隧道，再用 VNC 客户端连 `127.0.0.1:5900`：

```bash
ssh -N -L 5900:127.0.0.1:5900 user@example.com
```

`-localhost` 保证 VNC 只监听回环，必须经 SSH 隧道访问，不要直接开放 5900。

（RDP 用 `xrdp` 也行，但和现有 X 会话容易冲突，不推荐。）

---

## 方式 C：HTTP API（给不能跑 shell 的 AI）

控制台自带 HTTP API，**默认只监听 `127.0.0.1`，且默认无鉴权**，所以：

- 不要改 `DESKTOP_HOST` 为 `0.0.0.0`；
- 要远程访问就建 SSH 隧道；
- 如果必须暴露，先设 `DESKTOP_TOKEN`。

隧道：

```bash
ssh -N -L 8765:127.0.0.1:8765 user@example.com
```

然后对方访问 `http://127.0.0.1:8765/api/status`。

常用端点（POST 需要 `-X POST`）：

| 端点 | 方法 | 作用 |
|---|---|---|
| `/api/status` | GET | 运行状态、`send_enabled`、统计 |
| `/api/live` | GET | 最近事件 |
| `/api/replies` | GET | 发送记录（含 `sent`/`blocked`） |
| `/api/config` | GET/POST | 读/写 `.env`（谨慎） |
| `/api/start`、`/api/stop` | POST | 启停 Agent |

```bash
curl -s http://127.0.0.1:8765/api/status | head -c 400
curl -s -X POST http://127.0.0.1:8765/api/start
```

---

## 只读验收清单（接手第一件事）

按顺序跑，全部通过再动别的：

```bash
# 1. 微信在跑
pgrep -a wechat

# 2. 控制台在跑
ss -tln | grep 8765

# 3. 链路完整
cd ~/wechat-agent-linux
source ~/.local/state/wechat-agent-linux/graphical-session.env
export WECHAT_PYTHON="$HOME/.venvs/wechat-agent-linux/bin/python"
"$WECHAT_PYTHON" -m wechat_agent ready --config .env

# 4. 能读到消息（不发送）
"$WECHAT_PYTHON" -m wechat_agent read-messages --config .env --limit 5

# 5. 最近日志有没有反复报错
tail -50 ~/.local/state/wechat-agent-linux/desktop.log
```

关键错误码：`KEY_REQUIRED`（密钥失效）、`ATSPI_UNAVAILABLE`（图形会话不对）、
`WECHAT_VERSION_DRIFT`（客户端版本漂移）、`CHAT_NOT_FOUND`（群名映射不对）。

---

## 改代码 / 重启的固定动作

```bash
# 改完代码或人设后重启（带网页 UI，后台常驻）
bash scripts/restart-desktop.sh --start-agent
```

从 Windows 一条命令同步 + 重启：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\deploy\dev-sync.ps1 -StartAgent
```

---

## 红线（做了要重装环境）

1. **不要退出微信**（`pgrep -x wechat` 必须一直有进程）——退出后 `launch-wechat.sh`
   的私有会话结束，必须回到桌面重新 `bash scripts/launch-wechat.sh`。
2. **不要 `pkill -f wechat`**，包括 `xdotool key ctrl+q`。
3. **不要覆盖 `.env`**：Windows 那份带 `/home/wx/` 旧路径，覆盖后 `init-db` 会挂。
   用 `dev-sync.ps1`（已排除 `.env`）。
4. **不要重扫密钥后不修属主**：`linux_keys.json` 必须 `jdy:jdy` 且 `0600`。
5. **不要把 22 / 5900 / 8765 直接映射到公网**。VMware NAT 下的 `192.168.126.x`
   只有宿主机能直连；局域网其他机器要访问，用 VMware「NAT 设置 → 端口转发」或改桥接。
6. **不要 `sudo reboot` 前不确认自启动正常**——重启后靠
   `~/.config/autostart/wechat-{a11y,agent,no-dpms}.desktop` 拉起。

---

## 交给对方的信息模板

```
主机：203.0.113.10（VMware NAT，仅宿主机可达）
用户：jdy
端口：22
密钥：<把公钥加到 ~/.ssh/authorized_keys，或提供临时密码>
项目：~/wechat-agent-linux
解释器：~/wechat-agent-linux 的 venv → ~/.venvs/wechat-agent-linux/bin/python
图形会话：~/.local/state/wechat-agent-linux/graphical-session.env
控制台：http://127.0.0.1:8765（需 SSH 隧道）
日志：~/.local/state/wechat-agent-linux/desktop.log
```

真实 `.env` 里的账号 wxid、群 ID、模型地址属于敏感信息，按需给，不必整份外发。
