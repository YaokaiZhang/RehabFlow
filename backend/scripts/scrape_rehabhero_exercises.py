#!/usr/bin/env python3
"""Scrape Rehab Hero exercise pages into the JSONL ingestion contract.

The scraper uses the public sitemap and detail-page HTML only. It does not
download images or video assets, and it keeps progress in appendable JSONL so
an interrupted run can be resumed safely.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timezone
from html import unescape
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urljoin, urldefrag, urlparse, urlunparse

import requests


SOURCE_BASE_URL = "https://www.rehabhero.ca"
DEFAULT_SITEMAP = f"{SOURCE_BASE_URL}/sitemap.xml"
DEFAULT_OUTPUT = Path(__file__).resolve().parents[1] / "data" / "rehabhero_exercises.jsonl"
RETRYABLE_STATUS_CODES = frozenset({429, 500, 502, 503, 504})
USER_AGENT = "RehabFlow exercise importer/1.0 (+https://www.rehabhero.ca/exercise)"
DETAIL_PATH_RE = re.compile(r"^/exercise/[^/]+$", re.IGNORECASE)
VIDEO_HOST_RE = re.compile(r"(^|\.)((youtube\.com)|(youtu\.be)|(vimeo\.com))$", re.IGNORECASE)


def _clean_text(value: Any) -> str:
	return " ".join(unescape("" if value is None else str(value)).replace("\xa0", " ").split())


def _canonical_exercise_url(value: str, *, require_detail: bool = True, allow_query: bool = False) -> str:
	"""Normalize a Rehab Hero URL without preserving fragments or query strings."""
	cleaned = urldefrag(urljoin(SOURCE_BASE_URL, _clean_text(value)))[0]
	parsed = urlparse(cleaned)
	if parsed.scheme.lower() not in {"http", "https"}:
		return ""
	if parsed.hostname and parsed.hostname.lower() not in {"rehabhero.ca", "www.rehabhero.ca"}:
		return ""
	if parsed.query and not allow_query:
		return ""
	path = parsed.path.rstrip("/") or "/"
	if require_detail and not DETAIL_PATH_RE.fullmatch(path):
		return ""
	return urlunparse(("https", "www.rehabhero.ca", path, "", "", ""))


def is_exercise_detail_url(value: str) -> bool:
	return bool(_canonical_exercise_url(value))


def discover_exercise_urls(sitemap_xml: str) -> list[str]:
	"""Return deduplicated canonical detail URLs from a sitemap document."""
	root = ET.fromstring(sitemap_xml)
	urls: list[str] = []
	seen: set[str] = set()
	for element in root.iter():
		if element.tag.rsplit("}", 1)[-1] != "loc" or not element.text:
			continue
		url = _canonical_exercise_url(element.text)
		if url and url not in seen:
			seen.add(url)
			urls.append(url)
	return urls


@dataclass
class _TextItem:
	tag: str
	parts: list[str]
	inside_list_item: bool


class RehabHeroHTMLParser(HTMLParser):
	"""Extract page metadata, exercise-body text, and embedded video URLs."""

	_CONTENT_TAGS = frozenset({"h1", "h2", "h3", "h4", "h5", "h6", "p", "li"})
	_VOID_TAGS = frozenset(
		{
			"area",
			"base",
			"br",
			"col",
			"embed",
			"hr",
			"img",
			"input",
			"link",
			"meta",
			"param",
			"source",
			"track",
			"wbr",
		}
	)

	def __init__(self) -> None:
		super().__init__(convert_charrefs=True)
		self.meta: dict[str, str] = {}
		self.canonical_url = ""
		self.document_title = ""
		self.video_candidates: list[str] = []
		self.content_blocks: list[list[tuple[str, str]]] = []
		self._tag_stack: list[str] = []
		self._title_parts: list[str] = []
		self._capturing_title = False
		self._current_block: list[tuple[str, str]] | None = None
		self._content_block_depth: int | None = None
		self._active_items: list[_TextItem] = []

	def _record_video_candidates(self, value: Any) -> None:
		text = unescape("" if value is None else str(value))
		for raw_url in re.findall(r"https?://[^\"'<>\s]+", text, flags=re.IGNORECASE):
			candidate = raw_url.rstrip(".,;)")
			parsed = urlparse(candidate)
			if parsed.hostname and VIDEO_HOST_RE.search(parsed.hostname):
				if parsed.hostname.lower().endswith("youtube.com") and not (
					parsed.path.startswith("/embed/") or parsed.path == "/watch"
				):
					continue
				self.video_candidates.append(candidate)

	def _append_content_item(self, tag: str, item: _TextItem) -> None:
		if self._current_block is None:
			return
		text = _clean_text(" ".join(item.parts))
		if not text:
			return
		if tag == "li" or not item.inside_list_item:
			self._current_block.append((tag, text))

	def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
		tag = tag.lower()
		attributes = {key.lower(): value or "" for key, value in attrs}
		if tag == "meta":
			key = attributes.get("property") or attributes.get("name") or attributes.get("itemprop")
			content = attributes.get("content", "")
			if key and content:
				self.meta[key.casefold()] = _clean_text(content)
		elif tag == "link" and "canonical" in attributes.get("rel", "").casefold().split():
			self.canonical_url = attributes.get("href", "")
		elif tag == "title":
			self._capturing_title = True

		for attribute_name in ("src", "data-src", "href", "data-url", "data-html"):
			if attributes.get(attribute_name):
				self._record_video_candidates(attributes[attribute_name])

		classes = attributes.get("class", "").casefold().split()
		if tag == "div" and "sqs-html-content" in classes and self._current_block is None:
			self._current_block = []
			self._content_block_depth = len(self._tag_stack) + 1

		if self._current_block is not None and tag in self._CONTENT_TAGS:
			self._active_items.append(
				_TextItem(
					tag=tag,
					parts=[],
					inside_list_item=any(item.tag == "li" for item in self._active_items),
				)
			)

		if tag != "title" and tag not in self._VOID_TAGS:
			self._tag_stack.append(tag)

	def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
		self.handle_starttag(tag, attrs)
		if tag.lower() not in self._VOID_TAGS:
			self.handle_endtag(tag)

	def handle_endtag(self, tag: str) -> None:
		tag = tag.lower()
		if tag == "title":
			self.document_title = _clean_text(" ".join(self._title_parts))
			self._capturing_title = False

		if self._current_block is not None and self._active_items and self._active_items[-1].tag == tag:
			item = self._active_items.pop()
			self._append_content_item(tag, item)

		if self._current_block is not None and tag == "div" and self._content_block_depth == len(self._tag_stack):
			self.content_blocks.append(self._current_block)
			self._current_block = None
			self._content_block_depth = None

		if self._tag_stack:
			if self._tag_stack[-1] == tag:
				self._tag_stack.pop()
			elif tag in self._tag_stack:
				self._tag_stack.remove(tag)

	def handle_data(self, data: str) -> None:
		if self._capturing_title:
			self._title_parts.append(data)
		for item in self._active_items:
			item.parts.append(data)

	def close(self) -> None:
		super().close()
		if self._current_block is not None:
			self.content_blocks.append(self._current_block)
			self._current_block = None


def _strip_site_suffix(title: str) -> str:
	return re.sub(r"\s*(?:[\u2014\u2013-]\s*)?rehab hero\s*$", "", title, flags=re.IGNORECASE).strip()


def _section_index(items: list[tuple[str, str]], section_names: str | tuple[str, ...]) -> int | None:
	if isinstance(section_names, str):
		section_names = (section_names,)
	normalized_names = {name.casefold() for name in section_names}
	for index, (_, text) in enumerate(items):
		if text.casefold().rstrip(": ") in normalized_names:
			return index
	return None


def _best_content_block(blocks: Iterable[list[tuple[str, str]]]) -> list[tuple[str, str]]:
	best: list[tuple[str, str]] = []
	best_score = -1
	for block in blocks:
		joined = " ".join(text.casefold() for _, text in block)
		score = sum(label in joined for label in ("structures involved", "muscles involved", "related conditions"))
		if score > best_score or (score == best_score and len(block) > len(best)):
			best = block
			best_score = score
	return best


def _description_without_title(description: str, title: str) -> str:
	cleaned = _clean_text(description)
	if title and cleaned.casefold().startswith(title.casefold()):
		cleaned = cleaned[len(title) :].lstrip(" :-\u2013\u2014")
	return cleaned


def _select_video_url(candidates: Iterable[str]) -> str:
	seen: set[str] = set()
	for candidate in candidates:
		cleaned = unescape(candidate).strip()
		if cleaned and cleaned not in seen:
			seen.add(cleaned)
			return cleaned
	return ""


def parse_exercise_page(html: str, source_url: str) -> dict[str, Any]:
	parser = RehabHeroHTMLParser()
	parser.feed(html)
	parser.close()

	items = _best_content_block(parser.content_blocks)
	content_title = items[0][1] if items and items[0][0] in {"h1", "h2", "h3", "p"} else ""
	title = _strip_site_suffix(parser.meta.get("og:title") or parser.meta.get("twitter:title") or parser.document_title)
	title = title or content_title
	if not title:
		raise ValueError("exercise page has no title")

	structure_index = _section_index(items, ("Structures Involved", "Muscles Involved"))
	condition_index = _section_index(items, "Related Conditions")
	introduction_end = min(
		index for index in (structure_index, condition_index) if index is not None
	) if structure_index is not None or condition_index is not None else len(items)
	introduction_items = [
		text
		for index, (tag, text) in enumerate(items[:introduction_end])
		if tag != "li" and text.casefold() != content_title.casefold() and text.casefold() != title.casefold()
	]

	structures: list[str] = []
	if structure_index is not None:
		end = condition_index if condition_index is not None and condition_index > structure_index else len(items)
		structures = [text for tag, text in items[structure_index + 1 : end] if tag == "li"]

	related_conditions: list[str] = []
	if condition_index is not None:
		related_conditions = [text for tag, text in items[condition_index + 1 :] if tag == "li"]

	introduction = _clean_text(" ".join(introduction_items))
	if not introduction:
		introduction = _description_without_title(parser.meta.get("og:description", ""), title)

	canonical_url = _canonical_exercise_url(parser.canonical_url, allow_query=True)
	return {
		"url": canonical_url or _canonical_exercise_url(source_url),
		"title": title,
		"video_url": _select_video_url(parser.video_candidates),
		"introduction": introduction,
		"structures_involved": structures,
		"related_conditions": related_conditions,
		"scraped_at": datetime.now(timezone.utc).isoformat(),
		"source": "rehabhero.ca",
	}


def _read_text_source(source: str, session: requests.Session, *, timeout: float, retries: int, delay: float) -> str:
	if source.startswith(("http://", "https://")):
		return _fetch_text(session, source, timeout=timeout, retries=retries, delay=delay)
	return Path(source).read_text(encoding="utf-8")


def _fetch_text(
	session: requests.Session,
	url: str,
	*,
	timeout: float,
	retries: int,
	delay: float,
) -> str:
	last_error: Exception | None = None
	total_attempts = max(0, retries) + 1
	for attempt in range(1, total_attempts + 1):
		try:
			response = session.get(url, timeout=timeout)
			if response.status_code in RETRYABLE_STATUS_CODES and attempt < total_attempts:
				if delay > 0:
					time.sleep(min(30.0, delay * attempt))
				continue
			response.raise_for_status()
			return response.text
		except requests.RequestException as exc:
			last_error = exc
			if attempt >= total_attempts:
				break
			if delay > 0:
				time.sleep(min(30.0, delay * attempt))
	if last_error is None:
		last_error = RuntimeError(f"request failed for {url}")
	raise RuntimeError(f"request failed after {total_attempts} attempt(s): {url}: {last_error}") from last_error


def load_processed_urls(path: Path) -> set[str]:
	if not path.exists():
		return set()
	processed: set[str] = set()
	with path.open("r", encoding="utf-8") as handle:
		for line_number, line in enumerate(handle, start=1):
			if not line.strip():
				continue
			try:
				raw = json.loads(line)
			except json.JSONDecodeError as exc:
				raise ValueError(f"Invalid JSONL in {path} at line {line_number}") from exc
			url = _canonical_exercise_url(str(raw.get("url", "")))
			if url:
				processed.add(url)
	return processed


def _failure_path(output: Path, explicit: Path | None) -> Path:
	if explicit is not None:
		return explicit
	return output.with_name(f"{output.stem}.failures{output.suffix}")


def _jsonl_write(handle, payload: dict[str, Any]) -> None:
	handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True))
	handle.write("\n")
	handle.flush()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
	parser = argparse.ArgumentParser(description="Scrape Rehab Hero exercise detail pages into JSONL")
	parser.add_argument("--sitemap", default=DEFAULT_SITEMAP, help="Sitemap URL or repository-owned XML file")
	parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT, help="Exercise JSONL output path")
	parser.add_argument("--failures", type=Path, default=None, help="Failure JSONL path")
	parser.add_argument("--delay", type=float, default=1.0, help="Seconds between page requests and retry waits")
	parser.add_argument("--timeout", type=float, default=30.0, help="Per-request timeout in seconds")
	parser.add_argument("--retries", type=int, default=3, help="Additional retries for transient failures")
	parser.add_argument("--limit", type=int, default=0, help="Maximum detail pages to consider; zero means all")
	parser.add_argument("--resume", action="store_true", help="Skip URLs already present in the output JSONL")
	parser.add_argument("--overwrite", action="store_true", help="Replace existing output and failure JSONL files")
	return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
	args = parse_args(argv)
	if args.resume and args.overwrite:
		raise SystemExit("--resume and --overwrite cannot be used together")
	if args.delay < 0 or args.timeout <= 0 or args.retries < 0 or args.limit < 0:
		raise SystemExit("delay/timeout/retries/limit values are out of range")
	if args.output.exists() and not args.resume and not args.overwrite:
		raise SystemExit(f"output exists: {args.output}; use --resume or --overwrite")

	args.output.parent.mkdir(parents=True, exist_ok=True)
	failure_path = _failure_path(args.output, args.failures)
	failure_path.parent.mkdir(parents=True, exist_ok=True)
	if args.overwrite:
		args.output.write_text("", encoding="utf-8")
		failure_path.write_text("", encoding="utf-8")

	processed = load_processed_urls(args.output) if args.resume else set()
	session = requests.Session()
	session.headers.update({"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml"})
	sitemap_xml = _read_text_source(args.sitemap, session, timeout=args.timeout, retries=args.retries, delay=args.delay)
	urls = discover_exercise_urls(sitemap_xml)
	selected_urls = urls[: args.limit] if args.limit else urls
	todo_urls = [url for url in selected_urls if url not in processed]

	output_mode = "a" if args.resume else "w"
	failure_mode = "a" if args.resume else "w"
	fetched = parsed = failed = 0
	started_at = time.monotonic()
	with args.output.open(output_mode, encoding="utf-8") as output_handle, failure_path.open(
		failure_mode, encoding="utf-8"
	) as failure_handle:
		for index, url in enumerate(todo_urls, start=1):
			if fetched and args.delay > 0:
				time.sleep(args.delay)
			fetched += 1
			try:
				html = _fetch_text(
					session,
					url,
					timeout=args.timeout,
					retries=args.retries,
					delay=args.delay,
				)
				row = parse_exercise_page(html, url)
				if not row["url"] or not row["title"]:
					raise ValueError("parsed page is missing url or title")
				_jsonl_write(output_handle, row)
				parsed += 1
			except Exception as exc:
				failed += 1
				_jsonl_write(
					failure_handle,
					{
						"url": url,
						"error": f"{type(exc).__name__}: {exc}",
						"attempted_at": datetime.now(timezone.utc).isoformat(),
					},
				)
				print(f"[WARN] failed {url}: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
			if index == 1 or index % 25 == 0 or index == len(todo_urls):
				print(
					f"[progress] discovered={len(urls)} selected={len(selected_urls)} "
					f"already_processed={len(processed)} fetched={fetched} parsed={parsed} failed={failed}",
					flush=True,
				)

	print(
		f"[OK] discovered={len(urls)} selected={len(selected_urls)} "
		f"already_processed={len(processed)} fetched={fetched} parsed={parsed} failed={failed} "
		f"elapsed_seconds={time.monotonic() - started_at:.1f}",
		flush=True,
	)
	print(f"[OK] exercise JSONL: {args.output}", flush=True)
	print(f"[OK] failure JSONL: {failure_path}", flush=True)
	return 0 if failed == 0 else 1


if __name__ == "__main__":
	try:
		raise SystemExit(main())
	except KeyboardInterrupt:
		print("[WARN] interrupted; rerun with --resume to continue", file=sys.stderr)
		raise SystemExit(130)
