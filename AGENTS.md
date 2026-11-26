# AGENTS.md — 给接手这个仓库的 AI/人类贡献者的快速上手说明

> 目标：5 分钟内知道这个项目是什么、代码在哪、消息怎么流动、加功能改哪里、怎么验证。更细的专题文档见文末索引。

## 1. 项目是什么

一个部署在 Linux（Ubuntu + 微信 4.1.x）上的**微信 AI 群聊/私聊 Agent**：

- 用 AT-SPI + 屏幕/键鼠自动化驱动 Linux 版微信，读群消息、发文字/图片/原生语音/表情；
- 对话模型负责“回不回、说什么、用语音还是配图”，判定模型负责把群聊事实写进长期记忆（成员画像、关系、事件）；
- 自带 Web 控制台（`wechat_agent desktop`）、主动发言调度、抖音私信↔微信媒体桥、TTS（阿里云/Fish/OpenAI 兼容/ElevenLabs）、生图与角色配图缓存；
- 数据落在 SQLite（`memory.db`），配置在 `.env`（**密钥只放 .env，不进仓库**）。

## 2. 技术栈与运行形态

- Python 3.11；标准库为主，`httpx`、`pydantic-settings`、`Pillow`、`playwright`、`sqlalchemy`。
- 两个进程：微信客户端（保持登录，不要随意重启）+ `python -m wechat_agent desktop --port 8765`（控制台 + Agent）。
- 单 Agent 线程串行调用微信 UI；读消息循环、回复队列、主动调度、抖音桥各自有线程，但都通过同一把 UI 租约/队列串行化。
- 微信数据库是加密的，`platform/linux/db.py` 负责解密快照读取（只读），`keyscan` 负责取证密钥。

## 3. 目录地图（从“我要改什么”出发）

| 位置 | 职责 | 什么时候看/改 |
|---|---|---|
| `src/wechat_agent/agent.py` | 主循环：读消息→判定→记忆→生成回复→发送→统计；语音/配图/主动发言的编排 | 改回复流程、发送时机 |
| `src/wechat_agent/llm.py` | 对话模型调用、结构化回复 JSON 协议、提示词拼装、泄漏防护 | 改回复格式、提示词 |
| `src/wechat_agent/judge.py` | 单条消息判定（情绪/事件/事实主体），写入记忆 | 改记忆抽取、归属规则 |
| `src/wechat_agent/memory_orchestrator.py` / `memory_runtime.py` / `storage.py` | 记忆检索、整理、画像重建、SQLite 存储层 | 改记忆/画像/检索 |
| `src/wechat_agent/desktop.py` | 控制台 HTTP 服务 + Agent 控制器 + `/api/*` 路由 + 设置项定义 | 加接口、加配置项 |
| `src/wechat_agent/platforms/`、`platform/linux/` | 平台适配：Linux 微信 UI（AT-SPI）、浏览器平台（Playwright） | 改微信操作、加平台 |
| `src/wechat_agent/douyin_bridge/` | 抖音私信↔微信媒体桥（无头浏览器） | 抖音相关功能；详见 `docs/抖音桥-架构与扩展指南.md` |
| `src/wechat_agent/speech.py`、`tts/`、`voice*.py` | 语音识别/合成、音色管理、原生语音条发送 | 改语音 |
| `src/wechat_agent/image*.py`、`reaction_images.py` | 生图、图片编解码、角色配图缓存 | 改配图 |
| `src/wechat_agent/persona*.py`、`personas/` | 人设与生图风格（`references/image-style.md`） | 改角色设定 |
| `src/wechat_agent/proactive_scheduler.py` | 主动发言：冷却、额度、计划窗口、冷场主动 | 改主动策略 |
| `src/wechat_agent/web/` | 控制台页面模板与静态资源 | 改控制台 UI |
| `scripts/` | 部署/启动/验收脚本（`restart-desktop.sh` 等） | 部署运维 |
| `deploy/` | Ubuntu 部署与同步脚本（**不要提交 `deploy/out/`**） | 部署 |

## 4. 关键数据流（记这三条就够）

