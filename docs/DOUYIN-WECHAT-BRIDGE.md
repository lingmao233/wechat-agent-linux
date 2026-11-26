# 抖音私信 → 微信媒体桥

更新时间：2026-11-18。范围：让 AI 在抖音网页版私信里接收用户发来的**表情包**或**视频/视频链接**，自动送到与这个抖音会话绑定的微信联系人；反向微信表情由独立开关控制，默认自动转抖音。两条方向都走现有微信发送台账和数据库回读。

这份文档同时是边界说明和验收口径。**代码完成不等于实机验收**；文末列出必须由账号本人在虚拟机里完成的三步。

## 1. 用户看到的流程

```text
抖音私信（可见浏览器）
  用户： [表情包]        ← 不需要配任何文字
  用户： [视频/视频链接] ← 不需要配任何文字
        │
        ▼
  自动识别媒体类型（不交给模型）
        │
        ▼
  下载/校验媒体 ── 表情包：原图 → GIF（微信原生动态表情）
        │           视频：DTK 无水印流 → mp4
        ▼
  绑定关系：抖音会话 → 微信 wxid
        │
        ▼
  微信 Linux 后端发送（表情文件 / 视频文件）→ 数据库回读
        │
        ▼
  抖音回执：表情包/视频发到你微信了
```

反向（微信 → 抖音）由独立开关控制，默认自动：

```text
微信私聊（已绑定的联系人）
  用户： [微信表情包]   ← 不需要指令（auto_sticker=true）
        │
        ▼
  绑定关系反查：微信 wxid → 抖音会话
        │
        ▼
  读取微信表情原文件 → 抖音上传 → 页面回读
        │
        ▼
  微信回执：表情包已经发到抖音了
```

视频同样支持两种入口：

1. 直接转发视频卡片（有 aweme id 时自动导出）；
2. 直接发视频链接（自动导出）。

补充规则：

- **只支持一对一私聊**：群聊 `@chatroom` 在绑定/生成码时直接拒绝，反向轮询也跳过。
- **开关隔离**：普通私聊回复用 `WECHAT_PRIVATE_REPLY_ENABLED`，跨软件表情用 `DOUYIN_BRIDGE_WECHAT_ENABLED` / `DOUYIN_BRIDGE_WECHAT_AUTO_STICKER`，互不联动。
- **批量**：一次发多个表情包会逐个建任务/逐个上传；`DOUYIN_BRIDGE_MAX_JOBS_PER_HOUR` 默认 60。
- **多用户**：同一个抖音登录账号可绑定多个微信 wxid，轮询按绑定逐条打开会话。

## 2. 已实现 / 未验证

| 项 | 状态 |
|---|---|
| 指令解析、媒体上下文关联、去重、绑定码、任务状态机 | 已实现，离线单测覆盖 |
| DTK v5 客户端（异步任务、v4 兼容、无水印校验、大小/文件头校验） | 已实现；**已在真实 DTK 上解析并下载 mp4** |
| 视频下载多镜像回退与瞬时失败重试 | 已实现；真实视频重试后 `delivered`（5,304,658 字节，回读 `message_id` 非空） |
| 抖音图集（图文）→ 微信原生图片 | 已实现；真实图集任务 `delivered`，微信回读 `local_type=3` 图片（最多 `DOUYIN_BRIDGE_MAX_ALBUM_IMAGES` 张） |
| 表情包下载、GIF 转换、微信原生动态表情投递 | 已实现；**抖音表情包自动转发、微信收成 `appmsg type=8` 动态表情已真机通过** |
| 微信 → 抖音表情包 | 已实现；显式指令 + 微信表情原文件 → 抖音上传；**`button.MsgInputSendFileModalbtnSure` 页面内点击已真机验证，`SEND_MEDIA verified=True, modal_gone=True`** |
| 微信原生文件 / 视频发送 | 已实现；**文件、原生视频（type 43）真机通过** |
| 抖音网页版私信 DOM 读取、发送、conversation id 捕获 | 已实现；**会话列表、自动读取、图片/文本分类、自动回复已在真机通过** |
| 转发视频卡片（无链接）自动取分享链接 | **实验性、默认关闭**：卡片 DOM 没有 aweme id，详情浮层也不稳定暴露 id；真机验证未通过，暂停使用（见 18.7） |
| 控制台 API | 已实现；控制台页面暂未加独立卡片 |
| DTK 实例 | **已部署并导入抖音登录态**；API 在腾讯云 8000，VM 走 18000 SSH 隧道 |
| 真实端到端（抖音发 → 微信收） | **表情包自动转发已通过**；**视频链接已通过**（5.3MB mp4 真机投递 + 回读）；**图集已通过**（图片按原生图片消息投递）；纯抖音视频卡片待测 |
| 真实端到端（微信发 → 抖音收） | **微信表情已能上传到抖音**；页面内点击确认按钮后 `verified=True`，抖音出现本号发出的图片 |
| 多用户 / 批量 | 已实现：轮询按绑定逐条打开会话；批量表情逐个建任务/上传；单测覆盖两个绑定与两个并发媒体任务 |
| 私聊/群聊边界 | 绑定与轮询都拒绝 `@chatroom`；群聊回复开关与桥开关独立 |

没有完成第 14 节的三步之前，只能表述为“离线链路、页面适配和发送闸门已实现”，不能说“已经在真实抖音/微信上跑通”。

## 3. 架构

```text
BrowserPlatformAdapter (Playwright, persistent profile)
  ├─ 主 tab：公开内容浏览（原有）
  └─ chat tab：www.douyin.com/chat（本功能，新增）
        │  page.evaluate 读取消息行 / 点击会话 / 输入并回车
        ▼
DouyinChatSession ── ChatMessage(direction, kind, text, image_url, video_ref, fingerprint)
        │
        ▼
DouyinBridgeService
  ├─ intent.parse_command       确定性指令
  ├─ BridgeStore               绑定、任务、已见指纹、最后一条媒体
  ├─ MediaFetcher / DTKClient  图片原图 / 无水印 mp4
  └─ WeChatDelivery            构造 OutboundAction → LinuxBackend.send
        │
        ▼
LinuxBackend（原有发送台账、STOP、限速、数据库回读）
```

关键取舍：

- **不加第二个浏览器 profile。** Chromium 会锁住 user-data 目录；再开一个 profile 要么失败，要么要求第二次扫码。聊天 tab 与公开浏览 tab 共用同一个 persistent context。当前 VM 的 profile 在 `/home/jdy/wechat-agent-linux/browser-data/douyin`，浏览器窗口由 Playwright 从 VM 图形会话启动，地址是 `https://www.douyin.com/chat`；没有单独的“浏览器安装目录”需要手工维护。
- **Playwright 的所有调用都由桥的轮询线程独占。** Playwright 同步 API 的调度 greenlet 绑定创建线程；控制台 HTTP 线程只读缓存状态，`/api/douyin-bridge/run` 也只是唤醒轮询线程，不直接碰页面。
- **桥运行期间不要再打开「平台」页浏览。** 同一个 persistent context 被两个线程使用会触发 `browser_thread_conflict`。如果群里不需要平台搜索，建议同时设 `PLATFORM_INTENT_ENABLED=false`、`PLATFORM_PROACTIVE_ENABLED=false`，让浏览器只服务抖音桥。
- **不复制平台私有签名。** 视频解析交给独立部署的 DTK；本项目只读取可见页面和 DTK 的公开 HTTP 接口。
- **不让模型决定“要不要发”。** 只有 `intent.py` 能把文本变成转移指令；模型只负责普通聊天和措辞。

### 轮询策略：先读列表，不反复点开

旧实现每轮把绑定会话逐个点开读 DOM，10 个绑定会让抖音聊天列表不断跳转/闪现。现在：

1. 每轮只调用 `list_conversations()` 读会话列表的 `name / preview / unread`，**不点击**；
2. 把预览里的“刚刚/几分钟前/时间”归一化后做签名，和上一轮比较；
3. 只有签名变化（有人发消息/未读增加）或到达 `FULL_SCAN_SECONDS` 兜底时间，才点开对应会话读取；
4. 点开后仍核实会话标题；失败只跳过该绑定并退避 60 秒，不阻塞其他人；
5. 每轮默认只处理 3 个绑定，避免一次性切换太多。

