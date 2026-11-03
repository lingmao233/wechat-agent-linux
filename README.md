# wechat-agent-linux

Linux 微信 Agent 的完整 Python 源码仓库，也是 DSH 插件
[`dsh-plugin-wechat-agent`](https://github.com/lingmao233/dsh-plugin-wechat-agent)
一键部署时使用的 Agent 源码。

Agent 通过本地 SQLCipher 数据库读取微信消息，通过 AT-SPI / X11 操作微信；
发送结果必须经过数据库回读校验。平台侧支持小红书、抖音的可见浏览器适配器，
默认 dry-run，真实写操作需要显式开关和动作台账确认。

> 只使用**专用测试微信号**，不要用主号。自动化和自动回复存在账号限制风险。

## 与 DSH 插件的关系

普通用户不需要手动跑本仓库：

1. 在 DSH 中安装 [`dsh-plugin-wechat-agent`](https://github.com/lingmao233/dsh-plugin-wechat-agent)；
2. 打开「微信 → 部署 → 一键连接」，连接 Ubuntu / Xubuntu 24.04 LTS 虚拟机；
3. 点「一键部署到虚拟机」。

插件包内已经带了本仓库构建出的 `wechat-agent-linux-src.tar.gz`，部署时直接上传，
不需要虚拟机联网拉 GitHub。本仓库用于代码审查、二次开发和手动部署。

## 环境要求

- Ubuntu / Xubuntu 24.04 LTS，X11 桌面（Wayland 下需要 XWayland / X11 会话）；
- Python 3.11+；
- Linux 微信 4.1.x；
- D-Bus、AT-SPI、x11 工具（`setup-linux.sh` 会安装）；
- 一个专用测试微信号。

## 手动安装

```bash
git clone https://github.com/lingmao233/wechat-agent-linux.git
cd wechat-agent-linux
bash scripts/setup-linux.sh
```

`setup-linux.sh` 会安装系统依赖和 Python 依赖、创建带系统包的 venv，并在没有
`.env` 时复制 `.env.linux.example`。已有 `.env` 不会被覆盖。

编辑 `.env`，至少设置：

```dotenv
WECHAT_PLATFORM=linux
WECHAT_LINUX_DATA_DIR=/home/your-user/xwechat_files/wxid_example_suffix
WECHAT_ACCOUNT_ID=wxid_example
WECHAT_GROUP_NAMES=测试群
WECHAT_LINUX_CHAT_IDS='{"测试群":"123456789@chatroom"}'
WECHAT_ALLOW_SEND=false
```

启动微信（在图形会话的终端中）：

```bash
bash scripts/launch-wechat.sh
```

保持微信运行，在另一个终端启动 Agent：

```bash
source scripts/linux-session.sh
${WECHAT_PYTHON:-.venv/bin/python} -m wechat_agent keyscan
${WECHAT_PYTHON:-.venv/bin/python} -m wechat_agent ready
bash scripts/start-linux.sh desktop
```

控制台默认地址：<http://127.0.0.1:8765>。

更完整的 VM 创建、微信安装、X11 和排错步骤见
[deploy/README-XUBUNTU-DEPLOY.md](deploy/README-XUBUNTU-DEPLOY.md)。

## 安全默认值

- `WECHAT_ALLOW_SEND=false`：初始只读，确认群、人设和模型配置后再打开；
- 平台写操作默认 dry-run，真实发送需要开关和动作台账二次确认；
- 微信原始数据库只读，`keyscan` 只扫描并验证密钥；
- `.env`、数据库、日志、浏览器登录态和截图均被 `.gitignore` 排除；
- 不要提交模型 API Key、微信 Cookie、二维码、密码或真实 VM 地址。

## 许可证

MIT，见 [LICENSE](LICENSE)。`personas/dafeiyu` 保留自带的 MIT LICENSE。
