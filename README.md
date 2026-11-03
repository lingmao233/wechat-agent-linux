# 微信群聊 Agent · Linux

> 在 DeepSeek Harness 里使用：**Attune**（微信 AI Agent / WeChat AI agent，搜 微信agent、微信ai、wechatagent、wechatai）— <https://github.com/lingmao233/dsh-plugin-attune-wechat-agent>

以 Windows 仓库 `lingmao233/wechat-agent-windows` 的 `45333d608c1f656a7861f5278b5cf82aca568081` 为基线，保留聊天、人设、生图、联网研究、控制台和策略层，增加 Linux 平台适配与记忆、上下文、情绪运行时。

Linux 通过本地 SQLCipher 数据库读取消息，通过 AT-SPI / X11 操作微信。发送结果必须有数据库回读凭据；原文、群事件、长期画像、情绪和成员关系分别保存，并使用稳定账号、群、成员 ID 隔离。

小红书与抖音通过可见浏览器适配器接入：平台账号可绑定到微信成员，形成三号互联；浏览、评论和 @ 记忆跨平台共享；群里的“想去/攻略/推荐”等意图会先触发平台浏览并并入群回复，回复发送后可为已关联成员生成带原生 @ 的平台评论计划；主动浏览默认关闭。平台写操作默认 dry-run，真实发送需要写开关和动作台账二次确认，不绕过验证码或登录保护。设计边界见 [三号互联方案](docs/PLATFORM-THREE-WAY.md) 与 [平台适配器说明](docs/PLATFORM_ADAPTERS.md)。

当前开发与实机验收状态见 [STATUS.md](STATUS.md)；逐项证据与未达到的指标见 [验收记录](docs/ACCEPTANCE.md)。

完整部署使用 `dist/wechat_agent_linux-0.2.0.tar.gz` 源码包，包含文档、安装脚本、迁移和人设。另有 Python wheel；单独安装 wheel 时，需另外准备图形会话和人设配置，并设置 `PERSONA_FILE` 指向人设文件。

## 安装与启动

参考环境为 Ubuntu 24.04 / WSL2 + WSLg，Python 3.11+，Linux 微信 4.1.x。发送需要可用的图形会话。

```bash
bash scripts/setup-linux.sh
```

脚本安装 AT-SPI、D-Bus、X11 工具及 Python 依赖，创建带系统包的 Linux venv，并在 `.env` 不存在时复制 `.env.linux.example`。已有 `.env` 会保留。

如果当前目录中已有 Windows `.venv`，使用另一个 Linux 路径：

```bash
export WECHAT_VENV_DIR="$HOME/.venvs/wechat-agent-linux"
bash scripts/setup-linux.sh
export WECHAT_PYTHON="$WECHAT_VENV_DIR/bin/python"
```

在 `.env` 中设置账号目录和明确的群映射：

```dotenv
WECHAT_PLATFORM=linux
WECHAT_LINUX_DATA_DIR=/home/your-user/xwechat_files/wxid_example_suffix
WECHAT_ACCOUNT_ID=wxid_example
WECHAT_GROUP_NAMES=测试群
WECHAT_LINUX_CHAT_IDS='{"测试群":"123456789@chatroom"}'
WECHAT_ALLOW_SEND=false
```

微信未运行时，在图形会话的终端中启动它：

```bash
bash scripts/launch-wechat.sh
```

保留该启动进程，在另一个终端启动 Agent：

```bash
source scripts/linux-session.sh
${WECHAT_PYTHON:-.venv/bin/python} -m wechat_agent keyscan
${WECHAT_PYTHON:-.venv/bin/python} -m wechat_agent ready
bash scripts/start-linux.sh desktop
```

控制台默认地址为 `http://127.0.0.1:8765`。`keyscan` 只扫描并验证密钥；微信原始数据库保持只读。权限不足的处理、独立会话和 WSLg 说明见 [Linux 部署](docs/DEPLOYMENT-LINUX.md)。

## 复用现有配置

LLM、Embedding、生图和人设配置沿用 Windows 配置。Linux 必需变更为数据路径、平台、账号及群 ID 映射。记忆与检索全部在本地 SQL 上完成，不依赖任何远程记忆服务。

