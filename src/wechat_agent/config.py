from __future__ import annotations

from pathlib import Path
import sys
from typing import Annotated, Literal

from pydantic import AliasChoices, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


def _normalize_quiet_hours(value: object, label: str) -> str:
    """Normalize a ``HH:MM-HH:MM`` quiet-hours value.

    Operators type this by hand (sometimes with a Chinese IME), so accept the
    common variants instead of surfacing a raw pydantic ``Invalid isoformat
    string`` error: full-width colon, spaces around the dash, em-dash/tilde as
    the separator, and a single-digit hour.  Truly invalid input raises a
    Chinese error that names the setting and the expected format.
    """

    text = str(value or "").strip()
    for source, target in (("：", ":"), ("—", "-"), ("–", "-"), ("～", "-"), ("~", "-")):
        text = text.replace(source, target)
    parts = [part.strip() for part in text.split("-")]
    expected = f"{label}必须是 HH:MM-HH:MM 格式，例如 23:00-08:00"
    if len(parts) != 2:
        raise ValueError(expected)
    normalized: list[str] = []
    for part in parts:
        hour_text, separator, minute_text = part.partition(":")
        if not separator or not hour_text.strip() or not minute_text.strip():
            raise ValueError(expected)
        try:
            hour, minute = int(hour_text.strip()), int(minute_text.strip())
        except ValueError as exc:
            raise ValueError(expected) from exc
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            raise ValueError(expected)
        normalized.append(f"{hour:02d}:{minute:02d}")
    return "-".join(normalized)


