"""Visible browser adapters for the public-platform connector.

The adapter intentionally drives the normal web UI with a persistent browser
profile.  It does not alter fingerprints, inject private endpoints, bypass a
challenge, or retry a blocked write.  The browser is opened only when the
desktop operator asks for it, and write actions require a separate explicit
confirmation through :class:`PlatformService`.

The selectors are kept in this module because both platforms change their DOM
more often than the rest of the application.  The data returned by
``browse``/``read_comments`` always comes from the current page.  If a
comment does not expose a stable DOM identifier, it is skipped instead of
guessing an identifier from a display name.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
import re
import shutil
from threading import RLock
from time import monotonic, sleep
from typing import Any, Mapping
from urllib.parse import quote, urljoin, urlparse

from .models import BrowseItem, PlatformAction, PlatformComment, PlatformName


class BrowserPlatformError(RuntimeError):
    """Expected connector failure that should be shown in the console."""


class PlatformLoginRequired(BrowserPlatformError):
    """The visible browser needs the operator to complete login."""


class PlatformChallengeDetected(BrowserPlatformError):
    """A login, verification, or risk page was detected; stop safely."""


class PlatformWriteBlocked(BrowserPlatformError):
    """A write was blocked by the local safety gates."""


# Native mention handling -------------------------------------------------
#
# The ledger stores stable platform user IDs (and, when the identity layer
# knows them, the matching display names).  The adapter may only mention a
# user by driving the platform's own suggestion popup: typing a literal
# ``@display_name`` and sending it would claim a mention that the platform
# never registered.  A stable ID is the only authoritative key; a display
# name may break ties but never overrides a conflicting stable ID.
_MENTION_TEXT_RE = re.compile(r"@([^\s@,，。！？!?；;：:#（）()\[\]{}]+)")
_MENTION_ID_ATTRS: tuple[str, ...] = (
    "data-user-id",
    "data-userid",
    "data-uid",
    "data-id",
    "data-target-id",
    "data-value",
    "data-key",
)
_MENTION_NAME_ATTRS: tuple[str, ...] = (
    "aria-label",
    "data-user-name",
    "data-nickname",
    "data-name",
    "title",
)
_MENTION_ARIA_ATTRS: tuple[str, ...] = ("aria-label", "title")


@dataclass(frozen=True, slots=True)
class MentionTarget:
    """One authorized native mention taken from the action ledger metadata.

    ``user_id`` is the stable platform user ID and is the only identity that
    authorizes a click on a suggestion.  ``name`` is an optional display name
    used to pair a textual ``@placeholder`` with this ID and to confirm the
    posted comment; it never authorizes a mention by itself.
    """

    user_id: str
    name: str | None = None


@dataclass(frozen=True, slots=True)
class _MentionCandidate:
    """A visible native mention suggestion read from the current page."""

    node: Any
    stable_ids: tuple[str, ...]
    labels: tuple[str, ...]
    aria_labels: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _MentionComposition:
    """How a plan text is turned into native mentions plus plain text."""

    operations: tuple[tuple[str, Any], ...]
    chunks: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class BrowserProfile:
    platform: PlatformName
    home_url: str
    search_url: str
    item_patterns: tuple[re.Pattern[str], ...]
    comment_selectors: tuple[str, ...]
    editor_selectors: tuple[str, ...]
    login_markers: tuple[str, ...]
    challenge_markers: tuple[str, ...]
    authenticated_selectors: tuple[str, ...] = (
        "[data-e2e*='user']",
        "[data-testid*='user']",
        "a[href*='/profile']",
    )
    authenticated_markers: tuple[str, ...] = ("退出登录", "个人主页", "我的主页")
    reply_markers: tuple[str, ...] = ("回复", "Reply")
    send_markers: tuple[str, ...] = ("发送", "评论", "发布", "Send", "Post")
    # Native mention suggestion popup.  The platform may require a different
    # trigger string; the adapter types ``mention_trigger`` and then matches
    # the popup items against the ledger's stable IDs.
    mention_trigger: str = "@"
    mention_candidate_selectors: tuple[str, ...] = ()
    mention_candidate_item_selectors: tuple[str, ...] = ()


PROFILES: dict[PlatformName, BrowserProfile] = {
    "douyin": BrowserProfile(
        platform="douyin",
        home_url="https://www.douyin.com/",
        search_url="https://www.douyin.com/search/{query}",
        item_patterns=(
            re.compile(r"/video/[A-Za-z0-9_-]+"),
            re.compile(r"/note/[A-Za-z0-9_-]+"),
        ),
        comment_selectors=(
            "[data-comment-id]",
            "[data-e2e*='comment']",
            "[class*='comment-item']",
            "[class*='CommentItem']",
        ),
        editor_selectors=(
            "textarea",
            "[contenteditable='true']",
            "input[placeholder*='评论']",
            "input[placeholder*='回复']",
        ),
        login_markers=("登录", "扫码登录", "Log in", "Sign in"),
        challenge_markers=("验证码", "安全验证", "行为异常", "访问受限", "IP存在风险", "切换可靠网络", "captcha", "verify"),
        mention_trigger="@",
        mention_candidate_selectors=(
            "[data-e2e*='mention']",
            "[data-e2e*='suggest']",
            "[class*='mention-list']",
            "[class*='MentionList']",
            "[class*='mention']",
            "[class*='suggest']",
            "[class*='Suggestion']",
            "[role='listbox']",
        ),
        mention_candidate_item_selectors=(
            "[data-user-id]",
            "[data-userid]",
            "[data-uid]",
            "[role='option']",
            "[data-id]",
            "li",
            "[class*='item']",
            "[class*='Item']",
        ),
    ),
    "xiaohongshu": BrowserProfile(
        platform="xiaohongshu",
        home_url="https://www.xiaohongshu.com/explore",
        search_url="https://www.xiaohongshu.com/search_result?keyword={query}",
        item_patterns=(
            re.compile(r"/explore/[A-Za-z0-9_-]+"),
            re.compile(r"/discovery/item/[A-Za-z0-9_-]+"),
        ),
        comment_selectors=(
            "[data-comment-id]",
            "[data-e2e*='comment']",
            "[class*='comment-item']",
            "[class*='CommentItem']",
        ),
        editor_selectors=(
            "textarea",
            "[contenteditable='true']",
            "input[placeholder*='评论']",
            "input[placeholder*='回复']",
        ),
        login_markers=("登录", "扫码登录", "Log in", "Sign in"),
        challenge_markers=("验证码", "安全验证", "行为异常", "访问受限", "IP存在风险", "切换可靠网络", "captcha", "verify"),
        mention_trigger="@",
        mention_candidate_selectors=(
            "[data-e2e*='mention']",
            "[class*='mention']",
            "[class*='Mention']",
            "[class*='at-list']",
            "[class*='AtList']",
            "[class*='suggest']",
            "[class*='Suggest']",
            "[role='listbox']",
        ),
        mention_candidate_item_selectors=(
            "[data-user-id]",
            "[data-userid]",
            "[data-uid]",
            "[role='option']",
            "[data-id]",
            "li",
            "[class*='item']",
            "[class*='Item']",
        ),
    ),
}


class BrowserPlatformAdapter:
    """A guarded, visible Playwright session for one platform.

    This class is intentionally synchronous because the desktop HTTP console
    and the Weixin UI connector are synchronous.  The persistent context is
    created lazily by ``open`` and remains owned by the local desktop process.
    """

    def __init__(
        self,
        platform: PlatformName,
        *,
        data_dir: Path,
        executable_path: str | None = None,
        account_id: str = "unbound",
        headless: bool = False,
        scroll_steps: int = 5,
        page_timeout_ms: int = 20_000,
        write_enabled: bool = False,
        action_delay_seconds: float = 30.0,
        max_actions_per_hour: int = 5,
        mention_trigger: str | None = None,
        mention_candidate_selectors: tuple[str, ...] | list[str] | None = None,
        mention_candidate_item_selectors: tuple[str, ...] | list[str] | None = None,
        mention_match_retries: int = 2,
    ):
        if platform not in PROFILES:
            raise ValueError(f"unsupported_platform:{platform}")
        self.platform = platform
        profile = PROFILES[platform]
        profile_overrides: dict[str, object] = {}
        if mention_trigger is not None and str(mention_trigger).strip():
            profile_overrides["mention_trigger"] = str(mention_trigger)
        if mention_candidate_selectors is not None:
            profile_overrides["mention_candidate_selectors"] = tuple(
                str(selector).strip() for selector in mention_candidate_selectors if str(selector).strip()
            )
        if mention_candidate_item_selectors is not None:
            profile_overrides["mention_candidate_item_selectors"] = tuple(
                str(selector).strip() for selector in mention_candidate_item_selectors if str(selector).strip()
            )
        if profile_overrides:
            profile = replace(profile, **profile_overrides)  # type: ignore[arg-type]
        self.profile = profile
        self.account_id = account_id.strip() or "unbound"
        self.data_dir = Path(data_dir) / platform
        self.executable_path = executable_path.strip() if executable_path else None
        self.headless = bool(headless)
        self.scroll_steps = max(1, min(int(scroll_steps), 20))
        self.page_timeout_ms = max(5_000, min(int(page_timeout_ms), 120_000))
        self.write_enabled = bool(write_enabled)
        self.action_delay_seconds = max(0.0, float(action_delay_seconds))
        self.max_actions_per_hour = max(1, min(int(max_actions_per_hour), 100))
        # Initial attempt plus this many scroll-and-retry rounds when the
        # suggestion popup is visible but no candidate matches the stable ID.
        self.mention_match_retries = max(0, min(int(mention_match_retries), 5))
        self._lock = RLock()
        self._playwright: Any | None = None
        self._context: Any | None = None
        self._page: Any | None = None
        self._items: dict[str, BrowseItem] = {}
        self._comments: dict[str, PlatformComment] = {}
        self._action_times: list[float] = []
        self._last_error: str | None = None

    def login_status(self) -> dict[str, object]:
        with self._lock:
            status: dict[str, object] = {
                "platform": self.platform,
                "mode": "browser",
                "connected": bool(self._context and self._page),
                "logged_in": False,
                "account_id": self.account_id,
                "profile_dir": str(self.data_dir),
                "requires_manual_login": True,
                "write_enabled": self.write_enabled,
                "last_error": self._last_error,
            }
            if not self._page:
                return status
            status["url"] = self._page.url
            try:
                text = self._body_text()
                challenge = self._challenge_reason(text)
                status["challenge"] = challenge
                logged_in = not self._looks_logged_out(self._page.url, text)
                status["logged_in"] = logged_in and not challenge and self._authenticated_evidence(text)
                status["requires_manual_login"] = not status["logged_in"]
            except Exception as exc:  # browser may have been closed externally
                status["last_error"] = f"{type(exc).__name__}: {exc}"
            return status

    def open(self) -> dict[str, object]:
        """Launch a visible persistent browser and navigate to the home page."""
        with self._lock:
            try:
                self._ensure_browser()
                if not self._page.url or self._page.url == "about:blank":
                    self._page.goto(self.profile.home_url, wait_until="domcontentloaded")
                else:
                    self._page.goto(self.profile.home_url, wait_until="domcontentloaded")
                self._settle()
                self._last_error = None
            except Exception as exc:
                self._last_error = f"{type(exc).__name__}: {exc}"
                raise BrowserPlatformError(self._last_error) from exc
            return self.login_status()

    def close(self) -> None:
        with self._lock:
            context, playwright = self._context, self._playwright
            self._context = None
            self._page = None
            self._playwright = None
            if context:
                try:
                    context.close()
                except Exception:
                    pass
            if playwright:
                try:
                    playwright.stop()
                except Exception:
                    pass

    def browse(self, query: str = "", limit: int = 20) -> list[BrowseItem]:
        with self._lock:
            self._ensure_ready()
            self._ensure_no_challenge()
            target = (
                self.profile.search_url.format(query=quote(query.strip()))
                if query.strip()
                else self.profile.home_url
            )
            self._goto(target)
            found: dict[str, BrowseItem] = {}
            for _ in range(self.scroll_steps):
                self._ensure_no_challenge()
                for item in self._extract_items():
                    found.setdefault(item.item_id, item)
                    if len(found) >= max(1, min(int(limit), 100)):
                        break
                if len(found) >= max(1, min(int(limit), 100)):
                    break
                self._scroll_once()
            self._items.update(found)
            return list(found.values())[: max(1, min(int(limit), 100))]

    def read_comments(self, item_id: str, limit: int = 50) -> list[PlatformComment]:
        with self._lock:
            self._ensure_ready()
            item = self._items.get(item_id)
            if not item:
                raise ValueError(f"item_not_found:{item_id}")
            self._ensure_no_challenge()
            self._goto(item.url)
            found: dict[str, PlatformComment] = {}
            for _ in range(min(self.scroll_steps, 6)):
                self._ensure_no_challenge()
                for comment in self._extract_comments(item):
                    found.setdefault(comment.comment_id, comment)
                    if len(found) >= max(1, min(int(limit), 200)):
                        break
                if len(found) >= max(1, min(int(limit), 200)):
                    break
                self._scroll_once()
            self._comments.update(found)
            return list(found.values())[: max(1, min(int(limit), 200))]

    def build_comment(self, item_id: str, text: str, *, dry_run: bool = True) -> PlatformAction:
        item = self._items.get(item_id)
        if not item:
            raise ValueError(f"item_not_found:{item_id}")
        return PlatformAction(
            platform=self.platform,
            account_id=self.account_id,
            action_type="comment",
            target_id=item_id,
            text=_validate_text(text),
            target_url=item.url,
            dry_run=dry_run,
            metadata={"source": "current_page_browse", "operator_confirmation_required": True},
        )

    def build_reply(self, comment_id: str, text: str, *, dry_run: bool = True) -> PlatformAction:
        comment = self._comments.get(comment_id)
        if not comment:
            raise ValueError(f"comment_not_found:{comment_id}")
        item = self._items.get(comment.item_id)
        return PlatformAction(
            platform=self.platform,
            account_id=self.account_id,
            action_type="reply",
            target_id=comment_id,
            parent_comment_id=comment_id,
            text=_validate_text(text),
            target_url=item.url if item else None,
            dry_run=dry_run,
            metadata={"source": "current_page_comments", "operator_confirmation_required": True},
        )

    def execute_action(self, action: Mapping[str, object]) -> dict[str, object]:
        """Execute one already queued action after the console confirms it.

        Comments/replies that authorize native mentions must select the
        platform's own suggestion for every stable mention ID.  A literal
        ``@display_name`` is never typed as a substitute: if no candidate can
        be matched the write is blocked, because a plain-text ``@`` would
        claim a mention that the platform never registered.
        """
        with self._lock:
            if bool(action.get("dry_run")):
                raise PlatformWriteBlocked("dry_run_action_cannot_be_executed")
            if not self.write_enabled:
                raise PlatformWriteBlocked("platform_write_disabled")
            self._check_write_budget()
            target_url = str(action.get("target_url") or "").strip()
            if not target_url:
                raise BrowserPlatformError("action_target_url_missing")
            if not self._allowed_target_url(target_url):
                raise PlatformWriteBlocked("action_target_host_not_allowed")
            mention_targets = _mention_targets_from_metadata(action.get("metadata"))
            self._ensure_ready()
            self._ensure_no_challenge()
            if not self._authenticated_evidence(self._body_text()):
                raise PlatformLoginRequired(f"{self.platform}_manual_login_required")
            self._goto(target_url)
            self._ensure_no_challenge()
            action_type = str(action.get("action_type") or "")
            text = _validate_text(str(action.get("text") or ""))
            mention_names: list[str | None] = []
            composition: _MentionComposition | None = None
            if action_type == "reply":
                self._open_reply_editor(str(action.get("parent_comment_id") or action.get("target_id") or ""))
            elif action_type != "comment":
                raise BrowserPlatformError(f"unsupported_action_type:{action_type}")
            if mention_targets:
                composition = _plan_mention_composition(text, mention_targets)
                mention_names = self._compose_mention_comment(composition)
            else:
                self._fill_editor(text)
            self._ensure_no_challenge()
            self._click_send()
            self._settle()
            self._ensure_no_challenge()
            if composition is not None:
                if not self._mention_write_visible(composition.chunks, mention_names):
                    self._last_error = "native_mention_not_confirmed_in_page"
                    raise BrowserPlatformError("native_mention_not_confirmed_in_page")
            elif not self._text_visible(text):
                raise BrowserPlatformError("write_not_confirmed_in_page")
            self._action_times.append(monotonic())
            result: dict[str, object] = {
                "platform": self.platform,
                "action_type": action_type,
                "target_id": str(action.get("target_id") or ""),
                "confirmed_in_page": True,
            }
            if mention_targets:
                result["mention_target_ids"] = [target.user_id for target in mention_targets]
                result["mention_names"] = list(mention_names)
            return result

    # -- Playwright lifecycle -------------------------------------------------

    def _ensure_browser(self) -> None:
        if self._context and self._page:
            return
        try:
            from playwright.sync_api import sync_playwright
        except Exception as exc:  # pragma: no cover - depends on deployment
            raise BrowserPlatformError("playwright_not_installed") from exc
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self._playwright = sync_playwright().start()
        try:
            options: dict[str, object] = {
                "user_data_dir": str(self.data_dir),
                "headless": self.headless,
                "viewport": {"width": 1440, "height": 1000},
            }
            browser_executable = self.executable_path or _find_browser_executable()
            if browser_executable:
                options["executable_path"] = browser_executable
            self._context = self._playwright.chromium.launch_persistent_context(**options)
            self._page = self._context.pages[0] if self._context.pages else self._context.new_page()
            self._page.set_default_timeout(self.page_timeout_ms)
        except Exception:
            try:
                self._playwright.stop()
            except Exception:
                pass
            self._playwright = None
            self._context = None
            self._page = None
            raise

    def _ensure_ready(self) -> None:
        self._ensure_browser()
        if not self._page.url or self._page.url == "about:blank":
            self._page.goto(self.profile.home_url, wait_until="domcontentloaded")
        text = self._body_text()
        self._ensure_no_challenge(text)
        if self._looks_logged_out(self._page.url, text):
            raise PlatformLoginRequired(f"{self.platform}_manual_login_required")

    def _goto(self, target: str) -> None:
        self._page.goto(target, wait_until="domcontentloaded")
        self._settle()
        text = self._body_text()
        self._ensure_no_challenge(text)
        if self._looks_logged_out(self._page.url, text):
            raise PlatformLoginRequired(f"{self.platform}_manual_login_required")

    def _settle(self) -> None:
        # A short bounded wait lets lazy-loaded cards/comments settle without
        # turning a page change into a long blocking sleep.
        try:
            self._page.wait_for_load_state("networkidle", timeout=min(self.page_timeout_ms, 5_000))
        except Exception:
            pass
        sleep(0.35)

    def _scroll_once(self) -> None:
        self._page.mouse.wheel(0, 900)
        sleep(0.8)

    # -- Read extraction ------------------------------------------------------

    def _extract_items(self) -> list[BrowseItem]:
        out: list[BrowseItem] = []
        seen: set[str] = set()
        links = self._page.locator("a[href]")
        try:
            count = min(links.count(), 500)
        except Exception:
            return out
        for index in range(count):
            link = links.nth(index)
            try:
                href = str(link.get_attribute("href") or "").strip()
            except Exception:
                continue
            url = urljoin(self._page.url, href)
            match = next((p.search(urlparse(url).path) for p in self.profile.item_patterns), None)
            if not match:
                continue
            identifier = match.group(0).rstrip("/").rsplit("/", 1)[-1]
            kind = "video" if "/video/" in match.group(0) else "note"
            item_id = f"{self.platform}:{kind}:{identifier}"
            if item_id in seen:
                continue
            seen.add(item_id)
            try:
                title = " ".join((link.inner_text(timeout=500) or "").split())[:300]
            except Exception:
                title = ""
            if not title:
                try:
                    title = str(link.get_attribute("aria-label") or link.get_attribute("title") or "").strip()[:300]
                except Exception:
                    title = ""
            out.append(
                BrowseItem(
                    platform=self.platform,
                    item_id=item_id,
                    url=url,
                    title=title,
                    text=title,
                    metadata={"source_url": self._page.url},
                )
            )
        return out

    def _extract_comments(self, item: BrowseItem) -> list[PlatformComment]:
        out: list[PlatformComment] = []
        seen: set[str] = set()
        for selector in self.profile.comment_selectors:
            nodes = self._page.locator(selector)
            try:
                count = min(nodes.count(), 300)
            except Exception:
                continue
            for index in range(count):
                node = nodes.nth(index)
                comment_id = self._stable_dom_id(node)
                if not comment_id or comment_id in seen:
                    # Never synthesize a target ID from visible names/text.
                    continue
                try:
                    text = " ".join((node.inner_text(timeout=500) or "").split())[:2_000]
                except Exception:
                    continue
                if not text:
                    continue
                author_id = self._first_attr(node, "data-user-id", "data-uid", "data-author-id")
                author_name = self._first_attr(node, "data-author-name", "data-nickname") or ""
                if not author_name:
                    try:
                        author_name = " ".join((node.locator("[class*='name'],[class*='Name'],a").first.inner_text(timeout=300) or "").split())[:120]
                    except Exception:
                        author_name = ""
                parent_id = self._first_attr(node, "data-parent-id", "data-root-id")
                comment = PlatformComment(
                    platform=self.platform,
                    comment_id=comment_id,
                    item_id=item.item_id,
                    author_id=author_id,
                    author_name=author_name,
                    text=text,
                    parent_comment_id=parent_id,
                    metadata={"source_url": item.url},
                )
                seen.add(comment_id)
                out.append(comment)
        return out

    @staticmethod
    def _stable_dom_id(node: Any) -> str | None:
        for attr in ("data-comment-id", "data-id", "data-rpid", "id"):
            try:
                value = str(node.get_attribute(attr) or "").strip()
            except Exception:
                value = ""
            if value:
                return value
        return None

    @staticmethod
    def _first_attr(node: Any, *attrs: str) -> str | None:
        for attr in attrs:
            try:
                value = str(node.get_attribute(attr) or "").strip()
            except Exception:
                value = ""
            if value:
                return value
        return None

    # -- Guarded writes -------------------------------------------------------

    def _check_write_budget(self) -> None:
        now = monotonic()
        hour_ago = now - 3_600
        self._action_times = [value for value in self._action_times if value >= hour_ago]
        if len(self._action_times) >= self.max_actions_per_hour:
            raise PlatformWriteBlocked("platform_hourly_action_limit")
        if self._action_times and now - self._action_times[-1] < self.action_delay_seconds:
            raise PlatformWriteBlocked("platform_action_rate_limit")

    def _open_reply_editor(self, comment_id: str) -> None:
        if not comment_id:
            raise BrowserPlatformError("reply_comment_id_missing")
        target = None
        for selector in self.profile.comment_selectors:
            nodes = self._page.locator(selector)
            try:
                count = min(nodes.count(), 300)
            except Exception:
                continue
            for index in range(count):
                node = nodes.nth(index)
                if self._stable_dom_id(node) == comment_id:
                    target = node
                    break
            if target:
                break
        if target is None:
            raise BrowserPlatformError("reply_comment_not_visible")
        for marker in self.profile.reply_markers:
            try:
                button = target.get_by_text(marker, exact=False).first
                if button.is_visible():
                    button.click()
                    self._settle()
                    return
            except Exception:
                continue
        raise BrowserPlatformError("reply_button_not_found")

    def _fill_editor(self, text: str) -> None:
        for selector in self.profile.editor_selectors:
            editors = self._page.locator(selector)
            try:
                count = min(editors.count(), 20)
            except Exception:
                continue
            for index in range(count):
                editor = editors.nth(index)
                try:
                    if not editor.is_visible():
                        continue
                    editor.fill(text)
                    return
                except Exception:
                    continue
        raise BrowserPlatformError("comment_editor_not_found")

    def _click_send(self) -> None:
        for marker in self.profile.send_markers:
            try:
                button = self._page.get_by_role("button", name=re.compile(re.escape(marker), re.I)).first
                if button.is_visible():
                    button.click()
                    return
            except Exception:
                continue
        for marker in self.profile.send_markers:
            try:
                button = self._page.get_by_text(marker, exact=False).last
                if button.is_visible():
                    button.click()
                    return
            except Exception:
                continue
        raise BrowserPlatformError("comment_send_button_not_found")

    def _text_visible(self, text: str) -> bool:
        needle = " ".join(text.split())
        # Prefer a comment node so the editor itself cannot count as proof.
        for selector in self.profile.comment_selectors:
            nodes = self._page.locator(selector)
            try:
                count = min(nodes.count(), 300)
            except Exception:
                continue
            for index in range(count):
                try:
                    visible = nodes.nth(index).is_visible()
                    body = " ".join((nodes.nth(index).inner_text(timeout=300) or "").split())
                except Exception:
                    continue
                if visible and needle in body:
                    return True
        # Some pages render the newly posted comment in a generic list.  A
        # body match is accepted only after confirming the editor no longer
        # contains the submitted text.
        for selector in self.profile.editor_selectors:
            editors = self._page.locator(selector)
            try:
                count = min(editors.count(), 20)
            except Exception:
                continue
            for index in range(count):
                editor = editors.nth(index)
                try:
                    if not editor.is_visible():
                        continue
                    value = editor.input_value(timeout=300) if selector == "textarea" or selector.startswith("input") else editor.inner_text(timeout=300)
                    if needle == " ".join(str(value or "").split()):
                        return False
                except Exception:
                    continue
        return needle in " ".join(self._body_text().split())

    # -- Native mention selection ---------------------------------------------

    def _compose_mention_comment(self, composition: _MentionComposition) -> list[str | None]:
        """Type the comment text and select one native suggestion per mention.

        Plain text segments are typed around the native mentions so a mention
        keeps its intended position in the comment.  The literal
        ``@display_name`` placeholder from the plan is never typed.
        """
        editor = self._focus_editor()
        resolved_names: list[str | None] = []
        for kind, value in composition.operations:
            if kind == "text":
                chunk = str(value)
                if chunk:
                    self._type_into(editor, chunk)
                continue
            target = value
            resolved_names.append(self._insert_native_mention(editor, target))
        return resolved_names

    def _focus_editor(self) -> Any:
        for selector in self.profile.editor_selectors:
            editors = self._page.locator(selector)
            try:
                count = min(editors.count(), 20)
            except Exception:
                continue
            for index in range(count):
                editor = editors.nth(index)
                try:
                    if not editor.is_visible():
                        continue
                except Exception:
                    continue
                try:
                    editor.click()
                except Exception:
                    continue
                try:
                    editor.fill("")
                except Exception:
                    pass
                return editor
        raise BrowserPlatformError("comment_editor_not_found")

    def _type_into(self, editor: Any, text: str) -> None:
        """Type keystrokes so the platform's @-autocomplete listeners fire."""
        if not text:
            return
        sequencer = getattr(editor, "press_sequentially", None)
        if callable(sequencer):
            sequencer(text, delay=0)
            return
        typer = getattr(editor, "type", None)
        if callable(typer):
            typer(text)
            return
        keyboard = getattr(self._page, "keyboard", None)
        if keyboard is None:
            raise BrowserPlatformError("comment_editor_input_unavailable")
        keyboard.type(text)

    def _insert_native_mention(self, editor: Any, target: MentionTarget) -> str | None:
        self._type_into(editor, self.profile.mention_trigger)
        return self._wait_for_mention_candidate(target)

    def _wait_for_mention_candidate(self, target: MentionTarget) -> str | None:
        deadline = monotonic() + max(0.05, self.page_timeout_ms / 1000.0)
        retries_left = self.mention_match_retries
        saw_candidates = False
        while True:
            candidates = self._visible_mention_candidates()
            if candidates:
                saw_candidates = True
                match = self._match_mention_candidate(candidates, target)
                if match is not None:
                    node, resolved_name = match
                    try:
                        node.click()
                    except Exception as exc:
                        self._last_error = f"native_mention_candidate_click_failed:{target.user_id}"
                        raise BrowserPlatformError("native_mention_candidate_click_failed") from exc
                    sleep(0.2)
                    return resolved_name
                if retries_left <= 0:
                    break
                retries_left -= 1
                self._scroll_mention_candidates()
                if monotonic() >= deadline:
                    break
                sleep(0.15)
                continue
            if saw_candidates or monotonic() >= deadline:
                break
            sleep(0.2)
        self._last_error = f"native_mention_candidate_not_found:{target.user_id}"
        raise PlatformWriteBlocked("native_mention_candidate_not_found")

    def _visible_mention_candidates(self) -> list[_MentionCandidate]:
        """Read visible suggestions from the platform's own mention popup.

        Only suggestions nested inside a recognised popup container are
        trusted; a page-wide ``[data-id]`` fallback could click an unrelated
        element and send a comment without a registered mention.
        """
        found: list[_MentionCandidate] = []
        for container_selector in self.profile.mention_candidate_selectors:
            try:
                containers = self._page.locator(container_selector)
                container_count = min(containers.count(), 5)
            except Exception:
                continue
            for container_index in range(container_count):
                try:
                    container = containers.nth(container_index)
                    if not container.is_visible():
                        continue
                except Exception:
                    continue
                for item_selector in self.profile.mention_candidate_item_selectors:
                    before = len(found)
                    for candidate in self._candidates_in(container, item_selector):
                        if all(id(existing.node) != id(candidate.node) for existing in found):
                            found.append(candidate)
                    if len(found) > before:
                        break
                if found:
                    return found
        return found

    def _candidates_in(self, container: Any, item_selector: str) -> list[_MentionCandidate]:
        out: list[_MentionCandidate] = []
        try:
            nodes = container.locator(item_selector)
            count = min(nodes.count(), 100)
        except Exception:
            return out
        for index in range(count):
            try:
                node = nodes.nth(index)
                if not node.is_visible():
                    continue
            except Exception:
                continue
            out.append(_describe_mention_candidate(node))
        return out

    def _match_mention_candidate(
        self,
        candidates: list[_MentionCandidate],
        target: MentionTarget,
    ) -> tuple[Any, str | None] | None:
        """Match a suggestion to the ledger ID, using the name only as a hint.

        Rules, in order:
        1. a candidate whose stable DOM ID (``data-user-id``/``data-id``/...)
           equals the ledger ID;
        2. a candidate whose ``aria-label``/``title`` contains the ledger ID
           as a whole token;
        3. exactly one candidate whose display label matches the optional
           display name, and only when the candidate exposes no conflicting
           stable ID.  Multiple name matches are ambiguous and are refused.
        """
        wanted_id = _normalize_identifier(target.user_id)
        for candidate in candidates:
            if wanted_id and wanted_id in candidate.stable_ids:
                return candidate.node, _candidate_readback_name(candidate, target)
        if wanted_id:
            for candidate in candidates:
                if any(wanted_id in _identifier_tokens(label) for label in candidate.aria_labels):
                    return candidate.node, _candidate_readback_name(candidate, target)
        if target.name:
            named: list[_MentionCandidate] = []
            for candidate in candidates:
                if not any(_label_matches_name(label, target.name) for label in candidate.labels):
                    continue
                if candidate.stable_ids and wanted_id not in candidate.stable_ids:
                    # A differing stable ID means the display name is not proof.
                    continue
                named.append(candidate)
            if len(named) == 1:
                return named[0].node, _candidate_readback_name(named[0], target)
        return None

    def _scroll_mention_candidates(self) -> None:
        for container_selector in self.profile.mention_candidate_selectors:
            try:
                containers = self._page.locator(container_selector)
                count = min(containers.count(), 5)
            except Exception:
                continue
            for index in range(count):
                try:
                    container = containers.nth(index)
                    if not container.is_visible():
                        continue
                    container.evaluate("(element) => { element.scrollTop = element.scrollHeight; }")
                    return
                except Exception:
                    continue
        try:
            self._page.mouse.wheel(0, 600)
        except Exception:
            pass

    def _mention_write_visible(self, chunks: tuple[str, ...], names: list[str | None]) -> bool:
        """Confirm the posted comment shows the text and every @ display name."""
        wanted_chunks = [normalized for chunk in chunks if (normalized := " ".join(str(chunk).split()))]
        if any(not name for name in names):
            # Without a visible display name the native @ cannot be confirmed.
            return False
        wanted_names = [normalized for name in names if (normalized := _normalize_name(name))]
        if len(wanted_names) != len(names):
            return False
        for comment in self._visible_comment_texts():
            compact = " ".join(comment.split())
            if _contains_all(compact, wanted_chunks) and _contains_all(compact, wanted_names):
                return True
        # A body match is only accepted once the editor itself no longer holds
        # the composed text; otherwise a failed send could confirm itself.
        if self._editor_still_holds(wanted_chunks or wanted_names):
            return False
        body = " ".join(self._body_text().split())
        return _contains_all(body, wanted_chunks) and _contains_all(body, wanted_names)

    def _editor_still_holds(self, needles: list[str]) -> bool:
        if not needles:
            return False
        for selector in self.profile.editor_selectors:
            editors = self._page.locator(selector)
            try:
                count = min(editors.count(), 20)
            except Exception:
                continue
            for index in range(count):
                editor = editors.nth(index)
                try:
                    if not editor.is_visible():
                        continue
                    if selector == "textarea" or selector.startswith("input"):
                        value = editor.input_value(timeout=300)
                    else:
                        value = editor.inner_text(timeout=300)
                except Exception:
                    continue
                compact = " ".join(str(value or "").split())
                if compact and _contains_all(compact, needles):
                    return True
        return False

    def _visible_comment_texts(self) -> list[str]:
        texts: list[str] = []
        for selector in self.profile.comment_selectors:
            nodes = self._page.locator(selector)
            try:
                count = min(nodes.count(), 300)
            except Exception:
                continue
            for index in range(count):
                try:
                    node = nodes.nth(index)
                    if not node.is_visible():
                        continue
                    texts.append(" ".join(str(node.inner_text(timeout=500) or "").split()))
                except Exception:
                    continue
        return texts

    def _allowed_target_url(self, target_url: str) -> bool:
        host = (urlparse(target_url).hostname or "").casefold().rstrip(".")
        allowed = {
            "douyin": ("douyin.com",),
            "xiaohongshu": ("xiaohongshu.com", "xhslink.com"),
        }[self.platform]
        return any(host == suffix or host.endswith("." + suffix) for suffix in allowed)

    # -- Page guards ----------------------------------------------------------

    def _body_text(self) -> str:
        try:
            return str(self._page.locator("body").inner_text(timeout=1_500) or "")[:30_000]
        except Exception:
            return ""

    def _ensure_no_challenge(self, text: str | None = None) -> None:
        body = text if text is not None else self._body_text()
        reason = self._challenge_reason(body)
        if reason:
            raise PlatformChallengeDetected(f"{self.platform}_challenge_detected:{reason}")

    def _challenge_reason(self, text: str) -> str | None:
        lowered = text.casefold()
        for marker in self.profile.challenge_markers:
            if marker.casefold() in lowered:
                return marker
        return None

    def _looks_logged_out(self, url: str, text: str) -> bool:
        lowered_url = url.casefold()
        if any(token in lowered_url for token in ("login", "passport", "signin")):
            return True
        lowered = text.casefold()
        # A login marker alone is not enough because a page may keep a small
        # login link visible.  Require two markers or a login URL.
        hits = sum(1 for marker in self.profile.login_markers if marker.casefold() in lowered)
        return hits >= 2

    def _authenticated_evidence(self, text: str) -> bool:
        lowered = text.casefold()
        if any(marker.casefold() in lowered for marker in self.profile.authenticated_markers):
            return True
        for selector in self.profile.authenticated_selectors:
            try:
                if self._page.locator(selector).count() > 0:
                    return True
            except Exception:
                continue
        return False


