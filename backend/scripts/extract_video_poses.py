"""Extract offline MediaPipe pose data from downloaded demo videos.

The extractor stores frame samples and summary metrics in Postgres. Use
``--sample-fps <= 0`` to store every source video frame. Use ``--start-frame``
and ``--append`` for chunked extraction when full-video processing is too large
for the host.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path
from typing import Any

import cv2
import mediapipe as mp
from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.db.models import RehabExerciseVideo, VideoPoseExtraction
from app.db.session import Base, SessionLocal, engine

DEMO_PROFILE = "acl_knee_stiffness"
LANDMARK_NAMES = [item.name.lower() for item in mp.solutions.pose.PoseLandmark]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Extract pose data for downloaded rehab demo videos")
    parser.add_argument("--profile", default=DEMO_PROFILE)
    parser.add_argument("--slugs", nargs="*", default=[])
    parser.add_argument("--sample-fps", type=float, default=6.0, help="Sample rate in frames per second; <= 0 stores every source frame")
    parser.add_argument("--max-frames", type=int, default=360, help="Maximum sampled frames to store; <= 0 stores until the video ends")
    parser.add_argument("--start-frame", type=int, default=0, help="Source frame index to start reading from")
    parser.add_argument("--append", action="store_true", help="Append extracted frames to an existing pose row instead of replacing it")
    parser.add_argument("--min-detection-confidence", type=float, default=0.5)
    parser.add_argument("--model-complexity", type=int, default=1)
    return parser.parse_args()


def ensure_schema() -> None:
    Base.metadata.create_all(bind=engine)
    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE rehab_exercise_videos ADD COLUMN IF NOT EXISTS relevance_rank INTEGER NOT NULL DEFAULT 999"))
        conn.execute(text("ALTER TABLE rehab_exercise_videos ADD COLUMN IF NOT EXISTS relevance_score INTEGER NOT NULL DEFAULT 0"))


def angle_degrees(a: dict[str, float], b: dict[str, float], c: dict[str, float]) -> float | None:
    ab = (a["x"] - b["x"], a["y"] - b["y"])
    cb = (c["x"] - b["x"], c["y"] - b["y"])
    ab_len = math.hypot(*ab)
    cb_len = math.hypot(*cb)
    if ab_len == 0 or cb_len == 0:
        return None
    cosine = max(-1.0, min(1.0, (ab[0] * cb[0] + ab[1] * cb[1]) / (ab_len * cb_len)))
    return round(math.degrees(math.acos(cosine)), 2)


def summarize(values: list[float]) -> dict[str, float | int | None]:
    if not values:
        return {"count": 0, "min": None, "max": None, "mean": None}
    return {
        "count": len(values),
        "min": round(min(values), 2),
        "max": round(max(values), 2),
        "mean": round(sum(values) / len(values), 2),
    }


def landmark_payload(results: Any) -> list[dict[str, float | str]]:
    if not results.pose_landmarks:
        return []
    return [
        {
            "name": LANDMARK_NAMES[index],
            "x": round(landmark.x, 6),
            "y": round(landmark.y, 6),
            "z": round(landmark.z, 6),
            "visibility": round(float(landmark.visibility or 0), 6),
        }
        for index, landmark in enumerate(results.pose_landmarks.landmark)
    ]


def named_landmarks(landmarks: list[dict[str, Any]]) -> dict[str, dict[str, float]]:
    return {str(item["name"]): item for item in landmarks}


def metrics_for_frames(frames: list[dict[str, Any]]) -> dict[str, Any]:
    left_knee_angles: list[float] = []
    right_knee_angles: list[float] = []
    detected = 0

    for frame in frames:
        if frame.get("detected"):
            detected += 1
        metrics = frame.get("metrics") or {}
        left = metrics.get("left_knee_angle")
        right = metrics.get("right_knee_angle")
        if isinstance(left, (int, float)):
            left_knee_angles.append(float(left))
        if isinstance(right, (int, float)):
            right_knee_angles.append(float(right))

    processed = len(frames)
    return {
        "processed_frame_count": processed,
        "stored_frame_count": processed,
        "detection_rate": round(detected / processed, 4) if processed else 0.0,
        "left_knee_angle": summarize(left_knee_angles),
        "right_knee_angle": summarize(right_knee_angles),
    }


def merge_pose_data(existing: dict[str, Any] | None, extracted: dict[str, Any]) -> dict[str, Any]:
    if not existing:
        return extracted

    frame_by_index: dict[int, dict[str, Any]] = {}
    for frame in existing.get("frames", []):
        frame_index = frame.get("frame_index")
        if isinstance(frame_index, int):
            frame_by_index[frame_index] = frame
    for frame in extracted.get("frames", []):
        frame_index = frame.get("frame_index")
        if isinstance(frame_index, int):
            frame_by_index[frame_index] = frame

    merged_frames = [frame_by_index[index] for index in sorted(frame_by_index)]
    merged = dict(extracted)
    merged["frames"] = merged_frames
    return merged


def extract_video(
    path: Path,
    sample_fps: float,
    max_frames: int,
    min_detection_confidence: float,
    model_complexity: int,
    start_frame: int = 0,
) -> dict[str, Any]:
    capture = cv2.VideoCapture(str(path))
    if not capture.isOpened():
        raise RuntimeError(f"Could not open video: {path}")

    fps = float(capture.get(cv2.CAP_PROP_FPS) or 0.0)
    frame_count = int(capture.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    duration = float(frame_count / fps) if fps > 0 else 0.0
    sample_every = max(1, int(round(fps / sample_fps))) if fps > 0 and sample_fps > 0 else 1
    frame_limit = max_frames if max_frames > 0 else None
    index = max(0, start_frame)
    if index:
        capture.set(cv2.CAP_PROP_POS_FRAMES, index)

    frames: list[dict[str, Any]] = []

    with mp.solutions.pose.Pose(
        static_image_mode=False,
        model_complexity=model_complexity,
        enable_segmentation=False,
        min_detection_confidence=min_detection_confidence,
        min_tracking_confidence=0.5,
    ) as pose:
        while frame_limit is None or len(frames) < frame_limit:
            ok, frame = capture.read()
            if not ok:
                break
            if index % sample_every != 0:
                index += 1
                continue

            rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            results = pose.process(rgb)
            landmarks = landmark_payload(results)
            named = named_landmarks(landmarks)
            left_angle = None
            right_angle = None
            if landmarks:
                left_angle = angle_degrees(named["left_hip"], named["left_knee"], named["left_ankle"])
                right_angle = angle_degrees(named["right_hip"], named["right_knee"], named["right_ankle"])

            frames.append(
                {
                    "frame_index": index,
                    "timestamp_seconds": round(index / fps, 3) if fps > 0 else None,
                    "detected": bool(landmarks),
                    "landmarks": landmarks,
                    "metrics": {
                        "left_knee_angle": left_angle,
                        "right_knee_angle": right_angle,
                    },
                }
            )
            index += 1

    capture.release()
    return {
        "frame_count": frame_count,
        "fps": fps,
        "duration_seconds": duration,
        "pose_data": {
            "sample_fps": sample_fps,
            "sample_every_frames": sample_every,
            "landmark_names": LANDMARK_NAMES,
            "frames": frames,
        },
        "metrics": metrics_for_frames(frames),
    }


def upsert_extraction(db: Any, video: RehabExerciseVideo, path: Path, extracted: dict[str, Any], append: bool) -> dict[str, Any]:
    existing = video.pose_extraction if append else None
    pose_data = merge_pose_data(existing.pose_data if existing else None, extracted["pose_data"])
    metrics = metrics_for_frames(pose_data.get("frames", []))
    detected_frame_count = int(metrics["left_knee_angle"]["count"] or metrics["right_knee_angle"]["count"] or 0)
    detected_frame_count = sum(1 for frame in pose_data.get("frames", []) if frame.get("detected"))

    stmt = insert(VideoPoseExtraction).values(
        exercise_id=video.exercise_id,
        source_video_path=str(path),
        frame_count=extracted["frame_count"],
        detected_frame_count=detected_frame_count,
        fps=extracted["fps"],
        duration_seconds=extracted["duration_seconds"],
        detector_name="mediapipe_pose",
        detector_version=getattr(mp, "__version__", ""),
        pose_data=pose_data,
        metrics=metrics,
    )
    update_payload = {
        "source_video_path": stmt.excluded.source_video_path,
        "frame_count": stmt.excluded.frame_count,
        "detected_frame_count": stmt.excluded.detected_frame_count,
        "fps": stmt.excluded.fps,
        "duration_seconds": stmt.excluded.duration_seconds,
        "detector_name": stmt.excluded.detector_name,
        "detector_version": stmt.excluded.detector_version,
        "pose_data": stmt.excluded.pose_data,
        "metrics": stmt.excluded.metrics,
    }
    db.execute(stmt.on_conflict_do_update(index_elements=["exercise_id"], set_=update_payload))
    video.pose_status = "extracted"
    return metrics


def main() -> None:
    args = parse_args()
    ensure_schema()
    with SessionLocal() as db:
        stmt = select(RehabExerciseVideo).where(RehabExerciseVideo.demo_profile == args.profile)
        if args.slugs:
            stmt = stmt.where(RehabExerciseVideo.slug.in_(args.slugs))
        stmt = stmt.order_by(RehabExerciseVideo.relevance_rank.asc(), RehabExerciseVideo.title.asc())
        videos = db.execute(stmt).scalars().all()
        results: list[dict[str, Any]] = []
        for video in videos:
            if video.download_status != "downloaded" or not video.local_video_path:
                video.pose_status = "missing_video"
                results.append({"slug": video.slug, "status": "missing_video"})
                db.commit()
                continue
            path = Path(video.local_video_path)
            if not path.exists():
                video.pose_status = "missing_video"
                results.append({"slug": video.slug, "status": "missing_video", "path": str(path)})
                db.commit()
                continue
            try:
                extracted = extract_video(
                    path,
                    sample_fps=args.sample_fps,
                    max_frames=args.max_frames,
                    min_detection_confidence=args.min_detection_confidence,
                    model_complexity=args.model_complexity,
                    start_frame=args.start_frame,
                )
                metrics = upsert_extraction(db, video, path, extracted, append=args.append)
                db.commit()
                results.append({"slug": video.slug, "status": "extracted", "metrics": metrics})
            except Exception as exc:  # noqa: BLE001
                video.pose_status = "failed"
                db.commit()
                results.append({"slug": video.slug, "status": "failed", "error": f"{type(exc).__name__}: {exc}"})
        print(json.dumps({"profile": args.profile, "videos": results}, indent=2))


if __name__ == "__main__":
    main()