实测 10 个绑定：启动首轮点开 10 个建立基线，之后 90 秒内 `opened=0`，不再反复扫描/闪现。抖音没有公开的“新消息推送”接口；网页版列表预览就是可用的低成本信号，不需要下载 App。App 也不会提供更安全的自动化接口，反而增加风控面。

## 4. 为什么是网页版私信

- 抖音网页版有 `https://www.douyin.com/chat`，同结构的开源实现已经验证过会话列表、消息行、输入框和回车发送（`dsh-douyin-chat`，MIT，2026-11-09 探测）。
- 网页版消息 DOM **没有 message id**，所以本项目用内容指纹去重，而不是假装有稳定消息 ID。
- 会话的稳定 `conversation_id` 从 `get_by_conversation` 请求体里捕获；捕获不到时退回 `name:<hash>`，并在绑定记录里明确标注来源。多用户时每个绑定各有自己的 `conversation_id` 和 `wxid`，不靠昵称猜人。

风险：这是非官方接口。账号可能被限流、要求验证或封禁。默认低频、只监听已绑定的私聊会话、群聊不参与；出现验证码/安全验证立即停手，不绕过。

## 5. 绑定模型（多用户）

同一个抖音登录账号可以绑定多个微信联系人；每条绑定独立保存 `conversation_id`、`peer_name`、`wxid`。数据表本来就按 `account_id + conversation_id` 唯一，现在轮询也按绑定逐条打开会话。

```text
douyin_bridge_bindings
  account_id (本机微信账号) + conversation_id (抖音会话稳定键)
    → wxid / wx_name（一对一微信联系人）
```

只允许**一对一私聊**：`wxid` 以 `@chatroom` 结尾的群聊在绑定/生成码时直接拒绝，`poll_wechat` 也会跳过群绑定。

### 绑定方式

**方式 0：控制台界面（推荐）**

打开控制台左侧「抖音桥」页面，或直接访问 `http://127.0.0.1:8765/douyin-bridge`：

1. 「微信联系人」下拉框选择一对一联系人；
2. 点「刷新抖音会话」，在「抖音私聊」下拉框选择会话；
3. 点「绑定」；表格出现记录即生效，点「解绑」可删除。

界面不需要输入任何指令/绑定码；下拉框分别来自本机微信联系人和当前抖音私信列表。

**方式 A：控制台/API 直接绑定（多用户推荐）**

```bash
# 1) 查微信 wxid（只取一对一联系人）
curl -s http://127.0.0.1:8765/api/wechat/private-chats

# 2) 用抖音显示名 + 微信 wxid 绑定；不需要手算 conversation_id
curl -X POST http://127.0.0.1:8765/api/douyin-bridge/bindings \
  -H 'Content-Type: application/json' \
  -d '{"peer_name":"抖音昵称","wxid":"wxid_xxx","wx_name":"微信昵称"}'
```

`conversation_id` 会自动按 `name:<sha20>` 计算；如果你已经拿到捕获的稳定 id，也可以显式传 `conversation_id` 覆盖。

**方式 B：绑定码（用户在抖音侧自助绑定）**

```bash
curl -X POST http://127.0.0.1:8765/api/douyin-bridge/codes \
  -H 'Content-Type: application/json' \
  -d '{"wxid":"wxid_xxx","wx_name":"微信昵称","peer_name":"抖音昵称"}'
```

把返回的 6 位码发给该抖音用户，让他在对应抖音私聊里发：

```text
绑定码 123456
```

`peer_name` 会让桥临时轮询这个未绑定的抖音会话，收到码后自动完成绑定并停止临时轮询。多用户时每个用户各自生成一个码，必须带 `peer_name`；不带 `peer_name` 的码只适用于当前默认会话/第一个用户。

> 绑定码默认 10 分钟过期；群聊码会被拒绝。抖音显示名改名后需要重新绑定。

两种绑定方式：

1. **控制台手动绑定**：`POST /api/douyin-bridge/bindings`
   `{conversation_id, wxid, wx_name}`
2. **绑定码**（推荐，证明微信侧控制权）：
   - 操作员在控制台生成 6 位码：`POST /api/douyin-bridge/codes` `{wxid, wx_name}`；
   - 把码通过微信发给该联系人（或自己）；
   - 联系人在抖音私信里发 `绑定码 123456`；
   - 服务校验未过期、未使用，写入绑定；
   - 码只存 SHA-256，不存明文；默认 10 分钟有效。

绑定是**投递授权**，与 `platform_identities` 的 @ 白名单是两套 grant，互不自动升级。

## 6. 指令与上下文

支持的明确指令（示例）：

- `把这个表情包转到微信`
- `这个表情包帮我发到微信`
- `这个视频导出到微信`
- `这条无水印发我微信`
- `https://v.douyin.com/xxxx/ 导出到微信`
- `绑定码 123456`
- `转到微信了吗` / `算了，不转了`

规则：

- 必须同时出现**微信目的地**和**转移动词**；只是在聊“我在微信看到……”不算。
- 否定（`不要转到微信`）不触发；`先别发到微信`按取消处理。
- “能不能转到微信吗”是能力询问，不触发；`能不能帮我转到微信`是请求，会触发。
- 媒体和指令可以分两条消息：先发媒体，再在 `DOUYIN_BRIDGE_CONTEXT_SECONDS`（默认 300 秒）内说“转到微信”。
- 抖音侧收到新的表情/图片或视频会自动外发，不需要任何文字指令；普通文本不会触发。
- 反向微信 → 抖音默认自动（`DOUYIN_BRIDGE_WECHAT_AUTO_STICKER=true`）；关闭该开关后才必须说“转到抖音/发到抖音”。

去重：

- 媒体按 `direction|kind|正文|图片路径|视频引用` 做 SHA-256；CDN 签名参数不参与。
- 文本指令按“正文 + 当前引用的媒体指纹”做键，所以同一句“转到微信”对不同的表情包仍然有效。
- 普通文本按 10 分钟时间桶去重，避免轮询把同一句状态查询刷成多次回复。

## 7. 媒体处理

### 表情包

1. 从消息行里取最大的一张非头像图片 URL；
2. 用普通 HTTP 客户端下载（带 `Referer: https://www.douyin.com/`），拒绝非图片 magic；
3. 失败（403/超时）时通过当前浏览器 tab 的 `fetch(..., credentials: include)` 再取一次；
4. 原文件按 SHA-256 命名保存；
5. 生成一张 1024px 以内的白底 PNG 预览，用于微信原生图片消息；
6. 如果是 GIF/动画 WebP，再额外把原文件作为微信文件发送，保留动画。

微信侧默认：只发送**表情文件本身**（原 GIF；静态图转成一帧 GIF），不再发 PNG 预览图。微信把从文件选择框进来的 GIF 收成 `appmsg type=8` 动态表情，**也可能收成本地 `local_type=47` 原生表情**（两种都按 md5 + 字节数回读）。因此接收方看到的是表情，不是图片或文件。若 GIF 转换失败，会明确报错，不会偷偷退回原生图片。

### 图集（图文）

1. DTK 返回 `kind=image_album` 时不再报“没有视频流”，而是取 `media.images[].url/urls`；
2. 逐张下载并校验图片 magic（`MEDIA_*` 失败时回退到浏览器 tab 的 `fetch(..., credentials: include)`）；
3. 全部成功后才进入发送阶段，避免“只发了半套图”；
4. 微信侧逐张发**原生图片消息**（不是表情、不是文件），每张用独立幂等键，重试不会重发已回读的图；
5. 张数上限 `DOUYIN_BRIDGE_MAX_ALBUM_IMAGES`（默认 9）。DTK 的 `media.images` 有多少张就发多少张（部分“图文”帖本身只有 1 张图，实测三条链接都是 1 张）。

### 视频卡片（无链接）