def _validate_text(text: str) -> str:
    value = " ".join(str(text).split())
    if not value:
        raise ValueError("action_text_empty")
    if len(value) > 2_000:
        raise ValueError("action_text_too_long")
    return value


# -- Mention metadata helpers -------------------------------------------------


def _mention_targets_from_metadata(metadata: object) -> list[MentionTarget]:
    """Read optional native-mention targets from the ledger record metadata.

    ``mention_target_ids`` is required by the service whenever the action text
    contains ``@``.  ``mention_target_names`` is optional and index-aligned;
    when it is missing the adapter still works with stable IDs (the display
    name is then read from the matching suggestion and used for readback).
    """
    if not isinstance(metadata, Mapping):
        return []
    raw_ids = metadata.get("mention_target_ids")
    if raw_ids is None:
        return []
    user_ids = _normalize_mention_ids(raw_ids)
    if not user_ids:
        return []
    names = _normalize_mention_names(metadata.get("mention_target_names"))
    return [
        MentionTarget(user_id=user_id, name=names[index] if index < len(names) else None)
        for index, user_id in enumerate(user_ids)
    ]


def _normalize_mention_ids(raw: object) -> list[str]:
    if isinstance(raw, str):
        items: list[object] = [part for part in re.split(r"[,\s]+", raw) if part]
    elif isinstance(raw, (list, tuple, set, frozenset)):
        items = list(raw)
    else:
        raise PlatformWriteBlocked("native_mention_metadata_invalid")
    user_ids: list[str] = []
    for item in items:
        if item is None:
            continue
        if isinstance(item, bool) or not isinstance(item, (str, int)):
            raise PlatformWriteBlocked("native_mention_metadata_invalid")
        value = str(item).strip()
        if value and value not in user_ids:
            user_ids.append(value)
    return user_ids