class Settings(BaseSettings):
    """Environment-backed settings.

    Sending is opt-in. A group name is also required before the live adapter can
    open a conversation, which prevents an accidental broadcast to the current
    Weixin account.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    wechat_window_class: str = "mmui::MainWindow"
    wechat_platform: Literal["auto", "linux", "windows"] = "auto"
    wechat_account_id: str = ""
    wechat_linux_data_dir: Path | None = None
    wechat_linux_pid: int | None = None
    wechat_linux_key_file: Path = Path("state/linux_keys.json")
    wechat_linux_cache_dir: Path = Path("state/linux-db")
    wechat_linux_cursor_file: Path = Path("state/linux_cursors.json")
    wechat_linux_chat_ids: str = "{}"
    wechat_linux_backfill_messages: int = Field(default=50, ge=0, le=1000)
    wechat_linux_binary: Path = Path("/opt/wechat/wechat")
    wechat_linux_memory_scan_max_mb: int = Field(default=4096, ge=16, le=16384)
    wechat_linux_screenshot_dir: Path = Path("state/diagnostics")
    wechat_linux_verify_seconds: float = Field(default=5.0, gt=0, le=30)
    wechat_linux_image_xor_key: int | None = Field(default=None, ge=0, le=255)
    sticker_enabled: bool = True
    sticker_vision_enabled: bool = True
    sticker_frames: Literal[1, 4] = 1
    sticker_timeout_seconds: float = Field(default=10, gt=0, le=30)
    sticker_min_confidence: float = Field(default=0.6, ge=0, le=1)
    sticker_max_bytes: int = Field(default=20_000_000, ge=1024, le=50_000_000)
    sticker_panel_width: int = Field(default=474, ge=200, le=2000)
    sticker_panel_height: int = Field(default=474, ge=200, le=2000)
    sticker_panel_rows: int = Field(default=4, ge=1, le=10)
    sticker_panel_columns: int = Field(default=5, ge=1, le=10)
    vision_base_url: str = ""
    vision_api_key: str = ""
    vision_model: str = ""
    group_timezone: str = "Asia/Shanghai"
    memory_orchestrator_enabled: bool = False
    memory_event_extraction_enabled: bool = False
    memory_judge_min_confidence: float = Field(default=0.6, ge=0.3, le=0.95)
    memory_judge_daily_budget: int = Field(default=3000, ge=1, le=100_000)
    memory_judge_batch_size: int = Field(default=20, ge=1, le=20)
    memory_retrieval_max_rounds: int = Field(default=2, ge=1, le=2)
    memory_retrieval_max_calls: int = Field(default=8, ge=0, le=8)
    memory_retrieval_max_candidates: int = Field(default=60, ge=1, le=60)
    memory_context_token_budget: int = Field(default=4000, ge=500, le=8000)
    llm_context_window_tokens: int = Field(default=32768, ge=4096, le=2_000_000)
    memory_retrieval_deadline_seconds: float = Field(default=8, gt=0, le=30)
    memory_analysis_wait_seconds: float = Field(default=2, ge=0, le=5)
    memory_trace_retention_days: int = Field(default=7, ge=1, le=30)
    semantic_group_profile_enabled: bool = False
    semantic_profile_refresh_messages: int = Field(default=30, ge=1, le=1000)
    semantic_profile_min_interval_seconds: float = Field(default=1800, ge=0, le=86400)
    semantic_profile_max_interval_seconds: float = Field(default=21600, ge=60, le=86400)
    emotion_layer_enabled: bool = False
    emotion_tag_half_life_minutes: float = Field(default=45, gt=0)
    emotion_pad_half_life_hours: float = Field(default=6, gt=0)
    emotion_max_axis_delta: float = Field(default=0.10, gt=0, le=0.10)
    emotion_event_min_confidence: float = Field(default=0.85, ge=0.85, le=1)
    relation_negative_ttl_hours: float = Field(default=24, gt=0, le=24)
    relation_positive_half_life_days: float = Field(default=7, gt=0)
    proactive_schedule_enabled: bool = False
    proactive_window_min_minutes: int = Field(default=60, ge=1, le=1440)
    proactive_window_max_minutes: int = Field(default=120, ge=1, le=1440)
    proactive_window_duration_minutes: int = Field(default=15, ge=1, le=60)
    proactive_window_max_messages: int = Field(default=3, ge=0, le=3)
    proactive_message_min_gap_seconds: int = Field(default=180, ge=1, le=3600)
    proactive_message_max_gap_seconds: int = Field(default=480, ge=1, le=3600)
    proactive_max_per_group_hour: int = Field(default=3, ge=0, le=3)
    proactive_quiet_hours: str = "23:00-09:00"
    proactive_score_timeout_seconds: float = Field(default=10, gt=0, le=30)
    proactive_idle_enabled: bool = True
    proactive_idle_after_minutes: int = Field(default=30, ge=5, le=1440)
    proactive_idle_probability: float = Field(default=0.4, ge=0, le=1)
    proactive_idle_max_per_day: int = Field(default=2, ge=0, le=20)
    # A proactive candidate or a bare keyword hit is only a guess about who is
    # being addressed.  Before answering one, ask the model whether the message
    # targets the character at all.  Fail-open: errors count as "unclear".
    addressee_gate_enabled: bool = True
    addressee_judge_timeout_seconds: float = Field(default=8, gt=0, le=30)
    # A proactive candidate is one row, but people often send a burst of short
    # messages ("奖励是可以追zls" / "还有俩包苹果干").  The scheduler only picks
    # the last row; merging the same sender's adjacent rows into one turn stops
    # the model from answering the final fragment and ignoring what led up to it.
    proactive_block_enabled: bool = True
    proactive_block_gap_seconds: float = Field(default=60, ge=10, le=600)
    proactive_block_max_messages: int = Field(default=3, ge=1, le=5)
    wechat_exe: str | None = None
    # ``WECHAT_GROUP_NAMES`` is the multi-group setting.  The singular field
    # remains as a compatibility input for existing .env files and is kept in
    # sync with the first configured group after validation.
    wechat_group_name: str = ""
    wechat_group_names: Annotated[list[str], NoDecode] = Field(default_factory=list)
    # Private conversations are opt-in and independent from the group allowlist.
    # Every private conversation must be explicitly listed by stable wxid or
    # display name; an empty list closes private service completely.
    wechat_private_reply_enabled: bool = False
    wechat_private_chat_names: Annotated[list[str], NoDecode] = Field(default_factory=list)
    wechat_private_proactive_enabled: bool = False
    # Stable, local ledger for the one-time notification sent when a contact
    # is explicitly added to the private-chat allowlist.  It is deliberately
    # separate from the message cursor so changing polling state cannot cause
    # a welcome message to be replayed.
    wechat_private_welcome_state_file: Path = Path("state/private_welcome_sent.json")
    wechat_ai_aliases: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["AI", "ai", "机器人"]
    )
    # Names are only addressing forms: an explicit @ mention or the message
    # prefix.  Keep free-form trigger words separate so a normal answer that
    # happens to contain "AI" does not trigger the listener again.
    wechat_keywords: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["问AI", "小助手"]
    )
    wechat_self_name: str = ""
    wechat_allowed_members: Annotated[list[str], NoDecode] = Field(default_factory=list)
    wechat_allow_send: bool = False
    # Opt-in random participation in ordinary unaddressed conversation.
    # Scheduled windows can coexist and share the verified proactive ledger.
    wechat_proactive_reply: bool = False
    wechat_proactive_min_interval_seconds: float = Field(default=300.0, ge=0.0, le=86_400.0)
    wechat_proactive_max_per_hour: int = Field(default=6, ge=0, le=100)
    wechat_proactive_probability: float = Field(default=0.4, ge=0.0, le=1.0)
    wechat_poll_seconds: float = Field(default=2.0, ge=0.5, le=60.0)
    # Pending replies live in ``reply_jobs``; there is no in-memory backlog cap.
    # Reply frequency controls are deliberately disabled by default.  They
    # limit successful outbound replies after a trigger has already matched;
    # they do not turn ordinary group messages into model requests.
    wechat_reply_cooldown_seconds: float = Field(default=0.0, ge=0.0, le=86_400.0)
    wechat_max_replies_per_hour: int = Field(default=0, ge=0, le=1_000)
    wechat_stop_file: Path = Path("state/STOP")
    wechat_processing_lease_seconds: int = Field(default=300, ge=30, le=86400)
    wechat_send_retry_count: int = Field(default=3, ge=1, le=5)
    wechat_send_verify_seconds: float = Field(default=15.0, gt=1.0, le=120.0)
    wechat_skip_existing_on_start: bool = True
    # The UI adapter can only see the currently rendered chat page. Keep a
    # small, bounded window of messages from the current group for prompt context.
    wechat_context_messages: int = Field(default=12, ge=0, le=50)
    wechat_context_max_chars: int = Field(default=6000, ge=500, le=30_000)
    # A flat "last N rows" window mixes topics and lets one long media payload
    # eat the budget.  The reply prompt is built from one inferred topic
    # segment instead: walk back from the triggering message until the group
    # went quiet, keep the segment's opening lines plus the newest lines, and
    # mark what was cut.  Every number stays tunable for quiet groups.
    context_topic_enabled: bool = True
    context_topic_gap_seconds: int = Field(default=300, ge=30, le=3600)
    context_topic_max_minutes: int = Field(default=30, ge=1, le=1440)
    context_topic_max_messages: int = Field(default=12, ge=2, le=50)
    context_topic_head_messages: int = Field(default=3, ge=1, le=10)
    context_topic_scan_messages: int = Field(default=40, ge=12, le=200)
    context_topic_scan_chars: int = Field(default=12_000, ge=500, le=60_000)
    # One prompt row never carries more than this; the model gets a "chars" /
    # "truncated" marker instead of silently reading a cut sentence.
    context_message_max_chars: int = Field(default=1200, ge=100, le=30_000)
    # Image rows are downloaded from the local WeChat attachment store before
    # being sent as bounded data URLs to a vision-capable OpenAI-compatible
    # model. Recognition is opt-in at runtime and never uploads raw paths.
    wechat_image_recognition_enabled: bool = True
    wechat_image_max_count: int = Field(default=4, ge=0, le=12)
    wechat_image_max_bytes: int = Field(default=12_000_000, ge=100_000, le=100_000_000)
    # A picture stays prompt-visible for a few messages after it arrives, so a
    # follow-up question can still refer to it.  Older pictures only come back
    # when somebody explicitly mentions the image again.
    wechat_image_context_turns: int = Field(default=4, ge=0, le=50)
    wechat_image_reference_window: int = Field(default=30, ge=1, le=200)
    wechat_media_dir: Path = Path("state/wechat-media")
    # Auto tries configured Aliyun ASR, then WeChat's own conversion. Local
    # Whisper is optional and is never downloaded by the default provider.
    voice_enabled: bool = True
    voice_provider: Literal["auto", "aliyun", "wechat", "local"] = "auto"
    # WeChat's built-in conversion is preferred in ``wechat`` mode, but it can
    # silently return no transcript for a received voice.  When Aliyun audio is
    # configured, retry the saved SILK audio with the ASR model instead of
    # dropping the message.  Set to ``none`` to keep the old WeChat-only path.
    voice_fallback_provider: Literal["aliyun", "none"] = "aliyun"
    aliyun_audio_base_url: str = ""
    aliyun_audio_api_key: str = ""
    # Only remote image/audio calls use this; LAN chat and memory keep their routes.
    media_http_proxy_url: str = ""
    asr_model: str = "qwen-audio-3.1-asr-flash"
    # Speech synthesis provider.  Aliyun keeps its existing native API fields
    # for backward compatibility; the generic base URL/key override them when
    # set.  OpenAI-compatible gateways and ElevenLabs use the generic fields.
    tts_provider: Literal["aliyun", "openai_compatible", "elevenlabs"] = "aliyun"
    tts_base_url: str = ""
    tts_api_key: str = ""
    tts_model: str = "qwen-audio-3.1-tts-plus"
    tts_voice: str = "longanhuan_v3.1"
    tts_instruction: str = "用轻快、自然的年轻女声说话，像和朋友聊天，情绪跟随句子的意思。"
    tts_singing_instruction: str = "用轻快可爱的年轻女声带着清楚的旋律唱出来，短句之间有自然换气，不要念白。"
    tts_rate: float = Field(default=1.0, ge=0.5, le=2.0)
    tts_pitch: float = Field(default=1.0, ge=0.5, le=2.0)
    tts_language: Literal["auto", "zh", "en", "ja"] = "auto"
    tts_seed: int = Field(default=0, ge=0, le=65535)
    voice_reply_enabled: bool = True
    singing_reply_enabled: bool = True
    voice_reply_probability: float = Field(default=0.15, ge=0, le=1)
    voice_reply_cache_dir: Path = Path("state/voice-replies")
    # Local metadata and normalized reference clips for user-created (cloned)
    # voices.  The provider voice ID lives in TTS_VOICE; this directory keeps
    # only what the settings UI needs to list, preview and delete profiles.
    voice_profiles_dir: Path = Path("state/voice-profiles")
    wechat_voice_conversion_timeout_seconds: float = Field(default=25, ge=1, le=60)
    voice_model: str = "small"
    voice_language: str = "zh"
    voice_timeout_seconds: float = Field(default=120, ge=5, le=600)
    voice_validation_timeout_seconds: float = Field(default=10, gt=0, le=30)
    voice_max_duration_seconds: float = Field(default=120, ge=1, le=300)
    voice_max_bytes: int = Field(default=5_000_000, ge=1024, le=20_000_000)
    voice_cache_dir: Path = Path("state/voice")
    voice_clarification_min_interval_seconds: float = Field(default=60, ge=0, le=3600)
    # Generated pictures (image-model output) default next to the chat media so
    # deployments can keep every runtime file on one data disk.
    image_output_dir: Path = Path("state/generated")
    # Optional, reusable pictures of the active persona accompanying an ordinary
    # reply.  The character comes from the persona's image-style.md; without
    # that file this feature is disabled.  It never turns every reply into a
    # picture and uses the existing image model.
    reaction_images_enabled: bool = True
    reaction_image_probability: float = Field(default=0.3, ge=0, le=1)
    reaction_image_cache_dir: Path = Path("state/reaction-images")

    # The group style layer is a bounded, aggregate profile derived from
    # ordinary member messages.  It is kept per account + group and is only
    # refreshed after enough new messages arrive, so every reply does not
    # trigger another model call.
    group_style_enabled: bool = True
    group_style_min_messages: int = Field(default=20, ge=5, le=500)
    group_style_refresh_messages: int = Field(default=20, ge=1, le=500)
    group_style_sample_messages: int = Field(default=80, ge=10, le=500)
    group_style_max_chars: int = Field(default=1800, ge=300, le=10_000)
    # Style and AI profiles are rebuilt from a bounded recent window.  A
    # lifetime accumulator cannot drift with the group and contradicts the
    # "最近 N 条" label it prints.
    group_style_window_messages: int = Field(default=300, ge=50, le=5_000)
    # Only the compact natural-language projection reaches the prompt; the
    # full audit profile stays in SQL.
    profile_prompt_max_chars: int = Field(default=4_000, ge=500, le=20_000)
    # Semantic group profile (culture/jokes/nicknames/reply examples) is
    # synthesized from a bounded transcript, not from per-message counters.
    profile_synthesis_enabled: bool = True
    profile_synthesis_messages: int = Field(default=120, ge=20, le=500)
    profile_synthesis_max_input_chars: int = Field(default=6_000, ge=1_000, le=20_000)
    # Optional nightly local export for the style/behavior dataset.  It only
    # reads SQL and writes under data/ (gitignored); it never trains weights.
    training_export_enabled: bool = False
    training_export_dir: str = "data/training/daily"
    training_export_hour: int = Field(default=23, ge=0, le=23)
    training_export_minute: int = Field(default=30, ge=0, le=59)
    training_export_context_messages: int = Field(default=6, ge=1, le=30)
    training_export_anonymize: bool = True

    # Semantic memory.  The project accepts the generic EMBED_* names and the
    # existing WorkHub names already present in the local .env.  Secrets are
    # read at runtime only and are never included in diagnostics or prompts.
    embedding_enabled: bool = True
    embed_base: str = Field(
        default="",
        validation_alias=AliasChoices("EMBED_BASE", "WORKHUB_EMBED_BASE"),
    )
    embed_model: str = Field(
        default="",
        validation_alias=AliasChoices("EMBED_MODEL", "WORKHUB_EMBED_MODEL"),
    )
    embed_key: str = Field(
        default="",
        validation_alias=AliasChoices("EMBED_KEY", "WORKHUB_EMBED_KEY"),
    )
    # 0 means accept the provider's documented dimension and still require
    # every vector in one call to have the same length.  Set a positive value
    # when an operator wants a strict dimension check.
    embed_dimension: int = Field(default=0, ge=0, le=8192)
    embed_timeout_seconds: float = Field(default=45.0, gt=0, le=300.0)

    desktop_host: str = "127.0.0.1"
    desktop_port: int = Field(default=8765, ge=1, le=65535)
    desktop_token: str = ""

    # Public-platform browser connector.  It is deliberately opt-in: the
    # offline adapters remain the default until an operator explicitly turns
    # on a visible, persistent browser session on the desktop host.
    platform_browser_enabled: bool = False
    platform_browser_data_dir: Path = Path("browser-data")
    platform_browser_executable: str | None = None
    platform_browser_headless: bool = False
    douyin_account_id: str = "unbound"
    xiaohongshu_account_id: str = "unbound"
    # Entries are stable platform user IDs only.  Display names are not unique
    # and are never used as an authorization key.
    douyin_mention_allowlist: Annotated[list[str], NoDecode] = Field(default_factory=list)
    xiaohongshu_mention_allowlist: Annotated[list[str], NoDecode] = Field(default_factory=list)
    platform_browser_scroll_steps: int = Field(default=5, ge=1, le=20)
    platform_browser_page_timeout_ms: int = Field(default=20_000, ge=5_000, le=120_000)
    platform_write_enabled: bool = False
    platform_action_delay_seconds: float = Field(default=30.0, ge=0, le=86_400)
    platform_max_actions_per_hour: int = Field(default=5, ge=1, le=100)
    # Group-chat intent -> platform browsing/comment chain.  Every switch is
    # opt-in on top of the browser connector; a comment plan still defaults to
    # ``dry_run`` and can never be upgraded into a real write silently.
    platform_intent_enabled: bool = True
    platform_auto_comment_after_reply: bool = False
    platform_comment_max_chars: int = Field(default=200, ge=20, le=1000)
    platform_memory_enabled: bool = True
    platform_proactive_enabled: bool = False
    platform_proactive_interval_minutes: int = Field(default=240, ge=30, le=1440)
    platform_proactive_keywords: Annotated[list[str], NoDecode] = Field(default_factory=list)
    platform_quiet_hours: str = "23:00-08:00"

    # Optional online research. Search and fetch are separate switches. An
    # empty fetch-domain list allows public HTTP(S) hosts; setting the list
    # narrows retrieval to those domains. Local and private address targets
    # remain blocked by WebResearchClient.
    web_search_enabled: bool = False
    web_search_provider: Literal[
        "none",
        "brave",
        "tavily",
        "searxng",
        "deepseek-search-api",
    ] = "none"
    web_search_api_key: str = ""
    web_search_base_url: str = ""
    web_search_max_results: int = Field(default=5, ge=1, le=10)
    web_fetch_enabled: bool = False
    web_fetch_allowed_domains: Annotated[list[str], NoDecode] = Field(default_factory=list)
    web_fetch_max_chars: int = Field(default=6000, ge=1000, le=20_000)
    web_timeout_seconds: float = Field(default=15.0, gt=0, le=60.0)

    database_url: str = "sqlite:///state/memory.db"
    raw_retention_days: int = Field(default=30, ge=1)
    chat_half_life_days: float = Field(default=14, gt=0)
    interest_half_life_days: float = Field(default=90, gt=0)
    fact_half_life_days: float = Field(default=365, gt=0)

    llm_base_url: str = "https://api.deepseek.com/v1"
    llm_api_key: str = ""
    llm_model: str = "deepseek-chat"
    llm_timeout_seconds: float = Field(default=60.0, gt=0)
    llm_max_output_tokens: int = Field(default=3000, ge=32, le=16_384)
    llm_temperature: float = Field(default=0.7, ge=0.0, le=2.0)
    llm_top_p: float = Field(default=1.0, gt=0.0, le=1.0)
    llm_frequency_penalty: float = Field(default=0.0, ge=-2.0, le=2.0)
    llm_presence_penalty: float = Field(default=0.0, ge=-2.0, le=2.0)

    # Operator-provided speaking style.  The bundled default is the
    # dafeiyu-persona adaptation under personas/dafeiyu/; deployments can point
    # this at any Markdown persona file and its adjacent references/ folder.
    # Optional references/speech-style.md and references/image-style.md let a
    # persona own its voice and optional picture character independently.
    persona_enabled: bool = True
    persona_name: str = "大肥鱼"
    persona_file: Path = Path("personas/dafeiyu/SKILL.md")
    persona_source_url: str = "https://github.com/ERUIHNIYHBKBNF/dafeiyu-persona"
    persona_max_chars: int = Field(default=12_000, ge=500, le=50_000)
    # Deterministic post-checks keep replies human-like.  Legacy dafeiyu output
    # substitutions only apply to the bundled persona; custom personas use
    # neutral fallbacks.
    persona_style_filter: bool = True
    persona_max_emoji: int = Field(default=1, ge=0, le=5)

    # Optional second model endpoint.  It is kept separate from the text
    # model so a single agent can route image requests to an OpenAI-compatible
    # ``/v1/images/generations`` service without changing chat credentials.
    image_model_enabled: bool = False
    image_model_base_url: str = ""
    image_model_api_key: str = ""
    image_model: str = ""
    # Optional backup provider used only after the primary provider fails. The
    # credentials are kept separate so a provider outage does not require
    # changing the primary configuration or exposing either key in logs.
    image_model_fallback_base_url: str = ""
    image_model_fallback_api_key: str = ""
    image_model_fallback: str = ""
    image_model_size: str = "1024x1024"
    image_model_response_format: Literal["auto", "url", "b64_json"] = "auto"
    image_model_timeout_seconds: float = Field(default=120.0, gt=0, le=600.0)

    # Optional direct upload of generated images to Alibaba Cloud OSS. The
    # agent never sends generated bytes through another application gateway.
    image_upload_enabled: bool = False
    oss_region: str = "cn-shenzhen"
    oss_endpoint: str = "https://oss-cn-shenzhen.aliyuncs.com"
    oss_bucket: str = ""
    oss_access_key_id: str = ""
    oss_access_key_secret: str = ""
    oss_prefix: str = "wechat-agent-assets"
    oss_signed_url_ttl_seconds: int = Field(default=7200, ge=300, le=604_800)
    # Set this only when the bucket or CDN is intentionally public. If empty,
    # generated links are short-lived OSS presigned GET URLs.
    oss_public_base_url: str = ""
    image_upload_timeout_seconds: float = Field(default=120.0, gt=0, le=600.0)
    image_upload_max_bytes: int = Field(default=20_000_000, ge=1_000_000, le=100_000_000)
    binding_code_ttl_minutes: int = Field(default=10, ge=1, le=60)

    @field_validator("sticker_frames", mode="before")
    @classmethod
    def parse_sticker_frames(cls, value: object) -> object:
        """Accept ``STICKER_FRAMES=1`` / ``4`` from ``.env`` files.

        ``pydantic-settings`` hands environment values over as strings, and
        recent pydantic releases no longer coerce ``"1"`` into ``Literal[1, 4]``.
        Normalize the digit strings before validation so a hand-written or
        wizard-generated ``.env`` keeps working.
        """

        if isinstance(value, str) and value.strip().isdigit():
            return int(value.strip())
        return value

    @field_validator(
        "wechat_linux_data_dir", "wechat_linux_pid", "wechat_linux_image_xor_key", mode="before"
    )
    @classmethod
    def empty_optional_linux_value(cls, value):
        return None if isinstance(value, str) and not value.strip() else value

    @field_validator(
        "wechat_ai_aliases",
        "wechat_keywords",
        "wechat_allowed_members",
        "wechat_group_names",
        "wechat_private_chat_names",
        "douyin_mention_allowlist",
        "xiaohongshu_mention_allowlist",
        "platform_proactive_keywords",
        "web_fetch_allowed_domains",
        mode="before",
    )
    @classmethod
    def split_csv(cls, value: object) -> object:
        if isinstance(value, str):
            return [part.strip() for part in value.split(",") if part.strip()]
        return value

    @field_validator("proactive_quiet_hours", "platform_quiet_hours", mode="before")
    @classmethod
    def normalize_quiet_hours(cls, value: object, info) -> str:
        label = (
            "安静时段（PROACTIVE_QUIET_HOURS）"
            if info.field_name == "proactive_quiet_hours"
            else "平台静默时段（PLATFORM_QUIET_HOURS）"
        )
        return _normalize_quiet_hours(value, label)

    @model_validator(mode="after")
    def normalize_group_names(self) -> "Settings":
        """Accept both the new list setting and the old singular setting.

        A comma-separated value is deliberately parsed here instead of relying
        on pydantic-settings JSON decoding, so the Web UI and hand-edited
        ``.env`` files can use the same simple ``群A,群B`` format.
        """

        groups: list[str] = []
        # The new plural setting wins when present. This prevents a stale
        # WECHAT_GROUP_NAME line in an older .env from silently adding an
        # unintended listener after the operator switches to the Web UI.
        source_values = self.wechat_group_names or [self.wechat_group_name]
        for value in source_values:
            for item in str(value or "").split(","):
                item = item.strip()
                if item and item not in groups:
                    groups.append(item)
        self.wechat_group_names = groups
        self.wechat_group_name = groups[0] if groups else ""
        return self

    @model_validator(mode="after")
    def validate_runtime_ranges(self) -> "Settings":
        import json
        from zoneinfo import ZoneInfo
        from datetime import time

        ZoneInfo(self.group_timezone)
        mapping = json.loads(self.wechat_linux_chat_ids)
        if not isinstance(mapping, dict) or any(not isinstance(k, str) or not isinstance(v, str) for k, v in mapping.items()):
            raise ValueError("WECHAT_LINUX_CHAT_IDS 必须是名称到稳定 chat_id 的 JSON 对象")
        if self.aliyun_audio_base_url.strip():
            from .speech import audio_endpoint, SpeechError
            try:
                audio_endpoint(self.aliyun_audio_base_url, "")
            except SpeechError as exc:
                raise ValueError("ALIYUN_AUDIO_BASE_URL 必须是以 /api/v1 结尾的原生 API 根地址") from exc
        if self.tts_base_url.strip():
            from urllib.parse import urlsplit
            endpoint = urlsplit(self.tts_base_url.strip())
            if (endpoint.scheme not in {"http", "https"} or not endpoint.hostname
                    or endpoint.username or endpoint.password or endpoint.fragment):
                raise ValueError("TTS_BASE_URL 必须是无凭据的 HTTP(S) 地址")
        if self.media_http_proxy_url.strip():
            from urllib.parse import urlsplit
            proxy = urlsplit(self.media_http_proxy_url.strip())
            if (proxy.scheme not in {"http", "https"} or not proxy.hostname
                    or proxy.username or proxy.password or proxy.query or proxy.fragment
                    or proxy.path not in {"", "/"}):
                raise ValueError("MEDIA_HTTP_PROXY_URL 必须是无凭据的 HTTP(S) 代理地址")
        if self.proactive_window_max_minutes < self.proactive_window_min_minutes:
            raise ValueError("主动窗口最大间隔必须不小于最小间隔")
        if self.proactive_message_max_gap_seconds < self.proactive_message_min_gap_seconds:
            raise ValueError("主动消息最大间隔必须不小于最小间隔")
        # Both quiet-hours fields pass through normalize_quiet_hours() first, so
        # this loop is only a last-resort guard and always carries a readable
        # Chinese message instead of a raw pydantic isoformat error.
        for value, label in (
            (self.proactive_quiet_hours, "安静时段（PROACTIVE_QUIET_HOURS）"),
            (self.platform_quiet_hours, "平台静默时段（PLATFORM_QUIET_HOURS）"),
        ):
            try:
                start, end = value.split("-")
                time.fromisoformat(start)
                time.fromisoformat(end)
            except (ValueError, AttributeError) as exc:
                raise ValueError(f"{label}必须是 HH:MM-HH:MM 格式，例如 23:00-08:00") from exc
        return self

    @property
    def proactive_mode(self) -> str:
        if self.proactive_schedule_enabled and self.wechat_proactive_reply:
            return "mixed"
        return "scheduled" if self.proactive_schedule_enabled else "probability" if self.wechat_proactive_reply else "off"

    @property
    def effective_platform(self) -> str:
        return ("linux" if sys.platform.startswith("linux") else "windows") if self.wechat_platform == "auto" else self.wechat_platform

    @property
    def stop_file(self) -> Path:
        return self.wechat_stop_file

    def half_life_for(self, memory_type: Literal["chat", "interest", "fact"]) -> float:
        return {
            "chat": self.chat_half_life_days,
            "interest": self.interest_half_life_days,
            "fact": self.fact_half_life_days,
        }[memory_type]