1. 消息行只有封面图、播放角标或时长，DOM 里没有任何 `video/<id>` / `modal_id` / 分享链接；
2. 在**已绑定**会话里，按封面图或行文本精确定位这一行，点击卡片；
3. 等待新标签页或当前页 URL 变化，从 `https://www.douyin.com/video/<id>` 或 `?modal_id=<id>` 提取引用；也兼容页内详情浮层（读取浮层里的视频链接 / `modal_id`）；
4. 收起浮层/关闭新标签页并回到原会话，然后把引用交给 DTK 解析下载；取不到引用时回一句“转发一次，或把视频链接发我”，**不猜**；
5. 未绑定时不会点卡片，只回复绑定提示。

### B 站链接（无水印 MP4）

同一个桥也接受 B 站视频链接，两个入口都支持：

- **抖音私聊**里收到 `bilibili.com/video/BV...` / `b23.tv/...` 链接 → 下载后发到该会话绑定的微信；
- **微信已绑定私聊**里收到 B 站链接 → 下载后回传到同一个微信会话。

实现：直接读取 B 站网页播放的 DASH 分轨（`dash.video` + `dash.audio`），选 AVC 最高画质和最高音轨，分别下载后用 `ffmpeg -c copy` 无损合并为 MP4。没有平台烧录水印；作者自己压进画面的 logo/字幕无法去除。

- 1080P+/4K 需要 `BILIBILI_SESSDATA` 登录 Cookie（部分内容还要大会员）；留空则导出匿名可取的画质。
- 付费课程、DRM、需要绕过权限的内容不做。
- 实测：微信私聊发 `https://b23.tv/FJMQroq` → 25,976,475 字节 MP4 回传到微信，任务 `delivered`。


### 视频

1. 从卡片 DOM 的链接/属性或正文里取 aweme id / 分享链接；取不到时按上面的“视频卡片（无链接）”处理；
2. 调 DTK `POST /api/v1/parse`（v5 异步，`?wait=` 或轮询 `/api/v1/tasks/{id}`）；
3. 优先选明确 `watermark: false` 的流；只有 `watermark: true` 时拒绝；字段缺失时记录 `watermark=null`，不声称“已逐帧验证”；
4. 流式下载 mp4，校验 `ftyp` 文件头、Content-Type、大小上限；
5. **多镜像回退**：DTK 会给同一路流多个 CDN 地址（`media.video.urls`），客户端按顺序尝试；每条地址对 `ConnectError/超时/429/5xx` 自动重试 2 次，非视频响应自动换下一条镜像。下载请求单独构造 header，**不把 DTK 的 `Authorization`/`X-API-Key` 发给抖音 CDN**；
6. 作为微信文件发送（手机可播放/转发）。

**不做**像素级去水印、不裁剪、不重新编码。验收口径按“抖音平台水印/账号角标/平台片尾不出现”执行；作者自己画面里的署名可以保留。

## 8. 微信投递

`WeChatDelivery` 只构造 `OutboundAction` 并调用现有 `LinuxBackend.send`，因此自然继承：

- STOP 文件；
- `WECHAT_ALLOW_SEND`；
- 每分钟 20 条熔断、每小时上限、最小间隔；
- 幂等台账（同一 `idempotency_key` 不会重发）；
- 发送后数据库回读。

新增：

- `OutboundAction.file_paths`：文件消息，回读条件是 `appmsg/type == 6` 且标题等于文件名；
- `LinuxBackend.set_dm_allowlist_provider(...)`：绑定表里的 wxid 可以绕过“私聊白名单/成员白名单”，因为绑定本身就是授权；未绑定的 wxid 仍然被拒绝；
- `AtspiUI.send_file`：点“发送文件” → 等“选择文件”对话框 → 写文件名并回读 → 点“打开” → 等对话框关闭；
- **图集**：逐张 `deliver_image`，每张独立幂等键 `dybridge:<job_id>:album:<i>`；回执写“图集（N 张）发到你微信了”；
- **图片粘贴容错**：微信 a11y 桥在粘贴大图时偶尔几秒无响应。`send_image` 现在对单次 `getText` 的 AT-SPI 超时只重试（6 秒预算），不立即失败；若已粘贴但未点发送就失败，会尝试清掉输入框里的图片占位符（`\ufffc`），避免下一次被 `INPUT_OCCUPIED` 挡住。

图片发送会把透明背景合成白底，避免微信里透明区域变黑。

### 微信 → 抖音

- 只轮询**已绑定**的微信 wxid；用 `douyin_bridge_wechat_cursors` 记录已读 `local_id`，首次接上只建立基线，不回灌历史。
- 收到微信 `sticker`（`type=47`）时，`DOUYIN_BRIDGE_WECHAT_AUTO_STICKER=true` 就直接读取原文件上传抖音；关闭该开关时只记为“待转表情”，等用户说“转到抖音”再发。
- 自动模式直接读取原文件 → 在抖音会话里通过 `input[type=file]` 上传 → 点击 `button.MsgInputSendFileModalbtnSure`（“发送（1）”）→ 页面级回读（弹窗消失或出现新的本号图片）。关闭自动模式时，才由“转到抖音”触发同一上传动作。
- 结果用微信私聊回执：“表情包已经发到抖音了” / “结果不确定，先不自动重发”。
- 抖音上传的是 GIF/图片资源，不是抖音原生表情面板里的内置表情；抖音侧会显示为图片/GIF 消息。

## 9. 数据表

| 表 | 用途 |
|---|---|
| `douyin_bridge_bindings` | 抖音会话 ↔ 微信 wxid；唯一键 `account_id + conversation_id` |
| `douyin_bridge_codes` | 一次性绑定码的 SHA-256、过期时间、使用时间 |
| `douyin_bridge_jobs` | 一次转移的完整状态、媒体路径/SHA/大小、目标、回读到的消息 ID |
| `douyin_bridge_seen` | 消息内容指纹，7 天清理；不是聊天归档 |
| `douyin_bridge_peer_state` | 每个抖音会话最后一条媒体，供后续指令引用 |
| `douyin_bridge_wechat_cursors` | 每个绑定 wxid 已读到的微信 `local_id`，用于反向轮询不重复 |

任务状态：

```text
detected → resolving → downloading → ready
                                      ├─ dry-run：停在这里
                                      ▼
                                  delivering → delivered
                                      ├─ failed（确定没发出去）
                                      └─ uncertain（结果不明，禁止自动重发）
```

`delivered` 只由微信数据库回读产生；抖音回执不改变状态。

## 10. 配置

