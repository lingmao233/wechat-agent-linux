from __future__ import annotations

"""Bounded online research for an explicitly addressed WeChat question.

The client keeps online material in the current request context.  It never
writes search results into ``memory_facts`` or treats a web author as a group
member.  Search providers are explicit adapters because each provider has a
different response contract and credential policy.
"""

from dataclasses import dataclass, field
import base64
import binascii
from html import unescape
from html.parser import HTMLParser
import ipaddress
import re
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx

from .config import Settings


@dataclass(frozen=True, slots=True)
class WebSource:
    title: str
    url: str
    snippet: str = ""
    content: str = ""
    source_type: str = "web"
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def prompt_text(self) -> str:
        body = self.content.strip() or self.snippet.strip()
        return f"标题：{self.title}\n链接：{self.url}\n内容：{body[:6000]}"


class WebResearchClient:
    """Search public sources and fetch public HTTP(S) pages.

    An empty ``WEB_FETCH_ALLOWED_DOMAINS`` means all public hostnames are
    accepted. A non-empty list can still narrow the scope when needed. Local,
    loopback, link-local, reserved, and ``*.local`` hosts remain blocked so
    this feature cannot become an accidental internal proxy.
    """

    _SEARCH_HINTS = re.compile(
        r"最新|今天|现在|实时|查一下|搜索|联网|网址|链接|新闻|价格|天气|资料|来源|抖音|小红书|真的吗|什么时候|谁是|官网|教程|文档|下载",
        re.IGNORECASE,
    )
    _XHS_NOTE_URL = re.compile(
        r"https?://(?:www\.)?(?:xiaohongshu\.com|xhslink\.com)"
        r"/(?:explore/[A-Za-z0-9_-]+|discovery/item/[A-Za-z0-9_-]+|[A-Za-z0-9_/?#=&.-]+)",
        re.IGNORECASE,
    )

    def __init__(self, settings: Settings, *, platform_service: Any | None = None):
        self.settings = settings
        self.platform_service = platform_service

    def research(self, question: str, *, intent: dict[str, object] | None = None) -> list[WebSource]:
        query = " ".join((question or "").split())
        if len(query) < 2:
            return []

        research_intent = intent
        if research_intent is None and getattr(self.settings, "platform_intent_enabled", False):
            # An explicit intent from the agent wiring wins; otherwise the
            # shared text rules are used so a direct WebResearchClient caller
            # also gets the holiday/food/guide trigger.
            try:
                from .platform_automation import detect_intent

                available: list[str] = []
                adapters = getattr(self.platform_service, "adapters", None)
                if isinstance(adapters, dict) and adapters:
                    available = [str(name) for name in adapters]
                research_intent = detect_intent(query, available_platforms=available or ("douyin", "xiaohongshu"))
            except Exception:  # intent is optional; never fail research
                research_intent = None

        sources: list[WebSource] = []
        xhs_requested = self._mentions_xiaohongshu(query)
        if self.settings.platform_browser_enabled and self.platform_service:
            sources.extend(self._platform_sources(query, intent=research_intent))

        if self.settings.web_fetch_enabled:
            # Keep an explicitly requested URL ahead of search results so a
            # direct ``web_fetch`` style question is not truncated away.
            sources.extend(self._explicit_url_sources(query))
        if self.settings.web_search_enabled and self._should_search(query) and not self._is_direct_fetch(query):
            # A generic search relay often ranks unrelated social sites above
            # Xiaohongshu.  Add a site hint while keeping the original words;
            # the provider still decides which public result is usable.
            search_query = f"site:xiaohongshu.com {query}" if xhs_requested else query
            search_results = self._search(search_query)
            if xhs_requested:
                sources.extend(self._xiaohongshu_sources_from(search_results))
            else:
                sources.extend(search_results)
            if xhs_requested and not self._has_xiaohongshu_note(sources):
                # Some search relays return a related page whose snippet
                # contains the public XHS note URL.  Retry the unqualified
                # query once and extract only validated XHS note links.
                if search_query != query:
                    sources.extend(self._xiaohongshu_sources_from(self._search(query)))
            if xhs_requested and not self._has_xiaohongshu_note(sources):
                # Strip conversational words and try the Latin site name as
                # well. Several relays index xiaohongshu better than the
                # Chinese brand name and expose a note URL in a snippet.
                for variant in self._xiaohongshu_query_variants(query):
                    sources.extend(self._xiaohongshu_sources_from(self._search(variant)))
                    if self._has_xiaohongshu_note(sources):
                        break
            if xhs_requested and not self._has_xiaohongshu_note(sources):
                # The configured relay may return an empty/irrelevant result
                # set for social pages.  Bing's public HTML search is a small
                # read-only fallback; it does not need another API key and is
                # still filtered to Xiaohongshu hosts before fetching.
                sources.extend(self._search_xiaohongshu_public(query))
        # If the browser session is unavailable or a search relay returns no
        # XHS note, keep one public search page in the evidence set.  It can be
        # fetched by WEB_FETCH and gives the model a concrete page to inspect;
        # it never pretends that a login/challenge page is a note result.
        if xhs_requested and not any(source.source_type.startswith("xiaohongshu") or
                                     "xiaohongshu.com" in source.url.casefold() or
                                     "xhslink.com" in source.url.casefold() for source in sources):
            sources.append(self._xiaohongshu_search_source(query))

        max_results = self.settings.web_search_max_results
        deduped = self._dedupe(sources)[:max_results]
        if self.settings.web_fetch_enabled:
            deduped = [self._fetch_source(source) for source in deduped]
        return deduped

    def _should_search(self, query: str) -> bool:
        """Avoid a network call for ordinary conversational @ messages."""
        return bool(
            self._SEARCH_HINTS.search(query)
            or query.endswith(("?", "？"))
            or re.search(r"https?://\S+", query, flags=re.IGNORECASE)
        )

    @staticmethod
    def _mentions_xiaohongshu(query: str) -> bool:
        value = str(query or "").casefold()
        return "小红书" in query or "xiaohongshu" in value or re.search(r"\bxhs\b", value) is not None

    @staticmethod
    def _xiaohongshu_query_variants(query: str) -> list[str]:
        value = " ".join(str(query or "").split())
        value = re.sub(r"^(?:帮我|请|麻烦)?(?:查|搜|搜索|找|看看|抓取|读取)(?:一下|下)?", "", value)
        value = re.sub(r"(?:小红书|xiaohongshu|\bxhs\b)", "", value, flags=re.IGNORECASE)
        value = re.sub(r"^[\s的上中里]+", "", value).strip()
        if not value:
            return []
        return [f"xiaohongshu {value}", f"小红书 {value}"]

    @staticmethod
    def _xiaohongshu_search_source(query: str) -> WebSource:
        from urllib.parse import quote

        url = "https://www.xiaohongshu.com/search_result?keyword=" + quote(query.strip())
        return WebSource(
            title="小红书公开搜索结果",
            url=url,
            snippet=f"小红书公开搜索：{query[:300]}",
            source_type="xiaohongshu_search",
            metadata={"platform": "xiaohongshu", "query": query[:300], "fallback": True},
        )

    def _search_xiaohongshu_public(self, query: str) -> list[WebSource]:
        """Return public XHS note links when the configured relay misses them."""

        from urllib.parse import quote

        search_urls = [
            ("360", "https://www.so.com/s?q=" + quote("site:xiaohongshu.com " + query)),
            ("bing", "https://www.bing.com/search?q=" + quote("site:xiaohongshu.com " + query)),
            ("sogou", "https://www.sogou.com/web?query=" + quote("xiaohongshu " + query)),
        ]
        results: list[WebSource] = []
        seen_urls: set[str] = set()
        try:
            with httpx.Client(timeout=self.settings.web_timeout_seconds, follow_redirects=True,
                              headers={"User-Agent": "Mozilla/5.0 (wechat-agent public research)"}) as client:
                for provider, url in search_urls:
                    try:
                        response = client.get(url)
                        response.raise_for_status()
                    except (httpx.HTTPError, UnicodeError):
                        # One public search engine being blocked or rate
                        # limited must not suppress the remaining fallbacks.
                        continue
                    html = response.text
                    anchors = re.findall(
                        r"<a[^>]+href=[\"']([^\"']+)[\"'][^>]*>(.*?)</a>", html,
                        flags=re.IGNORECASE | re.DOTALL,
                    )
                    for raw_url, raw_title in anchors:
                        target = self._resolve_bing_target(unescape(raw_url).strip())
                        parsed = urlparse(target)
                        host = (parsed.hostname or "").casefold().rstrip(".")
                        path = parsed.path.casefold()
                        is_xhs_host = (host == "xiaohongshu.com" or host.endswith(".xiaohongshu.com")
                                       or host == "xhslink.com" or host.endswith(".xhslink.com"))
                        is_short_link = host == "xhslink.com" or host.endswith(".xhslink.com")
                        is_note_path = path.startswith("/explore/") or "/discovery/item/" in path
                        if not is_xhs_host or (not is_short_link and not is_note_path):
                            continue
                        canonical_url = target.rstrip("/")
                        if canonical_url in seen_urls:
                            continue
                        seen_urls.add(canonical_url)
                        title = _clean_html(unescape(raw_title))[:300]
                        results.append(WebSource(
                            title=title or target,
                            url=target,
                            snippet=f"公开搜索结果：{query[:300]}",
                            source_type="xiaohongshu_search_result",
                            metadata={"platform": "xiaohongshu", "provider": provider},
                        ))
                        if len(results) >= self.settings.web_search_max_results:
                            break
                    if len(results) >= self.settings.web_search_max_results:
                        break
        except (httpx.HTTPError, UnicodeError):
            return results
        return results

    @classmethod
    def _xiaohongshu_sources_from(cls, sources: list[WebSource]) -> list[WebSource]:
        """Keep direct XHS notes and extract note URLs embedded in snippets."""

        result: list[WebSource] = []
        for source in sources:
            direct = cls._is_xiaohongshu_note_url(source.url)
            urls = [source.url] if direct else []
            urls.extend(cls._XHS_NOTE_URL.findall(
                " ".join((source.title, source.snippet, source.content))
            ))
            for target in urls:
                target = unescape(target).rstrip(".,;:!?，。；：！？）)")
                if not cls._is_xiaohongshu_note_url(target):
                    continue
                result.append(WebSource(
                    title=source.title or target,
                    url=target,
                    snippet=source.snippet,
                    source_type="xiaohongshu_search_result",
                    metadata={
                        **source.metadata,
                        "platform": "xiaohongshu",
                        "extracted_from": source.url if source.url != target else "",
                    },
                ))
        return result

    @classmethod
    def _is_xiaohongshu_note_url(cls, url: str) -> bool:
        parsed = urlparse(str(url or "").strip())
        host = (parsed.hostname or "").casefold().rstrip(".")
        if host == "xhslink.com" or host.endswith(".xhslink.com"):
            return bool(parsed.path.strip("/"))
        if not (host == "xiaohongshu.com" or host.endswith(".xiaohongshu.com")):
            return False
        path = parsed.path.casefold()
        return path.startswith("/explore/") or "/discovery/item/" in path

    @classmethod
    def _has_xiaohongshu_note(cls, sources: list[WebSource]) -> bool:
        return any(cls._is_xiaohongshu_note_url(source.url) for source in sources)

    @staticmethod
    def _resolve_bing_target(url: str) -> str:
        """Resolve Bing's ``/ck/a?...&u=a1<base64>`` redirect target."""

        try:
            parsed = urlparse(url)
            if parsed.hostname != "www.bing.com" or parsed.path != "/ck/a":
                return url
            encoded = parse_qs(parsed.query).get("u", [""])[0]
            if encoded.startswith("a1"):
                raw = encoded[2:]
                raw += "=" * (-len(raw) % 4)
                return base64.urlsafe_b64decode(raw.encode()).decode("utf-8")
        except (ValueError, UnicodeError, binascii.Error):
            pass
        return url

    @staticmethod
    def _is_direct_fetch(query: str) -> bool:
        return bool(
            re.search(r"https?://\S+", query, flags=re.IGNORECASE)
            and re.search(r"抓取|读取(?:网页|链接)?|打开链接|fetch", query, flags=re.IGNORECASE)
        )

    @staticmethod
    def _explicit_url_sources(query: str) -> list[WebSource]:
        """Turn an explicitly supplied URL into a local-fetch candidate.

        This is the WeChat equivalent of DSH's separate ``web_fetch`` tool:
        the URL is still checked by ``_allowed_host`` before any HTTP request,
        and it never goes through the DeepSeek search relay.
        """
        urls = re.findall(r"https?://[^\s<>\"'）)】]+", query, flags=re.IGNORECASE)
        return [WebSource(title=url, url=url) for url in urls[:10]]

    def _search(self, query: str) -> list[WebSource]:
        provider = self.settings.web_search_provider
        try:
            if provider == "brave":
                return self._search_brave(query)
            if provider == "tavily":
                return self._search_tavily(query)
            if provider == "searxng":
                return self._search_searxng(query)
            if provider == "deepseek-search-api":
                return self._search_deepseek_api(query)
        except (httpx.HTTPError, ValueError, KeyError, TypeError):
            # A search outage must not prevent a normal AI answer.
            return []
        return []

    def _search_brave(self, query: str) -> list[WebSource]:
        if not self.settings.web_search_api_key:
            return []
        url = self.settings.web_search_base_url.strip() or "https://api.search.brave.com/res/v1/web/search"
        with httpx.Client(timeout=self.settings.web_timeout_seconds, follow_redirects=True) as client:
            response = client.get(
                url,
                params={"q": query, "count": self.settings.web_search_max_results},
                headers={"X-Subscription-Token": self.settings.web_search_api_key},
            )
            response.raise_for_status()
            data = response.json()
        rows = data.get("web", {}).get("results", [])
        return [
            WebSource(
                title=str(row.get("title") or "").strip(),
                url=str(row.get("url") or "").strip(),
                snippet=str(row.get("description") or "").strip(),
                metadata={"provider": "brave"},
            )
            for row in rows
            if row.get("url")
        ]

    def _search_tavily(self, query: str) -> list[WebSource]:
        if not self.settings.web_search_api_key:
            return []
        url = self.settings.web_search_base_url.strip() or "https://api.tavily.com/search"
        payload = {
            "api_key": self.settings.web_search_api_key,
            "query": query,
            "max_results": self.settings.web_search_max_results,
            "include_answer": False,
            "include_raw_content": False,
        }
        with httpx.Client(timeout=self.settings.web_timeout_seconds, follow_redirects=True) as client:
            response = client.post(url, json=payload)
            response.raise_for_status()
            data = response.json()
        return [
            WebSource(
                title=str(row.get("title") or "").strip(),
                url=str(row.get("url") or "").strip(),
                snippet=str(row.get("content") or "").strip(),
                metadata={"provider": "tavily", "score": row.get("score")},
            )
            for row in data.get("results", [])
            if row.get("url")
        ]

    def _search_searxng(self, query: str) -> list[WebSource]:
        base = self.settings.web_search_base_url.strip()
        if not base:
            return []
        url = base.rstrip("/") + "/search"
        with httpx.Client(timeout=self.settings.web_timeout_seconds, follow_redirects=True) as client:
            response = client.get(url, params={"q": query, "format": "json"})
            response.raise_for_status()
            data = response.json()
        return [
            WebSource(
                title=str(row.get("title") or "").strip(),
                url=str(row.get("url") or "").strip(),
                snippet=str(row.get("content") or "").strip(),
                metadata={"provider": "searxng", "engines": row.get("engines", [])},
            )
            for row in data.get("results", [])
            if row.get("url")
        ]

    def _search_deepseek_api(self, query: str) -> list[WebSource]:
        """Call the LAN DeepSeek search relay used by the DSH profile.

        The relay is a search-only endpoint.  Its configured base is the same
        value used in DSH (``.../v1/tools``), and the concrete route is
        ``POST /search``.  Page retrieval deliberately stays in
        :meth:`_fetch_source`, so ``WEB_FETCH_*`` continues to enforce the
        local HTTP policy and no URL is sent back through the relay.
        """
        base = self.settings.web_search_base_url.strip()
        api_key = self.settings.web_search_api_key.strip()
        if not base or not api_key:
            return []
        url = base.rstrip("/")
        if not url.casefold().endswith("/search"):
            url += "/search"
        payload = {
            "query": query,
            "max_results": self.settings.web_search_max_results,
        }
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Accept": "application/json",
        }
        with httpx.Client(timeout=self.settings.web_timeout_seconds, follow_redirects=False) as client:
            response = client.post(url, json=payload, headers=headers)
            response.raise_for_status()
            data = response.json()
        rows = data.get("results", []) if isinstance(data, dict) else []
        if not isinstance(rows, list):
            return []
        sources: list[WebSource] = []
        for row in rows:
            if not isinstance(row, dict) or not row.get("url"):
                continue
            snippet = row.get("snippet") or row.get("summary") or row.get("content") or ""
            metadata = {
                "provider": "deepseek-search-api",
                "site_name": row.get("site_name"),
                "authority": row.get("authority"),
                "rerank_score": row.get("rerank_score"),
                "date": row.get("date"),
            }
            sources.append(
                WebSource(
                    title=str(row.get("title") or row.get("url") or "").strip(),
                    url=str(row.get("url") or "").strip(),
                    snippet=str(snippet).strip(),
                    metadata={key: value for key, value in metadata.items() if value is not None},
                )
            )
        return sources

    def _platform_sources(
        self,
        query: str,
        *,
        intent: dict[str, object] | None = None,
    ) -> list[WebSource]:
        requested: list[str] = []
        if "抖音" in query or "douyin" in query.casefold():
            requested.append("douyin")
        if "小红书" in query or "xiaohongshu" in query.casefold() or "xhs" in query.casefold():
            requested.append("xiaohongshu")
        browse_query = " ".join(str(query or "").split())
        if isinstance(intent, dict) and intent.get("matched"):
            intent_query = str(intent.get("query") or "").strip()
            if intent_query:
                browse_query = intent_query
            for platform in intent.get("platforms") or ():
                name = str(platform)
                if name and name not in requested:
                    requested.append(name)
        adapters = getattr(self.platform_service, "adapters", None)
        if isinstance(adapters, dict) and adapters:
            # Only browse adapters this deployment actually enabled.
            requested = [name for name in requested if name in adapters]
        sources: list[WebSource] = []
        for platform in requested:
            try:
                items = self.platform_service.browse(platform, query=browse_query, limit=5)
            except Exception:
                continue
            for item in items:
                sources.append(
                    WebSource(
                        title=item.title or item.item_id,
                        url=item.url,
                        snippet=item.text,
                        source_type=platform,
                        metadata={
                            "platform": platform,
                            "item_id": item.item_id,
                            "item_url": item.url,
                            "author_id": item.author_id,
                            "author_name": item.author_name,
                            "author": item.author_name,
                            "query": browse_query[:300],
                        },
                    )
                )
                try:
                    comments = self.platform_service.read_comments(platform, item.item_id, limit=10)
                except Exception:
                    comments = []
                if comments:
                    comment_text = "\n".join(
                        f"{comment.author_name}（{comment.author_id or 'unknown'}）：{comment.text}"
                        for comment in comments
                    )
                    sources.append(
                        WebSource(
                            title=f"{item.title or item.item_id} 的公开评论",
                            url=item.url,
                            snippet=comment_text,
                            source_type=f"{platform}_comments",
                            metadata={
                                "platform": platform,
                                "item_id": item.item_id,
                                "item_url": item.url,
                                "author_name": item.author_name,
                                "author": item.author_name,
                                "query": browse_query[:300],
                            },
                        )
                    )
        return sources

    def _fetch_source(self, source: WebSource) -> WebSource:
        if source.source_type not in {"web", "xiaohongshu_search", "xiaohongshu_search_result"}:
            return source
        host = (urlparse(source.url).hostname or "").casefold().rstrip(".")
        if not host or not self._allowed_host(host):
            return source
        try:
            with httpx.Client(
                timeout=self.settings.web_timeout_seconds,
                follow_redirects=True,
                headers={"User-Agent": "wechat-agent/0.1 public-research"},
            ) as client:
                response = client.get(source.url)
                response.raise_for_status()
                final_host = (urlparse(str(response.url)).hostname or "").casefold().rstrip(".")
                if not self._allowed_host(final_host):
                    return source
                content_type = response.headers.get("content-type", "").casefold()
                if "text/html" not in content_type and "text/plain" not in content_type:
                    return source
                content = _clean_html(response.text)[: self.settings.web_fetch_max_chars]
                return WebSource(
                    title=source.title,
                    url=source.url,
                    snippet=source.snippet,
                    content=content,
                    source_type=source.source_type,
                    metadata={**source.metadata, "fetched": True, "final_url": str(response.url)},
                )
        except (httpx.HTTPError, UnicodeError):
            return source

    def _allowed_host(self, host: str) -> bool:
        if host in {"localhost", "localhost.localdomain"} or host.endswith(".local"):
            return False
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            address = None
        if address is not None and (address.is_private or address.is_loopback or address.is_link_local or address.is_reserved):
            return False
        allowed = [item.casefold().strip().lstrip(".") for item in self.settings.web_fetch_allowed_domains]
        if not allowed:
            return True
        return any(host == domain or host.endswith("." + domain) for domain in allowed if domain)

    @staticmethod
    def _dedupe(sources: list[WebSource]) -> list[WebSource]:
        result: list[WebSource] = []
        seen: set[str] = set()
        for source in sources:
            url = source.url.strip()
            # A note and its public comments intentionally share a URL but
            # carry different evidence.  Keep them as separate sources while
            # still collapsing duplicate rows from multiple providers.
            key = "\x1f".join((url, source.source_type, str(source.metadata.get("item_id") or "")))
            if not url or key in seen:
                continue
            seen.add(key)
            result.append(source)
        return result


class _VisibleTextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._hidden = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.casefold() in {"script", "style", "noscript", "svg"}:
            self._hidden += 1

    def handle_endtag(self, tag: str) -> None:
        if tag.casefold() in {"script", "style", "noscript", "svg"} and self._hidden:
            self._hidden -= 1

    def handle_data(self, data: str) -> None:
        if not self._hidden:
            value = " ".join(data.split())
            if value:
                self.parts.append(value)


def _clean_html(value: str) -> str:
    parser = _VisibleTextParser()
    parser.feed(value)
    return " ".join(parser.parts)
