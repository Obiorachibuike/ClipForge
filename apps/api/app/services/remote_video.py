"""Import public videos from supported social/video platforms.

The HTTP endpoint only enqueues work. yt-dlp runs in the existing worker so a
slow source never holds an API request open, and the downloaded file then enters
the same validated upload/probe pipeline as a browser upload.
"""
from __future__ import annotations

import mimetypes
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.errors import MediaError, NotFoundError, ValidationError
from app.core.logging import get_logger
from app.models import Job, Project, ProjectStatus, User
from app.services.job_service import JobService
from app.services.pipeline.workspace import workspace_for
from app.services.runtime import registry
from app.services.upload_service import UploadService

log = get_logger(__name__)

# Keep generic extraction off: accepting arbitrary hosts would turn this feature
# into a server-side request proxy. Subdomains are accepted for regional/mobile
# variants owned by each supported platform.
SUPPORTED_VIDEO_HOSTS = (
    "youtube.com",
    "youtu.be",
    "tiktok.com",
    "facebook.com",
    "fb.watch",
    "instagram.com",
    "vimeo.com",
    "dailymotion.com",
    "twitch.tv",
    "x.com",
    "twitter.com",
)


def _host_matches(host: str, suffix: str) -> bool:
    return host == suffix or host.endswith(f".{suffix}")


def validate_remote_video_url(value: str) -> str:
    """Return a normalised HTTPS URL or raise a field-safe validation error."""
    raw = value.strip()
    try:
        parsed = urlsplit(raw)
    except ValueError as exc:
        raise ValidationError("Enter a valid video URL.", field="url") from exc

    host = (parsed.hostname or "").rstrip(".").lower()
    if parsed.scheme.lower() not in {"http", "https"} or not host or parsed.username or parsed.password:
        raise ValidationError("Enter a public HTTP or HTTPS video URL.", field="url")
    if not any(_host_matches(host, allowed) for allowed in SUPPORTED_VIDEO_HOSTS):
        raise ValidationError(
            "That site is not supported yet. Use a YouTube, TikTok, Facebook, Instagram, Vimeo, Dailymotion, Twitch, X or Twitter URL.",
            field="url",
        )

    # Strip fragments (they are never sent to servers) while preserving platform
    # query parameters such as YouTube's video id and TikTok share metadata.
    return urlunsplit((parsed.scheme.lower(), parsed.netloc, parsed.path or "/", parsed.query, ""))


def run_import(db: Session, job: Job) -> dict:
    """Download one public video, store it, then queue the normal probe job."""
    source_url = validate_remote_video_url(str(job.payload.get("url") or ""))
    user = db.get(User, job.user_id) if job.user_id else None
    project = db.get(Project, job.project_id) if job.project_id else None
    if user is None or project is None or project.user_id != user.id:
        raise NotFoundError("Project or account no longer exists.", code="project_not_found")

    try:
        import yt_dlp
        from yt_dlp.utils import DownloadError
    except ImportError as exc:  # pragma: no cover - deployment packaging guard
        raise MediaError("URL imports are unavailable on this deployment.", code="url_import_unavailable") from exc

    project.status = ProjectStatus.PROCESSING.value
    db.commit()
    JobService.report_progress(db, job, stage="download", progress=3, message="Connecting to video source", force=True)

    with workspace_for(job.id) as workdir:
        output_template = str(workdir / "source.%(ext)s")

        def progress_hook(event: dict) -> None:
            registry.token(job.id).raise_if_cancelled()
            if event.get("status") != "downloading":
                return
            downloaded = float(event.get("downloaded_bytes") or 0)
            total = float(event.get("total_bytes") or event.get("total_bytes_estimate") or 0)
            progress = 8 + (downloaded / total * 62 if total > 0 else 4)
            JobService.report_progress(
                db,
                job,
                stage="download",
                progress=min(progress, 70),
                message="Downloading source video",
            )

        options = {
            "outtmpl": output_template,
            "format": "bv*+ba/b",
            "merge_output_format": "mp4",
            "noplaylist": True,
            "playlist_items": "1",
            "max_filesize": settings.media_max_upload_bytes,
            "socket_timeout": 30,
            "retries": 3,
            "fragment_retries": 3,
            "restrictfilenames": True,
            "quiet": True,
            "no_warnings": True,
            "progress_hooks": [progress_hook],
        }

        try:
            with yt_dlp.YoutubeDL(options) as downloader:
                info = downloader.extract_info(source_url, download=True)
        except DownloadError as exc:
            log.warning("remote_video.download_failed", host=urlsplit(source_url).hostname, error=str(exc)[:500])
            raise MediaError(
                "The platform could not provide that video. Check that it is public and the URL is correct.",
                code="video_download_failed",
            ) from exc

        candidates = [
            path
            for path in workdir.glob("source.*")
            if path.is_file() and path.suffix not in {".part", ".ytdl", ".json"}
        ]
        if not candidates:
            raise MediaError("The platform did not return a downloadable video.", code="video_download_failed")
        downloaded_path = max(candidates, key=lambda path: path.stat().st_size)
        size = downloaded_path.stat().st_size
        if size > settings.media_max_upload_bytes:
            raise MediaError("That video is larger than this deployment allows.", code="video_too_large")

        title = str(info.get("title") or info.get("id") or "imported-video").strip()
        title = " ".join(title.split())[:180] or "imported-video"
        filename = f"{title}{downloaded_path.suffix.lower()}"
        content_type = mimetypes.guess_type(filename)[0] or "video/mp4"

        JobService.report_progress(db, job, stage="store", progress=76, message="Saving imported video", force=True)
        session = UploadService.init_upload(
            db,
            user,
            filename=filename,
            size_bytes=size,
            content_type=content_type,
            project_id=project.id,
        )
        with downloaded_path.open("rb") as source:
            UploadService.stage_stream(db, user, session.upload_id, source)
        video, _session, probe_job = UploadService.complete_upload(db, user, session.upload_id)

    JobService.report_progress(db, job, stage="store", progress=96, message="Video imported; inspection queued", force=True)
    return {
        "video_id": video.id,
        "probe_job_id": probe_job.id,
        "platform": str(info.get("extractor_key") or info.get("extractor") or ""),
        "title": title,
    }