| 键 | 默认 | 说明 |
|---|---|---|
| `DOUYIN_BRIDGE_ENABLED` | false | 抖音→微信总开关；还需 `PLATFORM_BROWSER_ENABLED=true` |
| `DOUYIN_BRIDGE_WECHAT_ENABLED` | true | 微信→抖音表情开关，和普通私聊回复独立 |
| `DOUYIN_BRIDGE_WECHAT_AUTO_STICKER` | true | 与普通私聊回复独立；微信表情无需指令直接转抖音 |
| `DOUYIN_BRIDGE_CONVERSATION` | 空 | 唯一监听的私信会话显示名 |
| `DOUYIN_BRIDGE_ACCOUNT_ID` | 空 | 本地审计标识，不是密码 |
| `DOUYIN_BRIDGE_DRY_RUN` | true | 只准备、不点微信 |
| `DOUYIN_BRIDGE_REPLY_ENABLED` | false | 抖音侧回执/提示总开关；当前关闭以降低风控和误发 |
| `DOUYIN_BRIDGE_CHAT_ENABLED` | false | 非指令消息是否让模型接话 |
| `DOUYIN_BRIDGE_POLL_SECONDS` | 6 | 轮询间隔（≥3） |
| `DOUYIN_BRIDGE_CONTEXT_SECONDS` | 300 | 媒体→指令的有效间隔 |
| `DOUYIN_BRIDGE_MAX_JOBS_PER_HOUR` | 60 | 每小时成功投递上限；批量表情需要更高额度 |
| `DOUYIN_BRIDGE_BINDINGS_PER_POLL` | 3 | 每轮最多切几个绑定会话；多用户时轮询，降低切错/风控 |
| `DOUYIN_BRIDGE_LIST_SCAN` | true | 只读会话列表预览/未读，不逐个点开会话；预览变化才点进去 |
| `DOUYIN_BRIDGE_FULL_SCAN_SECONDS` | 600 | 兜底全量打开间隔；防止预览相同的新消息漏掉 |
| `DOUYIN_BRIDGE_MAX_VIDEO_MB` | 500 | 视频上限 |
| `DOUYIN_BRIDGE_MAX_STICKER_MB` | 10 | 单张图片上限 |
| `DOUYIN_BRIDGE_MAX_ALBUM_IMAGES` | 9 | 图集最多转发张数 |
| `DOUYIN_BRIDGE_MEDIA_DIR` | `state/douyin-bridge` | 媒体落盘目录 |
| `DOUYIN_BRIDGE_MEDIA_RETENTION_HOURS` | 2 | 失败/中断残留媒体的最长保留时长；发送成功的媒体会立即删除 |
| `DOUYIN_BRIDGE_CARD_CLICK_RESOLVE` | false | 实验性：点开视频卡片尝试取分享链接；真机未通过，**默认关闭**，关闭时卡片只会请用户补链接 |
| `DOUYIN_BRIDGE_QUIET_HOURS` | `23:00-08:00` | 仅限制普通聊天回复，不阻止用户主动发起的转移 |
| `DOUYIN_BRIDGE_WATCHDOG_ENABLED` | true | 页面卡死自愈总开关 |
| `DOUYIN_BRIDGE_WATCHDOG_IDENTITY_THRESHOLD` | 3 | 连续多少轮「所有点开的会话都身份校验失败」才触发恢复（≥2） |
| `DOUYIN_BRIDGE_WATCHDOG_CONSOLE_RESTART` | true | 是否允许最后一档自动重启控制台 |
| `DOUYIN_BRIDGE_WATCHDOG_CONSOLE_COOLDOWN_SECONDS` | 1800 | 两次自动重启控制台的最小间隔；24 小时内最多 5 次 |
| `DTK_BASE_URL` | 空 | DTK 根地址；当前 VM 填 `http://127.0.0.1:18000`（SSH 隧道） |
| `DTK_API_KEY` | 空 | DTK v5 的 Bearer / X-API-Key |
| `DTK_TIMEOUT_SECONDS` | 30 | 单次解析/下载超时 |
| `DTK_PARSE_WAIT_SECONDS` | 20 | `?wait=` 秒数，0 表示纯轮询 |
| `BILIBILI_ENABLED` | true | B 站链接导出开关 |
| `BILIBILI_SESSDATA` | 空 | B 站登录 Cookie，1080P+/4K 需要；只存本机 |
| `BILIBILI_MAX_VIDEO_MB` | 500 | B 站 MP4 大小上限 |
| `FFMPEG_PATH` | ffmpeg | DASH 音视频合并使用的 ffmpeg |

## 11. 控制台 API

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/douyin-bridge` | 状态、绑定、最近任务、计数、DTK/投递可用性 |
| GET | `/api/douyin-bridge/jobs?limit=50` | 任务列表 |
| GET | `/api/douyin-bridge/bindings` | 绑定列表 |
| GET | `/api/douyin-bridge/dom-probe` | 只读 DOM 诊断：当前会话标记、会话标题候选（不含消息正文） |
| POST | `/api/douyin-bridge/codes` | `{wxid, wx_name}` → 一次性绑定码 |
| POST | `/api/douyin-bridge/bindings` | `{conversation_id, wxid, wx_name}` 手动绑定 |
| POST | `/api/douyin-bridge/bindings/delete` | `{binding_id}` |
| POST | `/api/douyin-bridge/run` | 唤醒轮询线程立即跑一轮并处理已入队任务（不跨线程碰 Playwright） |
| POST | `/api/douyin-bridge/calibrate` | 导出选择器相关结构（不含消息正文） |
| POST | `/api/douyin-bridge/jobs/retry` | 只允许 `failed/ready/detected` |
| POST | `/api/douyin-bridge/jobs/cancel` | 已投递/结果不明不允许取消 |

所有接口都走控制台现有的 token 鉴权；没有新增监听端口。

## 12. 安全边界

- 只监听已绑定的一对一私聊会话；群聊不支持，不扫描未绑定私信，不主动开新会话。
- 每个绑定独立打开/读取；单个会话切错只跳过该会话，不再让整个轮询失败。每轮默认只切 3 个绑定（`DOUYIN_BRIDGE_BINDINGS_PER_POLL`）。
- **共享同一个微信 UI**：桥和普通私聊回复都通过同一个微信桌面进程的 AT-SPI/xdotool 操作。两者会靠桌面租约串行，但不能真正隔离；Chrome 覆盖微信、系统“打开方式”弹窗或 STOP 文件都会同时影响桥和私聊回复。需要彻底隔离只能用独立桌面/独立微信账号，不建议为同一账号起第二个微信实例。
- STOP 是全局发送闸门：`STOP` 文件存在时，桥和普通私聊回复都不能发；删除后才恢复。
- 未绑定的会话即使发来表情/视频/卡片也只回一句绑定提示：**不点卡片、不建任务、不发微信**；绑定关系是唯一投递授权。
- 只处理绑定后的投递；未绑定只回一句绑定说明，不创建任务。
- 不绕过验证码、登录保护、风控页；检测到即停止本轮。
- 不做批量、营销、陌生人私信；默认每小时 ≤10 次成功投递。
- 文件发送前核对会话标题；文件对话框回读不到目标文件名就不点“打开”。
- 结果不明（`SEND_UNCERTAIN` / `WECHAT_UNVERIFIED`）只记录，不自动重发。
- 日志和 API 不返回微信/抖音凭据、聊天全文或媒体正文；任务只保留必要的来源摘要。
- 媒体目录按本机文件权限保护；建议 `chmod 700 state/douyin-bridge`。

## 13. DTK 部署

可直接用 [deploy/dtk](../deploy/dtk/README.md) 里的 v4 单容器脚本；它只绑回环，靠 SSH 隧道给微信虚拟机用。

### 13.0 本轮实际部署（2026-11-18）

腾讯云上**已经有**一套 DTK v5（`/home/ubuntu/douyin-pwa/dtk`，容器 `dtk-api-1` / `dtk-worker-1` / `dtk-postgres-1` / `dtk-redis-1`），所以没有新建 v4：

- 停掉原 `us-app`（`/opt/us-app`，占用 8000）并 `docker compose down`（未删数据卷）；
- DTK `.env` 改为 `DTK_BIND_HOST=0.0.0.0`、`DTK_BIND_PORT=8000`，用 `COMPOSE_ENV_FILES` 重建 api；
- 新建专用控制台账号 `bridge-admin`，创建只带 `douyin:read` 的 API Key（写进 VM/本机 `.env`，不回显）；
- 从 VM 的已登录 Chrome 导出 62 个 cookie，带 UA / `zh-CN` / `Asia/Shanghai` 导入 DTK 身份池；
- 因为公网 8000 偶发连接重置，VM 通过 SSH 隧道 `127.0.0.1:18000 -> 腾讯云 127.0.0.1:8000` 访问 DTK；隧道脚本在 VM `~/bin/dtk-tunnel.sh`，当前已常驻；
- 真实验证：`https://v.douyin.com/L4NpDJ6/` 解析成功，`watermark=false`，下载 1,454,940 字节 mp4，文件头/哈希校验通过。

注意：8000 现在是 DTK 的 HTTP API（有 API Key），不再是 `us-app`；`us-app` 的容器和网络已删除，数据卷未动。公网 HTTP 会暴露 API Key 的使用路径，建议后续加 TLS 或安全组白名单；VM 侧已优先走隧道。


### 13.1 选型

| 版本 | 资源 | 适用 |
|---|---|---|
| v4.1.2（单容器） | Docker + 单容器，无 Postgres/Redis | 腾讯云 3.6GB 内存的轻量机更合适 |
| v5.1.x（推荐功能） | Docker Compose + PostgreSQL17/TimescaleDB + Redis + Python 3.12 | 有 8GB+ 内存或独立机器时 |

本项目的虚拟机只有 3.8GB 内存且未装 Docker，不建议把 DTK 塞进微信所在的 VM。推荐单独放在腾讯云，绑定 `127.0.0.1`，由本机通过 SSH 隧道访问，或只对虚拟机 IP 放行端口。

