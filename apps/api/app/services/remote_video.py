"""Import public videos from supported social/video platforms.

The HTTP endpoint only enqueues work. yt-dlp runs in the existing worker so a
slow source never holds an API request open, and the downloaded file then enters
the same validated upload/probe pipeline as a browser upload.
"""
from __future__ import annotations

import functools
import mimetypes
import socket
import urllib.request
from urllib.parse import urlsplit, urlunsplit

from app.core.config import settings
from app.core.errors import MediaError, NotFoundError, ValidationError
from app.core.logging import get_logger
from app.core.ssrf import (
    SafeRedirectHandler,
    SSRFBlocked,
    assert_public_url,
    is_public_ip,
    safe_host_for_logs,
)
from app.models import Job, Project, ProjectStatus, User
from app.services.job_service import JobService
from app.services.pipeline.workspace import workspace_for
from app.services.runtime import registry
from app.services.upload_service import UploadService
from sqlalchemy.orm import Session

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

    # Reject non-standard ports and embedded credentials here (cheap, no DNS).
    try:
        assert_public_url(raw, resolve=False)
    except SSRFBlocked as exc:
        raise ValidationError(
            "Enter a public HTTP or HTTPS video URL on the standard ports.", field="url"
        ) from exc

    # Strip fragments (they are never sent to servers) while preserving platform
    # query parameters such as YouTube's video id and TikTok share metadata.
    return urlunsplit((parsed.scheme.lower(), parsed.netloc, parsed.path or "/", parsed.query, ""))


def assert_public_host(host: str) -> None:
    """Refuse hosts that resolve to private, loopback or link-local addresses.

    The platform allowlist already prevents arbitrary hosts, but DNS is data
    supplied by an external party: a hostname that resolves inside the
    deployment's network must never be fetched. Every resolved address is
    checked (not just the first). The address policy itself lives in
    `app.core.ssrf` so URL imports and the downloader's request guard agree.
    """
    try:
        infos = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise MediaError(
            "That video host could not be resolved. Check the URL and try again.",
            code="video_host_unresolvable",
        ) from exc
    addresses = {info[4][0] for info in infos if info[4]}
    if not addresses:
        raise MediaError(
            "That video host could not be resolved. Check the URL and try again.",
            code="video_host_unresolvable",
        )
    for raw in addresses:
        if not is_public_ip(str(raw).split("%", 1)[0]):
            log.warning("remote_video.blocked_address", host=host, address=raw)
            raise MediaError(
                "That video URL points to an address this deployment will not fetch.",
                code="blocked_video_host",
            )


def _guarded_youtube_dl(yt_dlp):
    """Return a YoutubeDL subclass whose HTTP layer enforces the SSRF policy.

    yt-dlp follows redirects and resolves its own CDN URLs. Checking only the
    submitted page URL would let a redirect (or a rebinding DNS answer) reach
    ``169.254.169.254`` or a service on the private network. Every request is
    validated before urllib opens it, and redirect hops are validated by
    :class:`SafeRedirectHandler`.

    Falls back to the stock class if yt-dlp changes its networking internals;
    the preflight in :func:`run_import` still blocks the obvious cases.
    """
    try:
        from yt_dlp.networking._urllib import UrllibRH
        from yt_dlp.YoutubeDL import _REQUEST_HANDLERS, _RH_PREFERENCES
    except Exception:  # pragma: no cover - yt-dlp internal API changed
        log.warning("remote_video.ssrf_guard_unavailable")
        return yt_dlp.YoutubeDL

    class GuardedUrllibRH(UrllibRH):
        # Keep yt-dlp's registry key so its own compatibility paths still find
        # the handler they expect (`handlers['Urllib']`).
        RH_KEY = "Urllib"
        RH_NAME = "urllib"

        def _create_instance(self, proxies, cookiejar, legacy_ssl_support=None):
            opener = super()._create_instance(proxies, cookiejar, legacy_ssl_support)
            # Replace urllib's redirect handler: it would otherwise follow a
            # 30x to a private address before any application code runs.
            opener.handlers[:] = [
                handler
                for handler in opener.handlers
                if not isinstance(handler, urllib.request.HTTPRedirectHandler)
            ]
            opener.add_handler(SafeRedirectHandler())
            return opener

        def _send(self, request):
            # Validate the actual request URL, including the ones yt-dlp
            # discovers for manifests and fragments.
            assert_public_url(request.url, resolve=True)
            return super()._send(request)

    class GuardedYoutubeDL(yt_dlp.YoutubeDL):
        @functools.cached_property
        def _request_director(self):
            handlers = [
                GuardedUrllibRH if handler is UrllibRH else handler
                for handler in _REQUEST_HANDLERS.values()
            ]
            return self.build_request_director(handlers, _RH_PREFERENCES)

    return GuardedYoutubeDL