1. **收消息 → 回复**：读微信 DB 新消息 → `judge.py` 判定（是否对 AI 说、情绪、事实主体）→ 写记忆 → `llm.py` 生成结构化回复（`delivery: text|voice|image`）→ 语音/配图准备 → `backend.send()` → 回读微信 DB 核验 → 失败重试/放弃。
2. **记忆**：事实表（带主体/时间/原文）→ 成员画像/关系/事件 → 每轮按问题检索注入提示词；合并、归档、重建画像都走 `storage.py`，**账本是唯一真相**。
3. **抖音桥**：无头 Chromium 轮询抖音私信 → 媒体任务落 SQLite → 下载/转码 → 微信发送并回读 →（反向）读微信表情/命令发回抖音。

## 5. 必须遵守的约定

- **密钥/隐私**：任何 token、API key、cookie 只进 `.env`；不写死、不打印、不提交。测试与文档里用占位符。
- **发送必须回读核验**：UI 点击成功不等于发出，必须从微信 DB 读回（作者/会话/类型/大小）才算成功；结果不确定（uncertain）**不自动重发**，避免重复投递。
- **fail closed**：会话/身份无法确认时不猜、不发。
- **幂等**：出站动作带 `idempotency_key`，重放不重复发送；媒体任务按指纹去重。
- **不要动微信客户端进程**：登录态很脆弱；部署/调试只重启 Agent 或控制台（`scripts/restart-desktop.sh --start-agent --keep-log`）。
- **改表结构要兼容旧库**：新增列可空/带默认值，不 drop 旧表；`deploy/out/`、`state/`、`*.db`、日志不进仓库。
- **改了行为就补测试**（`tests/`），至少覆盖正常路径 + 失败/重试 + 幂等。

## 6. 本地运行与验证

```bash
# 安装
python -m venv .venv && .venv/bin/pip install -r requirements.txt
# 配置：复制 .env.example 为 .env，填入模型/服务凭据（不要提交）
# 启动控制台 + Agent（Linux + 已登录微信）
python -m wechat_agent desktop --start-agent --port 8765
# 只读状态
curl -s localhost:8765/api/status
curl -s localhost:8765/api/douyin-bridge
# 测试
pytest -q
# 重启（不重新扫码、不动微信客户端）
bash scripts/restart-desktop.sh --start-agent --keep-log --port 8765
```

## 7. 加一个功能的推荐路径

1. 先读对应模块 + 本文件 + 相关文档，确认消息流上插入点（收消息处/判定处/发消息处/定时器等）。
2. 配置项加到 `config.py`，控制台可见性加到 `desktop.py` 的设置项列表，示例加到 `.env.example`。
3. 用现有机制：回读核验、幂等键、限流/冷却、灰度开关（默认关闭更安全）。
4. 补离线测试（假 UI/假后端/临时 SQLite，不联网不发真消息）。
5. 部署到 VM：备份 → 同步源文件 → `py_compile` → `restart-desktop.sh --start-agent --keep-log` → 用 `/api/*` + 日志 + DB 回读验收。
6. 在 `STATUS.md`（本地运维日志，不发布）记录现象/根因/改动/测试/部署/边界。

## 8. 已知边界（踩过坑）

- 微信 Linux 自动化依赖 AT-SPI 与固定窗口/控件版本，微信大版本升级可能全线失效；改版先跑只读探针。
- 抖音网页版非官方接口：长驻 1.5–2 小时后会话切换会半卡死（`CHAT_IDENTITY_UNCONFIRMED` 占比 66–90%），不会自愈；看门狗只在“全部绑定失败”时升级，必要时重启浏览器/控制台。
- 抖音表情常被识别成 `kind=video`；静态表情要转成 2 帧 GIF 微信才收成原生表情（`local_type=47`）。
- 主动发言有冷却/每小时/每日额度与静默时段，修改前先看 `proactive_scheduler.py` 的额度统计口径。
- 项目无 GPU 依赖：生图/TTS 走云端；不要在控制台线程里做长耗时同步调用。

## 9. 文档索引

- `README.md`：项目介绍、安装、配置、使用。
- `docs/抖音桥-架构与扩展指南.md`：抖音桥文件地图、数据流、加功能流程、调试命令。
- `docs/DOUYIN-WECHAT-BRIDGE.md`：抖音桥设计细节与验收记录。
- `docs/生图提示词-*.md/txt`：角色配图的提示词模板。
- `docs/语音情绪与语速-开源方案调研.md`：TTS 情绪/语速控制的开源方案对比。