### 13.2 v4 单容器（腾讯云）

```bash
# 在腾讯云服务器上
docker run -d --name dtk-v4 --restart unless-stopped \
  -p 127.0.0.1:8080:80 \
  evil0ctal/douyin_tiktok_download_api:V4.1.2

# 取出配置，替换 Cookie 行（从已登录的抖音浏览器复制）
docker cp dtk-v4:/app/crawlers/douyin/web/config.yaml ./douyin-web.yaml
# 编辑 douyin-web.yaml 的 Cookie
docker rm -f dtk-v4
docker run -d --name dtk-v4 --restart unless-stopped \
  -p 127.0.0.1:8080:80 \
  -v "$PWD/douyin-web.yaml:/app/crawlers/douyin/web/config.yaml" \
  evil0ctal/douyin_tiktok_download_api:V4.1.2
```

v4 没有 API Key，因此**不要**把 8080 暴露到公网；只绑回环，用 SSH 隧道。

### 13.3 v5（有资源时）

按官方 `docker/compose.yml` 起 api/worker/migrate（+ 可选 browser profile），创建管理员和 `douyin:read` API Key，再把 `DTK_BASE_URL`、`DTK_API_KEY` 填到本机 `.env`。

### 13.4 接到本机

```dotenv
DTK_BASE_URL=http://127.0.0.1:8080
DTK_API_KEY=
```

如果 DTK 在另一台机器，优先 SSH 隧道：

```bash
ssh -N -L 8080:127.0.0.1:8080 ubuntu@<dtk-host>
```

## 14. 实机验收（必须由账号本人完成）

> 2026-11-18 进展：抖音→微信已改为**收到表情/视频就自动转发**，不需要指令；微信→抖音仍需明确说“转到抖音”。扫码、校准、dry-run、抖音表情自动投递、微信表情上传抖音均已完成真机验证；视频卡片仍未由本人真机验证。

### 第 1 步：抖音扫码登录

在虚拟机桌面打开可见浏览器，进入 `https://www.douyin.com/chat`，用抖音 App 扫码。登录后保持浏览器打开。

### 第 2 步：校准选择器

```bash
curl -sS -X POST http://127.0.0.1:8765/api/douyin-bridge/calibrate \
  -H "Content-Type: application/json" -d '{}' | python3 -m json.tool
```

检查返回的 `conversation_rows`、`zone_class`、`row_classes` 是否与代码里的选择器一致。不一致时先改 `src/wechat_agent/douyin_bridge/chat.py` 的类名片段，再继续。

### 第 3 步：dry-run 链路

```dotenv
DOUYIN_BRIDGE_ENABLED=true
PLATFORM_BROWSER_ENABLED=true
DOUYIN_BRIDGE_CONVERSATION=<会话名>
DOUYIN_BRIDGE_DRY_RUN=true
```

重启 Agent，绑定微信（控制台或绑定码），在抖音私信里发表情包 + “转到微信”。验收：

- 控制台 `/api/douyin-bridge/jobs` 出现 `ready` 任务；
- `media_path` 文件存在、是图片、SHA 非空；
- 微信没有新消息。

### 第 4 步：真实投递

先把 `DOUYIN_BRIDGE_DRY_RUN=false`，用**文件传输助手**或自己的小号做一次表情包和一次视频：

- 微信数据库回读到原生图片 / `appmsg type 6` 文件；
- 任务状态 `delivered`；
- 抖音收到回执；
- 重复发送同一句“转到微信”不会二次投递；
- 点 STOP 后不再发送。

在这四步完成前，不对外说“已跑通”。

## 15. 已知失败模式

| 现象 | 原因 | 处理 |
|---|---|---|
| `CHAT_LOGIN_REQUIRED` | 抖音网页版未登录/登录过期 | 在可见浏览器重新扫码 |
| `CHAT_LIST_TIMEOUT` | 页面改版或网络慢 | 跑 calibrate，更新选择器 |
| `CHAT_CONVERSATION_NOT_FOUND` | 会话名不一致/被改名 | 用控制台显示的会话名，或改配置 |
| `STICKER_URL_MISSING` | 图片是 blob/未暴露 src | 重新转发；或后续加元素截图兜底 |
| `DTK_NOT_CONFIGURED` | 没填 DTK 地址 | 按第 13 节部署 |
| `DTK_WATERMARKED` | DTK 只返回带水印流 | 换 DTK 身份/Cookie，或换视频；不强行发 |
| `DTK_NO_MEDIA` | 链接不是视频（图集/图文/失效）且没有可下载图片 | 图集会走图集转发；其余请用户重发正确链接 |
| `DTK_DOWNLOAD_FAILED` | CDN 连接瞬断/超时，多镜像重试后仍失败 | 任务失败，不自动重试；确认网络后手动 retry |
| `DTK_NOT_VIDEO` | 下载到的是音频/HTML 而不是 mp4 | 已自动换镜像；全部失败才报错 |
| `INPUT_OCCUPIED` | 微信输入框已有草稿（可能是上次粘贴残留） | 清空输入框后重试；新版失败时会自动清掉自己粘贴的 `\ufffc` |
| `WECHAT_SEND_FAILED: atspi_error` | 微信 a11y 桥短暂无响应 | 图片粘贴已改为重试；文本/文件发送失败请清草稿后重试 |
| `WECHAT_CHAT_NOT_FOUND` | 微信里没有这个 wxid/不是好友 | 先让联系人加机器人微信并生成绑定码 |
| `FILE_DIALOG_STUCK` | 文件选择框没关闭 | 结果不明，检查微信；不要自动重发 |
| `SEND_UNCERTAIN` | 点击后回读超时 | 人工确认微信；任务保持 uncertain |
| `CHAT_IDENTITY_UNCONFIRMED` 连续多轮全失败 | 无头聊天页卡住：列表和登录态都正常，但点开会话不再切换 | 看门狗自动恢复（重载页 → 重启浏览器 → 重启控制台，见第 19 节）；也可手工 `restart-desktop.sh --start-agent` |
| `CHAT_CHALLENGE` / `CHAT_LOGIN_REQUIRED` | 抖音要求验证码或登录已过期 | 必须人工扫码/过验证；看门狗只报警（ERROR + 控制台「自愈」显示需人工处理），不会自动重试 |

## 16. 后续

1. 控制台「抖音桥」页面：绑定、任务、校准、dry-run 开关的可视化。
2. 转发卡片的点击反查（当前只从 DOM 属性和正文取 id）。
3. 微信原生视频消息（当前是 mp4 文件）。
4. 多用户绑定码自助流程与每个联系人的独立配额。
5. 媒体保留策略（按天清理 `state/douyin-bridge`）。

## 17. 本轮实机证据（2026-11-18）

在 `user@vm-host` 的 Linux 图形会话、当前登录测试微信上完成：

