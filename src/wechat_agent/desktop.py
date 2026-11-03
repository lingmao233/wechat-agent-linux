from __future__ import annotations

import json
import logging
import mimetypes
import re
import threading
import time
import webbrowser
from collections import deque
from dataclasses import asdict
from datetime import datetime, timezone
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import StringIO
from pathlib import Path
from typing import Callable
from urllib.parse import parse_qs, unquote, urlencode, urlparse

from dotenv import dotenv_values
from dotenv.parser import parse_stream

from .agent import WeChatAgent
from .config import Settings
from .platforms.browser import BrowserPlatformAdapter
from .platforms.dry_run import DryRunPlatformAdapter
from .platforms.service import PlatformService
from .platform_automation import PlatformAutomation
from .platform_identity import PlatformIdentityService
from .platform_memory import PlatformMemoryService
from .storage import Storage
from .console_memory import MemoryConsole, config_fields
from .persona_designer import generate_persona_design
from .persona_upload import list_personas, upload_persona
from .voice_studio import VoiceProfileError, VoiceProfiles
from .web import STATIC_DIR, WebConsole
from .web.settings_groups import mark_schema


CONFIG_SCHEMA: list[dict[str, object]] = [
    # key, env, label, category, type, secret, description
    {"key": "wechat_window_class", "env": "WECHAT_WINDOW_CLASS", "label": "微信窗口类名", "category": "微信", "type": "text", "description": "通常保持 mmui::MainWindow。"},
    {"key": "wechat_exe", "env": "WECHAT_EXE", "label": "微信程序路径", "category": "微信", "type": "text", "description": "可留空，让程序自动寻找已运行的微信。"},
    {"key": "wechat_group_names", "env": "WECHAT_GROUP_NAMES", "label": "回复群聊（多个）", "category": "微信", "type": "text", "description": "逗号分隔多个群名或群 wxid；程序会同时监听这些群。"},
    {"key": "wechat_private_reply_enabled", "env": "WECHAT_PRIVATE_REPLY_ENABLED", "label": "开放私聊回复", "category": "微信", "type": "checkbox", "description": "只接收并回复白名单联系人的新消息；默认不主动发起私聊。"},
    {"key": "wechat_private_chat_names", "env": "WECHAT_PRIVATE_CHAT_NAMES", "label": "私聊白名单", "category": "微信", "type": "text", "description": "逗号分隔稳定 wxid 或昵称；名单为空时关闭全部私聊服务。"},
    {"key": "wechat_private_proactive_enabled", "env": "WECHAT_PRIVATE_PROACTIVE_ENABLED", "label": "允许私聊欢迎语", "category": "微信", "type": "checkbox", "description": "默认关闭。开启后，新加入白名单的联系人会收到一次欢迎语；随机和定时主动私聊始终关闭。"},
    {"key": "wechat_group_name", "env": "WECHAT_GROUP_NAME", "label": "旧版单群配置", "category": "微信", "type": "text", "description": "兼容旧 .env；新配置请使用上面的多个群设置。", "hidden": True},
    {"key": "wechat_ai_aliases", "env": "WECHAT_AI_ALIASES", "label": "怎么称呼机器人", "category": "微信", "type": "text", "description": "逗号分隔；消息以这些名称开头或明确 @ 机器人时才回复。"},
    {"key": "wechat_keywords", "env": "WECHAT_KEYWORDS", "label": "触发词", "category": "微信", "type": "text", "description": "逗号分隔；消息里出现这些词时进入回复。"},
    {"key": "wechat_self_name", "env": "WECHAT_SELF_NAME", "label": "机器人微信昵称", "category": "微信", "type": "text", "description": "用于识别 @ 机器人；不知道时可留空。"},
    {"key": "wechat_allowed_members", "env": "WECHAT_ALLOWED_MEMBERS", "label": "群成员白名单 ID", "category": "微信", "type": "text", "description": "逗号分隔，只填微信唯一 member_id/wxid；留空表示不额外限制成员。"},
    {"key": "wechat_allow_send", "env": "WECHAT_ALLOW_SEND", "label": "允许发出回复", "category": "微信", "type": "checkbox", "description": "关闭时只读取和记录，不会向微信发消息。"},
    {"key": "wechat_proactive_reply", "env": "WECHAT_PROACTIVE_REPLY", "label": "随机主动接话", "category": "微信", "type": "checkbox", "description": "群友正常聊天时也有概率参与，可与计划参与同时开启。默认关闭。"},
    {"key": "wechat_proactive_min_interval_seconds", "env": "WECHAT_PROACTIVE_MIN_INTERVAL_SECONDS", "label": "主动发言最短间隔（秒）", "category": "微信", "type": "number", "description": "两次主动发言之间至少间隔多久；默认 300 秒。"},
    {"key": "wechat_proactive_max_per_hour", "env": "WECHAT_PROACTIVE_MAX_PER_HOUR", "label": "主动发言每小时上限", "category": "微信", "type": "number", "description": "一小时内最多主动插话几次；默认 6 次。"},
    {"key": "wechat_proactive_probability", "env": "WECHAT_PROACTIVE_PROBABILITY", "label": "主动发言概率", "category": "微信", "type": "number", "description": "每条未点名他人的普通聊天获得接话机会的概率；模型仍可选择不回复。"},
    {"key": "wechat_poll_seconds", "env": "WECHAT_POLL_SECONDS", "label": "检查新消息间隔（秒）", "category": "微信", "type": "number", "description": "两次读取微信新消息之间的间隔。"},
    {"key": "wechat_reply_cooldown_seconds", "env": "WECHAT_REPLY_COOLDOWN_SECONDS", "label": "两次回复最短间隔（秒）", "category": "微信", "type": "number", "description": "限制成功回复之间的最短时间；0 表示不额外限制。"},
    {"key": "wechat_max_replies_per_hour", "env": "WECHAT_MAX_REPLIES_PER_HOUR", "label": "每小时最多回复数", "category": "微信", "type": "number", "description": "限制一小时内成功回复的数量；0 表示不设上限。"},
    {"key": "wechat_stop_file", "env": "WECHAT_STOP_FILE", "label": "紧急停止文件", "category": "微信", "type": "text", "description": "存在此文件时会立即关闭自动回复。"},
    {"key": "wechat_processing_lease_seconds", "env": "WECHAT_PROCESSING_LEASE_SECONDS", "label": "处理租约（秒）", "category": "微信", "type": "number", "description": "防止同一问题被多个轮询线程重复处理。"},
    {"key": "wechat_send_retry_count", "env": "WECHAT_SEND_RETRY_COUNT", "label": "发送失败重试次数", "category": "微信", "type": "number", "description": "原生文字/图片发送未确认时的有界重试次数。"},
    {"key": "wechat_send_verify_seconds", "env": "WECHAT_SEND_VERIFY_SECONDS", "label": "发送回读等待（秒）", "category": "微信", "type": "number", "description": "旧 UIA 路径等待本地数据库回读确认的时间。"},
    {"key": "wechat_skip_existing_on_start", "env": "WECHAT_SKIP_EXISTING_ON_START", "label": "启动跳过旧消息", "category": "微信", "type": "checkbox", "description": "避免启动时重复处理已显示的消息。"},
    {"key": "wechat_context_messages", "env": "WECHAT_CONTEXT_MESSAGES", "label": "上下文消息数", "category": "微信", "type": "number", "description": "传给模型的同群最近消息数量。"},
    {"key": "wechat_context_max_chars", "env": "WECHAT_CONTEXT_MAX_CHARS", "label": "上下文最大字符数", "category": "微信", "type": "number", "description": "限制上下文长度，避免请求过大。"},
    {"key": "wechat_image_recognition_enabled", "env": "WECHAT_IMAGE_RECOGNITION_ENABLED", "label": "识别群聊图片", "category": "微信", "type": "checkbox", "description": "读取本地微信图片附件并交给支持视觉输入的模型理解。"},
    {"key": "wechat_image_max_count", "env": "WECHAT_IMAGE_MAX_COUNT", "label": "单次最多图片数", "category": "微信", "type": "number", "description": "限制一次模型请求附带的图片数量。"},
    {"key": "wechat_image_max_bytes", "env": "WECHAT_IMAGE_MAX_BYTES", "label": "单张图片大小上限", "category": "微信", "type": "number", "description": "超过上限的本地附件不会上传给模型。"},
    {"key": "wechat_image_context_turns", "env": "WECHAT_IMAGE_CONTEXT_TURNS", "label": "图片参与上下文的消息数", "category": "微信", "type": "number", "description": "一张图只在之后这么多条消息内参与回答；更早的图要等群友重新提到才会再带上。"},
    {"key": "wechat_image_reference_window", "env": "WECHAT_IMAGE_REFERENCE_WINDOW", "label": "重新提到图片的追溯范围", "category": "微信", "type": "number", "description": "群友说“上面那张图”时，最多往回找多少条消息里的图片。"},
    {"key": "wechat_media_dir", "env": "WECHAT_MEDIA_DIR", "label": "图片临时目录", "category": "微信", "type": "text", "description": "解密后的图片临时保存目录。"},
    {"key": "image_output_dir", "env": "IMAGE_OUTPUT_DIR", "label": "生成图片目录", "category": "微信", "type": "text", "description": "生图模型生成的文件保存目录。"},
    {"key": "group_style_enabled", "env": "GROUP_STYLE_ENABLED", "label": "自动拟合群聊风格", "category": "人设", "type": "checkbox", "description": "按当前群的普通消息统计表达习惯，作为动态语气参考。"},
    {"key": "group_style_min_messages", "env": "GROUP_STYLE_MIN_MESSAGES", "label": "开始拟合所需消息数", "category": "人设", "type": "number", "description": "达到这个数量后才生成第一版群聊风格画像。"},
    {"key": "group_style_refresh_messages", "env": "GROUP_STYLE_REFRESH_MESSAGES", "label": "画像刷新间隔消息数", "category": "人设", "type": "number", "description": "新增多少条普通消息后重新计算一次画像。"},
    {"key": "group_style_sample_messages", "env": "GROUP_STYLE_SAMPLE_MESSAGES", "label": "画像指纹保留数", "category": "人设", "type": "number", "description": "保留多少条事件指纹用于防止重放计数；不会保存群聊原文。"},
    {"key": "group_style_max_chars", "env": "GROUP_STYLE_MAX_CHARS", "label": "群聊画像最大字符数", "category": "人设", "type": "number", "description": "限制动态风格摘要注入模型的长度。"},
    {"key": "desktop_host", "env": "DESKTOP_HOST", "label": "UI 绑定地址", "category": "高级", "type": "text", "description": "本机使用 127.0.0.1；远程访问必须配令牌和反向代理。"},
    {"key": "desktop_port", "env": "DESKTOP_PORT", "label": "UI 端口", "category": "高级", "type": "number", "description": "默认 8765；修改后需重启 UI 进程。"},
    {"key": "desktop_token", "env": "DESKTOP_TOKEN", "label": "UI 访问令牌", "category": "高级", "type": "password", "secret": True, "description": "绑定到非本机地址时必须设置。留空保持原值。"},
    {"key": "platform_browser_enabled", "env": "PLATFORM_BROWSER_ENABLED", "label": "启用抖音/小红书浏览器", "category": "平台", "type": "checkbox", "description": "启用后会打开可见浏览器并保存登录态。"},
    {"key": "platform_browser_data_dir", "env": "PLATFORM_BROWSER_DATA_DIR", "label": "浏览器数据目录", "category": "平台", "type": "text", "description": "保存抖音和小红书扫码登录态。"},
    {"key": "platform_browser_executable", "env": "PLATFORM_BROWSER_EXECUTABLE", "label": "浏览器程序路径", "category": "平台", "type": "text", "description": "留空自动寻找 Chrome 或 Edge。"},
    {"key": "platform_browser_headless", "env": "PLATFORM_BROWSER_HEADLESS", "label": "无头浏览器", "category": "平台", "type": "checkbox", "description": "需要扫码登录时必须关闭。"},
    {"key": "douyin_account_id", "env": "DOUYIN_ACCOUNT_ID", "label": "抖音账号 ID", "category": "平台", "type": "text", "description": "本地审计标识，不是密码；登录态由浏览器保存。"},
    {"key": "xiaohongshu_account_id", "env": "XIAOHONGSHU_ACCOUNT_ID", "label": "小红书账号 ID", "category": "平台", "type": "text", "description": "本地审计标识，不是密码；登录态由浏览器保存。"},
    {"key": "douyin_mention_allowlist", "env": "DOUYIN_MENTION_ALLOWLIST", "label": "抖音 @ 白名单 ID", "category": "平台", "type": "text", "description": "逗号分隔，只填平台唯一用户 ID。"},
    {"key": "xiaohongshu_mention_allowlist", "env": "XIAOHONGSHU_MENTION_ALLOWLIST", "label": "小红书 @ 白名单 ID", "category": "平台", "type": "text", "description": "逗号分隔，只填平台唯一用户 ID。"},
    {"key": "platform_browser_scroll_steps", "env": "PLATFORM_BROWSER_SCROLL_STEPS", "label": "滚动步数", "category": "平台", "type": "number", "description": "每次读取平台内容的滚动次数。"},
    {"key": "platform_browser_page_timeout_ms", "env": "PLATFORM_BROWSER_PAGE_TIMEOUT_MS", "label": "页面超时（毫秒）", "category": "平台", "type": "number", "description": "平台页面加载和查找控件的超时时间。"},
    {"key": "platform_write_enabled", "env": "PLATFORM_WRITE_ENABLED", "label": "允许平台评论/回复", "category": "平台", "type": "checkbox", "description": "开启后仍需在平台动作台账中二次确认。"},
    {"key": "platform_action_delay_seconds", "env": "PLATFORM_ACTION_DELAY_SECONDS", "label": "平台动作间隔（秒）", "category": "平台", "type": "number", "description": "连续评论或回复之间的最小间隔。"},
    {"key": "platform_max_actions_per_hour", "env": "PLATFORM_MAX_ACTIONS_PER_HOUR", "label": "每小时平台动作上限", "category": "平台", "type": "number", "description": "限制评论和回复的发送频率。"},
    {"key": "platform_intent_enabled", "env": "PLATFORM_INTENT_ENABLED", "label": "按群聊意图补平台搜索", "category": "平台", "type": "checkbox", "description": "群友说想看/攻略/推荐/好吃等意图时，额外浏览已启用的平台并并入回答上下文。"},
    {"key": "platform_auto_comment_after_reply", "env": "PLATFORM_AUTO_COMMENT_AFTER_REPLY", "label": "群回复后自动平台评论", "category": "平台", "type": "checkbox", "description": "默认关闭。开启后仍需「允许平台评论/回复」并受每小时频率限制；dry-run 计划不会被自动升级。"},
    {"key": "platform_comment_max_chars", "env": "PLATFORM_COMMENT_MAX_CHARS", "label": "平台评论最大字符数", "category": "平台", "type": "number", "description": "自动生成的平台评论上限，默认 200。"},
    {"key": "platform_memory_enabled", "env": "PLATFORM_MEMORY_ENABLED", "label": "平台浏览/评论进入共享记忆", "category": "平台", "type": "checkbox", "description": "把平台浏览、读到的评论和计划/已发评论写入平台记忆，回复时跨群读取。"},
    {"key": "platform_proactive_enabled", "env": "PLATFORM_PROACTIVE_ENABLED", "label": "启用主动浏览", "category": "平台", "type": "checkbox", "description": "后台按间隔浏览公开内容并生成评论计划；默认仍是 dry-run。"},
    {"key": "platform_proactive_interval_minutes", "env": "PLATFORM_PROACTIVE_INTERVAL_MINUTES", "label": "主动浏览间隔（分钟）", "category": "平台", "type": "number", "description": "两次主动浏览之间的间隔，最小 30 分钟。"},
    {"key": "platform_proactive_keywords", "env": "PLATFORM_PROACTIVE_KEYWORDS", "label": "主动浏览关键词", "category": "平台", "type": "text", "description": "逗号分隔；留空时从成员兴趣记忆取词。"},
    {"key": "platform_quiet_hours", "env": "PLATFORM_QUIET_HOURS", "label": "平台静默时段", "category": "平台", "type": "text", "description": "例如 23:00-08:00；该时段内不自动执行主动浏览。"},
    {"key": "web_search_enabled", "env": "WEB_SEARCH_ENABLED", "label": "启用联网搜索", "category": "联网", "type": "checkbox", "description": "群友明确提问时才进入搜索流程。"},
    {"key": "web_search_provider", "env": "WEB_SEARCH_PROVIDER", "label": "搜索服务", "category": "联网", "type": "select", "options": ["none", "deepseek-search-api", "brave", "tavily", "searxng"], "description": "可使用 DSH 的 deepseek-search-api 中转。"},
    {"key": "web_search_api_key", "env": "WEB_SEARCH_API_KEY", "label": "搜索 API Key", "category": "联网", "type": "password", "secret": True, "description": "留空保持原值；DSH 中转通常填 lan。"},
    {"key": "web_search_base_url", "env": "WEB_SEARCH_BASE_URL", "label": "搜索服务地址", "category": "联网", "type": "text", "description": "例如 http://192.168.1.50:8787/v1/tools。"},
    {"key": "web_search_max_results", "env": "WEB_SEARCH_MAX_RESULTS", "label": "搜索结果数", "category": "联网", "type": "number", "description": "每次搜索返回的最大结果数。"},
    {"key": "web_fetch_enabled", "env": "WEB_FETCH_ENABLED", "label": "启用网页抓取", "category": "联网", "type": "checkbox", "description": "抓取消息中的公开 HTTP(S) 页面正文。"},
    {"key": "web_fetch_allowed_domains", "env": "WEB_FETCH_ALLOWED_DOMAINS", "label": "抓取域名白名单", "category": "联网", "type": "text", "description": "留空允许公开网站；填写后只抓这些域名。"},
    {"key": "web_fetch_max_chars", "env": "WEB_FETCH_MAX_CHARS", "label": "网页最大字符数", "category": "联网", "type": "number", "description": "单页正文进入模型上下文的上限。"},
    {"key": "web_timeout_seconds", "env": "WEB_TIMEOUT_SECONDS", "label": "联网超时（秒）", "category": "联网", "type": "number", "description": "搜索和抓取的网络超时时间。"},
    {"key": "llm_base_url", "env": "LLM_BASE_URL", "label": "文字模型地址", "category": "模型", "type": "text", "description": "OpenAI 兼容的 /v1 地址。"},
    {"key": "media_http_proxy_url", "env": "MEDIA_HTTP_PROXY_URL", "label": "生图与语音代理地址", "category": "模型", "type": "text", "description": "留空时直接连接。虚拟机可填写宿主机的 HTTP 代理地址。"},
    {"key": "llm_api_key", "env": "LLM_API_KEY", "label": "文字模型 Key", "category": "模型", "type": "password", "secret": True, "description": "留空保持原值。"},
    {"key": "llm_model", "env": "LLM_MODEL", "label": "文字模型", "category": "模型", "type": "text", "description": "例如 deepseek-chat。"},
    {"key": "llm_timeout_seconds", "env": "LLM_TIMEOUT_SECONDS", "label": "模型超时（秒）", "category": "模型", "type": "number", "description": "文字模型请求超时时间。"},
    {"key": "llm_max_output_tokens", "env": "LLM_MAX_OUTPUT_TOKENS", "label": "最大输出 token", "category": "模型", "type": "number", "description": "限制文字回答长度。"},
    {"key": "llm_temperature", "env": "LLM_TEMPERATURE", "label": "回答随机性（Temperature）", "category": "模型", "type": "number", "description": "越低越稳定，越高越有变化；通常填 0 到 2。"},
    {"key": "llm_top_p", "env": "LLM_TOP_P", "label": "采样范围（Top-P）", "category": "模型", "type": "number", "description": "限制候选词范围；1 表示使用完整范围。"},
    {"key": "llm_frequency_penalty", "env": "LLM_FREQUENCY_PENALTY", "label": "重复词惩罚", "category": "模型", "type": "number", "description": "提高后会减少重复用词；0 表示不额外惩罚。"},
    {"key": "llm_presence_penalty", "env": "LLM_PRESENCE_PENALTY", "label": "新话题倾向", "category": "模型", "type": "number", "description": "提高后更倾向引入新词；0 表示不额外调整。"},
    {"key": "embedding_enabled", "env": "EMBEDDING_ENABLED", "label": "启用 Embedding", "category": "记忆", "type": "checkbox", "description": "关闭后使用本地词法召回，不调用 Embedding 服务。"},
    {"key": "embed_base", "env": "EMBED_BASE", "label": "Embedding 地址", "category": "记忆", "type": "text", "description": "支持 Ark 多模态完整地址或标准 OpenAI /embeddings 根地址。"},
    {"key": "embed_model", "env": "EMBED_MODEL", "label": "Embedding 模型", "category": "记忆", "type": "text", "description": "例如 doubao-embedding-vision-251215。"},
    {"key": "embed_key", "env": "EMBED_KEY", "label": "Embedding Key", "category": "记忆", "type": "password", "secret": True, "description": "只保存在运行机器 .env。"},
    {"key": "embed_dimension", "env": "EMBED_DIMENSION", "label": "Embedding 维度", "category": "记忆", "type": "number", "description": "0 表示接受服务端维度；当前 Ark 模型返回 2048。"},
    {"key": "persona_enabled", "env": "PERSONA_ENABLED", "label": "启用人设", "category": "人设", "type": "checkbox", "description": "只改变语气，不改变事实、安全规则和群成员隔离。"},
    {"key": "persona_name", "env": "PERSONA_NAME", "label": "人设名称", "category": "人设", "type": "text", "description": "例如：大肥鱼，或你自己上传的角色名。"},
    {"key": "persona_file", "env": "PERSONA_FILE", "label": "人设文件", "category": "人设", "type": "text", "description": "Markdown 文件路径；同目录 references/ 下的 lines.md、lore.md 会自动读取。"},
    {"key": "persona_source_url", "env": "PERSONA_SOURCE_URL", "label": "人设来源", "category": "人设", "type": "text", "description": "仅用于记录来源，不会在运行时联网下载。"},
    {"key": "persona_max_chars", "env": "PERSONA_MAX_CHARS", "label": "人设最大字符数", "category": "人设", "type": "number", "description": "限制注入模型的人设和参考资料长度。"},
    {"key": "persona_style_filter", "env": "PERSONA_STYLE_FILTER", "label": "人设输出检查", "category": "人设", "type": "checkbox", "description": "拦截自称 AI、吐泡泡、过激脏话，并限制表情数量。"},
    {"key": "persona_max_emoji", "env": "PERSONA_MAX_EMOJI", "label": "每条最多表情数", "category": "人设", "type": "number", "description": "默认 1；设为 0 可完全不用表情。"},
    {"key": "training_export_enabled", "env": "TRAINING_EXPORT_ENABLED", "label": "每晚导出训练数据", "category": "训练", "type": "checkbox", "description": "到点后把当天本地群聊导出到 data/；只导出，不自动训练权重。"},
    {"key": "training_export_dir", "env": "TRAINING_EXPORT_DIR", "label": "每日导出目录", "category": "训练", "type": "text", "description": "每天会在此目录下新建 YYYY-MM-DD 子目录；默认 data/training/daily。"},
    {"key": "training_export_hour", "env": "TRAINING_EXPORT_HOUR", "label": "每日导出小时", "category": "训练", "type": "number", "description": "本地时间 0~23；默认 23。"},
    {"key": "training_export_minute", "env": "TRAINING_EXPORT_MINUTE", "label": "每日导出分钟", "category": "训练", "type": "number", "description": "本地时间 0~59；默认 30。"},
    {"key": "training_export_context_messages", "env": "TRAINING_EXPORT_CONTEXT_MESSAGES", "label": "导出上下文条数", "category": "训练", "type": "number", "description": "behavior_pairs 每条样本带多少条前文；默认 6。"},
    {"key": "training_export_anonymize", "env": "TRAINING_EXPORT_ANONYMIZE", "label": "导出匿名化成员名", "category": "训练", "type": "checkbox", "description": "只替换成员/群标签；消息正文仍可能含昵称，分享前需人工检查。"},
    {"key": "reaction_images_enabled", "env": "REACTION_IMAGES_ENABLED", "label": "偶尔发送角色配图", "category": "角色配图", "type": "checkbox", "description": "由聊天模型选择纯文字、文字加一张图或单图；仅当当前人设的 references/image-style.md 存在时启用。新图准备好后才发送，已有图优先复用。"},
    {"key": "reaction_image_probability", "env": "REACTION_IMAGE_PROBABILITY", "label": "自动配图概率", "category": "角色配图", "type": "number", "description": "未选中语音的回复按此概率配一张图；模型负责内容，图片准备失败时退回文字。0 关闭自动配图。"},
    {"key": "reaction_image_cache_dir", "env": "REACTION_IMAGE_CACHE_DIR", "label": "可复用配图目录", "category": "角色配图", "type": "text", "description": "按当前人设的图片风格分别保存单幅角色配图及通用情绪、动作、文案；图片不放昵称或对象称呼。"},
    {"key": "image_model_enabled", "env": "IMAGE_MODEL_ENABLED", "label": "启用生图模型", "category": "生图 / OSS", "type": "checkbox", "description": "用于明确的生图请求，以及当前人设已配置图片风格时的概率性角色配图。"},
    {"key": "image_model_base_url", "env": "IMAGE_MODEL_BASE_URL", "label": "生图模型地址", "category": "生图 / OSS", "type": "text", "description": "OpenAI 兼容的图片生成服务地址。"},
    {"key": "image_model_api_key", "env": "IMAGE_MODEL_API_KEY", "label": "生图模型 Key", "category": "生图 / OSS", "type": "password", "secret": True, "description": "留空保持原值。"},
    {"key": "image_model", "env": "IMAGE_MODEL", "label": "生图模型名", "category": "生图 / OSS", "type": "text", "description": "由你的图片服务提供。"},
    {"key": "image_model_fallback_base_url", "env": "IMAGE_MODEL_FALLBACK_BASE_URL", "label": "备用生图地址", "category": "生图 / OSS", "type": "text", "description": "主端点失败后使用的 OpenAI 兼容图片服务地址。"},
    {"key": "image_model_fallback_api_key", "env": "IMAGE_MODEL_FALLBACK_API_KEY", "label": "备用生图 Key", "category": "生图 / OSS", "type": "password", "secret": True, "description": "留空保持原值；主端点失败后才使用。"},
    {"key": "image_model_fallback", "env": "IMAGE_MODEL_FALLBACK", "label": "备用生图模型名", "category": "生图 / OSS", "type": "text", "description": "备用图片服务提供的模型名。"},
    {"key": "image_model_size", "env": "IMAGE_MODEL_SIZE", "label": "图片尺寸", "category": "生图 / OSS", "type": "text", "description": "例如 1024x1024。"},
    {"key": "image_model_response_format", "env": "IMAGE_MODEL_RESPONSE_FORMAT", "label": "生图返回格式", "category": "生图 / OSS", "type": "select", "options": ["auto", "url", "b64_json"], "description": "auto 省略可选字段并接受 URL 或 base64，兼容 GPT-image 网关。"},
    {"key": "image_model_timeout_seconds", "env": "IMAGE_MODEL_TIMEOUT_SECONDS", "label": "生图超时（秒）", "category": "生图 / OSS", "type": "number", "description": "图片模型请求超时时间。"},
    {"key": "image_upload_enabled", "env": "IMAGE_UPLOAD_ENABLED", "label": "上传生图到 OSS", "category": "生图 / OSS", "type": "checkbox", "description": "开启后额外上传供其他客户端访问；失败仍会发送本地微信图片附件。"},
    {"key": "oss_region", "env": "OSS_REGION", "label": "OSS Region", "category": "生图 / OSS", "type": "text", "description": "例如 cn-shenzhen。"},
    {"key": "oss_endpoint", "env": "OSS_ENDPOINT", "label": "OSS Endpoint", "category": "生图 / OSS", "type": "text", "description": "例如 https://oss-cn-shenzhen.aliyuncs.com。"},
    {"key": "oss_bucket", "env": "OSS_BUCKET", "label": "OSS Bucket", "category": "生图 / OSS", "type": "text", "description": "上传目标 Bucket。"},
    {"key": "oss_access_key_id", "env": "OSS_ACCESS_KEY_ID", "label": "OSS AccessKey ID", "category": "生图 / OSS", "type": "password", "secret": True, "description": "留空保持原值。"},
    {"key": "oss_access_key_secret", "env": "OSS_ACCESS_KEY_SECRET", "label": "OSS AccessKey Secret", "category": "生图 / OSS", "type": "password", "secret": True, "description": "留空保持原值。"},
    {"key": "oss_prefix", "env": "OSS_PREFIX", "label": "OSS 对象前缀", "category": "生图 / OSS", "type": "text", "description": "生成图片保存的目录前缀。"},
    {"key": "oss_signed_url_ttl_seconds", "env": "OSS_SIGNED_URL_TTL_SECONDS", "label": "签名 URL 有效期（秒）", "category": "生图 / OSS", "type": "number", "description": "未配置公开域名时使用。"},
    {"key": "oss_public_base_url", "env": "OSS_PUBLIC_BASE_URL", "label": "OSS 公开域名", "category": "生图 / OSS", "type": "text", "description": "留空则返回签名 URL。"},
    {"key": "image_upload_timeout_seconds", "env": "IMAGE_UPLOAD_TIMEOUT_SECONDS", "label": "OSS 上传超时（秒）", "category": "生图 / OSS", "type": "number", "description": "生成图片上传 OSS 的超时时间。"},
    {"key": "image_upload_max_bytes", "env": "IMAGE_UPLOAD_MAX_BYTES", "label": "图片大小上限（字节）", "category": "生图 / OSS", "type": "number", "description": "防止异常大的图片占用带宽。"},
    {"key": "database_url", "env": "DATABASE_URL", "label": "数据库连接串", "category": "高级", "type": "text", "description": "默认使用 state/memory.db；共享部署可改为 PostgreSQL。"},
    {"key": "raw_retention_days", "env": "RAW_RETENTION_DAYS", "label": "原始消息保留天数", "category": "高级", "type": "number", "description": "原始消息过期清理周期。"},
    {"key": "chat_half_life_days", "env": "CHAT_HALF_LIFE_DAYS", "label": "聊天记忆半衰期（天）", "category": "高级", "type": "number", "description": "聊天事实召回的时间衰减。"},
    {"key": "interest_half_life_days", "env": "INTEREST_HALF_LIFE_DAYS", "label": "兴趣记忆半衰期（天）", "category": "高级", "type": "number", "description": "兴趣画像召回的时间衰减。"},
    {"key": "fact_half_life_days", "env": "FACT_HALF_LIFE_DAYS", "label": "事实记忆半衰期（天）", "category": "高级", "type": "number", "description": "稳定事实召回的时间衰减。"},
    {"key": "binding_code_ttl_minutes", "env": "BINDING_CODE_TTL_MINUTES", "label": "绑定码有效期（分钟）", "category": "高级", "type": "number", "description": "平台身份绑定确认码的有效期。"},
]
CONFIG_SCHEMA.extend(config_fields())
# Mark compact settings sections without duplicating the schema;
# /api/config still reads every entry.
mark_schema(CONFIG_SCHEMA)

