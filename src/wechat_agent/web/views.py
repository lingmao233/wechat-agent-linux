"""Server-rendered views for the compact DSH-style console."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from urllib.parse import urlencode

from ..timefmt import resolve_zone, stamp, zone_label
from . import render
from .queries import EVENT_TYPES, ConsoleData
from .settings_groups import SETTING_SECTIONS, VISIBLE_GROUP_ORDER

PHASE_LABELS = {
    "running": "正在工作",
    "starting": "正在启动",
    "stopping": "正在关闭",
    "stopped": "已关闭",
    "error": "出现错误",
}

NAV = (
    ("overview", "/", "概览", "home"),
    ("memory", "/memory", "回声", "echo"),
    ("platform", "/platform", "平台", "platform"),
    ("logs", "/logs", "日志", "logs"),
    ("settings", "/settings", "设置", "settings"),
)

MEMORY_VIEWS = (
    ("members", "成员画像", "users"),
    ("groups", "群像", "group"),
    ("facts", "记忆片段", "memory"),
    ("events", "关系事件", "event"),
    ("traces", "回答依据", "trace"),
)

VISIBLE_WIDGETS: dict[str, dict[str, object]] = {
    "WECHAT_GROUP_NAMES": {"placeholder": "例如：测试群 A, 测试群 B"},
    "WECHAT_PRIVATE_CHAT_NAMES": {"placeholder": "wxid_friend, 朋友昵称"},
    "WECHAT_AI_ALIASES": {"placeholder": "例如：小鱼, AI"},
    "WECHAT_KEYWORDS": {"placeholder": "例如：问 AI, 帮我看看"},
    "WECHAT_PROACTIVE_PROBABILITY": {"probability": True, "min": 0, "max": 1, "step": 0.01},
    "REACTION_IMAGE_PROBABILITY": {"probability": True, "min": 0, "max": 1, "step": 0.01},
    "VOICE_REPLY_PROBABILITY": {"probability": True, "min": 0, "max": 1, "step": 0.01},
    "LLM_BASE_URL": {"placeholder": "https://api.example.com/v1"},
    "LLM_MODEL": {"placeholder": "deepseek-chat"},
    "EMBED_BASE": {"placeholder": "https://ark.cn-beijing.volces.com/api/v3"},
    "EMBED_MODEL": {"placeholder": "doubao-embedding-vision-251215"},
    "PERSONA_NAME": {"placeholder": "例如：大肥鱼 / 自定义角色名"},
    "PERSONA_FILE": {"placeholder": "personas/dafeiyu/SKILL.md"},
    "TTS_VOICE": {"placeholder": "longanhuan_v3.1"},
    "IMAGE_MODEL": {"placeholder": "gpt-image-1"},
}

VISIBLE_GROUP_HINTS = {
    "微信": "设置群聊入口与私聊边界。私聊仅被动回复，不主动打扰。",
    "回复偏好": "用概率控制主动接话、配图和语音；0 表示不随机触发。",
    "模型": "文字模型使用 OpenAI 兼容接口。",
    "记忆": "配置向量服务；维度由服务端返回值自动探测。",
    "人设": "选择角色名称与本地人设文件。",
    "语音": "控制语音识别和默认合成音色。",
    "绘图 / OSS": "配置生图能力与主模型。",
    "平台": "可选的浏览器平台工具。",
    "训练": "只导出本地语料，不自动训练模型。",
}

QUICK_SWITCHES = (
    ("WECHAT_ALLOW_SEND", "允许发出回复", "关闭时只记录，不发送到微信"),
    ("WECHAT_PROACTIVE_REPLY", "随机接话", "群聊中按概率自然参与"),
    ("REACTION_IMAGES_ENABLED", "角色配图", "允许回复偶尔带一张图"),
    ("VOICE_REPLY_ENABLED", "语音回复", "允许生成原生语音条"),
    ("WEB_SEARCH_ENABLED", "联网搜索", "仅在需要时查询公开资料"),
    ("TRAINING_EXPORT_ENABLED", "每日语料导出", "保存到本地，不训练权重"),
)

# ---------------------------------------------------------------- 平台页常量
PLATFORM_LABELS: dict[str, str] = {"douyin": "抖音", "xiaohongshu": "小红书"}
PLATFORM_SHORTS: dict[str, str] = {"douyin": "抖", "xiaohongshu": "红"}
PLATFORM_MODE_LABELS = {
    "browser": "可见浏览器",
    "dry_run": "dry-run 适配器",
    "unbound": "未绑定账号",
}
PLATFORM_ACTION_LABELS = {
    "comment": "评论",
    "reply": "回复",
    "browse": "浏览",
    "like": "点赞",
}
PLATFORM_ACTION_STATUS_LABELS = {
    "dry_run": "dry-run 计划",
    "queued": "待确认",
    "approved": "已批准",
    "executing": "执行中",
    "sent": "已发送",
    "failed": "发送失败",
    "blocked": "已拦截",
    "skipped": "已跳过",
}
PLATFORM_ACTION_STATUS_KINDS = {
    "dry_run": "amber",
    "queued": "accent",
    "approved": "accent",
    "executing": "accent",
    "sent": "green",
    "failed": "red",
    "blocked": "red",
    "skipped": "",
}
PLATFORM_MEMORY_KIND_LABELS = {
    "browse": "浏览",
    "comment_seen": "看到评论",
    "comment_posted": "已发评论",
    "mention": "@提及",
}
PLATFORM_SECTION_KEYS = (
    "identities",
    "rows",
    "items",
    "bindings",
    "data",
    "actions",
    "memories",
    "comments",
    "results",
)


def _platform_text(value: object, limit: int = 0) -> str:
    text = str(value if value is not None else "").strip()
    if limit > 0 and len(text) > limit:
        return text[: max(1, limit - 1)] + "…"
    return text


def _platform_time_value(value: object) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    elif value is None or value == "":
        return None
    else:
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _platform_first_list(payload: object, keys: tuple[str, ...] = PLATFORM_SECTION_KEYS) -> list:
    """Accept a bare list or the common ``{key: [...]}`` envelopes."""
    if isinstance(payload, list):
        return payload
    if isinstance(payload, tuple):
        return list(payload)
    if isinstance(payload, dict):
        for key in keys:
            value = payload.get(key)
            if isinstance(value, list):
                return value
        for key in keys:
            value = payload.get(key)
            if isinstance(value, dict):
                for sub_key in ("rows", "items", "data", "records"):
                    if isinstance(value.get(sub_key), list):
                        return value[sub_key]
    return []


def _platform_ids(value: object) -> list[str]:
    """Normalize an allowlist/mention payload into unique stable ID strings."""
    if isinstance(value, str):
        value = [part.strip() for part in value.replace("，", ",").split(",")]
    if not isinstance(value, (list, tuple, set)):
        return []
    ids: list[str] = []
    for entry in value:
        if isinstance(entry, dict):
            entry = entry.get("user_id") or entry.get("id") or entry.get("platform_user_id") or ""
        text = str(entry if entry is not None else "").strip()
        if text and text not in ids:
            ids.append(text)
    return ids


def _platform_safe_url(value: object) -> str:
    """Only http(s) URLs may become links; never render javascript:/data:."""
    url = _platform_text(value)
    if url.lower().startswith(("http://", "https://")):
        return url
    return ""



def _proactive_text(status: dict) -> str:
    if not status.get("proactive_enabled"):
        return "已关闭"
    mode = str(status.get("proactive_mode") or "")
    if mode in {"scheduled", "mixed"}:
        minimum = status.get("proactive_window_min_minutes") or 60
        maximum = status.get("proactive_window_max_minutes") or 120
        window = f"{minimum} 分钟" if minimum == maximum else f"{minimum}–{maximum} 分钟"
        return ("随机 + 计划 · " if mode == "mixed" else "计划 · ") + window
    probability = round(float(status.get("proactive_probability") or 0) * 100)
    return f"随机接话 · {probability}%"


def _bool(value: object) -> bool:
    return value is True or str(value).strip().lower() in {"1", "true", "yes", "on"}


class WebConsole:
    def __init__(self, controller):
        self.controller = controller

    @property
    def settings(self):
        return self.controller.settings

    @property
    def storage(self):
        return self.controller._storage

    @property
    def memory_console(self):
        return self.controller.memory_console

    @property
    def runtime(self):
        return self.controller.memory_console.runtime

    def _data(self) -> ConsoleData:
        return ConsoleData(self.storage, self.settings)

    def _now_text(self) -> str:
        return datetime.now(resolve_zone(self.settings.group_timezone)).strftime("%Y-%m-%d %H:%M")

    def _base(self, active: str, status: dict | None = None) -> dict:
        status = status if status is not None else self.controller.status()
        return {
            "active": active,
            "nav": NAV,
            "status": status,
            "phase_text": PHASE_LABELS.get(str(status.get("phase") or ""), str(status.get("phase") or "未知")),
            "now_text": self._now_text(),
            "group_timezone": self.settings.group_timezone,
            "group_timezone_label": zone_label(self.settings.group_timezone),
        }

    def _events(self, limit: int) -> list[dict]:
        rows = []
        for item in self.controller.events(limit):
            rows.append({
                "at_text": self._data().time_text(item.get("at")),
                "level": str(item.get("level") or "INFO"),
                "source": str(item.get("source") or "desktop"),
                "message": str(item.get("message") or ""),
            })
        return rows

    def _config_values(self) -> dict:
        try:
            return dict(self.controller.config_snapshot().get("values") or {})
        except Exception:
            return {}

    @staticmethod
    def _group_scopes(scopes: list[dict]) -> list[dict]:
        return [scope for scope in scopes if scope.get("conversation_type", "group") == "group"]

    def overview(self) -> str:
        status = self.controller.status()
        summary = self._data().overview()
        config = self._config_values()
        stats = status.get("stats") or {}
        context = self._base("overview", status)
        context.update({
            "title": "概览",
            "hero_hint": status.get("last_error") or (
                "正在留意「%s」的新消息" % (status.get("group_name") or "未设置群聊")
                if status.get("thread_alive") else "启动后，助手会开始处理新消息。"
            ),
            "group_summary": status.get("group_name") or "尚未设置",
            "send_summary": "允许发送" if status.get("send_enabled") else "仅记录",
            "private_summary": (
                "白名单被动回复" if status.get("private_reply_enabled") else "私聊已关闭"
            ),
            "proactive_summary": _proactive_text(status),
            "training_summary": (
                "每天 %02d:%02d" % (
                    int(status.get("training_export_hour") or 23),
                    int(status.get("training_export_minute") or 30),
                ) if status.get("training_export_enabled") else "未开启"
            ),
            "today_replies": summary["today_replies"],
            "total_sent": summary["total_sent"],
            "stats": {
                "seen": int(stats.get("seen") or 0),
                "replied": int(stats.get("replied") or 0),
                "failed": int(stats.get("failed") or 0),
                "duplicate": int(stats.get("duplicate_questions") or 0),
            },
            "quick_switches": [
                {"env": env, "label": label, "hint": hint, "value": _bool(config.get(env))}
                for env, label, hint in QUICK_SWITCHES
            ],
            "last_error": status.get("last_error") or "",
        })
        return render("overview.html", **context)

    def members(self) -> str:
        return self.memory({"view": "members"})

    def member(self, member_id: str, query: dict) -> str:
        data = self._data()
        scopes = self._group_scopes(data.scopes(self.memory_console))
        detail = data.member_detail(
            self.runtime, scopes, member_id,
            account_id=str(query.get("account_id") or ""), group_id=str(query.get("group_id") or ""),
        )
        if detail is None:
            raise KeyError(member_id)
        return self.memory({
            **query, "view": "members", "focus_member": member_id,
            "account_id": detail["scope"]["account_id"], "group_id": detail["scope"]["group_id"],
        })

    def groups(self) -> str:
        return self.memory({"view": "groups"})

    def group(self, group_id: str, query: dict) -> str:
        data = self._data()
        scopes = self._group_scopes(data.scopes(self.memory_console))
        if data.select_scope(scopes, account_id=str(query.get("account_id") or ""), group_id=group_id) is None:
            raise KeyError(group_id)
        return self.memory({**query, "view": "groups", "group_id": group_id})

    def memory(self, query: dict) -> str:
        data = self._data()
        all_scopes = data.scopes(self.memory_console)
        group_scopes = self._group_scopes(all_scopes)
        view = str(query.get("view") or "members")
        if view not in {item[0] for item in MEMORY_VIEWS}:
            view = "members"
        available = group_scopes if view in {"members", "groups"} else all_scopes
        requested_account = str(query.get("account_id") or "")
        requested_group = str(query.get("group_id") or "")
        scope = data.select_scope(available, account_id=requested_account, group_id=requested_group)
        if scope is None and (view != "members" or requested_group):
            scope = available[0] if available else None
        selected_account = (
            scope["account_id"] if scope is not None
            else requested_account or (available[0]["account_id"] if available else "")
        )
        visible_scopes = [item for item in available if not selected_account or item["account_id"] == selected_account]
        if view == "members" and not requested_group:
            scope = None

        tabs = []
        for key, label, icon in MEMORY_VIEWS:
            target_scopes = group_scopes if key in {"members", "groups"} else all_scopes
            target_scope = scope if scope in target_scopes else None
            if target_scope is None:
                target_scope = next((item for item in target_scopes if item["account_id"] == selected_account), None)
            if target_scope is None and target_scopes:
                target_scope = target_scopes[0]
            params = {"view": key}
            if target_scope is not None:
                params.update(account_id=target_scope["account_id"], group_id=target_scope["group_id"])
            elif selected_account:
                params["account_id"] = selected_account
            tabs.append({"key": key, "label": label, "icon": icon, "url": "/memory?" + urlencode(params), "active": key == view})

        context = self._base("memory")
        context.update({
            "title": "回声",
            "view": view,
            "tabs": tabs,
            "scopes": visible_scopes,
            "scope": scope,
            "accounts": sorted({item["account_id"] for item in available}),
            "selected_account": selected_account,
            "scope_count": len(available),
            "member_groups": [],
            "total_members": 0,
            "member_detail": None,
            "group_detail": None,
            "emotion": None,
            "filters": {"member_id": str(query.get("member_id") or ""), "memory_type": str(query.get("memory_type") or "")},
            "event_filters": {"event_type": str(query.get("event_type") or ""), "member_id": str(query.get("event_member") or "")},
            "event_types": EVENT_TYPES,
            "facts": {"rows": [], "types": [], "members": [], "total": 0},
            "events": [],
            "traces": [],
            "trace_detail": None,
            "trace_json": "",
            "selected_trace": "",
            "source": None,
            "source_identifier": str(query.get("source") or ""),
            "coverage": None,
            "warnings": [],
        })
        if not available:
            return render("memory.html", **context)

        if view == "members":
            member_scopes = [scope] if scope is not None else visible_scopes
            context["member_groups"] = data.members(self.runtime, member_scopes)
            context["total_members"] = sum(item["member_count"] for item in context["member_groups"])
            focus = str(query.get("focus_member") or "")
            if focus and scope is not None:
                detail = data.member_detail(
                    self.runtime, available, focus,
                    account_id=scope["account_id"], group_id=scope["group_id"],
                )
                if detail is None:
                    raise KeyError(focus)
                context["member_detail"] = detail
        elif view == "groups" and scope is not None:
            context["group_detail"] = data.group(
                self.memory_console, available, scope["group_id"], account_id=scope["account_id"]
            )
            context["emotion"] = data.emotion(self.runtime, scope["account_id"], scope["group_id"])
        elif scope is not None:
            account_id, group_id = scope["account_id"], scope["group_id"]
            if view == "facts":
                context["facts"] = data.facts(
                    scope, member_id=context["filters"]["member_id"], memory_type=context["filters"]["memory_type"]
                )
            elif view == "events":
                event_types = [context["event_filters"]["event_type"]] if context["event_filters"]["event_type"] else []
                try:
                    raw_events = self.runtime.recent_events(
                        account_id, group_id,
                        member_id=context["event_filters"]["member_id"],
                        event_types=event_types, include_inactive=True, limit=50,
                    )
                    context["events"] = data.event_views(raw_events)
                except Exception:
                    context["events"] = []
            elif view == "traces":
                context["traces"] = data.recent_traces(scope)
                selected = str(query.get("trace") or (context["traces"][0]["request_id"] if context["traces"] else ""))
                context["selected_trace"] = selected
                context["trace_detail"] = data.trace(self.runtime, scope, selected) if selected else None
                context["trace_json"] = json.dumps(context["trace_detail"], ensure_ascii=False, indent=2, default=str) if context["trace_detail"] else ""

        if scope is not None:
            if context["source_identifier"]:
                context["source"] = data.source(scope, context["source_identifier"])
            analysis = scope.get("analysis") or {}
            features = scope.get("features") or {}
            context["coverage"] = {
                "message_count": scope["message_count"],
                "done": int(analysis.get("done") or 0),
                "pending": int(analysis.get("pending") or 0),
                "running": int(analysis.get("running") or 0),
                "failed": int(analysis.get("failed") or 0),
                "features": {
                    "retrieval": bool(features.get("retrieval")),
                    "event_extraction": bool(features.get("event_extraction")),
                    "semantic_profile": bool(features.get("semantic_profile")),
                },
            }
            if context["coverage"]["failed"]:
                context["warnings"].append(f"有 {context['coverage']['failed']} 项分析任务失败，可以重试。")
        return render("memory.html", **context)

    def settings_page(self, query: dict) -> str:
        snapshot = self.controller.config_snapshot()
        fields = list(snapshot.get("fields") or [])
        values = snapshot.get("values") or {}
        secrets = snapshot.get("configured_secrets") or {}
        section = str(query.get("section") or "general")
        valid_sections = {item[0] for item in SETTING_SECTIONS} | {"advanced"}
        if section not in valid_sections:
            section = "general"
        required_groups = next((item[2] for item in SETTING_SECTIONS if item[0] == section), ())

        visible_groups = []
        for group in VISIBLE_GROUP_ORDER:
            if group not in required_groups:
                continue
            members = [field for field in fields if field.get("visible") and field.get("group") == group]
            if members:
                visible_groups.append({
                    "name": group,
                    "hint": VISIBLE_GROUP_HINTS.get(group, ""),
                    "fields": [self._field(field, values, secrets, VISIBLE_WIDGETS) for field in members],
                })

        categories: dict[str, list] = {}
        for field in fields:
            if field.get("advanced"):
                categories.setdefault(str(field.get("category") or "其他"), []).append(field)
        selected_category = str(query.get("category") or (next(iter(categories), "")))
        if selected_category not in categories:
            selected_category = next(iter(categories), "")
        base_params = {"section": section}
        if section == "advanced" and selected_category:
            base_params["category"] = selected_category
        advanced_groups = []
        if section == "advanced" and selected_category:
            members = categories[selected_category]
            advanced_groups.append({
                "name": selected_category,
                "count": len(members),
                "fields": [self._field(field, values, secrets, {}) for field in members],
            })
        settings_tabs = [
            {"key": key, "label": label, "url": "/settings?" + urlencode({"section": key}), "active": section == key}
            for key, label, _ in SETTING_SECTIONS
        ]
        settings_tabs.append({"key": "advanced", "label": "高级配置", "url": "/settings?section=advanced", "active": section == "advanced"})
        advanced_tabs = [
            {
                "name": name, "count": len(members), "active": name == selected_category,
                "url": "/settings?" + urlencode({"section": "advanced", "category": name}),
            }
            for name, members in categories.items()
        ]
        context = self._base("settings")
        context.update({
            "title": "设置",
            "config_path": str(snapshot.get("path") or ""),
            "visible_groups": visible_groups,
            "advanced_groups": advanced_groups,
            "settings_tabs": settings_tabs,
            "advanced_tabs": advanced_tabs,
            "selected_section": section,
            "form_action": "/settings?" + urlencode(base_params),
            "show_private": any(group["name"] == "微信" for group in visible_groups),
            "visible_count": sum(1 for field in fields if field.get("visible")),
            "advanced_count": sum(1 for field in fields if field.get("advanced")),
            "total_count": len(fields),
            "saved": str(query.get("saved") or "") == "1",
            "error": str(query.get("error") or ""),
        })
        return render("settings.html", **context)

    @staticmethod
    def _field(field: dict, values: dict, secrets: dict, widgets: dict) -> dict:
        env = str(field.get("env") or "")
        return {
            "env": env,
            "key": str(field.get("key") or ""),
            "label": str(field.get("label") or env),
            "type": str(field.get("type") or "text"),
            "description": str(field.get("description") or ""),
            "secret": bool(field.get("secret")),
            "configured": bool(secrets.get(env)),
            "options": list(field.get("options") or []),
            "value": values.get(env, ""),
            "widget": widgets.get(env, {}),
            "group": str(field.get("group") or ""),
            "category": str(field.get("category") or ""),
        }

    def logs(self) -> str:
        data = self._data()
        try:
            statuses = self.storage.processing_summary()
        except Exception:
            statuses = {}
        context = self._base("logs")
        context.update({
            "title": "日志",
            "events": self._events(60),
            "replies": data.reply_ledger(100),
            "audits": data.audit_ledger(100),
            "statuses": statuses,
            "platforms": self.controller.platform_status(),
            "platform_actions": self.controller.platform_actions(20),
        })
        return render("logs.html", **context)

    # ------------------------------------------------------------- 平台页
    def _platform_time(self, value: object) -> str:
        parsed = _platform_time_value(value)
        if parsed is None:
            return ""
        return stamp(parsed, timezone_name=self.settings.group_timezone)

    def _platform_call(self, name: str, *args, **kwargs) -> tuple[object, bool]:
        """Call an optional controller method; tolerate a not-yet-merged backend."""
        method = getattr(self.controller, name, None)
        if not callable(method):
            return None, False
        try:
            return method(*args, **kwargs), True
        except TypeError:
            try:
                return method(*args), True
            except Exception:
                return None, True
        except Exception:
            # The endpoint exists but failed: render the empty state instead of
            # taking the whole page down.
            return None, True

    def _platform_status_cards(self) -> list[dict]:
        statuses: dict = {}
        try:
            raw = self.controller.platform_status()
            if isinstance(raw, dict):
                statuses = raw
        except Exception:
            statuses = {}
        cards = []
        for platform, label in PLATFORM_LABELS.items():
            info = statuses.get(platform)
            if not isinstance(info, dict):
                info = {}
            mode = _platform_text(info.get("mode")) or "未配置"
            connected = bool(info.get("connected"))
            logged_in = bool(info.get("logged_in"))
            challenge = _platform_text(info.get("challenge"))
            last_error = _platform_text(info.get("last_error"))
            if challenge:
                status_text, status_kind = "需要人工处理", "red"
            elif last_error:
                status_text, status_kind = "异常", "red"
            elif logged_in:
                status_text, status_kind = "已登录", "green"
            elif connected:
                status_text, status_kind = "已打开 · 待扫码", "amber"
            else:
                status_text, status_kind = "未登录", ""
            cards.append({
                "platform": platform,
                "label": label,
                "short": PLATFORM_SHORTS.get(platform, label[:1]),
                "mode": mode,
                "mode_label": PLATFORM_MODE_LABELS.get(mode, mode),
                "account_id": _platform_text(info.get("account_id")) or "unbound",
                "profile_dir": _platform_text(info.get("profile_dir")),
                "connected": connected,
                "connected_text": "已打开" if connected else "未打开",
                "logged_in": logged_in,
                "login_text": "已登录" if logged_in else "未登录",
                "status_text": status_text,
                "status_kind": status_kind,
                "write_enabled": bool(info.get("write_enabled")),
                "requires_manual_login": bool(info.get("requires_manual_login")),
                "challenge": challenge,
                "last_error": last_error,
                "mention_ids": _platform_ids(info.get("mention_allowlist_ids")),
            })
        return cards

    @staticmethod
    def _platform_identity_rows(payload: object) -> list[dict]:
        rows = []
        for entry in _platform_first_list(payload):
            if not isinstance(entry, dict):
                entry = {"id": entry}
            row_id = entry.get("id")
            if row_id is None:
                row_id = entry.get("identity_id") or entry.get("binding_id") or ""
            platform = _platform_text(entry.get("platform"))
            rows.append({
                "id": _platform_text(row_id),
                "platform": platform,
                "platform_label": PLATFORM_LABELS.get(platform, platform or "未知平台"),
                "platform_user_id": _platform_text(
                    entry.get("platform_user_id") or entry.get("user_id") or entry.get("platform_uid")
                ),
                "platform_user_name": _platform_text(
                    entry.get("platform_user_name") or entry.get("user_name") or entry.get("display_name")
                ),
                "member_id": _platform_text(entry.get("member_id") or entry.get("wxid")),
                "member_name": _platform_text(entry.get("member_name")),
                "status": _platform_text(entry.get("status")),
            })
        return rows

    @staticmethod
    def _platform_allowlist(payload: object, fallback: dict[str, list[str]]) -> list[dict]:
        raw: dict = {}
        if isinstance(payload, dict):
            for key in (
                "mention_allowlist",
                "mention_allowlists",
                "allowlists",
                "effective_mention_allowlists",
                "allowlist_ids",
            ):
                value = payload.get(key)
                if isinstance(value, dict):
                    raw = value
                    break
        merged: dict[str, list[str]] = {}
        for platform in PLATFORM_LABELS:
            merged[platform] = list(fallback.get(platform) or [])
        for platform, value in raw.items():
            key = _platform_text(platform)
            if not key:
                continue
            if isinstance(value, dict):
                for sub_key in ("ids", "user_ids", "targets", "allowlist_ids"):
                    if sub_key in value:
                        value = value[sub_key]
                        break
            ids = _platform_ids(value)
            if ids:
                merged[key] = ids
        order = list(PLATFORM_LABELS) + [key for key in merged if key not in PLATFORM_LABELS]
        return [
            {
                "platform": platform,
                "label": PLATFORM_LABELS.get(platform, platform),
                "ids": merged.get(platform) or [],
            }
            for platform in order
        ]

    def _platform_actions_view(self) -> tuple[list[dict], bool]:
        method = getattr(self.controller, "platform_actions", None)
        if not callable(method):
            return [], False
        try:
            raw = method(50)
        except TypeError:
            try:
                raw = method()
            except Exception:
                return [], True
        except Exception:
            return [], True
        rows = []
        for entry in _platform_first_list(raw):
            if not isinstance(entry, dict):
                continue
            platform = _platform_text(entry.get("platform"))
            status = _platform_text(entry.get("status"))
            dry_run = bool(entry.get("dry_run"))
            rows.append({
                "id": _platform_text(entry.get("id")),
                "action_key": _platform_text(entry.get("action_key")),
                "platform": platform,
                "platform_label": PLATFORM_LABELS.get(platform, platform or "未知"),
                "action_type": _platform_text(entry.get("action_type")),
                "action_label": PLATFORM_ACTION_LABELS.get(
                    _platform_text(entry.get("action_type")), _platform_text(entry.get("action_type")) or "动作"
                ),
                "target_id": _platform_text(entry.get("target_id") or entry.get("parent_comment_id")),
                "target_url": _platform_safe_url(entry.get("target_url")),
                "text": _platform_text(entry.get("text")),
                "status": status,
                "status_label": PLATFORM_ACTION_STATUS_LABELS.get(status, status or "未知"),
                "status_kind": PLATFORM_ACTION_STATUS_KINDS.get(status, ""),
                "reason": _platform_text(entry.get("reason")),
                "dry_run": dry_run,
                "dry_run_text": "dry-run" if dry_run else "真实",
                "can_execute": status in {"queued", "approved"} and not dry_run,
                "time_text": self._platform_time(entry.get("updated_at") or entry.get("created_at")),
            })
        return rows, True

    def _platform_memories_view(self, query: dict) -> tuple[list[dict], bool]:
        filters = {
            "limit": 50,
            "member_id": _platform_text(query.get("member_id")),
            "platform": _platform_text(query.get("platform")),
        }
        method = getattr(self.controller, "platform_memories", None)
        if not callable(method):
            return [], False
        payload = None
        for attempt in (
            lambda: method(dict(filters)),
            lambda: method(**{key: value for key, value in filters.items() if value}),
            lambda: method(),
        ):
            try:
                payload = attempt()
                break
            except TypeError:
                continue
            except Exception:
                payload = None
                break
        groups: dict[tuple[str, str], dict] = {}
        for entry in _platform_first_list(payload):
            if not isinstance(entry, dict):
                continue
            platform = _platform_text(entry.get("platform"))
            member_id = _platform_text(entry.get("member_id"))
            member_name = _platform_text(entry.get("member_name"))
            kind = _platform_text(entry.get("kind"))
            url = _platform_safe_url(entry.get("item_url") or entry.get("url"))
            key = (member_id, platform)
            group = groups.setdefault(key, {
                "member_id": member_id,
                "member_name": member_name,
                "platform": platform,
                "platform_label": PLATFORM_LABELS.get(platform, platform or "未知平台"),
                "rows": [],
            })
            if not group["member_name"] and member_name:
                group["member_name"] = member_name
            group["rows"].append({
                "kind": kind,
                "kind_label": PLATFORM_MEMORY_KIND_LABELS.get(kind, kind or "记录"),
                "kind_kind": "accent" if kind == "comment_posted" else "",
                "item_id": _platform_text(entry.get("item_id")),
                "title": _platform_text(entry.get("title"), 120),
                "text": _platform_text(entry.get("text"), 220),
                "query": _platform_text(entry.get("query")),
                "url": url,
                "time_text": self._platform_time(entry.get("created_at") or entry.get("updated_at")),
            })
        views = []
        for group in groups.values():
            if not group["member_name"]:
                group["member_name"] = group["member_id"] or "未关联成员"
            views.append(group)
        views.sort(key=lambda item: (item["platform_label"], item["member_name"]))
        return views, True

    def _platform_proactive_view(self, config: dict) -> dict:
        payload, available = self._platform_call("platform_proactive_status")
        if isinstance(payload, dict):
            data = payload
        elif isinstance(payload, list):
            data = {"recent_results": payload}
        else:
            data = {}
        enabled = data.get("enabled")
        interval = data.get("interval_minutes")
        keywords = data.get("keywords")
        quiet_hours = data.get("quiet_hours")
        last_run = data.get("last_run_at") or data.get("last_run") or data.get("last_run_time")
        last_result = data.get("last_result") or data.get("last_message") or data.get("last_summary")
        if not data:
            # Backend not merged yet: still show the effective .env values.
            enabled = config.get("PLATFORM_PROACTIVE_ENABLED")
            interval = config.get("PLATFORM_PROACTIVE_INTERVAL_MINUTES")
            keywords = config.get("PLATFORM_PROACTIVE_KEYWORDS")
            quiet_hours = config.get("PLATFORM_QUIET_HOURS")
        if isinstance(keywords, str):
            keywords = [part.strip() for part in keywords.replace("，", ",").split(",") if part.strip()]
        elif isinstance(keywords, (list, tuple)):
            keywords = [str(part).strip() for part in keywords if str(part).strip()]
        else:
            keywords = []
        if not keywords:
            effective = data.get("effective_keywords")
            if isinstance(effective, (list, tuple)):
                keywords = [str(part).strip() for part in effective if str(part).strip()]
        recent = []
        for entry in _platform_first_list(data, ("recent_results", "results", "recent")):
            if isinstance(entry, dict):
                text = entry.get("text") or entry.get("summary") or entry.get("title") or entry.get("message")
                if not text:
                    parts = []
                    for key in ("reason", "platform", "query", "items", "dry_run"):
                        value = entry.get(key)
                        if value not in (None, ""):
                            parts.append(f"{key}={value}")
                    text = " · ".join(parts)
                at = entry.get("at") or entry.get("created_at") or entry.get("time") or entry.get("started_at")
                recent.append({
                    "text": _platform_text(text, 200) or "已运行",
                    "time_text": self._platform_time(at),
                })
            else:
                recent.append({"text": _platform_text(entry, 200) or "已运行", "time_text": ""})
        return {
            "available": available,
            "enabled": _bool(enabled),
            "enabled_text": "已开启" if _bool(enabled) else "已关闭",
            "interval_text": f"{int(interval)} 分钟" if interval not in (None, "") and str(interval).strip() else "—",
            "keywords": keywords,
            "keywords_text": "、".join(keywords) if keywords else "未设置（使用人设 / 成员兴趣记忆取词）",
            "quiet_hours": _platform_text(quiet_hours) or "—",
            "last_run_text": self._platform_time(last_run) or "还没有运行记录",
            "last_result_text": _platform_text(last_result, 300),
            "recent": recent[:5],
            "last_error": _platform_text(data.get("last_error")),
        }

    def platform(self, query: dict | None = None) -> str:
        """抖音 / 小红书 / 微信三号互联控制台页。"""
        query = dict(query or {})
        config = self._config_values()
        accounts = self._platform_status_cards()
        identity_payload, identities_available = self._platform_call("platform_identities")
        identities = self._platform_identity_rows(identity_payload)
        fallback_allowlist = {card["platform"]: card["mention_ids"] for card in accounts}
        allowlist = self._platform_allowlist(identity_payload, fallback_allowlist)
        actions, actions_available = self._platform_actions_view()
        memories, memories_available = self._platform_memories_view(query)
        proactive = self._platform_proactive_view(config)
        write_enabled = _bool(config.get("PLATFORM_WRITE_ENABLED")) or any(
            card["write_enabled"] for card in accounts
        )
        identity_platforms = {row["platform"] for row in identities}
        context = self._base("platform")
        context.update({
            "title": "平台",
            "platform_accounts": accounts,
            "platform_allowlist": allowlist,
            "platform_identities": identities,
            "identities_available": identities_available,
            "identity_platforms": identity_platforms,
            "platform_actions": actions,
            "actions_available": actions_available,
            "platform_memory_groups": memories,
            "memories_available": memories_available,
            "platform_memory_total": sum(len(group["rows"]) for group in memories),
            "memory_filter": {
                "member_id": _platform_text(query.get("member_id")),
                "platform": _platform_text(query.get("platform")),
            },
            "platform_proactive": proactive,
            "write_enabled": write_enabled,
            "write_state_text": (
                "写开关已开启 · 真实发送仍需台账确认"
                if write_enabled
                else "写开关关闭 · 只生成 dry-run 计划"
            ),
            "platform_settings_url": "/settings?section=workspace",
            "platform_action_types": PLATFORM_ACTION_LABELS,
        })
        return render("platform.html", **context)

    def members_payload(self) -> dict:
        data = self._data()
        scopes = self._group_scopes(data.scopes(self.memory_console))
        members = []
        for scope in data.members(self.runtime, scopes):
            for member in scope["members"]:
                members.append({
                    "account_id": scope["account_id"],
                    "group_id": scope["group_id"],
                    "group_name": scope["group_name"],
                    "member_id": member["member_id"],
                    "member_name": member["member_name"],
                    "affinity": member["affinity"],
                    "mode": member["mode_label"],
                    "profile": member["profile"]["raw"],
                    "profile_fields": member["profile"]["fields"],
                    "recent_events": member["recent_events"],
                    "url": member["url"],
                })
        return {"generated_at": datetime.now(timezone.utc).isoformat(), "members": members}

    def groups_payload(self) -> dict:
        data = self._data()
        scopes = self._group_scopes(data.scopes(self.memory_console))
        groups = []
        for group in data.groups(self.memory_console, scopes):
            groups.append({
                "account_id": group["account_id"],
                "group_id": group["group_id"],
                "group_name": group["group_name"],
                "message_count": group["message_count"],
                "last_active_text": group["last_active_text"],
                "semantic_version": group["semantic"]["version"],
                "interests": group["semantic"]["interests"],
                "atmosphere": group["semantic"]["atmosphere"],
                "memes": group["semantic"]["memes"],
                "style": group["style"],
                "ai_style": group["ai"],
                "timeline": group["timeline"],
                "timeline_available": group["timeline_available"],
                "url": group["url"],
            })
        return {"generated_at": datetime.now(timezone.utc).isoformat(), "groups": groups}

    def login_page(self, error: str = "") -> str:
        return render("login.html", error=error)

    def not_found(self, message: str, status_code: int = 404) -> str:
        return render("error.html", status_code=status_code, message=message)
