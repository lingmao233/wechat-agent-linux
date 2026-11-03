# Xubuntu 虚拟机一键部署（VM + Xubuntu 24.04）

配套文档：DSH 插件仓库里的 `docs/DEPLOY-XUBUNTU-VM.md`（原理与完整手工步骤）和 `docs/DEPLOYMENT-LINUX.md`（Linux 部署入口）。

本目录把这套手工流程收敛成 **5 条幂等命令 + 1 个 Windows 推送脚本**，需要你亲手做的只剩：扫码登录微信、最后打开发送开关。

---

## 0. 边界：我能做到哪一步

我（Agent）只运行在这台 Windows 上，**没有任何通路进入你的虚拟机**，所以“我直接帮你部署”只能落成下面两种形态：

| 方式 | 你需要做 | 我负责 |
|---|---|---|
| **A. 你执行（推荐）** | 在 VM 里跑命令，把输出贴回来 | 已把脚本、配置迁移、群 ID 推导、sudo 扫描、自检全部写好；出错我按输出继续改 |
| **B. 给我 SSH** | 提供 IP/端口/用户名 + 私钥或临时密码，并允许从这台 Windows 登录 | 我用 `push-from-windows.ps1` 全程驱动，最后只剩扫码要你本人 |

即使走 B，**扫码登录微信、`sudo` 输密码、肉眼确认群名这三点必须在 VM 本地完成**，远程替不了。

---

## 1. 前置条件

### 1.1 硬性要求（缺一不可）

| 项目 | 要求 | 不满足的后果 |
|---|---|---|
| 宿主机 | 开启 VT-x/AMD-V，长期开机不休眠 | 宿主睡了，微信和 Agent 一起掉线 |
| VM 架构 | **x86_64** | 微信 Linux 版没有 ARM 包；适配器也只按 x86_64 验证 |
| VM 规格 | 4 vCPU / 8 GB / 40 GB（最低 4 GB / 25 GB） | 微信自己吃 1–2 GB，编译 `psycopg` 等依赖还要余量 |
| 网络 | 桥接或 NAT，能访问微信服务器 + 你的模型网关 | NAT 够出网；桥接省掉端口映射 |
| 系统 | **Xubuntu 24.04 LTS** | 22.04 是 Python 3.10，`requires-python >= 3.11` 直接卡住 |
| 桌面协议 | **X11**（`echo $XDG_SESSION_TYPE` 必须是 `x11`） | Wayland 下 `xdotool` 无法合成点击，只能读不能发 |
| 用户 | 有 sudo 的普通桌面用户，**建议就叫 `wx`** | 现有 `.env` 里 10 处路径是 `/home/wx/...`；不叫 wx 就得自动迁移（脚本已支持） |
| 微信 | 官方 Linux 版 **4.1.x** x86_64 `.deb` | 令牌常量、AT-SPI 控件名按 4.1.x 验证；魔改包/新版可能 `WECHAT_VERSION_DRIFT` |
| 微信账号 | **专用测试号**，已加入目标群 | 有封号风险；主号不要用 |
| 模型 | `LLM_BASE_URL/API_KEY/MODEL` 从 VM 可访问 | 例如 `http://192.168.1.50:8080/v1`（内网地址，按实际服务填），**VM 必须能连到这台** |
| 权限 | 首次 `keyscan` 能 sudo 一次 | Ubuntu 默认 `ptrace_scope=1`，读 `/proc/<pid>/mem` 会被拒 |
| 磁盘 | 项目分区 ≥ 5 GB 空闲 | 图片/表情缓存、`state/`、微信库会持续增长 |

### 1.2 可选

`x11vnc`（从 Windows 看桌面，**别用 xrdp**，它会新建会话导致微信不在里面）、VMware Tools、独立 `VISION_*` 视觉模型、`OPENVIKING_*`、`OSS_*`。

### 1.3 两个必须先想清楚的问题

1. **同一个微信号不能同时挂 Windows 端和 VM 端。** 微信电脑端多设备互踢：在 VM 扫码登录会把 Windows 端顶下线。手机在线不受影响。
2. **群 ID 要不要重算。** `@chatroom` ID 是群维度的，同账号同群一般不变；但换了账号就必须重算。`--phase check` 在映射为空时会直接打印可粘贴的 JSON。

---

## 2. 一键执行

### 2.1 方式 A：Windows 打包推送（需要 VM 能 SSH）

VM 里先装好 SSH：

```bash
sudo apt update && sudo apt install -y openssh-server
sudo systemctl enable --now ssh
ip -4 addr show          # 记下 IP，例如 192.168.1.50
```

Windows 这边（仓库根目录）：