AUTH_COOKIE = "wechat_agent_token"
PAGE_PATHS = frozenset({"/", "/members", "/groups", "/memory", "/settings", "/logs", "/platform"})
#: Every JSON route stays bounded; persona bundles may carry a whole folder.
JSON_BODY_LIMIT = 1_000_000
PERSONA_UPLOAD_BODY_LIMIT = 12_000_000
# Reference audio is uploaded as base64 JSON; the decoded sample is capped at
# 10 MB, so the transport ceiling needs room for the 4/3 expansion and fields.
VOICE_UPLOAD_BODY_LIMIT = 16_000_000


class DesktopController:
    """Local control plane for the selected Linux or Windows connector.

    The dashboard deliberately controls the same guarded ``WeChatAgent`` used
    by the CLI. It does not expose credentials or provide a second sending
    path. A server deployment can put the core service behind its own proxy;
    the selected UI connector owns the local Weixin desktop session.
    """

    def __init__(
        self,
        settings: Settings,
        *,
        agent_factory: Callable[[Settings], WeChatAgent] | None = None,
        config_path: Path | None = None,
    ):
        self.settings = settings
        self.config_path = (config_path or Path(".env")).expanduser()
        if not self.config_path.is_absolute():
            self.config_path = (Path.cwd() / self.config_path).resolve()
        self._agent_factory_override = agent_factory
        self._lock = threading.RLock()
        self._thread: threading.Thread | None = None
        self._agent: WeChatAgent | None = None
        self._phase = "stopped"
        self._last_error: str | None = None
        self._started_at: str | None = None
        self._events: deque[dict[str, object]] = deque(maxlen=300)
        self._configure_services()

    def _configure_services(self) -> None:
        """Build storage and platform adapters from the current settings."""
        settings = self.settings
        # A settings reload rebuilds the platform services; stop the previous
        # proactive worker before replacing it (no double scheduling).
        previous_automation = getattr(self, "_platform_automation", None)
        if previous_automation is not None:
            try:
                previous_automation.stop_worker()
            except Exception:
                pass
        self._storage = Storage(settings)
        self._storage.init_schema()
        self.memory_console = MemoryConsole(self._storage)
        self.voice_studio = VoiceProfiles(settings)
        if settings.platform_browser_enabled:
            adapters = {
                "douyin": BrowserPlatformAdapter(
                    "douyin",
                    data_dir=settings.platform_browser_data_dir,
                    executable_path=settings.platform_browser_executable,
                    account_id=settings.douyin_account_id,
                    headless=settings.platform_browser_headless,
                    scroll_steps=settings.platform_browser_scroll_steps,
                    page_timeout_ms=settings.platform_browser_page_timeout_ms,
                    write_enabled=settings.platform_write_enabled,
                    action_delay_seconds=settings.platform_action_delay_seconds,
                    max_actions_per_hour=settings.platform_max_actions_per_hour,
                ),
                "xiaohongshu": BrowserPlatformAdapter(
                    "xiaohongshu",
                    data_dir=settings.platform_browser_data_dir,
                    executable_path=settings.platform_browser_executable,
                    account_id=settings.xiaohongshu_account_id,
                    headless=settings.platform_browser_headless,
                    scroll_steps=settings.platform_browser_scroll_steps,
                    page_timeout_ms=settings.platform_browser_page_timeout_ms,
                    write_enabled=settings.platform_write_enabled,
                    action_delay_seconds=settings.platform_action_delay_seconds,
                    max_actions_per_hour=settings.platform_max_actions_per_hour,
                ),
            }
        else:
            adapters = {
                "douyin": DryRunPlatformAdapter("douyin"),
                "xiaohongshu": DryRunPlatformAdapter("xiaohongshu"),
            }
        self._platforms = PlatformService(
            self._storage,
            adapters,
            mention_allowlists={
                "douyin": settings.douyin_mention_allowlist,
                "xiaohongshu": settings.xiaohongshu_mention_allowlist,
            },
        )
        self._platform_identities = PlatformIdentityService(self._storage, settings)
        self._platform_memory = PlatformMemoryService(self._storage, settings)
        # Bound platform IDs extend the @ allowlist without copying IDs into
        # the .env file; the service reads it on every plan.
        self._platforms.set_mention_binding_provider(self._platform_identities.mention_ids_for_platform)
        # Display names are recorded as a hint for native candidate selection;
        # the stable ID remains the only authorization input.
        self._platforms.set_mention_name_resolver(self._platform_identities.mention_names_for_ids)
        llm = None
        if settings.llm_api_key:
            from .llm import LLMClient

            llm = LLMClient(settings)
        self._platform_automation = PlatformAutomation(
            self._storage,
            settings,
            self._platforms,
            llm=llm,
            identity=self._platform_identities,
            memory=self._platform_memory,
        )
        if settings.platform_proactive_enabled:
            self._platform_automation.start_worker()
        if self._agent_factory_override is None:
            self._agent_factory = lambda current_settings: WeChatAgent(
                current_settings,
                platform_service=self._platforms,
                platform_automation=self._platform_automation,
                platform_memory=self._platform_memory,
            )
        else:
            self._agent_factory = self._agent_factory_override

    def add_event(self, message: str, *, level: str = "INFO", source: str = "desktop") -> None:
        with self._lock:
            self._events.append(
                {
                    "at": datetime.now(timezone.utc).isoformat(),
                    "level": level.upper(),
                    "source": source,
                    "message": message[:1000],
                }
            )

    def events(self, limit: int = 100) -> list[dict[str, object]]:
        with self._lock:
            items = list(self._events)
        return items[-max(1, min(limit, 300)) :][::-1]

    def start(self) -> dict[str, object]:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return self.status()
            self.settings.stop_file.unlink(missing_ok=True)
            self._phase = "starting"
            self._last_error = None
            self._started_at = datetime.now(timezone.utc).isoformat()
            self._agent = None
            self._thread = threading.Thread(target=self._run_agent, name="wechat-agent", daemon=True)
            self._thread.start()
            self.add_event("已开启自动回复", source="control")
            return self.status()

    def stop(self, timeout: float = 8.0) -> dict[str, object]:
        self.settings.stop_file.parent.mkdir(parents=True, exist_ok=True)
        self.settings.stop_file.write_text("stop\n", encoding="utf-8")
        with self._lock:
            thread = self._thread
            if thread and thread.is_alive():
                self._phase = "stopping"
        if thread and thread is not threading.current_thread():
            thread.join(timeout=max(0.0, timeout))
        with self._lock:
            if not self._thread or not self._thread.is_alive():
                if self._phase != "error":
                    self._phase = "stopped"
            self.add_event("已关闭自动回复", source="control")
            return self.status()

    def clear_stop(self) -> dict[str, object]:
        self.settings.stop_file.unlink(missing_ok=True)
        with self._lock:
            if self._phase == "stopping":
                self._phase = "stopped"
            self.add_event("已清除 STOP 文件", source="control")
        return self.status()

    def reload_settings(self, new_settings: Settings) -> dict[str, object]:
        """Apply validated settings and restart the listener if it was running."""
        with self._lock:
            was_running = bool(self._thread and self._thread.is_alive())
        if was_running:
            self.stop()
        self.close_platforms()
        with self._lock:
            self.settings = new_settings
            self._configure_services()
            self._last_error = None
            self.add_event("配置已校验并重载；平台连接器已按新配置初始化", source="config")
        if was_running:
            self.start()
        return {"restarted": was_running, **self.status()}

    def config_snapshot(self) -> dict[str, object]:
        values: dict[str, object] = {}
        secrets: dict[str, bool] = {}
        model = self.settings.model_dump()
        for field in CONFIG_SCHEMA:
            key = str(field["key"])
            env_name = str(field["env"])
            value = model.get(key, "")
            if isinstance(value, Path):
                value = str(value)
            elif isinstance(value, list):
                value = ",".join(str(item) for item in value)
            is_secret = bool(field.get("secret"))
            secrets[env_name] = is_secret and bool(str(value))
            values[env_name] = "" if is_secret else value
        return {
            "path": str(self.config_path),
            "fields": CONFIG_SCHEMA,
            "values": values,
            "configured_secrets": secrets,
        }

    def design_persona(self, payload: dict[str, object]) -> dict[str, object]:
        """Generate a persona bundle from the console's text description."""

        if not self.settings.llm_api_key.strip():
            raise ValueError("请先在「模型」里配置文字模型 Key，再让模型生成人设")
        from .llm import LLMClient

        return generate_persona_design(LLMClient(self.settings), payload)

    def save_config(self, payload: dict[str, object]) -> dict[str, object]:
        raw_values = payload.get("values")
        if not isinstance(raw_values, dict):
            raise ValueError("config_values_must_be_object")
        allowed = {str(field["env"]): field for field in CONFIG_SCHEMA}
        unknown = sorted(str(key) for key in raw_values if str(key) not in allowed)
        if unknown:
            raise ValueError(f"unsupported_config_keys:{','.join(unknown)}")
        clear_secrets = {
            str(item)
            for item in (payload.get("clear_secrets") or [])
            if isinstance(item, str)
        }
        original = self.config_path.read_text(encoding="utf-8") if self.config_path.exists() else ""
        current = _parse_env_file(original)
        # Once the Web UI uses the plural setting, clear the legacy singleton
        # so an old .env line cannot keep an unintended group active (including
        # when the operator intentionally clears the group list).
        if "WECHAT_GROUP_NAMES" in raw_values and "WECHAT_GROUP_NAME" not in raw_values:
            current["WECHAT_GROUP_NAME"] = ""
        for env_name, value in raw_values.items():
            env_name = str(env_name)
            field = allowed[env_name]
            if bool(field.get("secret")) and not str(value or "").strip():
                if env_name not in clear_secrets:
                    continue
                value = ""
            if isinstance(value, bool):
                text = "true" if value else "false"
            elif value is None:
                text = ""
            else:
                text = str(value).strip()
            current[env_name] = text
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        updated = _render_env_file(original, current)
        self.config_path.write_text(updated, encoding="utf-8")
        try:
            new_settings = Settings(_env_file=self.config_path)
        except Exception as exc:
            self.config_path.write_text(original, encoding="utf-8")
            raise ValueError(f"配置校验失败: {str(exc)[:800]}") from exc
        old_host, old_port = self.settings.desktop_host, self.settings.desktop_port
        result = self.reload_settings(new_settings)
        result["config"] = self.config_snapshot()
        result["restart_required"] = (
            new_settings.desktop_host != old_host
            or new_settings.desktop_port != old_port
        )
        return result

    def live(self) -> dict[str, object]:
        scopes = self.memory_console.groups()
        group_styles: dict[str, object] = {}
        for scope in scopes:
            group_id = str(scope["group_id"])
            snapshot = self._storage.group_style_snapshot(str(scope["account_id"]), group_id)
            snapshot["enabled"] = self.settings.group_style_enabled
            group_styles[str(scope["account_id"]) + ":" + group_id] = snapshot
        group_style = (
            next(iter(group_styles.values()))
            if group_styles
            else {"enabled": self.settings.group_style_enabled, "ready": False}
        )
        return {
            "status": self.status(),
            "events": self.events(120),
            "replies": self.replies(80),
            "audit": self.audits(80),
            "group_style": group_style,
            "group_styles": group_styles,
            "platforms": self.platform_status(),
            "platform_actions": self.platform_actions(80),
        }

    def private_chats(self) -> dict[str, object]:
        """List discoverable one-to-one chats for the Web allowlist picker.

        Prefer the running Linux adapter so the endpoint reuses its already
        verified database/key state.  When the listener is stopped, create a
        read-only backend for the lookup and close it immediately.  Windows'
        UIA adapter has no safe contact enumeration surface, so it reports that
        the picker is unavailable instead of guessing from visible text.
        """

        with self._lock:
            active_ui = getattr(self._agent, "ui", None) if self._agent else None
            settings = self.settings
        discover = getattr(active_ui, "private_chats", None)
        owned = False
        backend = active_ui
        if not callable(discover):
            if settings.effective_platform != "linux":
                return {
                    "ready": False,
                    "supported": False,
                    "contacts": [],
                    "selected": list(settings.wechat_private_chat_names),
                    "hint": "当前 Windows 适配器不提供联系人枚举，请手动填写稳定 wxid。",
                }
            try:
                from .platform import create_backend

                backend = create_backend(settings)
                discover = getattr(backend, "private_chats", None)
                owned = True
            except Exception as exc:
                return {
                    "ready": False,
                    "supported": True,
                    "contacts": [],
                    "selected": list(settings.wechat_private_chat_names),
                    "error": f"{type(exc).__name__}: {str(exc)[:300]}",
                }
        try:
            contacts = discover() if callable(discover) else []
            return {
                "ready": True,
                "supported": True,
                "contacts": [
                    {"chat_id": str(getattr(chat, "chat_id", "")), "name": str(getattr(chat, "name", ""))}
                    for chat in contacts
                    if str(getattr(chat, "chat_id", "")).strip()
                ],
                "selected": list(settings.wechat_private_chat_names),
            }
        except Exception as exc:
            return {
                "ready": False,
                "supported": True,
                "contacts": [],
                "selected": list(settings.wechat_private_chat_names),
                "error": f"{type(exc).__name__}: {str(exc)[:300]}",
            }
        finally:
            if owned and backend is not None:
                close = getattr(backend, "close", None)
                if callable(close):
                    try:
                        close()
                    except Exception:
                        pass

    def status(self) -> dict[str, object]:
        with self._lock:
            thread_alive = bool(self._thread and self._thread.is_alive())
            agent_stats = asdict(self._agent.stats) if self._agent else {}
            phase = self._phase
            if phase == "starting" and thread_alive and self._agent:
                phase = "running"
            return {
                "phase": phase,
                "thread_alive": thread_alive,
                "started_at": self._started_at,
                "last_error": self._last_error,
                "group_configured": bool(getattr(self.settings, "wechat_group_names", None)),
                "group_name": ", ".join(getattr(self.settings, "wechat_group_names", None) or [self.settings.wechat_group_name]),
                "group_names": list(getattr(self.settings, "wechat_group_names", None) or []),
                "private_reply_enabled": self.settings.wechat_private_reply_enabled,
                "private_chat_names": list(self.settings.wechat_private_chat_names),
                "send_enabled": self.settings.wechat_allow_send,
                "proactive_enabled": self.settings.wechat_proactive_reply or self.settings.proactive_schedule_enabled,
                "proactive_mode": self.settings.proactive_mode,
                "proactive_window_min_minutes": self.settings.proactive_window_min_minutes,
                "proactive_window_max_minutes": self.settings.proactive_window_max_minutes,
                "proactive_idle_enabled": self.settings.proactive_idle_enabled,
                "proactive_min_interval_seconds": self.settings.wechat_proactive_min_interval_seconds,
                "proactive_max_per_hour": self.settings.wechat_proactive_max_per_hour,
                "proactive_probability": self.settings.wechat_proactive_probability,
                "reaction_images_enabled": self.settings.reaction_images_enabled,
                "reaction_image_probability": self.settings.reaction_image_probability,
                "voice_enabled": self.settings.voice_enabled,
                "voice_model": self.settings.voice_model,
                "voice_language": self.settings.voice_language,
                "voice_provider": self.settings.voice_provider,
                "asr_model": self.settings.asr_model,
                "voice_reply_enabled": self.settings.voice_reply_enabled,
                "voice_reply_probability": self.settings.voice_reply_probability,
                "tts_model": self.settings.tts_model,
                "tts_voice": self.settings.tts_voice,
                "audio_api_configured": bool(self.settings.aliyun_audio_api_key and self.settings.aliyun_audio_base_url),
                "persona_style_filter": self.settings.persona_style_filter,
                "persona_max_emoji": self.settings.persona_max_emoji,
                "training_export_enabled": self.settings.training_export_enabled,
                "training_export_hour": self.settings.training_export_hour,
                "training_export_minute": self.settings.training_export_minute,
                "stop_file_present": self.settings.stop_file.exists(),
                "stats": agent_stats,
                "processing": self._storage.processing_summary(),
            }

    def audits(self, limit: int = 50) -> list[dict[str, object]]:
        return self._storage.recent_audits(limit)

    def replies(self, limit: int = 50) -> list[dict[str, object]]:
        return self._storage.recent_reply_records(limit)

    def queue_details(self) -> dict[str, object]:
        """Drill-down for the overview's "处理队列" counter.

        The counter is based on the reply ledger; this payload explains which
        row is currently claimed, how old it is, and whether durable reply
        jobs are waiting behind it.
        """

        return self._storage.queue_details(
            lease_seconds=self.settings.wechat_processing_lease_seconds
        )

    def platform_status(self) -> dict[str, dict[str, object]]:
        return self._platforms.login_status()

    def platform_actions(self, limit: int = 50) -> list[dict[str, object]]:
        return self._platforms.actions(limit)

    def open_platform(self, platform: str) -> dict[str, object]:
        status = self._platforms.open(platform)  # type: ignore[arg-type]
        # A platform page can show cached "logged-in" chrome (e.g. the side
        # navigation) for a moment before the auth gate redirects to the QR
        # login.  Re-check once when the first read claims a session so the
        # console does not flash a false "已登录" state.
        if status.get("logged_in"):
            time.sleep(1.2)
            latest = self._platforms.login_status().get(platform)  # type: ignore[arg-type]
            if latest:
                status = latest
        return status

    def close_platform(self, platform: str) -> dict[str, object]:
        return self._platforms.close(platform)  # type: ignore[arg-type]

    def browse_platform(self, platform: str, query: str = "", limit: int = 20) -> list[dict[str, object]]:
        return [
            asdict(item)
            for item in self._platforms.browse(platform, query=query, limit=limit)  # type: ignore[arg-type]
        ]

    def read_platform_comments(self, platform: str, item_id: str, limit: int = 50) -> list[dict[str, object]]:
        return [
            asdict(comment)
            for comment in self._platforms.read_comments(platform, item_id=item_id, limit=limit)  # type: ignore[arg-type]
        ]

    def plan_platform_action(self, payload: dict[str, object]) -> dict[str, object]:
        platform = str(payload.get("platform") or "")
        action_type = str(payload.get("action_type") or "")
        text = str(payload.get("text") or "")
        dry_run = bool(payload.get("dry_run", True))
        mention_ids = _payload_mention_ids(payload.get("mention_ids"))
        if action_type == "comment":
            receipt = self._platforms.plan_comment(
                platform,
                str(payload.get("item_id") or ""),
                text,
                mention_ids=mention_ids,
                dry_run=dry_run,  # type: ignore[arg-type]
            )
        elif action_type == "reply":
            receipt = self._platforms.plan_reply(
                platform,
                str(payload.get("comment_id") or ""),
                text,
                mention_ids=mention_ids,
                dry_run=dry_run,  # type: ignore[arg-type]
            )
        else:
            raise ValueError("unsupported_platform_action_type")
        return asdict(receipt)

    def execute_platform_action(self, payload: dict[str, object]) -> dict[str, object]:
        return self._platforms.execute(
            str(payload.get("action_key") or ""),
            confirm=bool(payload.get("confirm")),
        )

    # -- three-way platform console API ---------------------------------------

    def platform_identities(self) -> dict[str, object]:
        """Association list plus the effective @ allowlist."""

        return self._platform_identities.identity_list_payload()

    def bind_platform_identity(self, payload: dict[str, object]) -> dict[str, object]:
        platform = str(payload.get("platform") or "").strip()
        if platform not in {"douyin", "xiaohongshu"}:
            raise ValueError("平台必须是 douyin 或 xiaohongshu（抖音/小红书）")
        platform_user_id = str(payload.get("platform_user_id") or "").strip()
        if not platform_user_id:
            raise ValueError("请填写平台稳定用户 ID（不是昵称）")
        member_id = str(payload.get("member_id") or "").strip()
        if not member_id:
            raise ValueError("请填写微信成员 ID（wxid）")
        identity = self._platform_identities.bind(
            platform=platform,
            platform_user_id=platform_user_id,
            platform_user_name=payload.get("platform_user_name") or "",
            member_id=member_id,
            member_name=payload.get("member_name") or "",
        )
        self.add_event(
            f"已绑定平台账号 {platform}:{platform_user_id} → 微信成员 {member_id}",
            source="platform",
        )
        return {
            "ok": True,
            "identity": identity,
            **self._platform_identities.identity_list_payload(),
        }

    def delete_platform_identity(self, payload: dict[str, object]) -> dict[str, object]:
        raw_id = payload.get("id")
        try:
            identity_id = int(str(raw_id).strip())
        except (TypeError, ValueError) as exc:
            raise ValueError("请提供要解绑的关联 ID（数字）") from exc
        deleted = self._platform_identities.delete(identity_id)
        self.add_event(
            f"平台关联 {identity_id} {'已解绑' if deleted else '不存在，未解绑'}",
            source="platform",
        )
        return {
            "ok": deleted,
            "deleted": deleted,
            "id": identity_id,
            **self._platform_identities.identity_list_payload(),
        }

    def platform_memories(self, query: dict[str, object] | None = None) -> dict[str, object]:
        values = query or {}
        try:
            limit = int(str(values.get("limit") or 50))
        except (TypeError, ValueError) as exc:
            raise ValueError("limit 必须是数字") from exc
        member_id = str(values.get("member_id") or "").strip() or None
        platform = str(values.get("platform") or "").strip() or None
        account = self._platform_identities.default_account_id
        rows = self._platform_memory.recent(
            account,
            member_id=member_id,
            platform=platform,
            limit=limit,
        )
        if not rows and not member_id:
            # The running agent may have discovered a real account ID after
            # the console bound identities; show its rows too.
            rows = self._storage.recent_platform_memories(
                "",
                member_id=member_id,
                platform=platform,
                limit=limit,
            )
        names = PlatformIdentityService.member_names(self._platform_identities.list())
        for row in rows:
            row["member_name"] = names.get(str(row.get("member_id") or ""), "")
        return {"account_id": account, "memories": rows, "count": len(rows)}

    def platform_proactive_status(self) -> dict[str, object]:
        return self._platform_automation.status()

    def run_platform_proactive(self) -> dict[str, object]:
        result = self._platform_automation.run_once(force=True)
        result["status"] = self._platform_automation.status()
        return result

    def close_platforms(self) -> None:
        automation = getattr(self, "_platform_automation", None)
        if automation is not None:
            try:
                automation.stop_worker()
            except Exception:
                pass
        for platform in ("douyin", "xiaohongshu"):
            try:
                self._platforms.close(platform)  # type: ignore[arg-type]
            except Exception:
                pass

    def _run_agent(self) -> None:
        try:
            agent = self._agent_factory(self.settings)
            with self._lock:
                self._agent = agent
                self._phase = "running"
                self.add_event("自动回复已运行", source="agent")
            agent.run()
        except Exception as exc:  # surfaced in the dashboard for diagnosis
            with self._lock:
                self._last_error = f"{type(exc).__name__}: {str(exc)[:500]}"
                self._phase = "error"
                self.add_event(self._last_error, level="ERROR", source="agent")
        finally:
            with self._lock:
                if self._phase != "error":
                    self._phase = "stopped"
                    self.add_event("自动回复已停止", source="agent")