| 项 | 结果 |
|---|---|
| 文件选择框 → 发送 | `AtspiUI.send_file` 把文件放进输入框附件区后，补点原生「发送」按钮；数据库新增 `local_type=25769803825`（base 49 + appmsg type 6）、`<title>` 为文件名 |
| `LinuxBackend.send(file_paths=...)` | `verified=True`、`message_type=file`，回读消息 ID 非空 |
| 视频文件 | 真实 DTK 解析 `v.douyin.com/L4NpDJ6` → `watermark=false` → 下载 1.45MB mp4；微信把它收成原生 `local_type=43` 视频消息，按精确字节数回读成功 |
| 动态表情 | GIF 经文件选择框被微信收成 `appmsg type=8`（原生动态表情），按 `appattach/totallen` + `fileext` 回读；旧的“只认 type 6”会误报 `SEND_UNVERIFIED`，已修 |
| 抖音私信读取 | 真机会话「测试甲」：会话列表、图片/文本分类、指令解析、回复均通过；头像 URL 曾被误判为视频，已加 `aweme-avatar` 过滤 |
| 真实表情包 dry-run | 抖音侧发 GIF + “把这个表情包转到微信” → 任务 `ready`，2,783,603 字节 GIF 落盘，微信未发送 |
| 真实表情包投递 | 关闭 dry-run 后重试同一任务 → 微信「测试甲」收到 `appmsg type=8` 动态表情；旧代码当时还发了一张 PNG 预览，后续已去掉 |
| 自动转发 | 抖音「测试甲」只发一个 GIF（不配文字）→ 自动任务 `delivered`，4,523,099 字节，微信收到 `appmsg type=8` 动态表情，无原生图片 |
| 微信 → 抖音 | 微信「测试甲」发 GIF + “转到抖音” → 桥读取微信表情、在抖音弹窗上传；抖音页面出现本号发出的图片；最终 `SEND_MEDIA verified=True, modal_gone=True` |
| 视频卡片分类 | 真实抖音视频卡片（无文字、`rowText=五颗星`、有播放控件）已分类为 `kind=video`；视频卡片没有 aweme id 时仍会请用户补链接 |
| 批量 / 多用户 | 单测：两个绑定、两个并发表情任务分别投递到两个 wxid；群绑定被拒绝 |
| 多用户轮询稳定性 | 真机 9 个绑定：`CHAT_IDENTITY_UNCONFIRMED`（点测试丁后仍是 测试成员5）曾让整轮失败；已改为每轮 3 个、单绑定失败跳过、身份不确认重试一次。修复后日志连续三轮 9 个会话全部 `conversation_verified` |
| 最近一次失败 | 17:40 任务 `STOPPED`：当时存在全局 `STOP` 文件，桥和私聊/群聊回复都被闸门挡住；文件已删除。另两条历史失败分别是微信窗口被挡（`CHAT_NOT_VISIBLE`）和数据库回读未确认（`SEND_UNVERIFIED`） |
| 批量表情只导出一个 | 根因：列表预览对连续表情不变，第一次打开时 DOM 只挂载了部分行，后续表情被列表扫描跳过。已改为打开后 0.7 秒二次读取，并在有新消息时把该会话保持“热点”20 秒，后续轮询继续打开读取；不同表情会各自建任务 |
| 20:01 的“发送失败” | 不是桥任务：是 AI 主动群发言（同仁四级一家人）报 `SEND_UNVERIFIED`；全库搜索确认该文本没有出现在任何会话，既没发错也没发成。已关闭空闲主动发言 `PROACTIVE_IDLE_ENABLED=false` |
| 收件人审计 | 22 条历史任务逐条核对：任务 `target_wxid`、`peer_name` 与绑定完全一致，0 条错配；19 条 delivered 的微信消息 id 也全部能在目标 wxid 的数据库里找到 |
| “视频发给 lyyyy” | 不是错发：17:32「测试甲」发的 `v.douyin.com/NKmpdBDNXTw/` 投给 wxid_placeholder001；17:41「测试成员6」又发了同一链接，投给他的绑定 lyyyy。两个用户各发了一次 |
| 乱发“没链接” | 旧逻辑对每个无 aweme id 的视频卡片都回一句提示。已改为自动转发失败静默跳过，并暂时关闭抖音侧回执 `DOUYIN_BRIDGE_REPLY_ENABLED=false` |
| 群聊回复受抢锁影响 | 微信 `desktop_lease` 原为非阻塞，桥持有 UI 时群回复直接 `SEND_BUSY`。已改为最多等 12 秒；`open_chat` 标题不匹配时重新聚焦并重试一次。修复后暂无新的群回复失败 |
| 收件人硬校验 | 任务目标 wxid 与绑定不一致直接 `TARGET_MISMATCH`；同名 peer 不再猜绑定；微信同名联系人拒绝绑定；抖音回执核实会话名后才发送 |
| WeChat 窗口被 Chrome 遮挡 | 真机发现：Chrome 覆盖微信时 `open_chat` 报 `CHAT_NOT_VISIBLE`/`DESKTOP_COMMAND_FAILED`；已加“最小化抖音 Chrome 再唤起微信”的兜底，但桌面焦点仍可能被页面抢回。建议桥运行时把微信窗口保持可见，或把 Chrome 放到副屏/另一个工作区 |
| 抖音真实视频卡片 | **未做**：需要「测试甲」在抖音里转发一个视频卡片，本轮只测了链接和假消息 |
| DTK | 真实解析/下载通过；身份池已导入抖音登录态；VM 走 18000 隧道 |
| 测试 | Windows 全量 **1066 passed**（新增分类/头像回归前）；Linux 桥 + 后端专项 **136 passed** |

验证用文件与图片发送到了「文件传输助手」和「测试甲」：文件传输助手 3 个 txt + 1 张测试图 + 2 个测试视频；「测试甲」收到 1 张表情包图片 + 1 个动态表情。均可自行删除。验证脚本和临时媒体已清理，未改动正式 `.env`、未重启微信或 Agent。

这组证据覆盖了“抖音私信（表情包）→ 桥接服务 → 微信”的完整链路，以及“DTK → 视频下载 → 微信原生视频”的媒体链路；**没有**覆盖“抖音私信里转发视频卡片 → 解析 aweme id”这一步。



## 18. 2026-11-18 下午补充：图集转发、下载回退与「本地地址」说明

### 18.1 为什么 `DTK_BASE_URL` 是 `127.0.0.1:18000`

解析**不在**微信虚拟机本地跑。DTK v5 部署在腾讯云 `203.0.113.10`（容器 `dtk-api-1` / `dtk-worker-1` / `dtk-postgres-1` / `dtk-redis-1`），监听容器内 `8000`。微信 VM 上用常驻 SSH 隧道把本机 18000 转发到服务器回环 8000：

```text
VM: ~/bin/dtk-tunnel.sh
  ssh -N -i ~/.ssh/tencent_dtk ... -L 127.0.0.1:18000:127.0.0.1:8000 user@example.com
```

所以 `.env` 里的 `DTK_BASE_URL=http://127.0.0.1:18000` 只是**隧道在本机的入口**；真正的解析、数据库和抖音身份池都在腾讯云。这样 DTK 的 API Key 和 8000 端口不需要从 VM 直接访问公网。排查顺序：`curl 127.0.0.1:18000/readyz`（VM）→ `docker ps` + `docker port dtk-api-1`（服务器）→ 隧道进程是否还在。

### 18.2 图集（图文）转发

- DTK `kind=image_album` 不再报 `DTK_NO_MEDIA`，改为下载 `media.images[].url/urls` 并逐张发**微信原生图片**。
- 张数上限 `DOUYIN_BRIDGE_MAX_ALBUM_IMAGES`（默认 9）；实际张数以 DTK 返回的 `media.images` 为准（实测三条“图文”链接都只有 1 张图）。
- 全部图片下载成功后才开始发送，避免半套图；每张独立幂等键，重试不会重复发送已回读的图。
- 回执文案：「图集（N 张）发到你微信了。」

### 18.3 下载连接失败修复

- DTK 的 `media.video.urls` 现在是**多镜像候选**（此前只取第一个地址就放弃）。
- 每个地址对 `ConnectError` / 连接/读取超时 / 429 / 5xx 自动重试（`download_attempts` 默认 2）；非视频响应（audio/html）自动换下一条镜像。
- 下载用独立 httpx client，只带 UA/Accept/Referer，不再把 DTK 的 `Authorization` / `X-API-Key` 带到抖音 CDN。
- 真实失败样本 `v.douyin.com/Q2lGCYbGNmI`：原任务 16:09 因 `DTK_DOWNLOAD_FAILED: ConnectError` 失败；同一解析结果用新客户端 0.66 秒下载 5,304,658 字节；通过控制台重试后 `delivered`，微信回读 `message_0.db:86`。

### 18.4 微信图片粘贴容错

- 真实图集首次投递时，微信 a11y 桥在粘贴后几秒无响应，`entry.queryText().getText()` 抛 `GLib.GError(code=1)`；旧代码把该错误继续抛，且失败截图函数 `diagnose()` 用 `re.sub` 处理 int code 又抛 `TypeError`，把真实错误掩盖成 `WECHAT_SEND_FAILED: expected string or bytes-like object`。
- 已修：`diagnose()` 对 code 做 `str()`；`backend.send` 记录完整 traceback 且诊断失败不影响原错误；`send_image` 粘贴阶段对单次 AT-SPI 超时重试（6 秒预算），失败时清掉自己粘贴的 `\ufffc` 占位符，避免下一次 `INPUT_OCCUPIED`。

