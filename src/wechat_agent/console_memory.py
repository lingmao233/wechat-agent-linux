"""Scoped management API and dashboard for the shared memory runtime."""
from __future__ import annotations

from datetime import datetime
import json
import re
import threading
import time
from urllib.parse import unquote

from sqlalchemy import or_, select

from .config import Settings
from .memory_jobs import aware
from .memory_runtime import MemoryConflict, MemoryRuntime
from .memory_schema import EpisodeRow, RelationshipStateRow, RetrievalTraceRow
from .proactive_scheduler import ProactiveScheduler
from .semantic_profiles import SemanticProfiles
from .storage import MessageRow


def config_fields() -> list[dict]:
    groups = {
        "Linux": {
            "wechat_platform": "运行平台", "wechat_account_id": "微信账号稳定 ID",
            "wechat_linux_data_dir": "微信账号数据目录", "wechat_linux_pid": "微信进程 PID（自动探测可留空）",
            "wechat_linux_key_file": "本地密钥缓存文件", "wechat_linux_cache_dir": "解密快照目录",
            "wechat_linux_cursor_file": "消息读取游标文件", "wechat_linux_chat_ids": "群名到稳定 ID 的映射（JSON）",
            "wechat_linux_backfill_messages": "首次读取的历史条数", "wechat_linux_binary": "微信二进制路径",
            "wechat_linux_memory_scan_max_mb": "密钥扫描内存上限（MB）", "wechat_linux_screenshot_dir": "失败截图目录",
            "wechat_linux_verify_seconds": "Linux 发送回读等待（秒）", "wechat_linux_image_xor_key": "图片 XOR 参数（可留空）",
        },
        "表情与视觉": {
            "sticker_enabled": "读取和发送表情", "sticker_vision_enabled": "理解表情含义", "sticker_frames": "GIF 理解帧数（1 或 4）",
            "sticker_timeout_seconds": "表情处理超时（秒）", "sticker_min_confidence": "表情最低置信度",
            "sticker_max_bytes": "表情下载大小上限", "sticker_panel_width": "表情面板宽度", "sticker_panel_height": "表情面板高度",
            "sticker_panel_rows": "表情面板行数", "sticker_panel_columns": "表情面板列数",
            "vision_base_url": "视觉模型地址", "vision_api_key": "视觉模型 Key", "vision_model": "视觉模型名称",
        },
        "语音识别": {
            "voice_enabled": "理解收到的语音", "voice_provider": "语音转文字方式",
            "asr_model": "阿里云语音识别模型", "voice_model": "可选本地语音模型或目录",
            "voice_language": "语音语言（zh 或 auto）", "voice_timeout_seconds": "识别最长等待（秒）",
            "wechat_voice_conversion_timeout_seconds": "微信转文字等待（秒）",
            "voice_validation_timeout_seconds": "转写可理解性检查超时（秒）",
            "voice_max_duration_seconds": "语音时长上限（秒）", "voice_max_bytes": "语音大小上限（字节）",
            "voice_cache_dir": "语音转写缓存目录", "voice_clarification_min_interval_seconds": "没听清提示最短间隔（秒）",
        },
        "语音回复": {
            "voice_reply_enabled": "启用语音回复", "voice_reply_probability": "随机语音概率",
            "tts_provider": "语音合成供应商",
            "tts_base_url": "通用 TTS API 地址", "tts_api_key": "通用 TTS API Key",
            "aliyun_audio_base_url": "阿里云语音原生 API 根地址", "aliyun_audio_api_key": "阿里云语音 API Key",
            "tts_model": "语音合成模型", "tts_voice": "声音名称", "tts_instruction": "说话风格",
            "tts_rate": "说话速度", "tts_pitch": "声音音调", "tts_language": "合成语言", "tts_seed": "合成随机种子",
            "voice_reply_cache_dir": "可复用语音目录",
        },
        "语义记忆": {
            "group_timezone": "群时间显示与日限额时区", "memory_orchestrator_enabled": "按问题主动检索记忆",
            "memory_event_extraction_enabled": "提取有来源的群事件", "memory_retrieval_max_rounds": "检索轮数上限",
            "memory_retrieval_max_calls": "远程调用次数上限", "memory_retrieval_max_candidates": "检索候选上限",
            "memory_context_token_budget": "记忆输入 token 预算", "llm_context_window_tokens": "文字模型上下文窗口 token",
            "memory_retrieval_deadline_seconds": "检索总超时（秒）", "memory_analysis_wait_seconds": "等待事件追齐（秒）",
            "memory_trace_retention_days": "回答溯源保留天数", "semantic_group_profile_enabled": "构建群语义画像",
            "semantic_profile_refresh_messages": "画像刷新所需新消息数", "semantic_profile_min_interval_seconds": "画像最短刷新间隔（秒）",
            "semantic_profile_max_interval_seconds": "活跃群画像最长检查间隔（秒）",
        },
        "情绪与关系": {
            "emotion_layer_enabled": "启用角色心情和成员关系", "emotion_tag_half_life_minutes": "情绪标签半衰期（分钟）",
            "emotion_pad_half_life_hours": "PAD 恢复半衰期（小时）", "emotion_max_axis_delta": "单消息每轴最大冲量",
            "emotion_event_min_confidence": "自动计分最低置信度", "relation_negative_ttl_hours": "临时扣分最长恢复时间（小时）",
            "relation_positive_half_life_days": "正向积累半衰期（天）",
        },
        "主动参与": {
            "proactive_schedule_enabled": "启用每群定时参与", "proactive_window_min_minutes": "参与窗口最短间隔（分钟）",
            "proactive_window_max_minutes": "参与窗口最长间隔（分钟）", "proactive_window_duration_minutes": "每个参与窗口持续（分钟）",
            "proactive_window_max_messages": "每窗口最多发言数", "proactive_message_min_gap_seconds": "窗口内最短发言间隔（秒）",
            "proactive_message_max_gap_seconds": "窗口内最长发言间隔（秒）", "proactive_max_per_group_hour": "每群每小时发言上限",
            "proactive_quiet_hours": "安静时段（例如 23:00-09:00）",
            "proactive_score_timeout_seconds": "参与意愿判断超时（秒）",
            "proactive_idle_enabled": "冷场时偶尔自己开话题",
            "proactive_idle_after_minutes": "安静多久后可开话题（分钟）",
            "proactive_idle_probability": "空闲窗口开话题概率",
            "proactive_idle_max_per_day": "每天最多冷场开话题次数",
            "addressee_gate_enabled": "回复前判断在跟谁说话",
            "addressee_judge_timeout_seconds": "判断消息对象超时（秒）",
        },
    }
    result = []
    for category, fields in groups.items():
        for key, label in fields.items():
            default = Settings.model_fields[key].default
            secret = key in {"vision_api_key", "aliyun_audio_api_key", "tts_api_key"}
            kind = "password" if secret else "checkbox" if isinstance(default, bool) else "number" if isinstance(default, (int, float)) or key == "wechat_linux_pid" else "text"
            item = {"key": key, "env": key.upper(), "label": label, "category": category, "type": kind, "secret": secret, "description": "保存后重新加载生效。"}
            if key == "wechat_platform":
                item.update(type="select", options=["auto", "linux", "windows"])
            if key == "voice_provider":
                item.update(type="select", options=["auto", "aliyun", "wechat", "local"])
            if key == "tts_provider":
                item.update(type="select", options=["aliyun", "openai_compatible", "elevenlabs"])
            if key == "tts_language":
                item.update(type="select", options=["auto", "zh", "en", "ja"])
            if category == "语音识别":
                item["description"] = {
                    "voice_enabled": "先转写，再判断称呼、关键词和随机接话；确实听不懂时请对方重说。",
                    "voice_provider": "auto：配置阿里云后优先尝试，失败或无法理解时用微信转文字；wechat：仅用微信，无需 ASR 模型；local：可选本地识别。",
                    "asr_model": "默认 qwen-audio-3.1-asr-flash；auto/aliyun 使用此模型，wechat 不会调用。实际方言准确度需用录音验证。",
                    "voice_model": "仅 local 使用，需额外安装 local-asr；auto/wechat 不下载 small 模型。",
                    "voice_language": "zh 适合中文群聊；auto 由模型识别语言，适合混合语言。",
                    "voice_timeout_seconds": "本地识别或音频解码超过此时间就中止；阿里云请求最长等待 60 秒。",
                    "wechat_voice_conversion_timeout_seconds": "等待微信将指定消息的转文字结果写入数据库；没有结果时不会猜测内容。",
                    "voice_validation_timeout_seconds": "聊天模型检查转写能否理解的等待时间，不补写或猜测原意。",
                    "voice_max_duration_seconds": "超过时长上限的音频不进入对话和记忆分析。",
                    "voice_max_bytes": "限制单条语音的音频文件大小，默认 5000000 字节。",
                    "voice_cache_dir": "按账号、群和成员保存转写，重复音频可以直接复用。",
                    "voice_clarification_min_interval_seconds": "同一群两次没听清或暂时无法读取的提示之间至少间隔多久。",
                }[key]
            if category == "语音回复":
                item["description"] = {
                    "voice_reply_enabled": "使用已配置音色发送微信原生语音条；关闭后也不响应明确的语音请求。声音完整生成后才发送。",
                    "voice_reply_probability": "普通回复先按此概率选择语音，未选中时再抽取配图。明确要求听声音或说话时优先语音；0 只关闭随机语音。",
                    "tts_provider": "aliyun：现有 Qwen-Audio，支持异步复刻；openai_compatible：任何实现 /v1/audio/speech 的服务，只支持预置音色；elevenlabs：支持即时复刻。",
                    "tts_base_url": "openai_compatible 填服务根地址（通常以 /v1 结尾）；elevenlabs 留空使用官方地址；aliyun 填 /api/v1 根地址，留空沿用阿里云字段。",
                    "tts_api_key": "通用 TTS Key，仅保存到运行机器 .env；留空保持原值，不会回传密钥。aliyun 留空时沿用阿里云语音 Key。",
                    "aliyun_audio_base_url": "阿里云专用字段，识别与合成共用。TTS_PROVIDER=aliyun 且通用地址为空时生效。",
                    "aliyun_audio_api_key": "阿里云专用字段，识别与合成共用。通用 TTS Key 为空时用于语音合成。",
                    "tts_model": "阿里云默认 qwen-audio-3.1-tts-plus；OpenAI 兼容填服务商模型名（如 tts-1、gpt-4o-mini-tts）；ElevenLabs 填 eleven_multilingual_v2 等。",
                    "tts_voice": "阿里云填音色 ID；OpenAI 兼容填 alloy、nova 等预置音色；ElevenLabs 填 voice_id。DIY 复刻成功后会自动写入。",
                    "tts_instruction": "只控制声音风格和情绪，实际说话内容由聊天模型生成；OpenAI 兼容仅 gpt-4o 系列使用，ElevenLabs 不使用。",
                    "tts_rate": "0.5 到 2.0，1 为原速；OpenAI 兼容映射为 speed，阿里云为 rate；ElevenLabs 暂不映射。",
                    "tts_pitch": "0.5 到 2.0，1 为原音调；仅阿里云原生 TTS 使用。",
                    "tts_language": "auto 由模型判断；zh 中文，en 英语，ja 日语。OpenAI 兼容与 ElevenLabs 由模型自行识别。",
                    "tts_seed": "0 到 65535；仅阿里云原生 TTS 使用，其余供应商忽略。",
                    "voice_reply_cache_dir": "按账号和群保存已完成音频；供应商、文字、声音、风格、语速等相同才能复用。",
                }[key]
            if key == "proactive_max_per_group_hour":
                item["description"] = "同时开启随机与计划参与时，共用已发送记录并采用较小的小时上限；0 暂停主动发言。"
            if key == "proactive_quiet_hours":
                item["description"] = "随机接话与计划参与共用此时段；开始和结束相同表示关闭安静时段。"
            if key == "addressee_gate_enabled":
                item["description"] = (
                    "主动插话和关键词命中前，先让模型判断这条消息是在跟谁说话；"
                    "判定在回其他真人时不回复，判定不确定时只作为旁观者接话，不认领成对自己的指责。"
                    "明确叫到机器人名字、@ 或引用机器人时仍正常回复，不走此判断。"
                )
            if key == "addressee_judge_timeout_seconds":
                item["description"] = "判断消息对象的最长等待时间；超时按“不确定”处理，不会卡住回复。"
            result.append(item)
    return result