```powershell
# 首次可能需要放行执行策略
powershell -ExecutionPolicy Bypass -File .\deploy\push-from-windows.ps1 `
  -VmHost 192.168.1.50 -User wx `
  -WechatDeb .\WeChatLinux_x86_64.deb `
  -PipIndex https://pypi.tuna.tsinghua.edu.cn/simple
```

脚本做四件事：打包源码（**不含** `.env`/`.venv`/`state`）→ scp 到 `~/` → 上传 `.env`（存成 `~/.env.from-windows`）→ ssh 内跑 `--phase base`。

换个阶段就换 `-Phase`：`-Phase key`、`-Phase check`、`-Phase autostart`。`-Tunnel` 会顺手建 `127.0.0.1:8765` 的 SSH 隧道。

### 2.2 方式 B：共享文件夹/U 盘拷过去（没有 SSH 也能做）

把 `deploy/out/` 里的两个文件拷进 VM（VMware 共享文件夹、拖拽、U 盘都行，同目录还有一个 `README-先看这个.txt`）：

- `wechat-agent-linux-src.tar.gz`（源码，无密钥）
- `env.from-windows`（你的真实 `.env`，**别外发**）

VM 内：

```bash
mkdir -p ~/wechat-agent-linux
tar -xzf ~/wechat-agent-linux-src.tar.gz -C ~/wechat-agent-linux
cd ~/wechat-agent-linux
mv ~/env.from-windows ~/.env.from-windows && chmod 600 ~/.env.from-windows

bash deploy/bootstrap-xubuntu.sh --phase base \
  --env-from ~/.env.from-windows \
  --wechat-deb ~/Downloads/WeChatLinux_x86_64.deb \
  --pip-index https://pypi.tuna.tsinghua.edu.cn/simple
```

`--pip-index` 在国内基本是必需的，否则 `pip install` 会卡在 PyPI。

---

## 3. 五个阶段（顺序不能颠倒）

| 阶段 | 谁做 | 命令 | 完成标志 |
|---|---|---|---|
| `base` | 脚本 | `bash deploy/bootstrap-xubuntu.sh --phase base --env-from ~/.env.from-windows --wechat-deb ~/WeChatLinux.deb` | 结尾打印「基础环境完成」，`.env` 里 `WECHAT_ALLOW_SEND=false` |
| 首次登录 | **你** | 桌面终端 ` /opt/wechat/wechat ` → 扫码 → 加入群 → 逐个打开每个群 → 菜单退出微信 | `ls -d ~/xwechat_files/*/db_storage ~/Documents/xwechat_files/*/db_storage 2>/dev/null` 有输出 |
| `agent` | 脚本 | `bash deploy/bootstrap-xubuntu.sh --phase agent` | 打印 `WECHAT_LINUX_DATA_DIR` 与账号 ID，`init-db` 成功 |
| 启动会话 | **你** | 桌面终端 `bash scripts/launch-wechat.sh`，**保持这个终端不关** | 微信窗口出现，另开终端 `source scripts/linux-session.sh` 后 `$DBUS_SESSION_BUS_ADDRESS` 非空 |
| `key` | 脚本 | `bash deploy/bootstrap-xubuntu.sh --phase key` | 生成 `~/.local/state/wechat-agent-linux/linux_keys.json`（0600，属主是你） |
| `check` | 脚本 | `bash deploy/bootstrap-xubuntu.sh --phase check` | 预检 0 个 `[FAIL]`，`ready` 退出码 0 |

`--phase` 支持逗号组合：`--phase base,agent`。

时间预算：`base` 15–25 分钟（apt + pip 下载为主），其余每步 2–5 分钟。

---

## 4. 只读验收（打开发送之前）

```bash
export WECHAT_PYTHON="$HOME/.venvs/wechat-agent-linux/bin/python"

# 读消息（不依赖当前微信页面）
"$WECHAT_PYTHON" -m wechat_agent read-messages --config .env --limit 30 --out state/read-check.json

# 控制台（只读）
bash scripts/start-linux.sh desktop --config .env
```

Windows 侧建隧道后浏览器开 `http://127.0.0.1:8765`：

```powershell
ssh -N -L 8765:127.0.0.1:8765 wx@192.168.1.50
```

`WECHAT_ALLOW_SEND=false` 时控制台只能读、记录，不会发消息。跑够几个小时、`KEY_REQUIRED`/`ATSPI_UNAVAILABLE` 不再反复出现，再进下一步。

---

## 5. 打开发送（灰度）

```bash
# 1) 先停掉正在跑的 Agent
"$WECHAT_PYTHON" -m wechat_agent --stop --config .env
```