### 18.5 本轮真机结果

| 项 | 结果 |
|---|---|
| 之前失败的视频（`Q2lGCYbGNmI`，5.3MB） | 重试后 `delivered`；微信回读 `local_id=86`，原生视频消息 |
| 图集（`-ntxpUiQbQM`，1 张图） | `delivered`；微信回读 `local_id=87`，`message_type=image` |
| 图集另两条链接（sf6vsCu724I / D_IPTNHR5jk） | 同样 delivered；微信回读 local_id=90 / 89，均为 message_type=image |
| 单帧 GIF 表情（后荡后荡） | 重试后微信回读 local_id=88：local_type=47 原生表情，sticker md5/len 与本地 GIF 一致（详见 18.6） |
| 单测 | 桥相关 + AT-SPI/后端相关新增用例全部通过（dtk 19、service 18、atspi/backend 75） |

验证截图：微信「测试甲」16:49 收到 0:17 视频与图集图片（本机 `state/tmp/diag/album-delivered.png`）；控制台显示任务 `delivered`、`media_bytes=5304658` / `323560`。

### 18.6 单帧 GIF 被微信收成原生表情（local_type=47）

- 之前只按 `appmsg type=8` 回读，导致单帧 GIF 明明已经送达却被误报 `SEND_UNVERIFIED`，任务停在 uncertain。
- 真机证据：`message_0.db` `local_id=88`，`is_self=1`，`local_type=47`（原生表情），`<emoji md5>` = `37253518667ad69f8928dda3bc57d4c3`、`len=123719`，与本地 `sticker_8faf36149c527e79_wechat.gif` 的 MD5/字节数**完全一致**。
- 已修：`LinuxBackend._verify(kind="file")` 在发送 `.gif` 时也接受 `local_type=47` 且 `sticker_size == 本地文件大小`、`sticker_md5` 非空的行，`message_type` 记为 `sticker`；尺寸不一致仍然不算成功。
- 结论：静态表情转一帧 GIF 的方案真机上成立（微信侧显示为表情，不是图片/文件）；缺少的是回读识别，不是发送。

### 18.7 转发视频卡片自动取分享链接（实验性，已暂停）

- 抖音私信里直接“转发视频卡片”时，DOM 只有封面图/播放角标/时长，没有分享链接；旧实现只会回“把链接发我”。
- 现在 `DouyinChatSession.resolve_video_ref(offset, image_url, row_text)`：按封面图或行文本定位卡片 → 点击 → 等待新标签页/URL 变化/页内浮层 → 提取 `video/<id>` 或 `modal_id` → 还原会话。
- `_auto_forward_media` 只在**已绑定**会话里调用解析；未绑定会话只回绑定提示（新增单测断言 `resolve_calls == []`、无任务、无微信投递）。
- 解析出的 id 统一转成 `https://www.douyin.com/video/<id>`；随后走 DTK → 下载 → 微信，与链接消息完全同一条链路。
- 单测：卡片解析成功后建任务并投递、解析失败回链接请求、`DOUYIN_BRIDGE_CARD_CLICK_RESOLVE=false` 不点击、未绑定不点击。
- **真机结果：未通过，暂停。** 实测卡片行 DOM 没有任何 aweme id / 分享链接；点击后打开的是全屏播放浮层（`commonModalFullScreen...`），其 DOM 里也没有本视频 id；从页面 HTML 里能扫到的是别的运营卡片 id（曾误取到 7516480886684568891，解析成无关图集），分享面板方案也未取到复制链接。为避免误发，已把 `DOUYIN_BRIDGE_CARD_CLICK_RESOLVE` 默认关闭并在 VM `.env` 置 `false`；后续如再实现，优先从播放接口响应或分享面板复制链接拿引用，拿不到就请用户补链接。

### 18.8 只读取「开启之后」的信息（持久 baseline，2026-11-18 晚）

- 需求：功能开启前已经存在的私信历史不读、不转发；开启后新到的消息才处理。
- 实现：新增 `douyin_bridge_baselines` 表（`account_id + conversation_id` 唯一）。每个会话**第一次**被轮询到时，把当时渲染出来的全部消息记为已读，并写入 `baselined_at` 作为开启截止时间；这个截止时间是持久化的，桥重启不会重新 baseline。
- 效果：
  - 开启前的历史消息：只标记已读，不建任务、不点卡片、不发微信；
  - 开启后（包括桥重启期间）新到的消息：只要不在 seen 表里，重启后仍会被正常处理；
  - 新绑定的会话：在它第一次被轮询时同样建立 baseline，之前的历史不补发。
- 测试：`test_existing_history_at_start_is_not_forwarded`、`test_restart_keeps_history_cutoff_and_processes_only_later_messages`（重启后旧消息跳过、截止后新消息照常投递）。
- 本轮部署后第一次轮询已为当前 9 个绑定会话写入 baseline；旧的「五颗星」卡片、历史表情/视频不会再次被处理。

### 18.9 回执发错会话的修复（2026-11-18 晚）

- 现象：一条媒体发送成功、抖音回执「表情包发到你微信了。」之后，同一条抖音会话里又出现一条「发到微信失败：…」，看起来像同一条消息先成功又失败。
- 原因：桥支持多绑定后，轮询按绑定逐个打开抖音会话；任务回执在 `_drain_jobs()` 里处理，此时当前打开的往往是**最后一个**会话（例如「测试甲」）。`_reply()` 原来直接往“当前会话”发文字，于是别的会话（例如「测试成员7」）的失败回执被发到了「测试甲」里，形成“成功后面跟一条失败”。
- 修复：`process_job()` 的每个回执都带 `conversation=job.peer_name`；`_reply(..., conversation=...)` 在发送前先 `open_conversation(job.peer_name)`，把回执送回任务自己的抖音会话。单测 `test_job_receipt_is_sent_to_the_jobs_own_douyin_conversation` 覆盖。
- 另一个独立问题（不是回执错乱）：失败那条本身是真的发送失败——绑定的微信联系人（如「测试成员7」）不在微信最近会话列表里，全局搜索兜底也没找到唯一联系人，`open_chat` 报 `CHAT_NOT_VISIBLE`。要支持这类“很久没聊”的联系人，需要继续完善微信全局搜索路径；当前只给该会话回失败回执，不会误发到别的会话。

### 18.10 媒体落盘清理（2026-11-18 晚）

- 需求：视频/表情包发送完成后不要长期占本地磁盘。
- 发送成功（`delivered`）后立即删除该任务的本地文件：原图/原视频、`_wechat.gif`、`_wechat.png` 预览和 `.part` 临时文件。若同一文件仍被未完成任务引用则保留，避免打断重试。
- 兜底清理：每 30 分钟扫一次媒体目录，删除超过 `DOUYIN_BRIDGE_MEDIA_RETENTION_HOURS`（默认 2 小时）的残留文件（失败/不确定任务的文件、调试截图、临时文件）；桥启动后第一轮也会先扫一次。
- 本轮已把历史遗留的 23 个文件（约 28MB）一次性清空，目录现在 0 个文件；之后只会在发送进行中短暂落盘。
- 单测：`test_delivered_media_files_are_deleted_immediately`、`test_expired_media_is_swept_but_recent_media_kept`、`test_failed_job_media_is_kept_for_retention`。
- 注意：失败/不确定任务的文件保留到保留时长（便于排查/人工重试）；要更激进可把保留时长调到 1 小时或直接改成失败也删除。

### 18.11 微信全局搜索 / 侧栏滚动补稳（2026-11-18 晚）

- 现象：很久没聊过的绑定联系人（如「测试成员7」）发送时 `open_chat` 报 `CHAT_NOT_VISIBLE`，回执“发到微信失败”。
- 原因一：微信 Chats 侧栏只渲染约 10 个会话，老联系人不在可见区；
  原因二：全局搜索兜底只等 **1.5 秒**，搜索结果在繁忙机器上常超过这个时间才出现，于是判成“搜索未找到唯一联系人”。
