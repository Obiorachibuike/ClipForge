"""Framing analysis job: detect faces / subjects and plan the vertical crop."""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.errors import MediaError, NotFoundError
from app.core.logging import get_logger
from app.models import AnalysisKind, Job, Project, Video, VideoAnalysis
from app.services.job_service import JobService
from app.services.media import ffmpeg
from app.services.media.framing import analyze_video
from app.services.media.vision import build_detector
from app.services.pipeline.workspace import workspace_for
from app.services.storage import get_storage

log = get_logger(__name__)


def run(db: Session, job: Job) -> dict:
    video_id = job.payload.get("video_id") or job.video_id
    video = db.get(Video, video_id) if video_id else None
    if video is None:
        raise NotFoundError("Video not found for framing analysis.", code="video_not_found")

    project = db.get(Project, video.project_id)
    aspect_ratio = str(job.payload.get("aspect_ratio") or (project.target_aspect_ratio if project else "9:16"))

    detector = build_detector()
    if not detector.available:
        # Refuse to fake tracking: record that framing analysis is unavailable.
        db.add(
            VideoAnalysis(
                video_id=video.id,
                kind=AnalysisKind.FRAMING.value,
                status="unavailable",
                detector=detector.name,
                data={},
                error_message=f"No face detector available ({detector.note}). Install opencv-python-headless or mediapipe.",
            )
        )
        db.commit()
        raise MediaError(
            "Face detection is unavailable on this server, so smart framing cannot run. "
            "A centre crop will be used instead.",
            code="vision_unavailable",
        )

    JobService.report_progress(db, job, stage="sampling", progress=5, message="Sampling frames", force=True)
    storage = get_storage()

    with workspace_for(job.id) as workdir:
        source = storage.materialize(video.storage_key, workdir)
        energy_curve = _cached_energy(db, video.id) or _measure_energy(db, job, source)
        analysis = analyze_video(
            source,
            source_width=video.width or None,
            source_height=video.height or None,
            duration=video.duration_seconds or ffmpeg.media_duration(source),
            aspect_ratio=aspect_ratio,
            detector=detector,
            energy_curve=energy_curve,
            job_id=job.id,
            workdir=workdir,
            progress_cb=lambda pct, message: JobService.report_progress(
                db, job, stage="tracking", progress=5 + pct * 0.9, message=message
            ),
        )

    existing = db.execute(
        select(VideoAnalysis).where(
            VideoAnalysis.video_id == video.id, VideoAnalysis.kind == AnalysisKind.FRAMING.value
        )
    ).scalars().first()
    if existing is None:
        existing = VideoAnalysis(video_id=video.id, kind=AnalysisKind.FRAMING.value)
        db.add(existing)
    existing.status = "completed"
    existing.detector = analysis["detector"]
    existing.data = analysis
    existing.error_message = ""
    db.commit()

    JobService.report_progress(db, job, stage="done", progress=99, message="Framing plan ready")
    return {
        "video_id": video.id,
        "strategy": analysis["framing_strategy"],
        "faces_detected": analysis["faces_detected"],
        "tracks": analysis["tracks"],
        "keyframes": len(analysis["keyframes"]),
        "confidence": analysis["tracking_confidence"],
        "detector": analysis["detector"],
    }


def _cached_energy(db: Session, video_id: str):
    row = db.execute(
        select(VideoAnalysis).where(VideoAnalysis.video_id == video_id, VideoAnalysis.kind == AnalysisKind.ENERGY.value)
    ).scalars().first()
    if row and row.data.get("times"):
        return row.data["times"], row.data["rms"]
    return None


def _measure_energy(db: Session, job: Job, source):
    try:
        envelope = ffmpeg.energy_envelope(source, hop_seconds=0.05, job_id=job.id)
    except Exception as exc:
        log.warning("framing.energy_failed", job_id=job.id, error=str(exc)[:200])
        return None
    if not envelope["times"]:
        return None
    return envelope["times"], envelope["rms"]


def framing_for_clip(db: Session, video_id: str) -> dict:
    """Latest framing plan for a video (empty dict when none exists)."""
    row = db.execute(
        select(VideoAnalysis)
        .where(VideoAnalysis.video_id == video_id, VideoAnalysis.kind == AnalysisKind.FRAMING.value)
        .order_by(VideoAnalysis.created_at.desc())
    ).scalars().first()
    return (row.data or {}) if row and row.status == "completed" else {}