编辑 `.env`：

```ini
WECHAT_ALLOW_SEND=true
WECHAT_ALLOWED_MEMBERS=wxid_member1,wxid_member2
WECHAT_REPLY_COOLDOWN_SECONDS=30
WECHAT_MAX_REPLIES_PER_HOUR=20
WECHAT_PROACTIVE_REPLY=false
```

先只放测试成员（填 **wxid** 不是昵称），每小时 10–20 次；在测试群 @ 机器人，然后核对：微信里有回复 → 控制台「发送记录」有 `allowed` 审计 → 数据库有回读凭据 → 同问题不重发 → 写 `STOP` 后不再发。

⚠️ **别在打开发送后重跑 `--phase base/agent`**：它们会把 `WECHAT_ALLOW_SEND` 改回 `false`（这是故意的 fail-closed 设计）。

---

## 6. 24 小时运行

```bash
bash deploy/bootstrap-xubuntu.sh --phase autostart
```

写入三个 XFCE 自启动项：`wechat-a11y`（私有 D-Bus + AT-SPI 启动微信）、`wechat-agent`（等会话文件后守护运行，日志在 `logs/agent.log`）、`wechat-no-dpms`（`xset s off; xset -dpms`）。

另外手工确认：设置 → 电源管理器 → 接交流电时「从不」休眠/挂起、关闭显示器「从不」；设置 → 屏幕保护 → 关闭并取消锁定。

不建议用 systemd 用户服务：它拿不到 `launch-wechat.sh` 生成的那套 `DISPLAY`/`DBUS_SESSION_BUS_ADDRESS`/`AT_SPI_BUS_ADDRESS`。

---

## 7. 配置差异（导入你的 `.env` 后只需要关心这些）

| 键 | 现状 | 说明 |
|---|---|---|
| `WECHAT_PLATFORM` | 已是 `linux` | 无需改 |
| `WECHAT_LINUX_DATA_DIR` | `/home/wx/xwechat_files/wxid_example_abcd` | `agent` 阶段会自动发现并改写（后缀通常不同） |
| `WECHAT_ACCOUNT_ID` | `wxid_example` | 目录名去掉最后 `_xxxx`；不一致时 `configure-linux-env.py` 会报错而不是静默覆盖 |
| `WECHAT_ALLOW_SEND` | 原值 `true` | 导入时强制改 `false` |
| `DATABASE_URL` 等 10 处 | `/home/wx/...` | 用户不是 `wx` 时脚本自动 `--migrate-home /home/wx` |
| `PLATFORM_BROWSER_ENABLED` | `true` | 抖音/小红书用；VM 上不用就设 `false`，省掉 `playwright install` |
| `VISION_*` | 空 | 留空即复用同一个多模态 LLM，符合预期 |
| `OPENVIKING_URL` | `https://42.193.142.187:7000` | 公网地址，VM 一般可达；超时只降级不影响文本 |

---

## 8. 失败对照

| 现象 | 处理 |
|---|---|
| `PTRACE_DENIED` | 已内置：`--phase key` 自动 `sudo` 重试一次并 `chown` 密钥文件；**不要**全局关 `ptrace_scope` |
| `ATSPI_UNAVAILABLE` | 微信是不是用 `launch-wechat.sh` 起的；Agent 终端有没有 `source scripts/linux-session.sh`；`$DBUS_SESSION_BUS_ADDRESS` 和微信是否同一个 |
| `DATA_DIRECTORY_MISSING` | 微信没登录完/没打开过群；`ls -d ~/xwechat_files/*/db_storage ~/Documents/xwechat_files/*/db_storage 2>/dev/null` 确认后再 `--phase agent` |
| `ACCOUNT_MISMATCH` | 换号了；用 `--data-dir` 显式指定，或确认后加 `--force-account` |
| `KEY_REQUIRED` | 重跑 `--phase key`；微信重装/数据库换代后密钥必须重扫 |
| `STICKER_BUTTON_UNAVAILABLE` | 先在微信里打开任意群聊；或 `sed -i 's/^STICKER_ENABLED=.*/STICKER_ENABLED=false/' .env` |
| `WECHAT_VERSION_DRIFT` | 换回验证过的 4.1.x，**不要绕过检查硬发** |
| `CHAT_NOT_FOUND` | 群显示名和微信里不完全一致；用 `--phase check` 打印的 `--mapping` JSON 覆盖 `WECHAT_LINUX_CHAT_IDS` |
| 微信黑屏/闪退 | `LIBGL_ALWAYS_SOFTWARE=1 /opt/wechat/wechat`；或 VM 设置里关掉 3D 加速 |
| 模型 401/超时 | VM 里 `curl -m 5 http://192.168.1.50:8080/v1/models`；跨网段时改桥接 |
| 控制台打不开 | 确认 `127.0.0.1:8765`、隧道建对、`8765` 没被占 |