class DesktopServer(ThreadingHTTPServer):
    allow_reuse_address = True

    def __init__(self, server_address, handler, controller: DesktopController, token: str):
        super().__init__(server_address, handler)
        self.controller = controller
        self.token = token.strip()
        self.web = WebConsole(controller)


class DesktopRequestHandler(BaseHTTPRequestHandler):
    server: DesktopServer

    def log_message(self, format: str, *args) -> None:  # noqa: A002 - stdlib signature
        # The console has its own audit/error view; avoid duplicating request
        # text into stdout where it may contain chat content.
        return

    def do_GET(self) -> None:  # noqa: N802 - stdlib signature
        parsed = urlparse(self.path)
        path = parsed.path
        if path.startswith("/static/"):
            return self._serve_static(path)
        if self._memory_route(path):
            if not self._authorized():
                return
            return self._dispatch_memory("GET", path)
        if self._is_page(path):
            if not self._page_authorized(parsed):
                return
            self._serve_page(parsed)
            return
        if not self._authorized():
            return
        if path == "/api/status":
            self._send_json(self.server.controller.status())
        elif path == "/api/audit":
            self._send_json(self.server.controller.audits())
        elif path == "/api/replies":
            self._send_json(self.server.controller.replies())
        elif path == "/api/queue":
            self._send_json(self.server.controller.queue_details())
        elif path == "/api/live":
            self._send_json(self.server.controller.live())
        elif path == "/api/events":
            self._send_json(self.server.controller.events())
        elif path == "/api/members":
            self._send_json(self.server.web.members_payload())
        elif path == "/api/groups":
            self._send_json(self.server.web.groups_payload())
        elif path == "/api/config":
            self._send_json(self.server.controller.config_snapshot())
        elif path == "/api/wechat/private-chats":
            self._send_json(self.server.controller.private_chats())
        elif path == "/api/personas":
            self._send_json(list_personas(self.server.controller.settings))
        elif path == "/api/voice/profiles":
            self._send_json(self.server.controller.voice_studio.snapshot())
        elif path == "/api/platforms":
            self._send_json(self.server.controller.platform_status())
        elif path == "/api/platform-actions":
            self._send_json(self.server.controller.platform_actions())
        elif path == "/api/platform-identities":
            self._send_json(self.server.controller.platform_identities())
        elif path == "/api/platform-memories":
            query = {key: values[-1] for key, values in parse_qs(parsed.query).items()}
            try:
                self._send_json(self.server.controller.platform_memories(query))
            except (TypeError, ValueError) as exc:
                self._send_json({"error": str(exc)}, 400)
        elif path == "/api/platform-proactive":
            self._send_json(self.server.controller.platform_proactive_status())
        else:
            self._send_json({"error": "not_found"}, 404)

    def do_POST(self) -> None:  # noqa: N802 - stdlib signature
        parsed = urlparse(self.path)
        path = parsed.path
        content_type = self.headers.get("Content-Type", "")
        # Progressive enhancement: the settings page is a plain HTML form, so
        # saving still works with JavaScript disabled.  The browser receives a
        # redirect back to the page instead of JSON.
        if path == "/settings" and "application/x-www-form-urlencoded" in content_type:
            if not self._page_authorized(parsed):
                return
            return self._handle_settings_form()
        if not self._authorized():
            return
        if self._memory_route(path):
            return self._dispatch_memory("POST", path)
        body_limit = (
            PERSONA_UPLOAD_BODY_LIMIT
            if path == "/api/personas/upload"
            else VOICE_UPLOAD_BODY_LIMIT
            if path == "/api/voice/profiles"
            else JSON_BODY_LIMIT
        )
        try:
            body = self._read_json_body(limit=body_limit)
            if path == "/api/start":
                payload = self.server.controller.start()
            elif path == "/api/stop":
                payload = self.server.controller.stop()
            elif path == "/api/clear-stop":
                payload = self.server.controller.clear_stop()
            elif path == "/api/config":
                payload = self.server.controller.save_config(body)
                self.server.token = self.server.controller.settings.desktop_token.strip()
            elif path == "/api/personas/design":
                payload = self.server.controller.design_persona(body)
            elif path == "/api/personas/upload":
                payload = upload_persona(
                    self.server.controller.settings,
                    body,
                    controller=self.server.controller,
                )
            elif path == "/api/voice/profiles":
                payload = self.server.controller.voice_studio.create(body)
            elif path == "/api/voice/profiles/import":
                payload = self.server.controller.voice_studio.import_current(body)
            elif path == "/api/voice/preview":
                payload = self.server.controller.voice_studio.preview(
                    str(body.get("profile_id") or ""),
                    body.get("text"),
                )
            elif path == "/api/voice/preview-current":
                payload = self.server.controller.voice_studio.preview_current(body.get("text"))
            elif re.fullmatch(r"/api/voice/profiles/[a-f0-9]{12}/(activate|refresh|delete)", path):
                profile_id, action = path.rsplit("/", 1)
                profile_id = profile_id.rsplit("/", 1)[-1]
                studio = self.server.controller.voice_studio
                if action == "activate":
                    profile, voice_id = studio.activate(profile_id)
                    saved = self.server.controller.save_config(
                        {"values": {"TTS_PROVIDER": str(profile.get("provider") or "aliyun"),
                                    "TTS_VOICE": voice_id}}
                    )
                    payload = {
                        "profile": profile,
                        "restarted": bool(saved.get("restarted")),
                        "config": saved.get("config"),
                    }
                elif action == "refresh":
                    payload = studio.refresh(profile_id)
                else:
                    payload = studio.delete(profile_id)
            elif path == "/api/platforms/open":
                payload = self.server.controller.open_platform(str(body.get("platform") or ""))
            elif path == "/api/platforms/close":
                payload = self.server.controller.close_platform(str(body.get("platform") or ""))
            elif path == "/api/platforms/browse":
                payload = self.server.controller.browse_platform(
                    str(body.get("platform") or ""),
                    query=str(body.get("query") or ""),
                    limit=int(body.get("limit", 20)),
                )
            elif path == "/api/platform-comments":
                payload = self.server.controller.read_platform_comments(
                    str(body.get("platform") or ""),
                    str(body.get("item_id") or ""),
                    limit=int(body.get("limit", 50)),
                )
            elif path == "/api/platform-actions/plan":
                payload = self.server.controller.plan_platform_action(body)
            elif path == "/api/platform-actions/execute":
                payload = self.server.controller.execute_platform_action(body)
            elif path == "/api/platform-identities":
                payload = self.server.controller.bind_platform_identity(body)
            elif path == "/api/platform-identities/delete":
                payload = self.server.controller.delete_platform_identity(body)
            elif path == "/api/platform-proactive/run":
                payload = self.server.controller.run_platform_proactive()
            else:
                self._send_json({"error": "not_found"}, 404)
                return
        except VoiceProfileError as exc:
            self._send_json({"error": exc.code}, 400)
            return
        except (ValueError, KeyError) as exc:
            self._send_json({"error": str(exc)}, 400)
            return
        except Exception as exc:  # surfaced as a bounded connector error
            self._send_json({"error": f"{type(exc).__name__}: {str(exc)[:500]}"}, 409)
            return
        self._send_json(payload)

    @staticmethod
    def _memory_route(path):
        return path.startswith(("/api/memory/", "/api/emotions/", "/api/proactive/"))

    def _dispatch_memory(self, method, path):
        from .memory_runtime import MemoryConflict
        try:
            query = {key: values[-1] for key, values in parse_qs(urlparse(self.path).query).items()}
            body = self._read_json_body() if method == "POST" else None
            result = self.server.controller.memory_console.dispatch(method, path, query, body)
            self._send_json(result)
        except MemoryConflict as exc:
            self._send_json({"error": str(exc), "code": "STATE_CONFLICT"}, 409)
        except KeyError as exc:
            self._send_json({"error": str(exc)}, 404)
        except (TypeError, ValueError) as exc:
            self._send_json({"error": str(exc)}, 400)

        except Exception as exc:
            logging.getLogger(__name__).error("memory_console_failed error_type=%s", type(exc).__name__)
            self._send_json({"error": "记忆管理暂不可用，请查看本地运行状态", "code": "INTERNAL_ERROR"}, 500)

    def _read_json_body(self, limit: int = JSON_BODY_LIMIT) -> dict[str, object]:
        try:
            length = int(self.headers.get("Content-Length", "0") or "0")
        except ValueError as exc:
            raise ValueError("请求体长度不合法") from exc
        if length <= 0:
            return {}
        if length > limit:
            raise ValueError(f"请求体过大（最多 {limit // 1_000_000} MB）")
        raw = self.rfile.read(length)
        try:
            decoded = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("请求体不是合法的 UTF-8 JSON") from exc
        if not isinstance(decoded, dict):
            raise ValueError("JSON 请求体必须是对象")
        return decoded

    @staticmethod
    def _is_page(path: str) -> bool:
        if path in PAGE_PATHS or path in ("/members/", "/groups/"):
            return True
        for prefix in ("/members/", "/groups/"):
            if path.startswith(prefix) and path[len(prefix) :]:
                return "/" not in path[len(prefix) :]
        return False

    def _supplied_token(self) -> str:
        supplied = self.headers.get("Authorization", "")
        if supplied.startswith("Bearer "):
            supplied = supplied.removeprefix("Bearer ").strip()
        supplied = supplied or self.headers.get("X-Desktop-Token", "")
        if supplied:
            return supplied
        header = self.headers.get("Cookie", "")
        if not header:
            return ""
        try:
            jar = SimpleCookie(header)
        except CookieError:
            return ""
        morsel = jar.get(AUTH_COOKIE)
        return morsel.value if morsel else ""

    def _authorized(self) -> bool:
        token = self.server.token
        if not token:
            return True
        if self._supplied_token() == token:
            return True
        self._send_json({"error": "unauthorized"}, 401)
        return False

    def _page_authorized(self, parsed) -> bool:
        """Protect rendered pages (they contain chat data) with the same token.

        A ``?token=`` link is accepted once and remembered in an HttpOnly
        cookie so refresh, back and deep links keep working.
        """

        token = self.server.token
        if not token:
            return True
        if self._supplied_token() == token:
            return True
        query_token = (parse_qs(parsed.query).get("token") or [""])[-1]
        if query_token and query_token == token:
            self._set_auth_cookie = True
            return True
        self._send_html(self.server.web.login_page(), status=401)
        return False

    def _serve_page(self, parsed) -> None:
        path = parsed.path
        query = {key: values[-1] for key, values in parse_qs(parsed.query).items()}
        web = self.server.web
        try:
            if path == "/":
                body = web.overview()
            elif path in ("/members", "/members/"):
                body = web.members()
            elif path.startswith("/members/"):
                body = web.member(unquote(path[len("/members/") :]), query)
            elif path in ("/groups", "/groups/"):
                body = web.groups()
            elif path.startswith("/groups/"):
                body = web.group(unquote(path[len("/groups/") :]), query)
            elif path == "/memory":
                body = web.memory(query)
            elif path == "/settings":
                body = web.settings_page(query)
            elif path == "/logs":
                body = web.logs()
            elif path == "/platform":
                body = web.platform()
            else:  # pragma: no cover - ``_is_page`` already narrowed this
                body = web.not_found("没有这个页面。")
        except KeyError:
            self._send_html(web.not_found("没有找到这个成员或群，可能还没有任何聊天记录。"), status=404)
            return
        except Exception as exc:  # a broken page must not take the server down
            logging.getLogger(__name__).error(
                "console_page_failed path=%s error_type=%s", path, type(exc).__name__
            )
            self.server.controller.add_event(
                f"页面渲染失败 {path}：{type(exc).__name__}", level="ERROR", source="console"
            )
            self._send_html(
                web.not_found("页面渲染失败，请查看控制台日志。", status_code=500), status=500
            )
            return
        self._send_html(body)

    def _serve_static(self, path: str) -> None:
        name = unquote(path[len("/static/") :])
        if not name or "\x00" in name or "/" in name or "\\" in name or name.startswith("."):
            return self._send_json({"error": "not_found"}, 404)
        target = STATIC_DIR / name
        try:
            body = target.read_bytes()
        except (OSError, ValueError):
            return self._send_json({"error": "not_found"}, 404)
        content_type = mimetypes.guess_type(name)[0] or "application/octet-stream"
        if content_type.startswith("text/") or content_type in {"application/javascript", "application/json"}:
            content_type += "; charset=utf-8"
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    def _handle_settings_form(self) -> None:
        query = {key: values[-1] for key, values in parse_qs(urlparse(self.path).query).items() if key in {"section", "category"}}
        try:
            length = int(self.headers.get("Content-Length", "0") or "0")
            raw = self.rfile.read(min(max(length, 0), 1_000_000)).decode("utf-8", "replace")
            parsed = parse_qs(raw, keep_blank_values=True)
            values: dict[str, str] = {}
            clear: list[str] = []
            for key, entries in parsed.items():
                if key == "clear_secret":
                    clear.extend(entry for entry in entries if entry)
                    continue
                if key == "token":
                    continue
                values[key] = entries[-1] if entries else ""
            self.server.controller.save_config({"values": values, "clear_secrets": clear})
            self.server.token = self.server.controller.settings.desktop_token.strip()
        except ValueError as exc:
            return self._redirect("/settings?" + urlencode({**query, "error": str(exc)[:300]}))
        except Exception as exc:
            logging.getLogger(__name__).error("settings_form_failed error_type=%s", type(exc).__name__)
            return self._send_json({"error": f"{type(exc).__name__}: {str(exc)[:300]}"}, 409)
        self._redirect("/settings?" + urlencode({**query, "saved": "1"}))

    def _redirect(self, location: str) -> None:
        self.send_response(303)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _send_json(self, payload: object, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, body: str, status: int = 200) -> None:
        encoded = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Cache-Control", "no-store")
        if getattr(self, "_set_auth_cookie", False):
            cookie = SimpleCookie()
            cookie[AUTH_COOKIE] = self.server.token
            cookie[AUTH_COOKIE]["path"] = "/"
            cookie[AUTH_COOKIE]["httponly"] = True
            cookie[AUTH_COOKIE]["samesite"] = "Lax"
            cookie[AUTH_COOKIE]["max-age"] = str(30 * 24 * 3600)
            self.send_header("Set-Cookie", cookie.output(header="").strip())
        self.end_headers()
        self.wfile.write(encoded)