- 修复：
  1. `open_chat` 先扫侧栏精确项，再走全局搜索（等待放宽到 4 秒，`联系人` 分组下精确匹配，仍拒绝同名歧义）；
  2. 搜索失败后兜底**滚动侧栏**：先滚到顶部，再按页向下滚（鼠标滚轮事件），每步重新扫精确名称，列表不再变化即停止；
  3. 侧栏精确扫描抽成 `_sidebar_chat_candidates()`，搜索/滚动/侧栏三路都只接受精确同名项，点击后仍回读会话标题确认身份。
- 真机验证：把之前失败的「测试成员7」任务重试 → `delivered`；微信 测试成员7 会话回读 `local_id=6`、原生表情 `sticker_size=74543`；回执「表情包发到你微信了。」现在出现在**测试成员7**的抖音会话里（不再串到测试甲）；发送完成后 3 个本地媒体文件被立即删除。
- 单测：新增 `test_open_chat_scrolls_sidebar_when_global_search_finds_nothing`、`test_scroll_sidebar_for_chat_finds_a_chat_below_the_fold`；AT-SPI/后端相关 **105 passed**。

### 18.12 会话身份校验修复：不再把消息读到别的会话（2026-11-18 晚）

- 事故：视频 `v.douyin.com/NKmpdBDNXTw` 在同一时间被建了两个任务，一个按「测试甲」投递成功，另一个被记到「测试成员6」名下并发给了它绑定的微信联系人 lyyyy；另有一条表情包也重复建任务后失败。
- 根因：`open_conversation` 的会话身份校验一直形同虚设——`_JS_ACTIVE` 按 `active/selected` 类名找当前会话，但抖音实际用 `conversationConversationItemcurConversation` 标记当前会话，所以读到的永远是空字符串；旧代码 `if active and active != wanted` 在空串时直接跳过校验。一旦点击没有真正切到目标会话（测试成员6 行可能未渲染/坐标滚动），编辑器又因为上一个会话还开着而存在，就会把**上一个会话（测试甲）的消息**读进来、按 测试成员6 的绑定建任务 → 发到 lyyyy。
- 修复：
  1. 会话列表/当前会话选择器改为 `...Itemwrapper`，当前会话用 `curConversation` 判定，标题取 `...Itemtitle`；
  2. `open_conversation` 点击后最多重试 3 个落点、每个等待 2.5 秒，**必须**读到当前会话标题等于目标才继续；读到空/不一致直接 `CHAT_IDENTITY_UNCONFIRMED`，不读任何消息；
  3. `read_messages` / `send_text` / `send_media` 前都调用 `_require_active`，身份不符就 fail closed（不读、不发）；
  4. 成功时记录 `douyin_bridge_conversation_verified name=…`，便于审计；新增 `GET /api/douyin-bridge/dom-probe` 只读诊断。
- 验证（部署后 30 秒）：28 次会话打开全部 `conversation_verified`，`CHAT_IDENTITY_UNCONFIRMED` 0 次、`poll_failed` 0 次；测试成员6 会话快照只剩它自己的系统消息，没有再读到测试甲的内容；修复后没有新建任何误归属任务。
- 说明：已经误发到 lyyyy 的那条视频无法撤回，需要手动删除；导致它的重复任务不会再重放。

## 19. 2026-11-24：页面卡死看门狗（自动恢复）

### 19.1 事故与结论

- 现象：2026-11-23 16:08 起，`desktop.log` 每轮都刷 `douyin_bridge_binding_skipped ... CHAT_IDENTITY_UNCONFIRMED`，到 11-24 14:32 累计 3000+ 条。只有当时已打开的「测试成员8」能通过校验，其余 13 个绑定全部失败，桥 22 小时没有转发任何抖音消息（任务计数停在 delivered 96）。
- 关键证据：`/api/douyin-bridge` 的 `chat.connected=true`、`url=https://www.douyin.com/chat`、`calibrated=true`；`/api/douyin-bridge/dom-probe` 能读到会话列表与输入框，页面没有验证码/登录页；日志里没有 captcha/验证/risk 字样。
- 结论：不是验证码/封号式风控，而是**长时间运行的无头聊天页卡住**（点击会话不再切换，`curConversation` 标记丢失）。身份校验继续 fail closed，于是只报警、不读数。
- 恢复：手工重启控制台进程（重新拉起 Playwright 浏览器、复用同一 profile）后，14 个绑定全部 `conversation_verified`、`CHAT_IDENTITY_UNCONFIRMED` 归零、无积压补投。
- 操作注意：**只重启 Agent（`/api/stop` → `/api/start`）不行**。Playwright 同步对象绑定创建它的线程，新 Agent 线程会报 `error: Cannot switch to a different thread`，页面状态也不会恢复。桥故障统一重启控制台进程。

### 19.2 看门狗规则

每轮 `poll_once()` 结束后统计「本轮真正点开过的绑定」：

1. 全部失败且错误属于 `CHAT_IDENTITY_UNCONFIRMED` / `CHAT_LIST_TIMEOUT` / `CHAT_BROWSER_THREAD_CONFLICT`（或文本 `Cannot switch to a different thread`）→ 连续失败轮数 +1；任意一个绑定成功（或出现非这类错误）→ 归零并清空恢复档位。
2. 连续失败达到 `DOUYIN_BRIDGE_WATCHDOG_IDENTITY_THRESHOLD`（默认 3）轮，按档位恢复一次：
   - 一档：重载 `/chat`，等会话列表重新渲染；
   - 二档：关掉整个 persistent context，用同一 profile 重新拉起（不重新扫码），重建聊天标签页；
   - 三档：调用 `scripts/restart-desktop.sh --start-agent --keep-log --port <desktop_port>` 重启控制台（`start_new_session` 脱离父进程，不会随被杀的旧控制台一起退出）。
3. 三档被冷却/次数限制/脚本缺失拦下时退回二档反复尝试，不会干等。
4. 出现 `CHAT_CHALLENGE` / `CHAT_LOGIN_REQUIRED` 时立刻置为「需人工处理」并打 ERROR，后续不再自动恢复；任一绑定恢复正常后自动解除。
5. 冷却：三档两次之间至少 `DOUYIN_BRIDGE_WATCHDOG_CONSOLE_COOLDOWN_SECONDS`（默认 1800 秒），24 小时最多 5 次；时间戳落在状态目录的 `douyin-bridge-watchdog.json`，重启进程也不会忘记。三档脚本输出（含前置检查失败原因）写入 `douyin-bridge-watchdog-restart.log`。

### 19.3 状态与日志

- `GET /api/douyin-bridge` 新增 `watchdog`：`enabled`、`threshold`、`streak`、`level`（0 未恢复 / 1 已重载页面 / 2 已重启浏览器）、`blocked`、`last_failure_at`、`last_recovery`。
- 控制台「抖音桥 → 运行状态」新增「自愈」一行，显示「连续失败 x/y」「已重载页面」「已重启浏览器」「需人工处理：…」。
- 日志关键字：`douyin_bridge_watchdog_identity_failure`、`douyin_bridge_watchdog_recovered`、`douyin_bridge_watchdog_recovery_failed`、`douyin_bridge_watchdog_operator_required`、`douyin_bridge_watchdog_cleared`。
- `restart-desktop.sh --keep-log` 只追加一行重启分隔线、不截断旧日志（看门狗重启控制台时使用）。

### 19.4 边界

- 看门狗只处理「页面卡住」这一类，不替人过验证码、不处理登录过期、不改抖音侧限流。
- 只重启 Agent 造成的线程冲突（`Cannot switch to a different thread`）也会被识别：前两档修不了，看门狗会自动走到第三档重启控制台。
- 若抖音前端真改了选择器，它会按三档试一遍并停在二档继续重试，同时留下明显 ERROR，需要人工更新选择器；不会再静默失败一整天。
- 本轮代码与文档已改；4 个看门狗单测与全量 pytest（1097 passed）已在本机通过。真实的故障注入（人为制造连续身份失败）没有在真实账号上做，三档自动恢复的触发路径等下一次真实卡死或由本人手工验证。
