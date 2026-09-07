"""Extract continuous demo video pose tracks one chunk at a time.

The default mode stores every source video frame, matching the cadence of the
live camera pose-recognition loop. Each chunk runs in a separate process and is
appended into one continuous pose track in Postgres.

Examples:
python scripts/extract_demo_video_pose_set.py
python scripts/extract_demo_video_pose_set.py --slugs knee-car --chunk-frames 60
python scripts/extract_demo_video_pose_set.py --sample-fps 10 --max-frames 300
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import cv2
from sqlalchemy import select

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.db.models import RehabExerciseVideo, VideoPoseExtraction
from app.db.session import SessionLocal

DEFAULT_SLUGS = [
    "knee-car",
    "seated-knee-extension-isometric",
    "supine-knee-extension-isometric",
    "front-leg-terminal-knee-extension",
    "towel-knee-pump",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Extract continuous ACL stiffness demo pose tracks")
    parser.add_argument("--slugs", nargs="*", default=DEFAULT_SLUGS)
    parser.add_argument("--sample-fps", type=float, default=0.0, help="Default 0 stores every source frame")
    parser.add_argument("--max-frames", type=int, default=0, help="Optional total sampled-frame cap per video; default 0 stores the whole video")
    parser.add_argument("--chunk-frames", type=int, default=60, help="Sampled frames per child extraction process")
    parser.add_argument("--model-complexity", type=int, default=0)
    parser.add_argument("--min-detection-confidence", type=float, default=0.5)
    parser.add_argument("--restart", action="store_true", help="Discard existing stored frames and start each video from frame 0")
    return parser.parse_args()


def video_source_frame_count(path: str) -> int:
    capture = cv2.VideoCapture(path)
    try:
        return int(capture.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    finally:
        capture.release()


def videos_for_slugs(slugs: list[str]) -> list[RehabExerciseVideo]:
    with SessionLocal() as db:
        stmt = select(RehabExerciseVideo).where(RehabExerciseVideo.slug.in_(slugs))
        videos = db.execute(stmt).scalars().all()
        by_slug = {video.slug: video for video in videos}
        return [by_slug[slug] for slug in slugs if slug in by_slug]


def next_stored_frame(exercise_id: object) -> int:
    with SessionLocal() as db:
        pose = db.execute(select(VideoPoseExtraction).where(VideoPoseExtraction.exercise_id == exercise_id)).scalar_one_or_none()
        if pose is None:
            return 0
        frames = pose.pose_data.get("frames", []) if pose.pose_data else []
        indexes = [frame.get("frame_index") for frame in frames if isinstance(frame.get("frame_index"), int)]
        return max(indexes) + 1 if indexes else 0


def chunk_starts(total_frames: int, chunk_frames: int, max_frames: int) -> list[int]:
    frame_total = min(total_frames, max_frames) if max_frames > 0 else total_frames
    if frame_total <= 0:
        return [0]
    step = max(1, chunk_frames)
    return list(range(0, frame_total, step))


def main() -> None:
    args = parse_args()
    script = Path(__file__).with_name("extract_video_poses.py")
    results: list[dict[str, Any]] = []

    for video in videos_for_slugs(args.slugs):
        if not video.local_video_path:
            payload = {"slug": video.slug, "returncode": 1, "error": "missing local video path"}
            print(json.dumps(payload, indent=2), flush=True)
            results.append(payload)
            continue

        total_frames = video_source_frame_count(video.local_video_path)
        resume_frame = 0 if args.restart else next_stored_frame(video.exercise_id)
        if args.max_frames > 0:
            resume_frame = min(resume_frame, args.max_frames)
        starts = [start for start in chunk_starts(total_frames, args.chunk_frames, args.max_frames) if start >= resume_frame]
        video_result: dict[str, Any] = {
            "slug": video.slug,
            "source_frame_count": total_frames,
            "chunks": len(starts),
            "resume_frame": resume_frame,
            "returncode": 0,
        }

        for chunk_number, start_frame in enumerate(starts):
            remaining = total_frames - start_frame
            if args.max_frames > 0:
                remaining = min(remaining, args.max_frames - start_frame)
            if remaining <= 0:
                break
            chunk_size = min(max(1, args.chunk_frames), remaining)
            command = [
                sys.executable,
                str(script),
                "--slugs",
                video.slug,
                "--sample-fps",
                str(args.sample_fps),
                "--max-frames",
                str(chunk_size),
                "--start-frame",
                str(start_frame),
                "--model-complexity",
                str(args.model_complexity),
                "--min-detection-confidence",
                str(args.min_detection_confidence),
            ]
            if chunk_number > 0 or resume_frame > 0:
                command.append("--append")

            print(json.dumps({"slug": video.slug, "chunk": chunk_number + 1, "start_frame": start_frame, "chunk_frames": chunk_size}), flush=True)
            completed = subprocess.run(command, check=False)
            if completed.returncode != 0:
                video_result["returncode"] = completed.returncode
                video_result["failed_chunk"] = chunk_number + 1
                video_result["failed_start_frame"] = start_frame
                break

        results.append(video_result)
        print(json.dumps(video_result, indent=2), flush=True)

    failed = [item for item in results if item["returncode"] != 0]
    print(json.dumps({"count": len(results), "failed": len(failed), "results": results}, indent=2))
    if failed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
