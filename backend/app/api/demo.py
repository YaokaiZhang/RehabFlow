from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import RehabExerciseVideo
from app.db.session import get_db

router = APIRouter(prefix="/demo", tags=["demo"])


def _media_url(local_path: str | None) -> str | None:
	if not local_path:
		return None
	path = Path(local_path)
	if path.name:
		return f"/media/rehab_videos/{path.name}"
	return None


def _video_payload(video: RehabExerciseVideo) -> dict[str, Any]:
	pose = video.pose_extraction
	return {
		"exercise_id": str(video.exercise_id),
		"slug": video.slug,
		"title": video.title,
		"source_url": video.source_url,
		"video_url": video.video_url,
		"local_video_path": video.local_video_path,
		"public_video_url": video.public_video_url or _media_url(video.local_video_path),
		"demo_profile": video.demo_profile,
		"relevance_rank": video.relevance_rank,
		"relevance_score": video.relevance_score,
		"relevance_notes": video.relevance_notes,
		"download_status": video.download_status,
		"pose_status": video.pose_status,
		"metadata": video.exercise_metadata,
		"pose_summary": None
		if pose is None
		else {
			"frame_count": pose.frame_count,
			"detected_frame_count": pose.detected_frame_count,
			"fps": pose.fps,
			"duration_seconds": pose.duration_seconds,
			"metrics": pose.metrics,
		},
	}


@router.get("/acl-knee-stiffness/videos")
def list_acl_knee_stiffness_videos(db: Session = Depends(get_db)) -> dict[str, Any]:
	stmt = (
		select(RehabExerciseVideo)
		.where(RehabExerciseVideo.demo_profile == "acl_knee_stiffness")
		.order_by(RehabExerciseVideo.relevance_rank.asc(), RehabExerciseVideo.title.asc())
	)
	videos = db.execute(stmt).scalars().all()
	return {
		"profile": "acl_knee_stiffness",
		"title": "Mild knee stiffness after ACL rehab",
		"description": "Low-to-moderate mobility and quadriceps-control references for a safe demo interaction.",
		"videos": [_video_payload(video) for video in videos],
	}


@router.get("/acl-knee-stiffness/poses/{exercise_id}")
def get_acl_pose_extraction(exercise_id: str, db: Session = Depends(get_db)) -> dict[str, Any]:
	try:
		exercise_uuid = uuid.UUID(exercise_id)
	except ValueError as exc:
		raise HTTPException(status_code=400, detail="Invalid exercise id") from exc

	video = db.get(RehabExerciseVideo, exercise_uuid)
	if video is None or video.demo_profile != "acl_knee_stiffness":
		raise HTTPException(status_code=404, detail="Demo video not found")
	if video.pose_extraction is None:
		raise HTTPException(status_code=404, detail="Pose extraction not available")

	pose = video.pose_extraction
	return {
		"video": _video_payload(video),
		"extraction": {
			"extraction_id": str(pose.extraction_id),
			"exercise_id": str(pose.exercise_id),
			"source_video_path": pose.source_video_path,
			"frame_count": pose.frame_count,
			"detected_frame_count": pose.detected_frame_count,
			"fps": pose.fps,
			"duration_seconds": pose.duration_seconds,
			"detector_name": pose.detector_name,
			"detector_version": pose.detector_version,
			"metrics": pose.metrics,
			"pose_data": pose.pose_data,
		},
	}