完整排错表见 DSH 插件仓库的部署文档第 14 节。

---

## 8.5 日常迭代：改人设 / 改代码

部署完成之后，最常用的两条路都不用重跑部署。

### 一次性准备（VM 里执行一次）

```bash
sudo apt install -y openssh-server && sudo systemctl enable --now ssh
```

Windows 侧可选免密：

```powershell
ssh-keygen -t ed25519 -f $env:USERPROFILE\.ssh\id_ed25519
type $env:USERPROFILE\.ssh\id_ed25519.pub | ssh wx@192.168.1.50 "mkdir -p ~/.ssh && cat >> ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys"
```

### A. 只改人设（30 秒）

直接在 VM 里改，不用同步：

```bash
mousepad ~/wechat-agent-linux/personas/dafeiyu/SKILL.md
```

改完在控制台点「停止」→「启动」，人设即重新加载（`references/lines.md`、`references/lore.md` 会自动一起读）。

### B. 改代码 + 人设，一条命令（推荐）

在 Windows 仓库根目录：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\deploy\dev-sync.ps1 -StartAgent
```

它做三件事：

1. `scp` 同步 `src/wechat_agent/`、`personas/`、`scripts/`（**不会碰 `.env`**）；
2. 通过 SSH 调 `scripts/restart-desktop.sh`，控制台以 `nohup` 后台重启，日志写 `~/.local/state/wechat-agent-linux/desktop.log`；
3. `-StartAgent` 时自动调 `/api/start`，新一轮代码立刻开始工作。

常用开关：

| 参数 | 作用 |
|---|---|
| `-StartAgent` | 重启后自动启动 Agent（不加则停在「已停止」，手动点启动） |
| `-NoRestart` | 只同步不重启 |
| `-Paths personas` | 只同步人设 |
| `-DryRun` | 只打印将执行的命令 |
| `-VmHost / -VmUser / -SshKey` | 目标机、用户、私钥路径 |

### C. 最舒服的长期方案

VS Code + **Remote - SSH** 扩展连 `wx@192.168.1.50`，打开 `~/wechat-agent-linux`，保存即写进 VM；改完在集成终端里跑：

```bash
bash scripts/restart-desktop.sh --start-agent
```

### 必须知道的三件事

1. **不要同步 `.env`**：Windows 那份还带着 `/home/wx/` 旧路径，覆盖回去前面全废。
2. **改代码只重启控制台不够**：UI 的「停止/启动」不会重新导入 Python 模块，必须重启 `desktop` 进程（`restart-desktop.sh` 就是干这个的）。venv 是 `pip install -e`，改 `.py` 不需要重新安装；改了 `pyproject.toml` 的依赖才需要 `pip install -e '.[test]'`。
3. **`launch-wechat.sh` 那个会话不能断**：`restart-desktop.sh` 复用它；微信退出或那个终端被关，脚本会明确报「无障碍总线已失效」。

---

## 9. 要让我远程接管，需要什么

- VM 的 IP（或端口映射后的宿主地址）、SSH 端口、用户名；
- 私钥文件路径（或临时密码），并确认允许从这台 Windows 登录；
- VM 已装 `openssh-server` 且防火墙放行；
- 最好先把 Xubuntu 装好、微信 `.deb` 下载好——图形安装我远程做不了。

拿到这些后我执行：

```powershell
.\deploy\push-from-windows.ps1 -VmHost <IP> -User wx -Phase base -WechatDeb <deb路径>
```

然后按阶段推进，中途要你扫码时我会停下来等你。

---

## 10. 本目录文件

| 文件 | 作用 |
|---|---|
| `bootstrap-xubuntu.sh` | VM 内编排器：`base` / `agent` / `key` / `check` / `autostart` / `report` |
| `push-from-windows.ps1` | Windows 侧：打包 → scp → ssh 执行 → 可选隧道 |
| `dev-sync.ps1` | Windows 侧：日常迭代，只同步代码/人设并重启控制台 |
| `../scripts/restart-desktop.sh` | VM 内：无终端后台重启控制台（SSH 也适用） |
| `out/wechat-agent-linux-src.tar.gz` | 已打好的源码包（不含 `.env`） |
| `out/env.from-windows` | 你的真实 `.env` 副本（含密钥，勿外发） |
| `README-XUBUNTU-DEPLOY.md` | 本文件 |

升级微信前、打开发送前、改 `.env` 前，先给 VM 打快照。