工作区根 `.env` 是 Linux 虚拟机当前配置的 Windows 编辑副本，保留 Linux 路径。修改任一端后运行 `scripts/sync-env.py` 或双击 Windows 的 `sync_env.cmd`，将新配置同步到另一端；覆盖前备份，两端同时修改时停止。用法见 [配置同步](docs/ENVIRONMENT.md#windows-与虚拟机配置同步)。真实配置被 Git 和发行包排除。

复制配置也会保留原发送和主动发言开关。本次没有用根 `.env` 启动常驻自动回复；实机检查使用隔离配置，仅在测试进程中临时允许发送。

## 记忆与情绪

| 开关 | 能力 |
|---|---|
| `MEMORY_EVENT_EXTRACTION_ENABLED` | **一条消息一次模型判定**：事件、长期事实、一句话画像，带原文证据 |
| `MEMORY_ORCHESTRATOR_ENABLED` | 每轮重新规划检索；词法（FTS5）＋向量＋结构化，RRF 融合后按预算注入 |
| `SEMANTIC_GROUP_PROFILE_ENABLED` | 分讨论片段生成群兴趣、表达习惯、氛围和群梗 |
| `EMOTION_LAYER_ENABLED` | 每群 PAD、按成员好感、逐笔正分账本、衰减、修复和撤销 |
| `PROACTIVE_SCHEDULE_ENABLED` | 每群持久参与窗口、租约和额度 |

判定不依赖关键词表：模型说"是什么"，代码只决定"能不能用"（证据必须是原文字串、置信度低于阈值进候选、@ 他人判为不确定、问句与转述降置信）；好感分值永远来自固定效果表，模型给不出数值。每日额度 `MEMORY_JUDGE_DAILY_BUDGET` 默认 3000 次调用，超预算排队到次日，不丢消息。群时间线、成员关系与日 / 周摘要由维护线程每天整理一次。

这些开关默认关闭以支持分阶段启用。消息随机接话和计划参与可以同时启用，共用主动额度；所有回复仍走发送闸门。启用顺序和回滚见 [记忆运行时](docs/MEMORY-RUNTIME.md)。

## 主动聊天与角色配图

受邀的 @、点名和关键词回复与主动发言分别记录；它们不占主动发言的冷却和小时额度。对其他人的 @ 不作为主动接话候选。主动额度按当前账号和群持久保存，重启也不会重新计算为零。控制台可区分受邀回复、主动接话和冷场开话题。

开启 `WECHAT_PROACTIVE_REPLY` 后，群友正常聊天也会按消息概率随机获得接话机会，无需命中特定社交词或等到冷场；`WECHAT_PROACTIVE_PROBABILITY` 默认 0.4，聊天模型仍可选择 `[不回复]`。可同时开启 `PROACTIVE_SCHEDULE_ENABLED`，按每群参与窗口择机接话，默认窗口间隔 60–120 分钟。两种入口共用已发送记录、主动冷却和安静时段；同时开启时小时上限取两者的较小值，同一条已回复消息不会再被计划接话。

`PROACTIVE_IDLE_ENABLED=true` 允许持续观察到冷场后自行开话题，默认需安静 30 分钟、每个符合条件的窗口概率 0.4、每天最多两次。30 分钟是资格门槛，不是固定发言周期；离线、观察缺口、安静时段、额度不足和正在等待回答的问题都会阻止这次主动发送。配置了成员白名单时不面向全群冷场开话题。主动回复也可以选择纯文字、单图或文字加一张图。

启用并配置 `IMAGE_MODEL_ENABLED` 后，`REACTION_IMAGES_ENABLED=true` 允许聊天模型在获得配图机会时选择纯文字、文字加一张图或单发一张图。默认 `REACTION_IMAGE_PROBABILITY=0.3`，模型仍可不配图。需要配图时，先完成图片生成或缓存读取，再发文字，随后把图片作为另一条原生图片消息发送；图片准备失败时只发自然文字兜底。发送关闭或 STOP 存在时不为这类可选配图生成图片。

配图提示词由聊天模型根据聊天和心情创作，主体和画风来自当前人设目录下的 `references/image-style.md`，不再写死任何角色。一次只发一张单幅、单一意思的图片，不做九宫格或多表情合集；可以带不超过 20 字的通用短文案，不放“用户”、群名、昵称或收件对象称呼。图片可以表达与嘴上说法有反差的心情，参考场景不会变成固定回复模板。自动配图启用时不会用任意收藏表情绕过这个配图机会。

如果当前人设没有 `references/image-style.md`，自动配图会直接关闭，不会偷偷沿用之前角色的形象；用户明确说“画一张……”的普通生图不走这条路径。替换人设时，把说话风格放在 `references/speech-style.md`，把角色外观放在 `references/image-style.md`，两者都是可选的。`speech-style.md` 存在时才会附加默认的俏皮语气轮换；自定义人设不提供该文件时不会收到大肥鱼的口头禅或傲娇示例。

`REACTION_IMAGE_CACHE_DIR` 默认是 `state/reaction-images`。缓存按当前 `image-style.md` 的内容分目录保存，切换人设不会误用旧角色的图片；通用情绪、动作、文案和图片一并保存，聊天模型优先从已有目录选择合适图片；相同情绪、动作、文案的重复请求也会复用缓存，避免重复生成。请保留该目录；生图临时文件会清理，缓存图片会留下。控制台“设置 → 常用”可调整自动配图概率；“高级配置 → 主动参与”可调整计划参与参数。

## 语音识别与原生语音回复

`VOICE_ENABLED=true` 只处理白名单内新收到的他人语音，先转写，再判断点名、关键词和随机接话。默认 `VOICE_PROVIDER=auto`：配置阿里云后使用 `qwen-audio-3.1-asr-flash`，失败或转写无法理解时尝试 Linux 微信自带“转文字”；未配置阿里云时直接使用微信。设为 `wechat` 后不会调用 ASR，也不需要下载 small 模型。`aliyun` 表示优先阿里云并保留微信回退。

在运行机器的 `.env` 或控制台“设置 → 高级配置 → 语音回复”配置 `ALIYUN_AUDIO_BASE_URL` 和 `ALIYUN_AUDIO_API_KEY`。地址使用阿里云控制台提供、以 `/api/v1` 结尾的原生 API 根地址；识别与合成共用密钥和地域。阿里云识别会上传这条有界音频；微信转文字只操作当前 Linux 微信。方言是否准确需用已知原意的真实录音验证，不能把支持“方言”等同于已验证潮汕话。

微信转换会核实账号、群、分片、本地 ID、服务器 ID 和发送者，只接受原消息数据库中保存的文字，不把界面 OCR 或另一条同长度语音当作结果。目标语音需要在近期消息画面可见；不在可见范围、缺音频、识别超时等技术问题会提示暂时没读出来。转写由当前聊天 LLM 检查可理解性，通过后才原样进入上下文和记忆；无法理解时友好请对方重说，不猜测原话。澄清默认每群至少间隔 60 秒。

`VOICE_REPLY_ENABLED=true`、`VOICE_REPLY_PROBABILITY=0.15` 允许聊天模型偶尔选择一条语音回复。默认使用阿里云 `qwen-audio-3.1-tts-plus`、音色 `longanhuan_v3.1`，风格可调整 `TTS_INSTRUCTION`。声音完整生成并检查后才开始微信录音；生成失败保留文字回复。`VOICE_REPLY_CACHE_DIR` 按账号和群保存可复用声音。原生语音与图片互斥；语音内容仍保留在本地回复台账供上下文使用，不额外向群里发送相同文字。

语音合成已不再写死阿里云，`TTS_PROVIDER` 可选 `aliyun`、`openai_compatible`、`elevenlabs`。`aliyun` 继续使用 `ALIYUN_AUDIO_BASE_URL` / `ALIYUN_AUDIO_API_KEY`；另两家使用通用 `TTS_BASE_URL` / `TTS_API_KEY`，并配合 `TTS_MODEL`、`TTS_VOICE`。`openai_compatible` 覆盖任何实现 `POST /v1/audio/speech` 的服务和本地网关，只支持预置音色；`elevenlabs` 支持预置音色和即时复刻。切换供应商后要同时改模型和音色，不要沿用阿里的 `qwen-*` / `longan*` 值。生成失败不会退化成伪造音频。

控制台“设置 → 高级配置 → 语音回复”中的 `TTS_VOICE` 决定音色，`TTS_INSTRUCTION` 调整说话风格。软萌女声可先用 Plus 支持的 `longanlingxin_v3.1`，配合“清甜软萌、亲昵俏皮、句尾柔和、吐字清楚”的指令；风格指令不能保证把现有声线变成任意指定声音。修改音色或指令会生成另一份缓存，不会误用旧样音。音色 ID 必须属于当前 TTS 模型和供应商。

DSH 设置页「声音 DIY」按供应商能力显示：阿里云和 ElevenLabs 可上传 10～20 秒清晰单人录音（5～30 秒可用；WAV / MP3 / M4A 等，无配乐或混响）并复刻；OpenAI 兼容供应商只显示预置音色和「试听当前音色」，不会显示上传框。阿里云后端会转成 24 kHz 单声道 PCM，返回的 `voice_id` 自动保存为本地音色资料。可先试听，再点「设为当前」写入 `TTS_PROVIDER` / `TTS_VOICE`；切换时按需重启监听。旧版已经手工配置的自定义音色可以点「导入到 DIY 列表」归档。录音会发送到所选供应商用于复刻，样例只保存在虚拟机 `state/voice-profiles/`；删除 DIY 音色只清理本地资料，不会尝试删除云端音色。声音定制需要当前 Key 具备声音复刻权限。

原生发送需要当前 Linux 微信提供“发送语音”入口，以及 `pactl` / `paplay`。`scripts/setup-linux.sh` 安装 `pulseaudio-utils`，`scripts/launch-wechat.sh` 保留系统 PulseAudio / PipeWire 连接。发送时创建独立虚拟麦克风，只给本次微信录音流播放准备好的 WAV，结束或取消后恢复默认输入并卸载设备。已有录音或通话时拒绝执行。声音最长 55 秒，不拆分、不转成音频附件。成功要求目标会话新增一条本号发出的原生语音，具有服务器 ID 且时长吻合；微信会重新编码，数据库回读不代表已核验音频逐字相同或对方已播放。结果不明时禁止自动重发。

需要离线识别时才安装 `pip install -e '.[local-asr]'` 并设置 `VOICE_PROVIDER=local`；`VOICE_MODEL=small` 和 `VOICE_LANGUAGE=zh` 只影响此模式。其 CPU int8 模型在可终止的子进程中热加载。Linux 的 SILK 解码器 `pilk` 需要本地编译工具及对应 Python 开发头文件，安装脚本会准备。转写缓存 `VOICE_CACHE_DIR=state/voice` 包含账号、群、成员、来源、音频或消息身份和模型配置，切换识别方式后不会复用旧方式的结果。

## 检查与验收

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 ${WECHAT_PYTHON:-.venv/bin/python} -m pytest -q
${WECHAT_PYTHON:-.venv/bin/python} -m compileall -q src scripts
${WECHAT_PYTHON:-.venv/bin/python} -m wechat_agent memory-status
```

普通测试使用临时数据库，隔离真实 `.env`，不发送微信消息。连续实发是显式运行的独立脚本，例如在已加入白名单的文件传输助手中：

```bash
${WECHAT_PYTHON:-.venv/bin/python} scripts/accept-linux.py \
  --config /private/acceptance.env --chat 'File Transfer' \
  --kind text --count 50 --allow-send --out state/acceptance/text-50.json
```

该脚本遵守 STOP 和频率限制，保存回读凭据的摘要，遇到未知结果即停止，不自动重发。具体用法见 [验收记录](docs/ACCEPTANCE.md)。

## 交付文档

- [DSH 插件：Attune 微信 AI Agent 工作区（含一键连接／部署／配置虚拟机）](docs/DSH-PLUGIN.md)
- [Xubuntu 虚拟机部署（含 VM、微信和 X11 会话）](docs/DEPLOY-XUBUNTU-VM.md)
- [Linux 部署、会话与配置](docs/DEPLOYMENT-LINUX.md)
- [Linux 重构方案与边界](docs/LINUX-REFACTOR.md)
- [记忆、上下文、情绪与调度](docs/MEMORY-RUNTIME.md)
- [FR / AC 验收映射与证据](docs/ACCEPTANCE.md)
- [Linux PRD](docs/PRD-LINUX.md) / [记忆与情绪 PRD](docs/PRD-MEMORY-CONTEXT-EMOTION.md)

给非技术用户的最短路径：在 DSH 里打开任意会话 → 顶部「Attune」标签页 →「部署」→ 点「一键连接」打开 DSH 风格操作弹窗 → 在弹窗里复制命令到虚拟机终端运行（自动装好 SSH 并打印连接码）→ 把连接码粘回弹窗、输入开机密码 →「一键连接」→「一键部署」，详见 [DSH 插件说明](docs/DSH-PLUGIN.md)。虚拟机里还没装 Ubuntu 也能从零开始：VMware Workstation 安装包（夸克网盘）、Xubuntu / Ubuntu Desktop 24.04 LTS 官方链接和「怎么打开终端」都写在 [部署文档 2.0 / 2.2 节](docs/DEPLOY-XUBUNTU-VM.md)，操作弹窗第 1 步底部也内置了同一份教程。如果插件设置里已经连过这台虚拟机，部署页会直接显示「已连接」而不再要你填一遍；要做检测、部署或更新时点第 1 步「用部署页接管」（默认沿用本机 SSH 密钥），断开后连接自动交还给插件设置。模型 Key 和回复群在「设置」子页：第一组就是「模型」（文字模型地址 / Key / 模型），同一页可直接把写好的角色设定（`.md` 或整个文件夹）拖进「人设文件上传」上传到虚拟机 `personas/`，勾选「上传后设为当前人设」即可切换；也可以用「人设设计」输入文字描述，选择是否生成说话风格和生图外观，由模型生成 `SKILL.md` 和可选的 `references/speech-style.md`、`references/image-style.md`，预览修改后再上传，不会继承当前角色的语气或外观。

原 Windows 文档和适配器保留在仓库中；Linux 部署以本页及 `DEPLOYMENT-LINUX.md` 为入口。