def run_import(db: Session, job: Job) -> dict:
    """Download one public video, store it, then queue the normal probe job."""
    source_url = validate_remote_video_url(str(job.payload.get("url") or ""))
    assert_public_host(urlsplit(source_url).hostname or "")
    user = db.get(User, job.user_id) if job.user_id else None
    project = db.get(Project, job.project_id) if job.project_id else None
    if user is None or project is None or project.user_id != user.id:
        raise NotFoundError("Project or account no longer exists.", code="project_not_found")

    try:
        import yt_dlp
        from yt_dlp.utils import DownloadError
    except ImportError as exc:  # pragma: no cover - deployment packaging guard
        raise MediaError("URL imports are unavailable on this deployment.", code="url_import_unavailable") from exc

    # Full preflight (including DNS): refuse an allow-listed hostname that
    # currently resolves to a private address. Request-time validation in the
    # guarded downloader then covers redirects and CDN hosts.
    try:
        assert_public_url(source_url, allowed_hosts=SUPPORTED_VIDEO_HOSTS, resolve=True)
    except SSRFBlocked as exc:
        log.warning("remote_video.blocked_url", host=safe_host_for_logs(source_url), error=str(exc)[:200])
        raise MediaError(
            "That video URL resolves to a private or reserved address and cannot be imported.",
            code="url_not_public",
        ) from exc

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
            # Stop the transfer itself, rather than downloading an unbounded
            # file and only checking its size afterwards.
            if downloaded > settings.media_max_upload_bytes:
                raise MediaError(
                    "That video is larger than this deployment allows.", code="video_too_large"
                )
            total = float(event.get("total_bytes") or event.get("total_bytes_estimate") or 0)
            progress = 8 + (downloaded / total * 62 if total > 0 else 4)
            JobService.report_progress(
                db,
                job,
                stage="download",
                progress=min(progress, 70),
                message="Downloading source video",
            )

        rejection: dict[str, str] = {}

        def duration_filter(info: dict, *, incomplete: bool = False) -> str | None:
            """Ask the extractor to skip sources beyond this deployment's limit.

            The probe enforces the same limit after download, so a source whose
            duration is only known during/after download is still rejected.
            """
            if incomplete:
                return None
            limit = int(settings.media_max_duration_seconds or 0)
            if limit <= 0:
                return None
            duration = info.get("duration")
            try:
                value = float(duration) if duration is not None else None
            except (TypeError, ValueError):
                return None
            if value is not None and value > limit:
                rejection["reason"] = "video_too_long"
                return f"Video exceeds the {limit}-second limit"
            return None

        options = {
            "outtmpl": output_template,
            "match_filter": duration_filter,
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

        downloader_cls = _guarded_youtube_dl(yt_dlp)
        try:
            with downloader_cls(options) as downloader:
                info = downloader.extract_info(source_url, download=True)
        except MediaError:
            raise
        except DownloadError as exc:
            if rejection.get("reason") == "video_too_long":
                minutes = max(1, int(settings.media_max_duration_seconds // 60))
                raise MediaError(
                    f"That video is longer than the {minutes}-minute limit for this deployment.",
                    code="video_too_long",
                ) from exc
            log.warning(
                "remote_video.download_failed",
                host=safe_host_for_logs(source_url),
                error=str(exc)[:500],
            )
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