class MemoryConsole:
    def __init__(self, storage):
        self.storage, self.settings = storage, storage.settings
        self.runtime = MemoryRuntime(storage)
        self.profiles = SemanticProfiles(self.runtime)
        self.scheduler = ProactiveScheduler(self.runtime)
        self._health_lock = threading.Lock()
        self._health_checked = 0.0
        self._health_result = {}

    def health(self, *, refresh: bool = False):
        with self._health_lock:
            if self._health_result and not refresh and time.monotonic() - self._health_checked < 30:
                return dict(self._health_result)
            analysis_pending = sum(scope["analysis"].get("pending", 0) for scope in self.runtime.groups())
            self._health_result = {"status": "connected" if not analysis_pending else "busy",
                "analysis_pending": analysis_pending, "checked_at": self.runtime.clock().isoformat()}
            self._health_checked = time.monotonic()
            return dict(self._health_result)

    def groups(self):
        rows = self.runtime.groups()
        if self.settings.wechat_account_id:
            rows = [row for row in rows if row["account_id"] == self.settings.wechat_account_id]
        return rows

    def scope(self, group: str, account: str):
        if not account:
            raise ValueError("SCOPE_REJECTED: 必须显式指定 account_id")
        if not any(row["account_id"] == account and row["group_id"] == group for row in self.groups()):
            raise KeyError("SCOPE_REJECTED: 账号/群不存在或不在当前管理范围")
        return account, group

    @staticmethod
    def date(value):
        if not value:
            return None
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError("时间必须包含时区，例如 +08:00")
        return aware(parsed)

    def source(self, account, group, identifier):
        local_ids = [int(identifier)] if identifier.isdigit() and len(identifier) < 19 else []
        with self.storage.Session() as session:
            row = session.scalar(select(MessageRow).where(MessageRow.account_id == account, MessageRow.group_id == group, or_(MessageRow.external_message_id == identifier, MessageRow.id.in_(local_ids))))
            if row is None:
                raise KeyError("SOURCE_EXPIRED: 来源不存在或已过期")
            return {"message_id": row.external_message_id or str(row.id), "local_id": row.id, "actor_member_id": row.member_id, "actor_name": row.member_name, "source_role": row.source_role, "content": row.text if row.lifecycle_status == "active" else "原文已过期" if row.lifecycle_status == "expired" else "来源已删除或被更正", "lifecycle_status": row.lifecycle_status, "occurred_at": aware(row.source_created_at or row.observed_at).isoformat(), "version": row.object_version}

    def dispatch(self, method, path, query, body=None):
        body = body or {}
        data = body if method == "POST" else query
        account = str(data.get("account_id") or "")
        if path == "/api/memory/groups" and method == "GET":
            return self.groups()
        if path == "/api/memory/health" and method == "GET":
            return self.health(refresh=str(data.get("refresh") or "").lower() in {"1", "true"})
        if path == "/api/memory/retry" and method == "POST":
            account, group = self.scope(str(data.get("group_id") or ""), account)
            return {"retried": self.runtime.retry_failed(account_id=account, group_id=group)}
        if path == "/api/memory/replay" and method == "POST":
            account, group = self.scope(str(data.get("group_id") or ""), account)
            if not isinstance(data.get("dry_run", True), bool):
                raise ValueError("dry_run 必须是布尔值")
            since, until = self.date(data.get("since")), self.date(data.get("until"))
            if not since or not until:
                raise ValueError("回填必须明确 since / until 时间范围")
            return self.runtime.replay(account_id=account, group_id=group, since=since, until=until, dry_run=data.get("dry_run", True))
        match = re.fullmatch(r"/api/memory/traces/([^/]+)", path)
        if match and method == "GET":
            account, group = self.scope(str(data.get("group_id") or ""), account)
            result = self.runtime.trace(unquote(match[1]), account_id=account)
            if result["snapshot"]["group_id"] != group:
                raise KeyError("SCOPE_REJECTED")
            return result
        match = re.fullmatch(r"/api/emotions/events/([^/]+)/revert", path)
        if match and method == "POST":
            account, group = self.scope(str(data.get("group_id") or ""), account)
            identifier = unquote(match[1])
            with self.storage.Session() as session:
                event = session.get(EpisodeRow, identifier)
                if not event or event.account_id != account or event.group_id != group:
                    raise KeyError("SCOPE_REJECTED")
            if type(data.get("expected_version")) is not int:
                raise ValueError("expected_version 必须是整数")
            return self.runtime.revert_event(identifier, reason=str(data.get("reason") or ""), expected_version=data["expected_version"], account_id=account)
        match = re.fullmatch(r"/api/(memory|emotions|proactive)/groups/([^/]+)(?:/(.*))?", path)
        if not match or method != "GET":
            raise KeyError("not_found")
        area, group, endpoint = match.groups()
        account, group = self.scope(unquote(group), account)
        if area == "proactive" and not endpoint:
            return self.scheduler.status(account, group)
        if area == "emotions":
            member = unquote(endpoint[8:]) if endpoint and endpoint.startswith("members/") else ""
            if endpoint and not member:
                raise KeyError("not_found")
            result = self.runtime.emotion.snapshot(account, group, member, now=self.runtime.clock())
            if not member:
                with self.storage.Session() as session:
                    members = list(session.scalars(select(RelationshipStateRow.member_id).where(RelationshipStateRow.account_id == account, RelationshipStateRow.group_id == group)))
                result["members"] = [self.runtime.emotion.snapshot(account, group, identifier, now=self.runtime.clock()) for identifier in members]
            return result
        if endpoint == "profile":
            return {**self.profiles.snapshot(account, group), "enabled": self.settings.semantic_group_profile_enabled}
        if endpoint == "events":
            return self.runtime.recent_events(account, group, member_id=str(data.get("member_id") or ""), event_types=[value for value in str(data.get("event_type") or "").split(",") if value], since=self.date(data.get("since")), until=self.date(data.get("until")), limit=max(1, int(data.get("limit") or 50)), include_inactive=str(data.get("include_inactive") or "").lower() in {"1", "true"})
        if endpoint == "traces":
            with self.storage.Session() as session:
                return [{"request_id": row.request_id, "status": row.status, "created_at": aware(row.created_at).isoformat(), "question_message_id": row.question_message_id} for row in session.scalars(select(RetrievalTraceRow).where(RetrievalTraceRow.account_id == account, RetrievalTraceRow.group_id == group).order_by(RetrievalTraceRow.created_at.desc()).limit(50))]
        if endpoint and endpoint.startswith("sources/"):
            return self.source(account, group, unquote(endpoint[8:]))
        raise KeyError("not_found")
