"""Ingest the ACL knee stiffness demo video set into Postgres.

The source corpus is the Rehab Hero JSONL file already used by the vector
knowledge pipeline. This script selects at most five videos that fit the demo
interaction, downloads them with yt-dlp when requested, and upserts the sorted
records into the relational database.

Examples:
python scripts/demo_video_ingest.py --download
python scripts/demo_video_ingest.py --download --limit 3
python scripts/demo_video_ingest.py --no-download
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.db.models import RehabExerciseVideo
from app.db.session import Base, SessionLocal, engine
from app.vector import load_rehab_exercises

DEFAULT_INPUT = Path(__file__).resolve().parents[2] / "misc" / "rehab_exercises_total.jsonl"
DEFAULT_VIDEO_DIR = Path(__file__).resolve().parents[1] / "data" / "rehab_videos"
DEFAULT_MANIFEST = Path(__file__).resolve().parents[1] / "data" / "acl_knee_stiffness_videos.jsonl"
DEMO_PROFILE = "acl_knee_stiffness"

DEMO_SELECTION = [
    ("knee-car", 98, "Gentle multi-angle knee mobility for stiffness without load."),
    ("seated-knee-extension-isometric", 94, "Low-load knee extensor activation with the knee in a tolerable seated range."),
    ("supine-knee-extension-isometric", 91, "Supine quad activation option for early extension control and confidence."),
    ("front-leg-terminal-knee-extension", 88, "Terminal knee extension control for restoring end-range quadriceps function."),
    ("towel-knee-pump", 82, "Gentle seated knee gapping/pumping reference for stiffness-dominant symptoms."),
]


@dataclass(frozen=True)
class DemoChoice:
    slug: str
    rank: int
    score: int
    notes: str


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Ingest ACL knee stiffness demo videos")
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--video-dir", type=Path, default=DEFAULT_VIDEO_DIR)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--limit", type=int, default=5, help="Maximum videos to ingest, capped at 5")
    parser.add_argument("--download", dest="download", action="store_true", default=True)
    parser.add_argument("--no-download", dest="download", action="store_false")
    parser.add_argument("--force-download", action="store_true")
    return parser.parse_args()


def normalize_video_url(video_url: str) -> str:
    if not video_url:
        return ""
    if video_url.startswith("//"):
        video_url = f"https:{video_url}"

    parsed = urlparse(video_url)
    query = parse_qs(parsed.query)
    for key in ("src", "url"):
        value = query.get(key, [""])[0]
        if value:
            return normalize_video_url(unquote(value))
    return video_url


def public_media_url(local_path: str | None) -> str | None:
    if not local_path:
        return None
    name = Path(local_path).name
    return f"/media/rehab_videos/{name}" if name else None


def ensure_schema() -> None:
    Base.metadata.create_all(bind=engine)
    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE rehab_exercise_videos ADD COLUMN IF NOT EXISTS relevance_rank INTEGER NOT NULL DEFAULT 999"))
        conn.execute(text("ALTER TABLE rehab_exercise_videos ADD COLUMN IF NOT EXISTS relevance_score INTEGER NOT NULL DEFAULT 0"))


def download_video(slug: str, video_url: str, output_dir: Path, force: bool = False) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    existing = sorted(output_dir.glob(f"{slug}.*"))
    if existing and not force:
        existing_video = [match for match in existing if match.suffix.lower() in {".mp4", ".webm", ".mov"}]
        return existing_video[-1] if existing_video else existing[-1]
    runner = ["yt-dlp"] if shutil.which("yt-dlp") else [str(Path(sys.base_prefix) / "bin" / "python"), "-m", "yt_dlp"]

    command = [
        *runner,
        "--no-playlist",
        "--format",
        "bv*[ext=mp4]+ba[ext=m4a]/b[ext=mp4]/best",
        "--merge-output-format",
        "mp4",
        "--max-filesize",
        "80M",
        "--socket-timeout",
        "5",
        "--retries",
        "1",
        "--extractor-retries",
        "1",
        "--output",
        str(output_dir / f"{slug}.%(ext)s"),
        video_url,
    ]
    subprocess.run(command, check=True)
    matches = sorted(output_dir.glob(f"{slug}.*"))
    if not matches:
        raise FileNotFoundError(f"yt-dlp completed but no file matched {slug}.*")
    video_matches = [match for match in matches if match.suffix.lower() in {".mp4", ".webm", ".mov"}]
    return video_matches[-1] if video_matches else matches[-1]


def append_manifest(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str))
        handle.write("\n")


def main() -> None:
    args = parse_args()
    limit = max(0, min(args.limit, 5))
    choices = [DemoChoice(slug, index + 1, score, notes) for index, (slug, score, notes) in enumerate(DEMO_SELECTION[:limit])]
    choice_by_slug = {choice.slug: choice for choice in choices}

    exercises = {exercise.slug: exercise for exercise in load_rehab_exercises(args.input)}
    missing = [slug for slug in choice_by_slug if slug not in exercises]
    if missing:
        raise SystemExit(f"Missing source exercises: {', '.join(missing)}")

    ensure_schema()
    now = datetime.now(timezone.utc).isoformat()
    processed: list[dict[str, Any]] = []

    with SessionLocal() as db:
        for choice in choices:
            exercise = exercises[choice.slug]
            video_url = normalize_video_url(exercise.video_url)
            local_path: str | None = None
            download_status = "not_downloaded"
            error = ""

            if args.download:
                try:
                    local = download_video(choice.slug, video_url, args.video_dir, force=args.force_download)
                    local_path = str(local)
                    download_status = "downloaded"
                except Exception as exc:  # noqa: BLE001
                    error = f"{type(exc).__name__}: {exc}"
                    download_status = "failed"

            metadata = {
                "video_provider": exercise.video_provider,
                "video_id": exercise.video_id,
                "introduction": exercise.introduction,
                "structures_involved": exercise.structures_involved,
                "related_conditions": exercise.related_conditions,
                "demo_selected_at": now,
                "download_error": error,
            }
            payload = {
                "exercise_id": uuid.UUID(exercise.exercise_id),
                "slug": exercise.slug,
                "title": exercise.title,
                "source_url": exercise.source_url,
                "video_url": video_url,
                "local_video_path": local_path,
                "public_video_url": public_media_url(local_path),
                "demo_profile": DEMO_PROFILE,
                "relevance_rank": choice.rank,
                "relevance_score": choice.score,
                "relevance_notes": choice.notes,
                "download_status": download_status,
                "exercise_metadata": metadata,
            }
            stmt = insert(RehabExerciseVideo).values(**payload)
            update_payload = {key: stmt.excluded[key] for key in payload if key != "exercise_id"}
            db.execute(stmt.on_conflict_do_update(index_elements=["exercise_id"], set_=update_payload))
            record = {**payload, "exercise_id": str(payload["exercise_id"])}
            append_manifest(args.manifest, record)
            processed.append(record)
        db.commit()

    print(json.dumps({"profile": DEMO_PROFILE, "count": len(processed), "videos": processed}, indent=2, default=str))


if __name__ == "__main__":
    main()
