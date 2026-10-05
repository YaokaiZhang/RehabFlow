from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest


SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

from scrape_rehabhero_exercises import (  # noqa: E402
	discover_exercise_urls,
	is_exercise_detail_url,
	load_processed_urls,
	parse_exercise_page,
)


DETAIL_HTML = """
<html>
  <head>
    <title>Rotator Cuff Raise - Rehab Hero</title>
    <meta property="og:title" content="Rotator Cuff Raise - Rehab Hero">
    <meta property="og:description" content="ROTATOR CUFF RAISE The raise description.">
    <link rel="canonical" href="https://www.rehabhero.ca/exercise/rotator-cuff-raise?utm_source=test">
  </head>
  <body>
    <div class="sqs-html-content" data-sqsp-text-block-content>
      <p><span>ROTATOR CUFF RAISE</span></p>
      <p>The raise description for the exercise.</p>
      <p><strong>Structures Involved:</strong></p>
      <ul><li><p>Supraspinatus</p></li><li>Rotator Cuff</li></ul>
      <p><strong>Related Conditions:</strong></p>
      <ul><li><p>Shoulder Pain</p></li></ul>
      <div data-provider-name="YouTube" data-html="&lt;iframe src=&quot;https://www.youtube.com/embed/abc123?feature=oembed&quot;&gt;&lt;/iframe&gt;"></div>
    </div>
  </body>
</html>
"""


def test_detail_url_filter_excludes_categories_and_query_urls() -> None:
	assert is_exercise_detail_url("https://www.rehabhero.ca/exercise/rotator-cuff-raise")
	assert not is_exercise_detail_url("https://www.rehabhero.ca/exercise/category/Shoulder")
	assert not is_exercise_detail_url("https://www.rehabhero.ca/exercise/rotator-cuff-raise?format=json")


def test_sitemap_urls_are_canonical_and_deduplicated() -> None:
	sitemap = """
	<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
	  <url><loc>https://www.rehabhero.ca/exercise/one/</loc></url>
	  <url><loc>/exercise/one</loc></url>
	  <url><loc>https://www.rehabhero.ca/exercise/category/Shoulder</loc></url>
	</urlset>
	"""
	assert discover_exercise_urls(sitemap) == ["https://www.rehabhero.ca/exercise/one"]


def test_page_parser_extracts_body_sections_and_embedded_video() -> None:
	row = parse_exercise_page(DETAIL_HTML, "https://www.rehabhero.ca/exercise/rotator-cuff-raise")
	assert row["url"] == "https://www.rehabhero.ca/exercise/rotator-cuff-raise"
	assert row["title"] == "Rotator Cuff Raise"
	assert row["introduction"] == "The raise description for the exercise."
	assert row["structures_involved"] == ["Supraspinatus", "Rotator Cuff"]
	assert row["related_conditions"] == ["Shoulder Pain"]
	assert row["video_url"] == "https://www.youtube.com/embed/abc123?feature=oembed"


def test_page_parser_allows_missing_video() -> None:
	page_without_video = DETAIL_HTML.split('<div data-provider-name="YouTube"', 1)[0] + "</div>\n  </body>\n</html>"
	row = parse_exercise_page(page_without_video, "https://www.rehabhero.ca/exercise/rotator-cuff-raise")
	assert row["video_url"] == ""


def test_page_parser_maps_muscles_involved_to_structures() -> None:
	page = DETAIL_HTML.replace("Structures Involved:", "Muscles Involved:")
	row = parse_exercise_page(page, "https://www.rehabhero.ca/exercise/rotator-cuff-raise")
	assert row["structures_involved"] == ["Supraspinatus", "Rotator Cuff"]


def test_resume_reads_only_valid_processed_detail_urls(tmp_path: Path) -> None:
	path = tmp_path / "exercises.jsonl"
	path.write_text(
		"\n".join(
			[
				json.dumps({"url": "https://www.rehabhero.ca/exercise/one"}),
				json.dumps({"url": "https://www.rehabhero.ca/exercise/category/Shoulder"}),
			]
		)
		+ "\n",
		encoding="utf-8",
	)
	assert load_processed_urls(path) == {"https://www.rehabhero.ca/exercise/one"}


def test_resume_rejects_malformed_jsonl(tmp_path: Path) -> None:
	path = tmp_path / "exercises.jsonl"
	path.write_text("not-json\n", encoding="utf-8")
	with pytest.raises(ValueError, match="Invalid JSONL"):
		load_processed_urls(path)
