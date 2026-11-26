"""Visible-browser reader/writer for Douyin private messages.

Douyin's web chat (``https://www.douyin.com/chat``) exposes no message ids and
renders a virtualized list.  The adapter therefore:

* keeps one tab in the *same* persistent browser context as the public
  platform connector, so the operator scans a QR code once;
* reads only the visible conversation and returns bounded, classified rows;
* derives a content fingerprint for dedupe instead of pretending an id exists;
* never clicks a challenge page and never invents a sender identity.

Selectors live here because Douyin changes its DOM far more often than the
bridge does.  ``calibrate()`` records the class fragments actually present on
the operator's account, which is the first thing to look at after a redesign.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from datetime import datetime
from pathlib import Path
import threading
import time
from typing import Any
from urllib.parse import urlparse

from .models import ChatMessage
from .intent import extract_aweme_ref, normalize_text

log = logging.getLogger(__name__)

CHAT_PAGE = "https://www.douyin.com/chat"

_LOGIN_MARKERS = ("扫码登录", "登录后", "请先登录", "Log in", "Sign in")
_CHALLENGE_MARKERS = ("验证码", "安全验证", "行为异常", "访问受限", "IP存在风险", "captcha", "verify")

_JS_ROWS = r"""
(limit) => {
  const zone = document.querySelector('[class*="messageMessageListlist"]')
    || document.querySelector('[class*="messageMessageListwrapper"]')
    || document.querySelector('[class*="messageMessageList"]');
  if (!zone) return [];
  let rows = [...zone.querySelectorAll('[class*="messageMessageBoxmessageBox"]')];
  if (!rows.length) rows = [...zone.querySelectorAll('[class*="messageMessageBox"]')];
  if (!rows.length) rows = [...zone.children];
  const out = [];
  for (const r of rows) {
    if (out.length >= limit) break;
    const textEl = r.querySelector('[class*="TextMessageTexttextInnerContent"], [class*="TextMessageTextpureText"], [class*="bubbleTextContent"], [class*="textInnerContent"]');
    const text = ((textEl && (textEl.innerText || textEl.textContent)) || '').trim();
    const whoEl = r.querySelector('[class*="MessageBoxMessageTitleavatarName"], [class*="avatarName"], [class*="MessageTitle"]');
    const who = ((whoEl && whoEl.innerText) || '').trim().slice(0, 48);
    const imgs = [...r.querySelectorAll('img')].filter((e) => {
      const w = e.getBoundingClientRect().width;
      const cls = String(e.className || '');
      const src = String(e.currentSrc || e.src || '');
      return w >= 56 && !/avatar|Avatar|icon|Icon|commonStreak|ConversationIcon|aweme-avatar|~c5_\d+x\d+/.test(cls + ' ' + src);
    });
    let image = '';
    if (imgs.length) {
      imgs.sort((a, b) => b.getBoundingClientRect().width - a.getBoundingClientRect().width);
      image = String(imgs[0].currentSrc || imgs[0].src || '');
    }
    const links = [...r.querySelectorAll('a[href]')].map((a) => String(a.href || '')).filter(Boolean).slice(0, 8);
    const attrs = [];
    for (const e of [...r.querySelectorAll('*')].slice(0, 120)) {
      for (const a of [...e.attributes]) {
        if (/aweme|video|modal_id|item_id|data-id/i.test(a.name + '=' + a.value)) attrs.push(String(a.value));
      }
    }
    let self = false;
    try {
      const rr = r.getBoundingClientRect();
      const target = textEl || imgs[0] || r;
      const tr = target.getBoundingClientRect();
      self = (tr.x + tr.width / 2) > (rr.x + rr.width / 2 + 20);
    } catch (e) { self = false; }
    const cls = String(r.className || '');
    const html = (r.innerHTML || '').slice(0, 6000);
    const rowText = (r.innerText || '').trim().slice(0, 500);
    const duration = (rowText.match(/\b\d{1,2}:\d{2}\b/) || [''])[0];
    const card = /share|Share|card|Card|video|Video/.test(cls) || /aweme|video|modal_id/i.test(html);
    const play = /play|Play|video|Video/.test(cls) || /play|Play/.test(html)
      || Boolean(r.querySelector('[class*="play"], [class*="Play"], [aria-label*="播放"]'));
    out.push({ who, text, image, links, attrs: attrs.slice(0, 20), self, cls: cls.slice(0, 300), card, play, rowText, duration });
  }
  return out;
}
"""

_JS_CONVERSATIONS = r"""
() => {
  const rows = [...document.querySelectorAll('[class*="conversationConversationItemwrapper"]')];
  return rows.slice(0, 80).map((r) => {
    const titleEl = r.querySelector('[class*="conversationConversationItemtitle"]');
    const name = ((titleEl && titleEl.innerText) || '').trim().split('\n')[0].trim();
    const text = (r.innerText || '').replace(/\n+/g, ' | ').trim();
    const unread = (r.innerText || '').match(/(?:^|\n)(\d{1,3})(?:\n|$)/);
    return { name, preview: text.slice(0, 240), unread: unread ? Number(unread[1]) : 0 };
  }).filter((x) => x.name);
}
"""

_JS_ACTIVE = r"""
() => {
  const rows = [...document.querySelectorAll('[class*="conversationConversationItemwrapper"]')];
  const active = rows.find((r) => /curConversation/i.test(String(r.className || '')));
  if (!active) return '';
  const titleEl = active.querySelector('[class*="conversationConversationItemtitle"]');
  const text = ((titleEl && titleEl.innerText) || active.innerText || '').trim();
  const clean = text
    .split('\n')[0]
    .replace(/\s*(?:刚刚|\d+分钟前|昨天(?:\s*\d{1,2}:\d{2})?|前天(?:\s*\d{1,2}:\d{2})?|\d{1,2}:\d{2})\s*$/, '')
    .trim();
  return clean;
}
"""

_JS_FIND_CHAT = r"""
(want) => {
  const norm = (s) => (s || '')
    .replace(/\s*\(\d+\)\s*$/, '')
    .replace(/\s*(?:刚刚|\d+分钟前|昨天(?:\s*\d{1,2}:\d{2})?|前天(?:\s*\d{1,2}:\d{2})?|\d{1,2}:\d{2})\s*$/, '')
    .trim();
  const rows = [...document.querySelectorAll('[class*="conversationConversationItemwrapper"]')];
  for (const el of rows) {
    const titleEl = el.querySelector('[class*="conversationConversationItemtitle"]');
    const first = ((titleEl && titleEl.innerText) || '').trim().split('\n')[0].trim();
    if (first === want || norm(first) === want) {
      el.scrollIntoView({ block: 'center' });
      el.setAttribute('data-bridge-open', '1');
      const r = el.getBoundingClientRect();
      if (r.width > 0 && r.height > 0) {
        return { ok: true, x: r.x + Math.min(120, r.width / 2), y: r.y + Math.min(28, r.height / 2), name: first };
      }
    }
  }
  return {
    ok: false,
    names: rows.slice(0, 12).map((e) => {
      const titleEl = e.querySelector('[class*="conversationConversationItemtitle"]');
      return ((titleEl && titleEl.innerText) || '').trim().split('\n')[0] || '';
    }),
  };
}
"""

_JS_RESET_CHAT_SCROLL = r"""
() => {
  const rows = [...document.querySelectorAll('[class*="conversationConversationItemwrapper"]')];
  if (!rows.length) return false;
  let node = rows[0].parentElement;
  while (node && node !== document.documentElement) {
    const style = getComputedStyle(node);
    if (node.scrollHeight > node.clientHeight + 20 && /(auto|scroll)/.test(style.overflowY)) {
      node.scrollTop = 0;
      return true;
    }
    node = node.parentElement;
  }
  return false;
}
"""

_JS_SCROLL_CHAT_LIST = r"""
() => {
  const rows = [...document.querySelectorAll('[class*="conversationConversationItemwrapper"]')];
  if (!rows.length) return false;
  let node = rows[0].parentElement;
  while (node && node !== document.documentElement) {
    const style = getComputedStyle(node);
    if (node.scrollHeight > node.clientHeight + 20 && /(auto|scroll)/.test(style.overflowY)) {
      const before = node.scrollTop;
      node.scrollTop = Math.min(node.scrollHeight, before + Math.max(240, node.clientHeight * 0.8));
      return node.scrollTop !== before;
    }
    node = node.parentElement;
  }
  return false;
}
"""

_JS_SET_CHAT_SCROLL = r"""
(pos) => {
  const el = document.querySelector('[class*="conversationConversationListwrapper"]');
  if (!el) return { ok: false };
  el.scrollTop = pos;
  return { ok: true, top: el.scrollTop, height: el.scrollHeight, client: el.clientHeight };
}
"""

_JS_SEARCH_RESULT = r"""
(want) => {
  const nodes = [...document.querySelectorAll('div,li,button,a,span')];
  const candidates = [];
  for (const el of nodes) {
    const text = (el.innerText || '').trim();
    if (!text) continue;
    const first = text.split('\n')[0].trim();
    if (first !== want) continue;
    const r = el.getBoundingClientRect();
    if (r.width < 40 || r.height < 16 || r.bottom <= 0 || r.top >= innerHeight) continue;
    candidates.push({ el, area: r.width * r.height, x: r.x + Math.min(120, r.width / 2), y: r.y + Math.min(28, r.height / 2) });
  }
  candidates.sort((a, b) => a.area - b.area);
  if (!candidates.length) return { ok: false };
  const chosen = candidates[0];
  chosen.el.setAttribute('data-bridge-search', '1');
  return { ok: true, x: chosen.x, y: chosen.y };
}
"""

_JS_EDITOR = r"""
() => {
  const ed = [...document.querySelectorAll('div[contenteditable="true"]')]
    .find((e) => /messageEditor|editor-kit-container/.test(String(e.className || '')));
  if (!ed) return null;
  const r = ed.getBoundingClientRect();
  return { x: Math.round(r.x + r.width / 2), y: Math.round(r.y + r.height / 2), w: Math.round(r.width), h: Math.round(r.height) };
}
"""


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def canonical_image_url(url: str) -> str:
    """Drop volatile CDN query strings while keeping a meaningful identity."""

    text = str(url or "").strip()
    if not text:
        return ""
    try:
        parsed = urlparse(text)
    except ValueError:
        return text
    if parsed.scheme in {"http", "https"}:
        return f"{parsed.netloc}{parsed.path}"
    return text


def canonical_video_ref(value: str) -> str:
    """Turn a bare aweme id into a shareable Douyin URL; keep URLs as-is."""

    text = str(value or "").strip()
    if not text:
        return ""
    if re.fullmatch(r"\d{15,25}", text):
        return f"https://www.douyin.com/video/{text}"
    return text


def message_fingerprint(
    *,
    direction: str,
    kind: str,
    text: str,
    image_url: str,
    video_ref: str,
) -> str:
    material = "\x1f".join(
        [
            direction,
            kind,
            normalize_text(text)[:300],
            canonical_image_url(image_url),
            str(video_ref or "")[:300],
        ]
    )
    return _sha(material)


def parse_row(row: dict[str, Any], *, observed_at: datetime | None = None) -> ChatMessage:
    """Classify one JS-extracted row; pure, so it is covered without a browser."""

    text = normalize_text(row.get("text") or "")
    row_text = normalize_text(row.get("rowText") or "")
    duration = str(row.get("duration") or "").strip()
    image = str(row.get("image") or "").strip()
    if image and re.search(r"aweme-avatar|/avatar/|~c5_\d+x\d+", image, re.IGNORECASE):
        image = ""
    links = [str(value or "") for value in (row.get("links") or [])]
    attrs = [str(value or "") for value in (row.get("attrs") or [])]
    combined = " ".join([text, row_text, *links, *attrs])
    video_ref = extract_aweme_ref(combined)
    if video_ref and re.search(r"aweme-avatar|/avatar/", video_ref, re.IGNORECASE):
        video_ref = ""
    card = bool(row.get("card"))
    play = bool(row.get("play"))

    if video_ref:
        kind = "video"
    elif duration or (play and image and not text):
        # A forwarded video card shows a duration badge or a play control and
        # often has no text node.  Stickers have neither, so they stay images.
        kind = "video"
    elif card and play and image and text and len(text) <= 200:
        kind = "video"
    elif image:
        kind = "image"
    elif text:
        kind = "text"
    else:
        kind = "unknown"

    direction = "out" if row.get("self") or row.get("who") == "__self__" else "in"
    return ChatMessage(
        direction=direction,  # type: ignore[arg-type]
        kind=kind,  # type: ignore[arg-type]
        text=text,
        image_url=image,
        video_ref=video_ref,
        fingerprint=message_fingerprint(
            direction=direction,
            kind=kind,
            text=text,
            image_url=image,
            video_ref=video_ref,
        ),
        observed_at=observed_at,
        raw={k: row.get(k) for k in ("who", "cls", "card", "play", "links", "attrs", "rowText", "duration")},
    )


class DouyinChatError(RuntimeError):
    def __init__(self, code: str, message: str = ""):
        super().__init__(f"{code}: {message}" if message else code)
        self.code = code
        self.message = message


class DouyinChatSession:
    """One chat tab in the adapter's persistent context."""

    def __init__(self, adapter: Any, settings: Any):
        self.adapter = adapter
        self.settings = settings
        self._lock = threading.RLock()
        self._context = None
        self._page = None
        self._conversation = ""
        self._conversation_id = ""
        self._last_error = ""
        self._last_checked = 0.0
        self._connected = False
        self._url = ""
        self._last_calibration: dict[str, Any] = {}

    @property
    def page(self):
        return self._page

    def status(self) -> dict[str, Any]:
        # Deliberately does not touch the Playwright page.  This method is
        # called from console HTTP threads, while the page belongs to the
        # bridge poll thread; a Playwright call here would cross greenlets.
        return {
            "connected": bool(self._connected),
            "conversation": self._conversation,
            "conversation_id": self._conversation_id,
            "url": self._url,
            "last_error": self._last_error,
            "calibrated": bool(self._last_calibration),
        }

    def close(self) -> None:
        with self._lock:
            page, self._page = self._page, None
            self._context = None
            self._connected = False
            if page is not None:
                try:
                    page.close()
                except Exception:
                    pass

    def recover_page(self, *, relaunch_browser: bool = False) -> dict[str, Any]:
        """Rebuild the chat tab after conversation switching stalls.

        A long-lived headless tab can keep the login and the conversation list
        while clicks no longer switch the open chat; every poll then fails with
        CHAT_IDENTITY_UNCONFIRMED and nothing is forwarded.  Reloading ``/chat``
        is the cheap fix; ``relaunch_browser`` closes the whole persistent
        context and launches it again from the same profile, so the Douyin
        login is kept (no new QR scan).

        Must run on the bridge poll thread: the synchronous Playwright objects
        are bound to the thread that created them.
        """

        with self._lock:
            page = self._page
            self._page = None
            self._connected = False
            self._conversation = ""
            self._conversation_id = ""
            self._last_calibration = {}
            if relaunch_browser:
                if page is not None:
                    try:
                        page.close()
                    except Exception:
                        pass
                closer = getattr(self.adapter, "close", None)
                if not callable(closer):
                    raise DouyinChatError("CHAT_BROWSER_RELAUNCH_UNSUPPORTED", "浏览器适配器不支持重新拉起")
                try:
                    closer()
                except Exception as exc:
                    raise DouyinChatError("CHAT_BROWSER_RELAUNCH_FAILED", str(exc)[:400]) from exc
            elif page is not None and not page.is_closed():
                try:
                    page.reload(wait_until="domcontentloaded", timeout=45_000)
                    self._wait_for_list(page, timeout=40)
                    self._page = page
                except DouyinChatError as exc:
                    if exc.code in {"CHAT_CHALLENGE", "CHAT_LOGIN_REQUIRED"}:
                        raise
                    try:
                        page.close()
                    except Exception:
                        pass
                except Exception:
                    try:
                        page.close()
                    except Exception:
                        pass
            page = self._ensure_page()
            self._guard_page(page)
            rows = page.evaluate(
                "() => document.querySelectorAll('[class*=\"conversationConversationItemwrapper\"]').length"
            )
            return {
                "ok": True,
                "relaunch": bool(relaunch_browser),
                "url": page.url,
                "rows": int(rows or 0),
            }

    def _ensure_page(self):
        if self._page is not None and not self._page.is_closed():
            try:
                if "/chat" not in (self._page.url or ""):
                    # The operator may have clicked a video or a profile in the
                    # shared tab; return to the chat list instead of reading a
                    # random page as if it were a conversation.
                    try:
                        self._page.goto(
                            str(getattr(self.settings, "douyin_bridge_chat_url", CHAT_PAGE) or CHAT_PAGE),
                            wait_until="commit",
                            timeout=60_000,
                        )
                    except Exception:
                        pass
                    self._wait_for_list(self._page, timeout=30)
            except DouyinChatError:
                raise
            except Exception:
                pass
            self._connected = True
            self._url = self._page.url
            return self._page
        try:
            context = self.adapter.ensure_session()
        except Exception as exc:
            raise DouyinChatError("CHAT_BROWSER_THREAD_CONFLICT", str(exc)[:400]) from exc
        if context is None:
            raise DouyinChatError("CHAT_CONTEXT_UNAVAILABLE", "浏览器上下文不可用")
        page = context.new_page()
        page.set_default_timeout(max(60_000, int(getattr(self.settings, "platform_browser_page_timeout_ms", 20_000))))
        self._page = page
        try:
            page.goto(
                str(getattr(self.settings, "douyin_bridge_chat_url", CHAT_PAGE) or CHAT_PAGE),
                wait_until="commit",
                timeout=60_000,
            )
        except Exception:
            # A heavy Douyin page often exceeds a 20 s DOMContentLoaded wait
            # while the app itself is already usable; the conversation-list
            # poll below is the real readiness check.
            pass
        self._wait_for_list(page, timeout=30)
        self._guard_page(page)
        self._connected = True
        self._url = page.url
        return page

    def _wait_for_list(self, page, *, timeout: float = 30.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                if page.evaluate("() => document.querySelectorAll('[class*=\"conversationConversationItem\"]').length > 0"):
                    return
            except Exception:
                pass
            time.sleep(0.4)
        # Give the caller a precise reason instead of a generic timeout.
        body = ""
        try:
            body = page.inner_text("body")[:4000]
        except Exception:
            pass
        if any(marker in body for marker in _CHALLENGE_MARKERS):
            raise DouyinChatError("CHAT_CHALLENGE", "抖音页面出现验证码或安全验证")
        if any(marker in body for marker in _LOGIN_MARKERS) or ("登录" in body and "退出登录" not in body):
            raise DouyinChatError("CHAT_LOGIN_REQUIRED", "抖音网页版未登录，请先在可见浏览器扫码")
        raise DouyinChatError("CHAT_LIST_TIMEOUT", "抖音私信会话列表没有渲染")

    def _guard_page(self, page) -> None:
        try:
            body = page.inner_text("body")[:6000]
        except Exception:
            return
        if any(marker in body for marker in _CHALLENGE_MARKERS):
            raise DouyinChatError("CHAT_CHALLENGE", "抖音页面出现验证码或安全验证")

    def list_conversations(self) -> list[dict[str, Any]]:
        with self._lock:
            page = self._ensure_page()
            value = page.evaluate(_JS_CONVERSATIONS)
            return value if isinstance(value, list) else []

    def refresh_conversations(self) -> list[dict[str, Any]]:
        """Scroll the live chat list and return every rendered conversation."""

        def collect() -> list[dict[str, Any]]:
            collected: dict[str, dict[str, Any]] = {}
            try:
                page.evaluate(_JS_RESET_CHAT_SCROLL)
                time.sleep(0.3)
            except Exception:
                pass
            for _ in range(40):
                try:
                    value = page.evaluate(_JS_CONVERSATIONS)
                except Exception:
                    value = []
                for row in value if isinstance(value, list) else []:
                    if not isinstance(row, dict):
                        continue
                    name = str(row.get("name") or "").strip()
                    if name and not re.fullmatch(r"\d{8,}", name):
                        collected.setdefault(name, row)
                if not collected:
                    time.sleep(0.5)
                    continue
                try:
                    moved = page.evaluate(_JS_SCROLL_CHAT_LIST)
                except Exception:
                    moved = False
                if not moved:
                    break
                time.sleep(0.35)
            return list(collected.values())

        with self._lock:
            page = self._ensure_page()
            if "/chat" not in page.url:
                page.goto("https://www.douyin.com/chat", wait_until="domcontentloaded", timeout=20000)
            rows = collect()
            if rows:
                return rows
            # Only reload when the live DOM is empty/broken; a fresh reload
            # does not always restore recent contacts immediately.
            page.reload(wait_until="domcontentloaded", timeout=30000)
            if "/chat" not in page.url:
                page.goto("https://www.douyin.com/chat", wait_until="domcontentloaded", timeout=20000)
            time.sleep(2)
            return collect()

    def _search_conversation(self, page, wanted: str) -> dict[str, Any] | None:
        """Use Douyin's global search when the virtualized list omits a chat."""

        try:
            box = page.locator(
                'input[placeholder*="搜索"], input[placeholder*="Search"], input[type="search"]'
            )
            if not box.count():
                return None
            field = box.first
            field.click()
            field.fill(wanted)
            time.sleep(1.5)
            hit = page.evaluate(_JS_SEARCH_RESULT, wanted)
            if not hit or not hit.get("ok"):
                return None
            return {**hit, "name": wanted}
        except Exception:
            return None

    def open_conversation(self, target: str | None = None) -> dict[str, Any]:
        """Open one conversation by display name; capture its stable id if exposed."""

        with self._lock:
            page = self._ensure_page()
            context = page.context
            # A misclick on a profile/avatar opens a new Douyin tab.  Left
            # unchecked those tabs stack up, steal focus and make the screen
            # flash between profile pages and the chat list.
            for other in list(context.pages):
                if other is not page:
                    try:
                        other.close()
                    except Exception:
                        pass
            if "/chat" not in page.url:
                try:
                    page.goto("https://www.douyin.com/chat", wait_until="domcontentloaded", timeout=20000)
                except Exception as exc:
                    raise DouyinChatError("CHAT_PAGE_RECOVERY_FAILED", str(exc)[:200]) from exc
            wanted = normalize_text(target or getattr(self.settings, "douyin_bridge_conversation", ""))
            if not wanted:
                raise DouyinChatError("CHAT_TARGET_MISSING", "未配置 DOUYIN_BRIDGE_CONVERSATION")
            active = str(page.evaluate(_JS_ACTIVE) or "").strip()
            if active and normalize_text(active) == wanted and page.evaluate(_JS_EDITOR):
                self._conversation = active
                return {"ok": True, "name": active, "conversation_id": self._conversation_id or self._name_key(active), "opened": True}

            captured: list[str] = []

            def on_request(request) -> None:
                try:
                    text = f"{request.url}\n{request.post_data or ''}"
                except Exception:
                    return
                if "get_by_conversation" in text or "conversation_id" in text:
                    match = re.search(r"(?<!\d)(\d{16,20})(?!\d)", text)
                    if match:
                        captured.append(match.group(1))

            page.on("request", on_request)
            try:
                hit = page.evaluate(_JS_FIND_CHAT, wanted)
                if not hit or not hit.get("ok"):
                    # The list is virtualized; a chat that received a new
                    # message can move below the rendered window.  Reset to
                    # the top and scroll through the list before declaring it
                    # missing.  Without this, a newly active 测试丁 was skipped
                    # even though it was reachable one scroll away.
                    try:
                        page.evaluate(_JS_RESET_CHAT_SCROLL)
                        for _ in range(40):
                            hit = page.evaluate(_JS_FIND_CHAT, wanted)
                            if hit and hit.get("ok"):
                                break
                            if not page.evaluate(_JS_SCROLL_CHAT_LIST):
                                break
                            time.sleep(0.2)
                    except Exception:
                        pass
                if not hit or not hit.get("ok"):
                    # Directly drive the real sidebar scroller.  A generic
                    # scrollIntoView/ancestor walk can be overridden by the
                    # virtual list, leaving chats below the fold unreachable.
                    try:
                        info = page.evaluate(_JS_SET_CHAT_SCROLL, 0)
                        height = int((info or {}).get("height") or 0)
                        client = int((info or {}).get("client") or 0)
                        step = max(260, int(client * 0.7))
                        position = 0
                        while position <= height + step:
                            page.evaluate(_JS_SET_CHAT_SCROLL, position)
                            time.sleep(0.2)
                            hit = page.evaluate(_JS_FIND_CHAT, wanted)
                            if hit and hit.get("ok"):
                                break
                            position += step
                    except Exception:
                        pass
                if not hit or not hit.get("ok"):
                    raise DouyinChatError(
                        "CHAT_CONVERSATION_NOT_FOUND",
                        f"找不到会话「{wanted}」；当前可见：{', '.join((hit or {}).get('names') or [])[:300]}",
                    )
                spots = [hit, {"x": hit["x"], "y": hit["y"] + 14}, {"x": max(4, hit["x"] - 40), "y": hit["y"]}]
                active = ""
                switched = False
                def wait_for_target(deadline_seconds: float) -> bool:
                    nonlocal active, switched
                    deadline = time.monotonic() + deadline_seconds
                    while time.monotonic() < deadline:
                        try:
                            editor = bool(page.evaluate(_JS_EDITOR))
                        except Exception:
                            editor = False
                        try:
                            candidate = str(page.evaluate(_JS_ACTIVE) or "").strip()
                        except Exception:
                            candidate = ""
                        if candidate:
                            active = candidate
                        if editor and candidate and normalize_text(candidate) == wanted:
                            switched = True
                            return True
                        time.sleep(0.2)
                    return False

                # Click the DOM row itself first; coordinate clicks miss when the
                # internal list scrolls the row under a sticky header.
                try:
                    marker = page.locator('[data-bridge-open="1"]')
                    if marker.count():
                        marker.first.click(force=True, timeout=3000)
                        wait_for_target(2.5)
                except Exception:
                    pass
                if not switched:
                    for spot in spots:
                        page.mouse.click(float(spot["x"]), float(spot["y"]))
                        if wait_for_target(2.5):
                            break
                if not switched:
                    # Never continue with an unverified active conversation: the
                    # previous implementation accepted an empty active name and
                    # then read the still-open chat as if it were the target,
                    # which cross-attributed messages to another binding.
                    if not active:
                        raise DouyinChatError(
                            "CHAT_IDENTITY_UNCONFIRMED",
                            f"点开会话后无法确认当前会话，未读取「{wanted}」的任何消息",
                        )
                    raise DouyinChatError(
                        "CHAT_IDENTITY_UNCONFIRMED",
                        f"点开后当前会话是「{active}」，不是「{wanted}」",
                    )
                log.info("douyin_bridge_conversation_verified name=%s", wanted)
            finally:
                try:
                    page.remove_listener("request", on_request)
                except Exception:
                    pass
                try:
                    page.evaluate("() => document.querySelectorAll('[data-bridge-open],[data-bridge-search]').forEach((e) => { e.removeAttribute('data-bridge-open'); e.removeAttribute('data-bridge-search'); })")
                except Exception:
                    pass
                try:
                    # A profile link or a chat card can open a second tab.
                    # Close everything except the chat tab and restore /chat
                    # so the next binding does not inherit a profile page.
                    for extra in list(context.pages):
                        if extra is not page:
                            try:
                                extra.close()
                            except Exception:
                                pass
                    if "/chat" not in page.url:
                        page.goto("https://www.douyin.com/chat", wait_until="domcontentloaded", timeout=20000)
                except Exception:
                    pass

            conversation_id = self._name_key(str(hit.get("name") or wanted))
            self._conversation = str(hit.get("name") or wanted)
            self._conversation_id = conversation_id
            return {
                "ok": True,
                "name": self._conversation,
                "conversation_id": conversation_id,
                "captured_id": captured[-1] if captured else "",
                "opened": True,
            }

    def _require_active(self, page, context: str = "") -> None:
        """Fail closed unless the page's active conversation is the one we opened."""

        if not self._conversation:
            return
        active = ""
        deadline = time.monotonic() + 1.5
        while time.monotonic() < deadline:
            try:
                active = str(page.evaluate(_JS_ACTIVE) or "").strip()
            except Exception:
                active = ""
            if active and normalize_text(active) == normalize_text(self._conversation):
                return
            time.sleep(0.15)
        raise DouyinChatError(
            "CHAT_IDENTITY_UNCONFIRMED",
            f"{context}无法确认当前会话（当前「{active or '未知'}」，期望「{self._conversation}」）",
        )

    @staticmethod
    def _name_key(name: str) -> str:
        return "name:" + _sha(normalize_text(name))[:20]

    def read_messages(self, limit: int = 20) -> list[ChatMessage]:
        with self._lock:
            page = self._ensure_page()
            if not self._conversation:
                raise DouyinChatError("CHAT_NOT_OPEN", "尚未打开目标会话")
            self._require_active(page, "读取消息前")
            rows = page.evaluate(_JS_ROWS, max(1, min(int(limit), 60)))
            if not isinstance(rows, list):
                return []
            now = datetime.now().astimezone()
            messages = [parse_row(row, observed_at=now) for row in rows if isinstance(row, dict)]
            # The rendered list is newest-first; callers reason chronologically.
            return list(reversed(messages))

    def conversation_preview(self) -> str:
        with self._lock:
            page = self._ensure_page()
            try:
                rows = page.evaluate(_JS_CONVERSATIONS) or []
            except Exception:
                return ""
            wanted = normalize_text(self._conversation)
            for row in rows:
                if normalize_text(row.get("name")) == wanted:
                    return str(row.get("preview") or "")
            return ""

    def send_text(self, text: str) -> dict[str, Any]:
        value = str(text or "").strip()
        if not value:
            raise DouyinChatError("CHAT_EMPTY_TEXT", "待发送文本为空")
        with self._lock:
            page = self._ensure_page()
            self._require_active(page, "发送文字前")
            box = page.evaluate(_JS_EDITOR)
            if not box:
                raise DouyinChatError("CHAT_EDITOR_MISSING", "当前没有打开的会话输入框")
            page.mouse.click(float(box["x"]), float(box["y"]))
            time.sleep(0.2)
            page.keyboard.insert_text(value)
            time.sleep(0.3)
            page.keyboard.press("Enter")
            time.sleep(1.2)
            head = value[:12]
            verified = False
            deadline = time.monotonic() + 4.0
            while time.monotonic() < deadline:
                try:
                    messages = self.read_messages(limit=6)
                    if any(message.direction == "out" and head and head in message.text for message in messages):
                        verified = True
                        break
                    preview = self.conversation_preview()
                    if head and head in preview:
                        verified = True
                        break
                except Exception:
                    pass
                time.sleep(0.4)
            return {"ok": True, "verified": verified, "preview": self.conversation_preview()[:200]}

    def resolve_video_ref(
        self,
        offset: int = 0,
        *,
        image_url: str = "",
        row_text: str = "",
    ) -> str:
        """Click a forwarded video card and return a shareable video reference.

        A pure forwarded card often exposes no link at all: the DOM has only a
        cover image, a play badge and a duration.  This method finds that row
        (by cover image or row text when available, else by ``offset``), clicks
        it, waits for the detail page/tab/overlay, and returns a full
        ``https://www.douyin.com/video/<id>`` reference.  The chat conversation
        is restored before returning.  Callers must still verify by parse.
        """

        with self._lock:
            page = self._ensure_page()
            position = page.evaluate(
                r"""
                (args) => {
                  const zone = document.querySelector('[class*="messageMessageListlist"]')
                    || document.querySelector('[class*="messageMessageListwrapper"]')
                    || document.querySelector('[class*="messageMessageList"]');
                  if (!zone) return null;
                  let rows = [...zone.querySelectorAll('[class*="messageMessageBoxmessageBox"]')];
                  if (!rows.length) rows = [...zone.querySelectorAll('[class*="messageMessageBox"]')];
                  if (!rows.length) rows = [...zone.children];
                  const center = (el) => {
                    el.scrollIntoView({ block: 'center' });
                    const r = el.getBoundingClientRect();
                    return {
                      x: r.x + r.width / 2,
                      y: r.y + r.height / 2,
                      html: String(el.outerHTML || '').slice(0, 12000),
                      ids: idsIn(el),
                    };
                  };
                  const idsIn = (el) => {
                    const ids = new Set();
                    const collect = (text) => {
                      let m;
                      const re = /(?:aweme_?id|item_?id|modal_?id|awemeId|itemId|modalId)["'=:\s\\\/]{1,4}(\d{15,25})/gi;
                      while ((m = re.exec(String(text || '')))) ids.add(m[1]);
                      const re2 = /\/video\/(\d{15,25})/g;
                      while ((m = re2.exec(String(text || '')))) ids.add(m[1]);
                    };
                    collect(el.outerHTML);
                    for (const child of [el, ...el.querySelectorAll('*')]) {
                      for (const attr of [...child.attributes]) {
                        collect(attr.name + '=' + attr.value);
                      }
                    }
                    return [...ids].slice(0, 10);
                  };
                  const needle = String(args.image || '').split('?')[0];
                  if (needle) {
                    for (const row of rows) {
                      for (const img of row.querySelectorAll('img')) {
                        const src = String(img.currentSrc || img.src || '').split('?')[0];
                        if (!src) continue;
                        if (src === needle || (needle.length > 40 && src.includes(needle.slice(-60)))) {
                          const r = img.getBoundingClientRect();
                          if (r.width >= 40) {
                            img.scrollIntoView({ block: 'center' });
                            const b = img.getBoundingClientRect();
                            return {
                              x: b.x + b.width / 2,
                              y: b.y + b.height / 2,
                              html: String(row.outerHTML || '').slice(0, 12000),
                              ids: idsIn(row),
                            };
                          }
                          return center(row);
                        }
                      }
                    }
                  }
                  if (args.rowText) {
                    const want = String(args.rowText).replace(/\s+/g, '').slice(0, 24);
                    for (const row of rows) {
                      const text = (row.innerText || '').replace(/\s+/g, '');
                      if (want && text.includes(want)) return center(row);
                    }
                  }
                  const row = rows[Math.max(0, Number(args.offset) || 0)];
                  return row ? center(row) : null;
                }
                """,
                {
                    "offset": max(0, int(offset)),
                    "image": str(image_url or ""),
                    "rowText": str(row_text or ""),
                },
            )
            if not position:
                return ""
            row_html = str(position.get("html") or "")
            row_ids = [
                str(value) for value in (position.get("ids") or [])
                if re.fullmatch(r"\d{15,25}", str(value))
            ]
            log.info("douyin_card_row ids=%s", json.dumps(sorted(set(row_ids))[:10]))
            if len(set(row_ids)) == 1:
                # The card DOM already names the shared aweme; no click needed.
                return canonical_video_ref(sorted(set(row_ids))[0])
            context = page.context
            before_pages = list(context.pages)
            before_url = page.url
            try:
                # The click fires Douyin's aweme-detail API; the response is the
                # most reliable source of the real aweme id (the page also
                # contains ids of banners/recommendations that must not be used).
                ref, captured_detail = self._click_card_and_capture(page, position, row_html)
                if ref:
                    return ref
                if captured_detail:
                    # A detail response arrived but was unusable/mismatched;
                    # never fall back to an arbitrary id scraped from the page.
                    self._debug_card_failure(page, before_pages, before_url, row_html)
                    return ""
                ref = self._await_video_detail(page, before_pages, before_url)
                if not ref:
                    # Some accounts render the detail as an in-page overlay
                    # that changes neither the tab nor the URL.  Prefer the
                    # share panel's copied link, then fall back to DOM ids.
                    if self._card_overlay_open(page):
                        ref = self._share_link_from_overlay(page)
                if not ref:
                    for _ in range(3):
                        if not self._card_overlay_open(page):
                            break
                        ref = self._video_ref_from_overlay(page)
                        if ref:
                            break
                        time.sleep(0.6)
                if not ref:
                    self._debug_card_failure(page, before_pages, before_url, row_html)
                return ref
            finally:
                self._restore_chat_after_card(page, before_pages, before_url)

    def _click_card_and_capture(self, page, position: dict[str, Any], row_html: str) -> tuple[str, bool]:
        """Click the card and capture the aweme-detail response it triggers.

        Returns ``(ref, captured_detail)``.  ``captured_detail`` is True when a
        detail response was seen, even if it did not validate against the card
        author; callers must not fall back to page-wide id guessing in that case.
        """

        captured: dict[str, Any] | None = None
        seen: list[str] = []
        all_urls: list[str] = []
        page_responses: list[Any] = []

        def on_response(response: Any) -> None:
            try:
                url = str(response.url)
            except Exception:
                return
            if len(all_urls) < 40:
                all_urls.append(url[:240])
            if "/aweme/" in url or "detail" in url:
                page_responses.append(response)

        try:
            page.on("response", on_response)
        except Exception:
            pass
        try:
            page.mouse.click(float(position["x"]), float(position["y"]))
            page.wait_for_timeout(4500)
        finally:
            try:
                page.remove_listener("response", on_response)
            except Exception:
                pass
            for response in page_responses[-10:]:
                try:
                    seen.append(str(response.url)[:200])
                except Exception:
                    continue
        for response in page_responses:
            try:
                payload = response.json()
            except Exception:
                continue
            detail = payload.get("aweme_detail") if isinstance(payload, dict) else None
            if isinstance(detail, dict) and detail.get("aweme_id"):
                captured = detail
                break
        log.warning(
            "douyin_card_network captured=%s urls=%s all=%s",
            bool(captured),
            json.dumps(seen, ensure_ascii=False)[:1000],
            json.dumps(all_urls, ensure_ascii=False)[:3000],
        )
        if captured is None:
            return "", False
        detail = captured
        aweme_id = str(detail.get("aweme_id") or "")
        author = ""
        author_node = detail.get("author")
        if isinstance(author_node, dict):
            author = str(author_node.get("nickname") or "")
        desc = str(detail.get("desc") or "")
        card_author = ""
        match = re.search(r'authorName">([^<]+)<', row_html)
        if match:
            card_author = match.group(1).strip()
        log.info(
            "douyin_card_detail aweme_id=%s author=%s card_author=%s desc=%s",
            aweme_id, author[:40], card_author[:40], desc[:80],
        )
        if not re.fullmatch(r"\d{15,25}", aweme_id):
            return "", True
        if card_author and author and card_author not in author and author not in card_author:
            log.warning(
                "douyin_card_detail_author_mismatch aweme_id=%s author=%s card_author=%s",
                aweme_id, author[:40], card_author[:40],
            )
            return "", True
        return canonical_video_ref(aweme_id), True

    def _debug_card_failure(self, page, before_pages: list, before_url: str, row_html: str) -> None:
        """Log a bounded snapshot when a card click produced no video ref."""

        try:
            after_url = page.url
            pages = [{"url": getattr(item, "url", "")[:300]} for item in page.context.pages]
            frames = [str(frame.url)[:300] for frame in page.frames]
        except Exception:
            after_url, pages, frames = "?", [], []
        probe: dict[str, Any] = {}
        try:
            probe = page.evaluate(
                r"""
                () => {
                  const visible = (el) => {
                    const r = el.getBoundingClientRect();
                    return r.width > 120 && r.height > 80 && r.bottom > 0 && r.top < innerHeight;
                  };
                  const out = { url: location.href, title: document.title };
                  out.videos = [...document.querySelectorAll('video')]
                    .map((v) => String(v.currentSrc || v.src || '')).filter(Boolean).slice(0, 4);
                  out.videoLinks = [...document.querySelectorAll('a[href]')]
                    .map((a) => String(a.href || ''))
                    .filter((h) => /\/video\/|modal_id=|aweme_id=/.test(h)).slice(0, 6);
                  out.bigNodes = [...document.querySelectorAll('[data-e2e], [data-aweme-id], [data-item-id], [class*="modal"], [class*="Modal"], [class*="video"], [class*="Video"], [class*="overlay"]')]
                    .filter(visible).slice(0, 12).map((el) => ({
                      tag: el.tagName,
                      e2e: el.getAttribute('data-e2e'),
                      aweme: el.getAttribute('data-aweme-id'),
                      item: el.getAttribute('data-item-id'),
                      cls: String(el.className || '').slice(0, 140),
                    }));
                  const html = String(document.documentElement.outerHTML || '');
                  const ids = new Set();
                  for (const m of html.matchAll(/(?:aweme_id|item_id|modal_id)[=":\s]+(\d{15,25})/g)) ids.add(m[1]);
                  for (const m of html.matchAll(/\/video\/(\d{15,25})/g)) ids.add(m[1]);
                  out.htmlIds = [...ids].slice(0, 10);
                  return out;
                }
                """
            )
        except Exception as exc:
            probe = {"error": type(exc).__name__, "message": str(exc)[:200]}
        shot = ""
        try:
            directory = Path(getattr(self.settings, "douyin_bridge_media_dir", "state/douyin-bridge"))
            directory.mkdir(parents=True, exist_ok=True)
            target = directory / ("card-resolve-debug-" + time.strftime("%Y%m%d-%H%M%S") + ".png")
            page.screenshot(path=str(target), full_page=False)
            shot = str(target)
        except Exception:
            pass
        log.warning(
            "douyin_card_resolve_debug before_url=%s after_url=%s pages=%s frames=%s screenshot=%s probe=%s row_html=%s",
            str(before_url)[:300],
            str(after_url)[:300],
            json.dumps(pages, ensure_ascii=False)[:600],
            json.dumps(frames, ensure_ascii=False)[:1000],
            shot,
            json.dumps(probe, ensure_ascii=False)[:2400],
            row_html.replace("\n", " ")[:800],
        )

    def _await_video_detail(self, page, before_pages: list, before_url: str, *, timeout: float = 6.0) -> str:
        """Wait for the card click to open a new tab or change the URL."""

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                new_pages = [item for item in page.context.pages if item not in before_pages]
            except Exception:
                new_pages = []
            if new_pages:
                try:
                    ref = extract_aweme_ref(new_pages[-1].url)
                except Exception:
                    ref = ""
                for item in new_pages:
                    try:
                        item.close()
                    except Exception:
                        pass
                if ref:
                    return canonical_video_ref(ref)
            try:
                current = page.url
            except Exception:
                current = before_url
            if current != before_url:
                ref = extract_aweme_ref(current)
                if ref:
                    return canonical_video_ref(ref)
            try:
                frames = list(page.frames)
            except Exception:
                frames = []
            for frame in frames:
                try:
                    frame_ref = extract_aweme_ref(str(frame.url))
                except Exception:
                    frame_ref = ""
                if frame_ref:
                    log.info("douyin_card_frame_ref url=%s ref=%s", str(frame.url)[:200], frame_ref[:120])
                    return canonical_video_ref(frame_ref)
            if self._card_overlay_open(page):
                # In-page fullscreen detail: no tab/URL change to wait for.
                return ""
            time.sleep(0.4)
        return ""

    def _card_overlay_open(self, page) -> bool:
        try:
            return bool(
                page.evaluate(
                    r"""
                    () => {
                      const nodes = [...document.querySelectorAll(
                        '[class*="commonModal"], [class*="Modal"], [class*="modal"], [class*="FullScreen"]'
                      )];
                      return nodes.some((el) => {
                        const r = el.getBoundingClientRect();
                        return r.width > 300 && r.height > 200 && r.bottom > 0 && r.top < innerHeight;
                      });
                    }
                    """
                )
            )
        except Exception:
            return False

    def _share_link_from_overlay(self, page) -> str:
        """Open the overlay share panel and copy the short share link.

        The full-screen player often carries no aweme id in its DOM; the share
        panel's copy action is the canonical way to obtain a parseable link.
        """

        try:
            page.context.grant_permissions(
                ["clipboard-read", "clipboard-write"], origin="https://www.douyin.com"
            )
        except Exception:
            pass
        share_at = page.evaluate(
            r"""
            () => {
              const roots = [...document.querySelectorAll(
                '[class*="commonModal"], [class*="Modal"], [class*="FullScreen"]'
              )].filter((el) => {
                const r = el.getBoundingClientRect();
                return r.width > 300 && r.height > 200;
              });
              const out = [];
              for (const root of roots) {
                for (const el of root.querySelectorAll('*')) {
                  const label = String(el.getAttribute('aria-label') || el.className || el.innerText || '').toLowerCase();
                  if (!/share|分享/.test(label)) continue;
                  const r = el.getBoundingClientRect();
                  if (r.width > 8 && r.height > 8 && r.width < 220 && r.height < 160 && r.top < 200) {
                    out.push({ x: r.x + r.width / 2, y: r.y + r.height / 2, cls: String(el.className || '').slice(0, 100), label: label.slice(0, 40) });
                  }
                }
              }
              return out.slice(0, 8);
            }
            """
        )
        log.info("douyin_card_share_candidates %s", json.dumps(share_at, ensure_ascii=False)[:600])
        if not isinstance(share_at, list) or not share_at:
            return ""
        page.mouse.click(float(share_at[0]["x"]), float(share_at[0]["y"]))
        page.wait_for_timeout(900)
        copy_at = page.evaluate(
            r"""
            () => {
              const wanted = /^(复制链接|复制分享链接|复制|copy( link)?|Copy( link)?)$/i;
              const out = [];
              for (const el of document.querySelectorAll('button, [role="button"], [class*="copy"], [class*="Copy"], span, div')) {
                const text = String(el.innerText || '').trim();
                const label = String(el.getAttribute('aria-label') || '').trim();
                if (!wanted.test(text) && !wanted.test(label)) continue;
                const r = el.getBoundingClientRect();
                if (r.width < 8 || r.height < 8 || r.bottom <= 0 || r.top >= innerHeight) continue;
                out.push({ x: r.x + r.width / 2, y: r.y + r.height / 2, text: text.slice(0, 20), label: label.slice(0, 20) });
              }
              return out.slice(0, 8);
            }
            """
        )
        log.info("douyin_card_copy_candidates %s", json.dumps(copy_at, ensure_ascii=False)[:600])
        if isinstance(copy_at, list) and copy_at:
            page.mouse.click(float(copy_at[0]["x"]), float(copy_at[0]["y"]))
            page.wait_for_timeout(700)
        link = ""
        try:
            link = str(
                page.evaluate(
                    "async () => { try { return await navigator.clipboard.readText(); } catch (e) { return ''; } }"
                )
                or ""
            )
        except Exception:
            link = ""
        if not link:
            try:
                link = str(
                    page.evaluate(
                        r"""
                        () => {
                          const body = String(document.body.innerText || '');
                          const m = body.match(/https?:\/\/v\.douyin\.com\/[A-Za-z0-9_-]+\/?/);
                          return m ? m[0] : '';
                        }
                        """
                    )
                    or ""
                )
            except Exception:
                link = ""
        log.info("douyin_card_share_link %s", link[:200])
        if not link:
            return ""
        link = link.strip()
        if link.startswith("http") and "douyin.com" in link:
            return link
        return ""

    def _video_ref_from_overlay(self, page) -> str:
        """Read a video reference from a visible in-page detail overlay."""

        try:
            value = page.evaluate(
                r"""
                () => {
                  const visible = (el) => {
                    const r = el.getBoundingClientRect();
                    return r.width > 300 && r.height > 200 && r.bottom > 0 && r.top < innerHeight;
                  };
                  const roots = [
                    ...document.querySelectorAll(
                      '[class*="commonModal"], [class*="Modal"], [class*="modal"], [class*="FullScreen"]'
                    ),
                  ].filter(visible);
                  const idFrom = (text) => {
                    let m = String(text || '').match(/(?:aweme_id|item_id|modal_id)[=":\s]+(\d{15,25})/);
                    if (m) return m[1];
                    m = String(text || '').match(/\/video\/(\d{15,25})/);
                    return m ? m[1] : '';
                  };
                  for (const root of roots) {
                    for (const a of root.querySelectorAll('a[href]')) {
                      const href = String(a.href || '');
                      if (/\/video\/|modal_id=|aweme_id=/.test(href)) return href;
                    }
                    const found = idFrom(root.innerHTML);
                    if (found) return 'https://www.douyin.com/video/' + found;
                  }
                  return '';
                }
                """
            )
        except Exception:
            return ""
        return canonical_video_ref(extract_aweme_ref(str(value or "")))

    def _restore_chat_after_card(self, page, before_pages: list, before_url: str) -> None:
        """Close detail tabs/overlays and reopen the conversation, best effort."""

        try:
            for item in list(page.context.pages):
                if item not in before_pages:
                    try:
                        item.close()
                    except Exception:
                        pass
        except Exception:
            pass
        try:
            page.bring_to_front()
        except Exception:
            pass
        try:
            if page.url != before_url or self._card_overlay_open(page):
                page.keyboard.press("Escape")
                time.sleep(0.6)
        except Exception:
            pass
        try:
            if self._card_overlay_open(page):
                close_at = page.evaluate(
                    r"""
                    () => {
                      const roots = [...document.querySelectorAll(
                        '[class*="commonModal"], [class*="Modal"], [class*="modal"], [class*="FullScreen"]'
                      )].filter((el) => {
                        const r = el.getBoundingClientRect();
                        return r.width > 300 && r.height > 200;
                      });
                      for (const root of roots) {
                        for (const el of root.querySelectorAll('button, [role="button"], [aria-label], [class*="close"], [class*="Close"], svg')) {
                          const label = String(
                            el.getAttribute('aria-label') || el.className || el.innerText || ''
                          ).toLowerCase();
                          if (!/close|关闭|收起|×/.test(label) && !/close/i.test(String(el.className || ''))) continue;
                          const r = el.getBoundingClientRect();
                          if (r.width > 0 && r.height > 0 && r.top < 140) {
                            return { x: r.x + r.width / 2, y: r.y + r.height / 2 };
                          }
                        }
                      }
                      return null;
                    }
                    """
                )
                if close_at:
                    page.mouse.click(float(close_at["x"]), float(close_at["y"]))
                    time.sleep(0.5)
        except Exception:
            pass
        try:
            if page.url != before_url:
                page.go_back(wait_until="domcontentloaded", timeout=15_000)
                self._wait_for_list(page, timeout=10)
        except Exception:
            pass
        try:
            if self._conversation:
                self.open_conversation(self._conversation)
        except Exception:
            pass

    def send_media(self, path: str) -> dict[str, Any]:
        """Send a local image/GIF into the open Douyin conversation.

        Douyin's web chat accepts a file through the hidden ``input[type=file]``
        and then shows a "send file" confirmation.  This is the only path for
        an arbitrary sticker asset (the native sticker panel only has Douyin's
        own set).  The caller still verifies the job/DB; this method reports
        only what the page showed.
        """

        source = Path(path)
        if not source.is_file() or source.stat().st_size <= 0:
            raise DouyinChatError("CHAT_MEDIA_MISSING", "待发送表情文件不存在或为空")
        with self._lock:
            page = self._ensure_page()
            self._require_active(page, "发送媒体前")
            if not page.evaluate(_JS_EDITOR):
                raise DouyinChatError("CHAT_NOT_OPEN", "当前没有打开的抖音会话")
            before = self.read_messages(limit=20)
            before_images = sum(1 for item in before if item.direction == "out" and item.kind == "image")
            file_input = page.locator("input[type=file]").first
            if file_input.count() == 0:
                raise DouyinChatError("CHAT_UPLOAD_MISSING", "抖音页面没有上传入口")
            file_input.set_input_files(str(source))
            for _ in range(24):
                try:
                    if any(
                        frame.evaluate("() => Boolean(document.querySelector('[class*=\"MsgInputSendFileModal\"]'))")
                        for frame in page.frames
                    ):
                        break
                except Exception:
                    pass
                time.sleep(0.5)
            time.sleep(0.5)
            clicked = False
            debug: dict[str, Any] = {"frames": []}
            finder = r"""
                () => {
                  document.querySelectorAll('[data-dy-send]').forEach((e) => e.removeAttribute('data-dy-send'));
                  const modal = document.querySelector('[class*="MsgInputSendFileModal"]');
                  if (!modal) return { modal: false, candidates: [] };
                  const candidates = [];
                  for (const e of modal.querySelectorAll('*')) {
                    const text = (e.innerText || '').trim();
                    if (!/^发送/.test(text)) continue;
                    const r = e.getBoundingClientRect();
                    if (r.width >= 40 && r.height >= 20 && r.bottom > 0 && r.top < innerHeight) {
                      candidates.push({ text: text.slice(0, 20), cls: String(e.className || '').slice(0, 120), area: r.width * r.height });
                      e.__bridgeSend = true;
                    }
                  }
                  candidates.sort((a, b) => a.area - b.area);
                  if (candidates.length) {
                    const all = [...modal.querySelectorAll('*')].filter((e) => e.__bridgeSend);
                    all.sort((a, b) => {
                      const ra = a.getBoundingClientRect(), rb = b.getBoundingClientRect();
                      return ra.width * ra.height - rb.width * rb.height;
                    });
                    all[0].setAttribute('data-dy-send', '1');
                  }
                  return { modal: true, candidates: candidates.slice(0, 8) };
                }
            """
            # The upload confirmation is a normal button in the main frame.
            # Its class is stable across the current web build; click it with
            # Playwright's actionability checks before any coordinate logic.
            for frame in page.frames:
                try:
                    if frame.evaluate(
                        "() => { const b = document.querySelector('button.MsgInputSendFileModalbtnSure'); if (!b) return false; b.click(); return true; }"
                    ):
                        clicked = True
                        break
                except Exception as exc:
                    debug.setdefault("button_error", f"{type(exc).__name__}: {exc}")
            if not clicked:
                for frame in page.frames:
                    try:
                        info = frame.evaluate(finder)
                    except Exception:
                        continue
                    debug["frames"].append({"url": frame.url, "modal": bool(info.get("modal")), "candidates": info.get("candidates") or []})
                    if not info.get("modal") or not info.get("candidates"):
                        continue
                    try:
                        marker = frame.locator("[data-dy-send='1']")
                        if marker.count():
                            marker.last.click(force=True, timeout=3000)
                            clicked = True
                            break
                    except Exception as exc:
                        debug.setdefault("click_error", f"{type(exc).__name__}: {exc}")
                    try:
                        frame.evaluate("() => { const e = document.querySelector('[data-dy-send=\"1\"]'); if (e) e.click(); }")
                        clicked = True
                        break
                    except Exception:
                        continue
            if not clicked:
                page.keyboard.press("Enter")
            time.sleep(1.0)
            for _ in range(10):
                try:
                    if not page.evaluate("() => Boolean(document.querySelector('[class*=\"MsgInputSendFileModal\"]'))"):
                        break
                except Exception:
                    break
                time.sleep(0.3)
            time.sleep(1.5)
            try:
                Path("/tmp/douyin-send-debug.json").write_text(json.dumps(debug, ensure_ascii=False), encoding="utf-8")
            except Exception:
                pass
            after = self.read_messages(limit=20)
            after_images = sum(1 for item in after if item.direction == "out" and item.kind == "image")
            try:
                modal_gone = not any(
                    frame.evaluate("() => Boolean(document.querySelector('[class*=\"MsgInputSendFileModal\"]'))")
                    for frame in page.frames
                )
            except Exception:
                modal_gone = False
            # The Douyin chat has no message id to read back.  A dismissed
            # upload confirmation plus a new outbound image is the strongest
            # page-level evidence available; either one is sufficient here,
            # while the job layer still records exactly what was observed.
            return {
                "ok": True,
                "verified": bool(modal_gone or after_images > before_images),
                "modal_gone": modal_gone,
                "before": before_images,
                "after": after_images,
            }

    def fetch_image_bytes(self, url: str, *, max_bytes: int) -> bytes:
        """Fetch a chat image with the page's cookies; fall back to page fetch."""

        target = str(url or "").strip()
        if target.startswith("data:"):
            match = re.match(r"^data:image/[^;]+;base64,(.+)$", target, re.IGNORECASE | re.DOTALL)
            if not match:
                raise DouyinChatError("CHAT_IMAGE_BAD_DATA_URL", "图片 data URL 无法解析")
            import base64

            data = base64.b64decode(match.group(1))
            if len(data) > max_bytes:
                raise DouyinChatError("CHAT_IMAGE_TOO_LARGE", "图片超过大小上限")
            return data
        if not target.startswith(("http://", "https://", "blob:")):
            raise DouyinChatError("CHAT_IMAGE_BAD_URL", "图片地址不是 HTTP(S)/blob")
        with self._lock:
            page = self._ensure_page()
            if target.startswith(("http://", "https://")):
                try:
                    response = page.context.request.get(target, headers={"Referer": "https://www.douyin.com/"}, timeout=20_000)
                    if response.ok:
                        body = response.body()
                        if body:
                            if len(body) > max_bytes:
                                raise DouyinChatError("CHAT_IMAGE_TOO_LARGE", "图片超过大小上限")
                            return body
                except DouyinChatError:
                    raise
                except Exception:
                    pass
            data_url = page.evaluate(
                r"""
                async (url) => {
                  const response = await fetch(url, { credentials: 'include' });
                  if (!response.ok) throw new Error('HTTP ' + response.status);
                  const blob = await response.blob();
                  return await new Promise((resolve, reject) => {
                    const reader = new FileReader();
                    reader.onload = () => resolve(String(reader.result || ''));
                    reader.onerror = () => reject(new Error('read failed'));
                    reader.readAsDataURL(blob);
                  });
                }
                """,
                target,
            )
            match = re.match(r"^data:image/[^;]+;base64,(.+)$", str(data_url or ""), re.IGNORECASE | re.DOTALL)
            if not match:
                raise DouyinChatError("CHAT_IMAGE_FETCH_FAILED", "浏览器没有取回图片")
            import base64

            data = base64.b64decode(match.group(1))
            if len(data) > max_bytes:
                raise DouyinChatError("CHAT_IMAGE_TOO_LARGE", "图片超过大小上限")
            return data

    def dom_probe(self) -> dict[str, Any]:
        """Read-only DOM diagnostics for conversation identity (no message bodies)."""

        with self._lock:
            page = self._ensure_page()
            value = page.evaluate(
                r"""
                () => {
                  const rows = [...document.querySelectorAll('[class*="conversationConversationItem"]')];
                  const out = {
                    href: location.href,
                    rows: rows.slice(0, 25).map((r) => ({
                      text: ((r.innerText || '').split('\n')[0] || '').slice(0, 40),
                      cls: String(r.className || '').slice(0, 200),
                      aria: r.getAttribute('aria-selected'),
                    })),
                    active: [],
                    headers: [],
                    editor: Boolean(document.querySelector('div[contenteditable="true"]')),
                  };
                  for (const r of rows) {
                    const cls = String(r.className || '');
                    if (/active|current|selected|highlight/i.test(cls)) {
                      out.active.push({ text: ((r.innerText || '').split('\n')[0] || '').slice(0, 40), cls: cls.slice(0, 200) });
                    }
                  }
                  const els = [...document.querySelectorAll('div,span,h1,h2,h3,p')].filter((e) => {
                    const text = (e.innerText || '').trim();
                    if (!text || text.length > 24) return false;
                    const r = e.getBoundingClientRect();
                    return r.width > 0 && r.height > 0 && r.top >= 0 && r.top < 240 && r.left > innerWidth * 0.28;
                  });
                  out.headers = els.slice(0, 40).map((e) => {
                    const r = e.getBoundingClientRect();
                    return {
                      tag: e.tagName,
                      text: (e.innerText || '').trim().slice(0, 40),
                      cls: String(e.className || '').slice(0, 200),
                      x: Math.round(r.x), y: Math.round(r.y), w: Math.round(r.width),
                    };
                  });
                  const titles = [...document.querySelectorAll('[class*="conversationConversationItemtitle"]')]
                    .map((e) => (e.innerText || '').trim()).filter(Boolean).slice(0, 10);
                  out.names = [...new Set(titles)];
                  out.scrollChain = [];
                  try {
                    let node = rows[0];
                    for (let i = 0; i < 10 && node; i += 1) {
                      const style = getComputedStyle(node);
                      out.scrollChain.push({
                        tag: node.tagName,
                        cls: String(node.className || '').slice(0, 160),
                        scrollTop: node.scrollTop,
                        scrollHeight: node.scrollHeight,
                        clientHeight: node.clientHeight,
                        overflowY: style.overflowY,
                        position: style.position,
                      });
                      node = node.parentElement;
                    }
                  } catch (e) {}
                  out.windowScrollY = window.scrollY;
                  out.nameMatches = [];
                  for (const name of out.names) {
                    for (const e of document.querySelectorAll('div,span,h1,h2,h3,p')) {
                      if ((e.innerText || '').trim() !== name) continue;
                      const r = e.getBoundingClientRect();
                      if (r.width <= 0 || r.height <= 0) continue;
                      const chain = [];
                      let p = e;
                      for (let i = 0; i < 4 && p; i += 1) {
                        chain.push({ tag: p.tagName, cls: String(p.className || '').slice(0, 120) });
                        p = p.parentElement;
                      }
                      out.nameMatches.push({
                        name,
                        tag: e.tagName,
                        cls: String(e.className || '').slice(0, 160),
                        x: Math.round(r.x), y: Math.round(r.y),
                        w: Math.round(r.width), h: Math.round(r.height),
                        chain,
                      });
                    }
                  }
                  out.wrapperClasses = [...document.querySelectorAll('[class*="conversationConversationItemwrapper"]')]
                    .slice(0, 8).map((e) => String(e.className || ''));
                  const zone = document.querySelector('[class*="messageMessageList"]');
                  out.zoneAncestors = [];
                  let node = zone;
                  for (let i = 0; i < 6 && node; i += 1) {
                    out.zoneAncestors.push({
                      tag: node.tagName,
                      cls: String(node.className || '').slice(0, 160),
                      firstLine: ((node.innerText || '').split('\n')[0] || '').slice(0, 40),
                    });
                    node = node.parentElement;
                  }
                  return out;
                }
                """
            )
            return value if isinstance(value, dict) else {}

    def calibrate(self, *, live: bool = True) -> dict[str, Any]:
        """Dump selector-relevant structure, never message bodies.

        ``live=False`` only returns the result cached by the poll thread, so a
        console HTTP thread never touches the Playwright page.
        """

        if not live:
            return dict(self._last_calibration) or {
                "available": False,
                "hint": "先让桥轮询一轮（POST /api/douyin-bridge/run），再读校准结果",
            }
        with self._lock:
            page = self._ensure_page()
            payload = page.evaluate(
                r"""
                () => {
                  const zone = document.querySelector('[class*="messageMessageListlist"]')
                    || document.querySelector('[class*="messageMessageListwrapper"]');
                  const rows = zone ? [...zone.querySelectorAll('[class*="messageMessageBox"]')].slice(0, 3) : [];
                  return {
                    url: location.href,
                    title: document.title,
                    conversation_rows: document.querySelectorAll('[class*="conversationConversationItem"]').length,
                    editor: Boolean(document.querySelector('div[contenteditable="true"]')),
                    zone_class: zone ? String(zone.className || '') : '',
                    row_classes: rows.map((r) => String(r.className || '')),
                    row_shapes: rows.map((r) => ({
                      text_nodes: r.querySelectorAll('[class*="TextMessage"]').length,
                      images: r.querySelectorAll('img').length,
                      links: r.querySelectorAll('a[href]').length,
                    })),
                  };
                }
                """
            )
            result = payload if isinstance(payload, dict) else {}
            self._last_calibration = result
            return dict(result)
