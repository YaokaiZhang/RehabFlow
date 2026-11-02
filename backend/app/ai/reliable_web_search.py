"""Allowlisted web search through Claude's server-side web_search tool."""
from __future__ import annotations

from collections.abc import Iterable, Mapping
import logging
import os
import re
from typing import Any
from urllib.parse import urlparse

from app.core.config import get_settings

logger = logging.getLogger(__name__)

DEFAULT_RELIABLE_WEB_DOMAINS = (
    "aaos.org",
    "cdc.gov",
    "clevelandclinic.org",
    "hss.edu",
    "hopkinsmedicine.org",
    "mayoclinic.org",
    "medlineplus.gov",
    "nih.gov",
    "nhs.uk",
)

_MARKDOWN_URL_RE = re.compile(r"\[([^\]]{1,240})\]\((https://[^)\s]+)\)")
_URL_RE = re.compile(r"""https://[^\s<>\]\)"']+""")


def _configured_domains() -> tuple[str, ...]:
    raw = os.getenv("REHAB_WEB_SEARCH_ALLOWED_DOMAINS", "").strip()
    if not raw:
        return DEFAULT_RELIABLE_WEB_DOMAINS
    requested = {
        value.strip().lower().lstrip(".")
        for value in raw.split(",")
        if value.strip()
    }
    return tuple(domain for domain in DEFAULT_RELIABLE_WEB_DOMAINS if domain in requested)


def _setting(name: str, default: str) -> str:
    value = os.getenv(name)
    if value:
        return value
    try:
        configured = getattr(get_settings(), name)
    except Exception:
        return default
    return str(configured or default)


def _normalized_domain(value: object) -> str:
    return str(value or "").strip().lower().lstrip(".")


def _safe_url(value: object, allowed_domains: Iterable[str]) -> str:
    candidate = str(value or "").strip().rstrip(".,;:")
    parsed = urlparse(candidate)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
    ):
        return ""
    hostname = parsed.hostname.lower().rstrip(".")
    domains = tuple(_normalized_domain(domain) for domain in allowed_domains)
    if not any(hostname == domain or hostname.endswith("." + domain) for domain in domains):
        return ""
    return candidate


def _scrub_unapproved_urls(text: str, allowed_domains: Iterable[str]) -> str:
    def replace(match: re.Match[str]) -> str:
        return match.group(0) if _safe_url(match.group(0), allowed_domains) else "[unapproved source omitted]"

    return _URL_RE.sub(replace, text).strip()


def _block_value(block: Any, name: str) -> Any:
    if isinstance(block, Mapping):
        return block.get(name)
    return getattr(block, name, None)


def _response_text_and_citations(content: Iterable[Any]) -> tuple[str, list[tuple[str, str]]]:
    """Extract answer text and cited sources from Claude response content blocks."""
    texts: list[str] = []
    citations: list[tuple[str, str]] = []
    for block in content or ():
        block_type = _block_value(block, "type")
        if block_type == "text":
            text = _block_value(block, "text")
            if isinstance(text, str) and text.strip():
                texts.append(text.strip())
            for citation in _block_value(block, "citations") or ():
                url = _block_value(citation, "url")
                title = _block_value(citation, "title") or ""
                if url:
                    citations.append((str(title).strip(), str(url).strip()))
        elif block_type == "web_search_tool_result":
            results = _block_value(block, "content")
            # A failed search returns an error object instead of a list.
            if isinstance(results, (list, tuple)):
                for result in results:
                    url = _block_value(result, "url")
                    title = _block_value(result, "title") or ""
                    if url:
                        citations.append((str(title).strip(), str(url).strip()))
    text = "\n\n".join(dict.fromkeys(texts))
    for title, url in _MARKDOWN_URL_RE.findall(text):
        citations.append((title.strip(), url.strip()))
    for url in _URL_RE.findall(text):
        citations.append(("", url))
    return text, citations


def _web_search_tool_type(model: str) -> str:
    # The dynamic-filtering variant is documented for Opus/Sonnet; Haiku uses the basic one.
    return "web_search_20250305" if model.startswith("claude-haiku") else "web_search_20260209"


