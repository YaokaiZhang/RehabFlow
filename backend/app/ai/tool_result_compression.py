"""Deterministic, content-first rendering for Consultant tool results."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Any, Mapping
from urllib.parse import urlparse


MAX_RENDERED_TOOL_RESULT_CHARS = 24_000
MAX_RENDERED_TOOL_ITEM_CHARS = 6_000

_STRUCTURED_TEXT_KEYS = (
    "text",
    "content",
    "description",
    "exercise_description",
    "instructions",
    "indications",
    "contraindications",
    "summary",
    "details",
    "body",
    "value",
    "name",
    "title",
    "snippet",
    "page_content",
    "raw_content",
    "introduction",
    "structures_involved",
    "related_conditions",
    "source_url",
)


@dataclass(frozen=True)
class CompressedToolResult:
    """The model-facing rendering plus the raw/provider boundary metadata."""

    tool_name: str
    status: str
    rendered: str
    items: tuple[Mapping[str, Any], ...] = ()
    source_refs: tuple[Mapping[str, str], ...] = ()
    result_count: int = 0
    error: str = ""


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (int, float, bool)):
        return str(value)
    if isinstance(value, (list, tuple)):
        parts = [
            _text(item)
            for item in value
            if isinstance(item, (str, int, float, bool, Mapping, list, tuple))
        ]
        return " ".join(part for part in parts if part)
    if isinstance(value, Mapping):
        parts: list[str] = []
        for key in _STRUCTURED_TEXT_KEYS:
            if key not in value:
                continue
            text = _text(value[key])
            if text and text not in parts:
                parts.append(text)
        return " ".join(parts)
    return ""


def _first(record: Mapping[str, Any], keys: tuple[str, ...]) -> str:
    for key in keys:
        value = record.get(key)
        if value not in (None, "", [], {}):
            text = _text(value)
            if text:
                return text
    payload = record.get("payload")
    if isinstance(payload, Mapping):
        for key in keys:
            value = payload.get(key)
            if value not in (None, "", [], {}):
                text = _text(value)
                if text:
                    return text
    return ""


def _record_value(record: Mapping[str, Any], key: str) -> Any:
    value = record.get(key)
    if value not in (None, "", [], {}):
        return value
    payload = record.get("payload")
    if isinstance(payload, Mapping):
        return payload.get(key)
    return None


def _records(raw: Any) -> list[Mapping[str, Any]]:
    if isinstance(raw, Mapping):
        for key in ("results", "documents", "items", "data"):
            value = raw.get(key)
            if isinstance(value, (list, tuple)):
                return [item for item in value if isinstance(item, Mapping)]
        return [raw]
    if isinstance(raw, (list, tuple)):
        return [item for item in raw if isinstance(item, Mapping)]
    return []


def _collection_payload(
    raw: Any,
    keys: tuple[str, ...] = ("results", "documents", "items", "data"),
) -> tuple[list[Any] | None, bool]:
    if not isinstance(raw, Mapping):
        return None, False
    for key in keys:
        if key not in raw:
            continue
        value = raw.get(key)
        return (
            list(value) if isinstance(value, (list, tuple)) else None,
            True,
        )
    return None, False


def _bounded(value: str, limit: int) -> str:
    normalized = re.sub(r"\s+", " ", value).strip()
    if len(normalized) <= limit:
        return normalized
    return normalized[: max(0, limit - 38)].rstrip() + " [content truncated]"


def _record_content(record: Mapping[str, Any], *, web: bool) -> str:
    keys = (
        ("content", "text", "page_content", "raw_content", "snippet", "summary")
        if web
        else (
            "content",
            "text",
            "page_content",
            "exercise_description",
            "description",
            "instructions",
            "indications",
            "contraindications",
            "summary",
        )
    )
    parts: list[str] = []
    for key in keys:
        text = _text(_record_value(record, key))
        if text and text not in parts:
            parts.append(text)
    if not web:
        for key, label in (
            ("introduction", "Introduction"),
            ("structures_involved", "Structures involved"),
            ("related_conditions", "Related conditions"),
        ):
            text = _text(_record_value(record, key))
            if text and not any(text in existing for existing in parts):
                parts.append(f"{label}: {text}")
        for key in ("body_area", "target_body_part", "difficulty", "duration", "sets", "repetitions"):
            text = _first(record, (key,))
            if text:
                parts.append(f"{key.replace('_', ' ')}: {text}")
        source_url = _first(record, ("source_url", "url"))
        if source_url and not any(source_url in existing for existing in parts):
            parts.append(f"Source URL: {source_url}")
    return " ".join(parts)


def _identity(record: Mapping[str, Any], title: str, content: str, *, web: bool) -> str:
    key = _first(
        record,
        ("url", "exercise_id", "document_id", "source_id", "id", "slug")
        if web
        else ("exercise_id", "document_id", "source_id", "id", "slug"),
    )
    if key:
        return key
    return hashlib.sha256(f"{title}|{content}".encode("utf-8")).hexdigest()


def _render_items(
    label: str,
    entries: list[tuple[str, str]],
    *,
    max_chars: int,
) -> str:
    if not entries:
        return f"{label}: no evidence was found."
    rendered = [f"{label} results:"]
    for index, (title, content) in enumerate(entries, start=1):
        candidate = f"[{index}] {label} | {title}\n{content}".strip()
        next_text = "\n\n".join((*rendered, candidate))
        if len(next_text) > max_chars:
            rendered.append("[remaining results omitted after the deterministic tool-result budget]")
            break
        rendered.append(candidate)
    return "\n\n".join(rendered)


def compress_kb_results(
    raw: Any,
    *,
    max_items: int = 5,
    max_chars: int = MAX_RENDERED_TOOL_RESULT_CHARS,
) -> CompressedToolResult:
    """Render useful exercise content without exposing provider JSON/metadata."""
    if isinstance(raw, Mapping) and raw.get("error"):
        return CompressedToolResult(
            tool_name="rehab_exercise_kb_search",
            status="failed",
            rendered=(
                "Rehab Exercise Knowledge Base unavailable: the provider returned an error."
            ),
            error=str(raw.get("error") or "provider returned an error"),
        )
    collection_items, has_collection = _collection_payload(raw)
    if (
        raw is None
        or not isinstance(raw, (Mapping, list, tuple))
        or (has_collection and collection_items is None)
        or (
            isinstance(raw, Mapping)
            and not has_collection
            and not any(key in raw for key in ("content", "text", "title", "name"))
        )
        or (
            has_collection
            and collection_items
            and any(not isinstance(item, Mapping) for item in collection_items)
        )
    ):
        return CompressedToolResult(
            tool_name="rehab_exercise_kb_search",
            status="failed",
            rendered=(
                "Rehab Exercise Knowledge Base unavailable: the provider returned an invalid response."
            ),
        )
    records = (
        [item for item in collection_items if isinstance(item, Mapping)]
        if has_collection and collection_items is not None
        else _records(raw)
    )
    source_items: list[Any] = (
        list(collection_items)
        if has_collection and collection_items is not None
        else list(raw)
        if isinstance(raw, (list, tuple))
        else records
    )
    if source_items and (
        any(not isinstance(item, Mapping) for item in source_items)
        or any(
            not _record_content(item, web=False)
            for item in source_items
            if isinstance(item, Mapping)
        )
    ):
        return CompressedToolResult(
            tool_name="rehab_exercise_kb_search",
            status="failed",
            rendered=(
                "Rehab Exercise Knowledge Base unavailable: the provider returned "
                "a malformed nonempty result."
            ),
        )
    entries: list[tuple[str, str]] = []
    retained: list[Mapping[str, Any]] = []
    seen: set[str] = set()
    malformed_record = False
    for record in records:
        title = _first(record, ("title", "name", "exercise_name", "exercise_id", "document_id"))
        content = _bounded(_record_content(record, web=False), MAX_RENDERED_TOOL_ITEM_CHARS)
        if not content:
            malformed_record = True
            continue
        identity = _identity(record, title, content, web=False)
        if identity in seen:
            continue
        seen.add(identity)
        title = _bounded(title or "Exercise knowledge", 240)
        entries.append((title, content))
        retained_item: dict[str, Any] = {"title": title, "content": content}
        source_url = _first(record, ("source_url", "url"))
        if source_url:
            retained_item["source_url"] = _bounded(source_url, 2_000)
        retained.append(retained_item)
        if len(entries) >= max(1, min(max_items, 8)):
            break
    if malformed_record:
        return CompressedToolResult(
            tool_name="rehab_exercise_kb_search",
            status="failed",
            rendered=(
                "Rehab Exercise Knowledge Base unavailable: the provider returned "
                "a malformed nonempty result."
            ),
        )
    if not entries and not (
        (has_collection and collection_items == [])
        or (not has_collection and isinstance(raw, (list, tuple)) and not raw)
    ):
        return CompressedToolResult(
            tool_name="rehab_exercise_kb_search",
            status="failed",
            rendered=(
                "Rehab Exercise Knowledge Base unavailable: the provider returned "
                "no usable source-backed result."
            ),
        )
    rendered = _render_items("Rehab Exercise Knowledge Base", entries, max_chars=max_chars)
    return CompressedToolResult(
        tool_name="rehab_exercise_kb_search",
        status="ok_nonempty" if entries else "ok_empty",
        rendered=rendered,
        items=tuple(retained),
        result_count=len(entries),
    )


def _valid_url(value: str) -> bool:
    parsed = urlparse(value)
    return parsed.scheme == "https" and bool(parsed.netloc) and not parsed.username and not parsed.password


def compress_web_results(
    raw: Any,
    *,
    max_items: int = 5,
    max_chars: int = MAX_RENDERED_TOOL_RESULT_CHARS,
) -> CompressedToolResult:
    """Render web search content and retain only explicit source references."""
    if isinstance(raw, Mapping) and raw.get("error"):
        return CompressedToolResult(
            tool_name="web_search",
            status="failed",
            rendered="Web Search unavailable: the web search provider returned an error.",
            error=str(raw.get("error") or "provider returned an error"),
        )
    if raw is None or not isinstance(raw, (Mapping, list, tuple)):
        return CompressedToolResult(tool_name="web_search", status="failed", rendered="Web Search unavailable: the provider returned an invalid response.")
    collection_items, has_collection = _collection_payload(raw)
    if (
        (has_collection and collection_items is None)
        or (
            has_collection
            and collection_items
            and any(not isinstance(item, Mapping) for item in collection_items)
        )
    ):
        return CompressedToolResult(
            tool_name="web_search",
            status="failed",
            rendered="Web Search unavailable: the provider returned an invalid response.",
        )
    records = (
        [item for item in collection_items if isinstance(item, Mapping)]
        if has_collection and collection_items is not None
        else _records(raw)
    )
    source_items: list[Any] = (
        list(collection_items)
        if has_collection and collection_items is not None
        else list(raw)
        if isinstance(raw, (list, tuple))
        else records
    )
    if source_items and (
        any(not isinstance(item, Mapping) for item in source_items)
        or any(
            not _record_content(item, web=True)
            or not _valid_url(_first(item, ("url", "link")))
            for item in source_items
            if isinstance(item, Mapping)
        )
    ):
        return CompressedToolResult(
            tool_name="web_search",
            status="failed",
            rendered="Web Search unavailable: the provider returned a malformed nonempty result.",
        )
    entries: list[tuple[str, str]] = []
    refs: list[Mapping[str, str]] = []
    retained: list[Mapping[str, Any]] = []
    seen: set[str] = set()
    malformed_record = False
    for record in records:
        title = _first(record, ("title", "name", "url")) or "Web result"
        content = _bounded(_record_content(record, web=True), MAX_RENDERED_TOOL_ITEM_CHARS)
        url = _first(record, ("url", "link"))
        if not content or not _valid_url(url):
            malformed_record = True
            continue
        identity = _identity(record, title, content, web=True)
        if identity in seen:
            continue
        seen.add(identity)
        content = f"URL: {url}\n{content}"
        safe_title = _bounded(title, 240)
        refs.append({"label": "Web Search", "title": safe_title, "url": url})
        entries.append((_bounded(title, 240), content))
        retained.append({"title": safe_title, "content": content, "url": url})
        if len(retained) >= max(1, min(max_items, 8)):
            break
    if malformed_record:
        return CompressedToolResult(
            tool_name="web_search",
            status="failed",
            rendered=(
                "Web Search unavailable: the provider returned a malformed "
                "nonempty result."
            ),
        )
    if not entries:
        collection_is_empty = (
            (has_collection and collection_items == [])
            or (not has_collection and isinstance(raw, (list, tuple)) and not raw)
        )
        if not collection_is_empty:
            return CompressedToolResult(
                tool_name="web_search",
                status="failed",
                rendered=(
                    "Web Search unavailable: the provider returned no usable "
                    "source-backed result."
                ),
            )
        return CompressedToolResult(
            tool_name="web_search",
            status="ok_empty",
            rendered=(
                "Web Search returned no usable result; no online evidence is available "
                "from this search."
            ),
        )
    rendered = _render_items("Web Search", entries, max_chars=max_chars)
    return CompressedToolResult(
        tool_name="web_search",
        status="ok_nonempty",
        rendered=rendered,
        items=tuple(retained),
        source_refs=tuple(refs),
        result_count=len(entries),
    )


def compressed_tool_failure(tool_name: str, status: str, error: str = "") -> CompressedToolResult:
    messages = {
        "timeout": "timed out",
        "blocked": "was blocked by policy",
        "invalid": "received an invalid request",
        "failed": "failed",
    }
    message = messages.get(status, "was unavailable")
    return CompressedToolResult(
        tool_name=tool_name,
        status=status,
        rendered=f"{tool_name}: the tool {message}; do not infer evidence from this result.",
        error=error,
    )
