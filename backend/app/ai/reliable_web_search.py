"""Allowlisted web search through the OpenAI Responses API."""
from __future__ import annotations

from collections.abc import Iterable, Mapping
import logging
import os
import re
from typing import Any
from urllib.parse import urlparse

import requests

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


def _response_text_and_citations(payload: Mapping[str, Any]) -> tuple[str, list[tuple[str, str]]]:
    texts: list[str] = []
    citations: list[tuple[str, str]] = []
    top_level_text = payload.get("output_text")
    if isinstance(top_level_text, str) and top_level_text.strip():
        texts.append(top_level_text.strip())
    for item in payload.get("output", ()) or ():
        if not isinstance(item, Mapping):
            continue
        if item.get("type") != "message":
            continue
        for content in item.get("content", ()) or ():
            if not isinstance(content, Mapping) or content.get("type") != "output_text":
                continue
            text = content.get("text")
            if isinstance(text, str) and text.strip():
                texts.append(text.strip())
            annotations = content.get("annotations", ()) or ()
            for annotation in annotations:
                if not isinstance(annotation, Mapping):
                    continue
                url = annotation.get("url")
                title = annotation.get("title") or annotation.get("text") or ""
                if url:
                    citations.append((str(title).strip(), str(url).strip()))
    text = "\n\n".join(dict.fromkeys(texts))
    for title, url in _MARKDOWN_URL_RE.findall(text):
        citations.append((title.strip(), url.strip()))
    for url in _URL_RE.findall(text):
        citations.append(("", url))
    return text, citations


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
        self.api_key = _setting("openai_api_key", "") if api_key is None else api_key
        self.base_url = (base_url or _setting("openai_api_base", "https://api.openai.com/v1")).rstrip("/")
        self.model = model or _setting("openai_model", "gpt-5.4-mini")
        configured = allowed_domains if allowed_domains is not None else _configured_domains()
        self.allowed_domains = tuple(sorted({
            _normalized_domain(domain)
            for domain in configured
            if _normalized_domain(domain)
        }))
        self.max_results = max_results if max_results is not None else _max_results()
        self.max_results = max(1, min(int(self.max_results), 8))
        self.timeout_seconds = timeout_seconds if timeout_seconds is not None else 20.0
        self.timeout_seconds = max(0.1, min(float(self.timeout_seconds), 60.0))

    def __call__(self, query: str) -> dict[str, Any]:
        normalized_query = str(query or "").strip()
        if not normalized_query:
            return {"error": "web search query is empty"}
        if not self.api_key:
            return {"error": "OPENAI_API_KEY is not configured"}
        if not self.allowed_domains:
            return {"error": "no reliable web search domains are configured"}

        body = {
            "model": self.model,
            "input": (
                "Search only the allowed reliable sources. Return a short list of "
                "source titles, exact HTTPS URLs, and concise source-supported snippets. "
                "Do not provide medical advice or use unsupported model knowledge. "
                f"Query: {normalized_query}"
            ),
            "tools": [{
                "type": "web_search",
                "filters": {"allowed_domains": list(self.allowed_domains)},
            }],
            "max_output_tokens": 1400,
        }
        try:
            response = requests.post(
                f"{self.base_url}/responses",
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                json=body,
                timeout=self.timeout_seconds,
            )
        except requests.RequestException:
            logger.warning("reliable web search request failed")
            return {"error": "reliable web search request failed"}
        if response.status_code >= 400:
            logger.warning("reliable web search provider returned HTTP %s", response.status_code)
            return {"error": "reliable web search provider returned an error"}
        try:
            payload = response.json()
        except ValueError:
            return {"error": "reliable web search returned invalid JSON"}
        if not isinstance(payload, Mapping):
            return {"error": "reliable web search returned an invalid response"}

        text, citations = _response_text_and_citations(payload)
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