def _timeout_seconds() -> float:
    # A Claude web_search turn (search + cited summary) typically takes 15-30s.
    try:
        return float(os.getenv("REHAB_WEB_SEARCH_TIMEOUT_SECONDS", "45"))
    except (TypeError, ValueError):
        return 45.0


def _max_results() -> int:
    try:
        return max(1, min(int(os.getenv("REHAB_WEB_SEARCH_MAX_RESULTS", "5")), 8))
    except (TypeError, ValueError):
        return 5


class ReliableWebSearch:
    """Search only configured reliable domains and return source-bearing records."""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
        allowed_domains: Iterable[str] | None = None,
        max_results: int | None = None,
        timeout_seconds: float | None = None,
    ) -> None:
        self.api_key = _setting("anthropic_api_key", "") if api_key is None else api_key
        self.base_url = base_url or _setting("anthropic_base_url", "") or None
        self.model = model or _setting("anthropic_model", "claude-haiku-5-5")
        configured = allowed_domains if allowed_domains is not None else _configured_domains()
        self.allowed_domains = tuple(sorted({
            _normalized_domain(domain)
            for domain in configured
            if _normalized_domain(domain)
        }))
        self.max_results = max_results if max_results is not None else _max_results()
        self.max_results = max(1, min(int(self.max_results), 8))
        self.timeout_seconds = timeout_seconds if timeout_seconds is not None else _timeout_seconds()
        self.timeout_seconds = max(0.1, min(float(self.timeout_seconds), 60.0))

    def __call__(self, query: str) -> dict[str, Any]:
        normalized_query = str(query or "").strip()
        if not normalized_query:
            return {"error": "web search query is empty"}
        if not self.api_key:
            return {"error": "LLM_KEY is not configured"}
        if not self.allowed_domains:
            return {"error": "no reliable web search domains are configured"}

        try:
            import anthropic
        except ImportError:
            return {"error": "anthropic package is not installed"}

        client = anthropic.Anthropic(
            api_key=self.api_key,
            base_url=self.base_url,
            timeout=self.timeout_seconds,
            max_retries=1,
        )
        messages: list[dict[str, Any]] = [{
            "role": "user",
            "content": (
                "Search once; the search tool is already restricted to approved sources. Reply with at most "
                "five bullets, one line each: source title, exact HTTPS URL, and a "
                "source-supported snippet of under 25 words. Do not provide medical advice "
                "or use unsupported model knowledge. "
                f"Query: {normalized_query}"
            ),
        }]
        request: dict[str, Any] = {
            "model": self.model,
            "max_tokens": 4096,
            "output_config": {"effort": "low"},
            "tools": [{
                "type": _web_search_tool_type(self.model),
                "name": "web_search",
                "allowed_domains": list(self.allowed_domains),
                "max_uses": 1,
            }],
        }
        content: list[Any] = []
        try:
            response = client.messages.create(messages=messages, **request)
            content.extend(response.content)
            # A paused server-tool loop resumes by resending the assistant turn.
            if response.stop_reason == "pause_turn":
                messages.append({"role": "assistant", "content": response.content})
                response = client.messages.create(messages=messages, **request)
                content.extend(response.content)
        except anthropic.APIStatusError as exc:
            logger.warning("reliable web search provider returned HTTP %s", exc.status_code)
            return {"error": "reliable web search provider returned an error"}
        except anthropic.APIError:
            logger.warning("reliable web search request failed")
            return {"error": "reliable web search request failed"}
        if response.stop_reason == "refusal":
            return {"error": "reliable web search was declined by the provider", "results": []}

        text, citations = _response_text_and_citations(content)
        safe_text = _scrub_unapproved_urls(text, self.allowed_domains)
        results: list[dict[str, str]] = []
        seen: set[str] = set()
        for title, raw_url in citations:
            url = _safe_url(raw_url, self.allowed_domains)
            if not url or url in seen:
                continue
            seen.add(url)
            results.append({
                "title": title or urlparse(url).netloc,
                "url": url,
                "content": safe_text or f"Source: {url}",
            })
            if len(results) >= self.max_results:
                break
        if not results:
            return {
                "error": "reliable web search returned no allowed HTTPS sources",
                "results": [],
            }
        return {"results": results}


def default_reliable_web_search(query: str) -> dict[str, Any]:
    return ReliableWebSearch()(query)