def _payload_mention_ids(value: object) -> list[str]:
    """Accept JSON arrays or a comma-separated operator input, never names."""
    if value is None:
        return []
    if isinstance(value, str):
        return [part.strip() for part in value.split(",") if part.strip()]
    if isinstance(value, (list, tuple)):
        return [str(part).strip() for part in value if str(part).strip()]
    raise ValueError("mention_ids_must_be_array_or_csv")


def _parse_env_file(text: str) -> dict[str, str]:
    return {name: value for name, value in dotenv_values(stream=StringIO(text), interpolate=False).items()
            if value is not None}


def _render_env_file(original: str, values: dict[str, str]) -> str:
    previous = _parse_env_file(original)
    seen: set[str] = set()
    lines: list[str] = []
    for binding in parse_stream(StringIO(original)):
        name = binding.key
        source = binding.original.string
        if not binding.error and name in values:
            seen.add(name)
            if previous.get(name) != values[name]:
                prefix = "export " if source.lstrip().startswith("export ") else ""
                source = f"{prefix}{name}={_quote_env(values[name])}\n"
        lines.append(source)
    missing = [name for name in values if name not in seen]
    if missing:
        if lines and not "".join(lines).endswith("\n\n"):
            lines.append("\n" if "".join(lines).endswith("\n") else "\n\n")
        lines.append("# Managed by WeChat Agent desktop UI\n")
        lines.extend(f"{name}={_quote_env(values[name])}\n" for name in missing)
    return "".join(lines).rstrip() + "\n"