def _normalize_mention_names(raw: object) -> list[str | None]:
    if raw is None:
        return []
    if isinstance(raw, str):
        items: list[object] = [part for part in re.split(r"[,\n]+", raw)]
    elif isinstance(raw, (list, tuple)):
        items = list(raw)
    else:
        return []
    names: list[str | None] = []
    for item in items:
        if item is None or isinstance(item, bool) or not isinstance(item, (str, int)):
            names.append(None)
            continue
        text = str(item).strip().lstrip("@").strip()
        names.append(text or None)
    return names


def _plan_mention_composition(text: str, targets: list[MentionTarget]) -> _MentionComposition:
    """Turn ``@display_name`` placeholders into native-mention operations.

    Each authorized stable ID is paired with exactly one textual placeholder
    (by display name first, then by order).  A count mismatch means the plan
    text and the authorization metadata disagree; the write is blocked rather
    than sending a leftover literal ``@`` as plain text.
    """
    tokens = list(_MENTION_TEXT_RE.finditer(text))
    if not tokens:
        operations: list[tuple[str, Any]] = [("mention", target) for target in targets]
        operations.append(("text", text))
        return _MentionComposition(operations=tuple(operations), chunks=(text,))
    if len(tokens) != len(targets):
        raise PlatformWriteBlocked("native_mention_text_unresolved")
    pairs: list[int | None] = [None] * len(targets)
    used = [False] * len(tokens)
    for index, target in enumerate(targets):
        wanted = _normalize_name(target.name)
        if not wanted:
            continue
        for token_index, token in enumerate(tokens):
            if used[token_index]:
                continue
            if _normalize_name(token.group(1)) == wanted:
                pairs[index] = token_index
                used[token_index] = True
                break
    for index in range(len(targets)):
        if pairs[index] is not None:
            continue
        for token_index in range(len(tokens)):
            if not used[token_index]:
                pairs[index] = token_index
                used[token_index] = True
                break
    if any(pair is None for pair in pairs) or not all(used):
        raise PlatformWriteBlocked("native_mention_text_unresolved")
    ordered = sorted(
        ((int(pairs[index]), targets[index]) for index in range(len(targets))),
        key=lambda item: item[0],
    )
    operations = []
    chunks: list[str] = []
    cursor = 0
    for token_index, target in ordered:
        token = tokens[token_index]
        chunk = text[cursor : token.start()]
        if chunk:
            operations.append(("text", chunk))
        chunks.append(chunk)
        operations.append(("mention", target))
        cursor = token.end()
    tail = text[cursor:]
    if tail:
        operations.append(("text", tail))
    chunks.append(tail)
    return _MentionComposition(operations=tuple(operations), chunks=tuple(chunks))


