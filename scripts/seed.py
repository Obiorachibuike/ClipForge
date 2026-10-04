#!/usr/bin/env python
"""Seed ClipForge: schema, system caption presets, an account, and optionally a
fully processed demo project built from a real video file.

Everything here goes through the same services the HTTP API uses, so seeded data
is indistinguishable from data created by the UI. Nothing is fabricated: the
demo project's transcript, candidates, framing and render are produced by running
the real jobs.

Usage:
    python scripts/seed.py                                   # schema + styles only
    python scripts/seed.py --user                            # + demo account
    python scripts/seed.py --user --asset demo/creator_masterclass.mp4 \
        --narration-script demo/narration.txt --process
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
API_ROOT = REPO_ROOT / "apps" / "api"
sys.path.insert(0, str(API_ROOT))

os.environ.setdefault("DATABASE_URL", f"sqlite:///{REPO_ROOT / '.data' / 'clipforge.db'}")

from sqlalchemy import select  # noqa: E402

from app.core.config import settings  # noqa: E402
from app.core.db import get_session_factory, init_engine_if_needed  # noqa: E402
from app.models.base import utcnow  # noqa: E402
from app.core.security import hash_password  # noqa: E402
from app.models import Job, JobType, Plan, Project, User, Video  # noqa: E402
from app.models import ClipCandidate  # noqa: E402
from app.services.bootstrap import prepare_directories  # noqa: E402
from app.services.caption_service import CaptionService  # noqa: E402
from app.services.clip_service import ClipService  # noqa: E402
from app.services.job_service import JobService  # noqa: E402
from app.services.pipeline.discover_job import run as run_discover  # noqa: E402
from app.services.pipeline.framing_job import run as run_framing  # noqa: E402
from app.services.pipeline.render_job import run as run_render  # noqa: E402
from app.services.pipeline.transcribe import run as run_transcribe  # noqa: E402
from app.services.pipeline.video_jobs import run_probe  # noqa: E402
from app.services.upload_service import UploadService  # noqa: E402

DEFAULT_EMAIL = "demo@clipforge.dev"
DEFAULT_PASSWORD = "clipforge-demo-2024"


def seed_schema() -> None:
    prepare_directories()
    init_engine_if_needed()  # creates tables when AUTO_CREATE_TABLES is on
    db = get_session_factory()()
    try:
        created = CaptionService.ensure_system_styles(db)
        print(f"caption presets ready ({len(created)} created)")
    finally:
        db.close()


def get_or_create_user(email: str, password: str, name: str = "") -> User:
    db = get_session_factory()()
    try:
        user = db.execute(select(User).where(User.email == email.lower())).scalars().first()
        if user:
            print(f"user exists: {user.email} ({user.id})")
            db.expunge(user)
            return user
        user = User(
            email=email.lower(),
            name=name or email.split("@")[0].replace(".", "_").title(),
            hashed_password=hash_password(password),
            is_active=True,
            email_verified_at=utcnow(),
            plan=Plan.PRO.value,
        )
        db.add(user)
        db.commit()
        db.refresh(user)
        db.expunge(user)
        print(f"created user: {user.email} ({user.id})")
        return user
    finally:
        db.close()


def run_job(db, job: Job, runner, label: str, user_id: str) -> dict:
    """Execute a job through its real runner and record the outcome."""
    started = time.time()
    JobService.mark_started(db, job, worker_id="seed-script")
    try:
        result = runner(db, job)
    except Exception as exc:
        JobService.mark_failed(db, job, exc)
        raise SystemExit(f"{label} failed: {type(exc).__name__}: {exc}") from exc
    JobService.mark_succeeded(db, job, result or {})
    print(f"  {label}: ok in {time.time() - started:.1f}s")
    return result or {}


def seed_demo_project(user_id: str, asset: Path, process: bool, script_path: Path | None) -> None:
    db = get_session_factory()()
    try:
        user = db.get(User, user_id)
        project = Project(
            user_id=user_id,
            name=f"Demo - {asset.stem.replace('_', ' ').title()}",
            description="Seeded from a real local video file.",
            source_language="en",
        )
        db.add(project)
        db.commit()
        db.refresh(project)

        session = UploadService.init_upload(
            db,
            user,
            filename=asset.name,
            size_bytes=asset.stat().st_size,
            content_type="video/mp4",
            project_id=project.id,
        )
        print(f"upload session {session.upload_id}: {session.total_chunks} chunks of {session.chunk_size} bytes")
        with asset.open("rb") as handle:
            index = 0
            while True:
                chunk = handle.read(session.chunk_size)
                if not chunk:
                    break
                UploadService.receive_chunk(db, user, session.upload_id, index, chunk)
                index += 1
                if index % 5 == 0:
                    print(f"  uploaded chunk {index}/{session.total_chunks}", end="\r", flush=True)
        print()
        video, session, probe_job = UploadService.complete_upload(db, user, session.upload_id)
        print(f"uploaded video {video.id} ({video.size_bytes / 1_048_576:.1f} MB)")

        if script_path and script_path.exists():
            video.narration_script = script_path.read_text().strip()
            db.commit()
            print(f"narration script attached ({len(video.narration_script.split())} words)")
        else:
            print("no narration script: transcription will need a speech model")

        if not process:
            print("stopping before processing (pass --process to run the pipeline)")
            print(f"project id: {project.id}")
            return

        # 1. probe (the job the upload endpoint already queued)
        run_job(db, probe_job, run_probe, "probe", user_id)

        # 2. transcribe
        transcribe_job = JobService.create(
            db,
            type_=JobType.VIDEO_TRANSCRIBE.value,
            payload={"video_id": video.id},
            user_id=user_id,
            project_id=project.id,
            video_id=video.id,
            enqueue=False,
        )
        transcript_result = run_job(db, transcribe_job, run_transcribe, "transcribe", user_id)
        print(
            f"    words={transcript_result.get('words')} segments={transcript_result.get('segments')} "
            f"provider={transcript_result.get('provider')}"
        )

        # 3. framing analysis (faces, saliency, crop path)
        framing_job = JobService.create(
            db,
            type_=JobType.VIDEO_ANALYZE_FRAMING.value,
            payload={"video_id": video.id},
            user_id=user_id,
            project_id=project.id,
            video_id=video.id,
            enqueue=False,
        )
        run_job(db, framing_job, run_framing, "framing analysis", user_id)

        # 4. clip discovery
        discover_job = JobService.create(
            db,
            type_=JobType.PROJECT_DISCOVER_CLIPS.value,
            payload={"project_id": project.id},
            user_id=user_id,
            project_id=project.id,
            enqueue=False,
        )
        discovery = run_job(db, discover_job, run_discover, "discover clips", user_id)
        print(f"    candidates={discovery.get('candidates')}")

        candidates = (
            db.execute(
                select(ClipCandidate)
                .where(ClipCandidate.project_id == project.id)
                .order_by(ClipCandidate.score.desc())
            )
            .scalars()
            .all()
        )
        if not candidates:
            print("no candidates produced; stopping here")
            return
        for candidate in candidates[:5]:
            print(
                f"    #{candidate.rank if hasattr(candidate, 'rank') else '-'} "
                f"score={candidate.score:.3f} {candidate.start_time:6.1f}s → {candidate.end_time:6.1f}s "
                f"{(candidate.title or '')[:60]}"
            )

        # 5. turn the best candidate into a clip and render it
        clip = ClipService.create_from_candidate(db, user, candidates[0], preset="tiktok")
        render = ClipService.enqueue_render(db, user, clip, preset="tiktok")
        render_job = db.get(Job, render.job_id)
        result = run_job(db, render_job, run_render, "render clip", user_id)
        size_mb = (result.get("size_bytes") or 0) / 1_048_576
        print(f"    rendered {size_mb:.1f} MB → {result.get('storage_key')}")

        print("\ndemo project ready")
        print(f"  project id: {project.id}")
        print(f"  clip id:    {clip.id}")
    finally:
        db.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--user", action="store_true", help="create the demo account")
    parser.add_argument("--email", default=DEFAULT_EMAIL)
    parser.add_argument("--password", default=DEFAULT_PASSWORD)
    parser.add_argument("--asset", default="", help="video file to upload as a demo project")
    parser.add_argument("--narration-script", default="", help="text file containing the spoken script")
    parser.add_argument("--process", action="store_true", help="run probe/transcribe/analyze/discover/render")
    args = parser.parse_args()

    print(f"database: {settings.database_url}")
    seed_schema()

    if not args.user:
        print("schema ready. Pass --user to also create the demo account.")
        return 0

    user = get_or_create_user(args.email, args.password)
    if args.asset:
        asset = Path(args.asset)
        if not asset.is_absolute():
            asset = REPO_ROOT / asset
        if not asset.exists():
            raise SystemExit(f"asset not found: {asset}\nBuild it first: python scripts/make_demo_asset.py")
        script_path = Path(args.narration_script) if args.narration_script else None
        seed_demo_project(user.id, asset, args.process, script_path)
    else:
        print("no --asset given; account created without a project")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