def _quote_env(value: str) -> str:
    value = str(value)
    if value and all(char.isalnum() or char in "._/:@%+,-" for char in value):
        return value
    escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("\r", "\\r").replace("\n", "\\n")
    return f'"{escaped}"'


def create_server(
    settings: Settings,
    controller: DesktopController | None = None,
    *,
    config_path: Path | None = None,
    host: str | None = None,
    port: int | None = None,
) -> DesktopServer:
    """Build the HTTP server; ``host``/``port`` override the configured values.

    The CLI passes ``--host``/``--port`` so the DSH plugin can choose the bind
    address it will proxy to without rewriting ``.env``.
    """

    host = (host or settings.desktop_host).strip() or "127.0.0.1"
    port = settings.desktop_port if port is None else port
    if host not in {"127.0.0.1", "localhost", "::1"} and not settings.desktop_token.strip():
        raise ValueError("DESKTOP_TOKEN 必须在桌面控制台绑定非本机地址时设置")
    controller = controller or DesktopController(settings, config_path=config_path)
    return DesktopServer((host, port), DesktopRequestHandler, controller, settings.desktop_token)


class DesktopLogHandler(logging.Handler):
    """Mirror agent logs into the UI's bounded real-time event feed."""

    def __init__(self, controller: DesktopController):
        super().__init__()
        self.controller = controller

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.controller.add_event(
                self.format(record),
                level=record.levelname,
                source=record.name or "log",
            )
        except Exception:
            # Logging must never break the agent or HTTP server.
            pass


def serve(
    settings: Settings,
    *,
    open_browser: bool = False,
    config_path: Path | None = None,
    host: str | None = None,
    port: int | None = None,
) -> None:
    server = create_server(settings, config_path=config_path, host=host, port=port)
    log_handler = DesktopLogHandler(server.controller)
    log_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    root_logger = logging.getLogger()
    root_logger.addHandler(log_handler)
    bound_host, bound_port = server.server_address[:2]
    url = f"http://{bound_host}:{bound_port}/"
    print(f"wechat agent desktop UI: {url}", flush=True)
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.controller.stop()
        server.controller.close_platforms()
        root_logger.removeHandler(log_handler)
        server.server_close()