def _describe_mention_candidate(node: Any) -> _MentionCandidate:
    stable_ids: list[str] = []
    for attr in _MENTION_ID_ATTRS:
        value = _normalize_identifier(_safe_attribute(node, attr))
        if value and value not in stable_ids:
            stable_ids.append(value)
    aria_labels: list[str] = []
    for attr in _MENTION_ARIA_ATTRS:
        value = _safe_attribute(node, attr)
        if value and value not in aria_labels:
            aria_labels.append(value)
    labels: list[str] = []
    try:
        raw_text = str(node.inner_text(timeout=500) or "")
    except Exception:
        raw_text = ""
    for line in raw_text.splitlines():
        normalized = " ".join(line.split())
        if normalized and normalized not in labels:
            labels.append(normalized)
    for attr in _MENTION_NAME_ATTRS:
        value = " ".join(_safe_attribute(node, attr).split())
        if value and value not in labels:
            labels.append(value)
    if not labels:
        labels = list(aria_labels)
    return _MentionCandidate(
        node=node,
        stable_ids=tuple(stable_ids),
        labels=tuple(labels),
        aria_labels=tuple(aria_labels),
    )


def _candidate_readback_name(candidate: _MentionCandidate, target: MentionTarget) -> str | None:
    if target.name:
        return target.name
    if candidate.labels:
        first = candidate.labels[0].lstrip("@").strip()
        # Suggestion rows often append the platform ID after the display name.
        return (first.split(" ")[0] or None) if first else None
    return None


def _safe_attribute(node: Any, name: str) -> str:
    try:
        return str(node.get_attribute(name) or "").strip()
    except Exception:
        return ""


def _normalize_identifier(value: object) -> str:
    return str(value or "").strip().casefold()


def _identifier_tokens(label: str) -> set[str]:
    return {
        _normalize_identifier(part)
        for part in re.split(r"[\s,，:：;；/\\()（）\[\]{}]+", label)
        if part.strip()
    }


def _normalize_name(value: object) -> str:
    text = " ".join(str(value or "").split())
    while text.startswith("@"):
        text = text[1:].lstrip()
    return text.casefold()


def _label_matches_name(label: str, target_name: str) -> bool:
    normalized_label = _normalize_name(label)
    normalized_target = _normalize_name(target_name)
    if not normalized_label or not normalized_target:
        return False
    if normalized_label == normalized_target:
        return True
    if not normalized_label.startswith(normalized_target):
        return False
    remainder = normalized_label[len(normalized_target) :]
    return bool(remainder) and remainder[0] in " \t（(·|-—|"


def _contains_all(haystack: str, needles: list[str]) -> bool:
    lowered = haystack.casefold()
    return all(needle.casefold() in lowered for needle in needles)


def _find_browser_executable() -> str | None:
    """Prefer an installed user browser when Playwright's bundle is absent."""
    candidates = [
        shutil.which("chrome"),
        shutil.which("msedge"),
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    ]
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return str(candidate)
    return None
