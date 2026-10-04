"""Smart framing: find who is on screen, follow them, and plan a crop.

Priority order implemented here (exactly as documented in the UI):
    1. active speaker   - face track that also correlates with speech activity
    2. detected face    - largest/most persistent face track
    3. multiple faces   - widen or alternate between the two dominant tracks
    4. main visual subject - saliency/edge-density centroid
    5. centre crop fallback

The output is a keyframe path (time -> crop window) that is smoothed with a dead
zone and a maximum pan speed, so the reframe follows the subject without the
jittery "random camera movement" that naive per-frame cropping produces.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Sequence

from app.core.config import settings
from app.core.errors import MediaError
from app.core.logging import get_logger
from app.services.media import ffmpeg
from app.services.media.vision import FaceBox, FaceDetector, SaliencyAnalyzer, build_detector

log = get_logger(__name__)

ASPECTS: dict[str, float] = {"9:16": 9 / 16, "1:1": 1.0, "16:9": 16 / 9, "4:5": 4 / 5, "3:4": 3 / 4}
DEFAULT_KEYFRAME_LIMIT = 32  # keeps the generated FFmpeg crop expression bounded

# Tracking thresholds
IOU_MATCH = 0.25
MAX_TRACK_AGE = 4  # frames without a match before a track is closed
DEAD_ZONE = 0.18  # fraction of crop width the subject may drift before we pan
MAX_PAN_PX_PER_SECOND_RATIO = 0.55  # of crop width per second
MIN_CROP_SCALE = 0.55  # never zoom in more than this fraction of the source width
HEADROOM = 0.42  # keep the face centre slightly above the middle of the frame


@dataclass
class Track:
    id: int
    boxes: list[tuple[float, FaceBox]] = field(default_factory=list)  # (time, box)

    @property
    def last_box(self) -> FaceBox | None:
        return self.boxes[-1][1] if self.boxes else None

    @property
    def last_time(self) -> float:
        return self.boxes[-1][0] if self.boxes else 0.0

    @property
    def first_time(self) -> float:
        return self.boxes[0][0] if self.boxes else 0.0

    @property
    def hits(self) -> int:
        return len(self.boxes)

    @property
    def score(self) -> float:
        """Persistence x size x confidence: a stable, big, confident face."""
        if not self.boxes:
            return 0.0
        area = max(box.area for _, box in self.boxes)
        return self.hits * (1.0 + math.log1p(area))

    def box_at(self, time: float) -> FaceBox | None:
        if not self.boxes:
            return None
        best = min(self.boxes, key=lambda item: abs(item[0] - time))
        return best[1] if abs(best[0] - time) <= 1.2 else None


@dataclass
class FrameSample:
    time: float
    faces: list[FaceBox]
    subject: tuple[float, float, float]  # x_ratio, y_ratio, confidence
    energy: float = 0.0


# ----------------------------------------------------------------- analysis ---
def analyze_video(
    video_path: Path,
    *,
    duration: float,
    aspect_ratio: str = "9:16",
    sample_fps: float | None = None,
    detector: FaceDetector | None = None,
    energy_curve: tuple[list[float], list[float]] | None = None,
    job_id: str | None = None,
    workdir: Path | None = None,
    progress_cb: Callable[[float, str], None] | None = None,
    max_frames: int | None = None,
    source_width: int | None = None,
    source_height: int | None = None,
) -> dict[str, Any]:
    """Detect, track and plan. Returns a serialisable framing analysis.

    Frames are decoded at a reduced size for speed, so every measured box is
    scaled back into *source video* coordinates before planning. Keyframes are
    therefore always expressed in the same units the renderer crops in.
    """
    if not ffmpeg.have_ffmpeg():
        raise MediaError("FFmpeg is required for framing analysis.", code="ffmpeg_missing")
    import cv2

    detector = detector or build_detector()
    saliency = SaliencyAnalyzer()
    fps = float(sample_fps or settings.media_frame_sample_fps)
    limit = int(max_frames or settings.media_max_analysis_frames)
    total_frames = max(1, min(limit, int(duration * fps)))
    if duration > 0:
        fps = min(fps, total_frames / duration) if total_frames / duration > 0 else fps
    frame_dir = (workdir or video_path.parent) / f"frames_{video_path.stem}"
    frame_dir.mkdir(parents=True, exist_ok=True)

    samples: list[FrameSample] = []
    width = height = 0  # sampled frame size (decoder output, not the video size)
    try:
        batch_seconds = 8.0
        elapsed = 0.0
        while elapsed < duration and len(samples) < total_frames:
            batch_dir = frame_dir / f"b{int(elapsed):06d}"
            batch_start = elapsed
            batch_duration = min(batch_seconds, duration - elapsed)
            frames = ffmpeg.sample_frames(
                video_path,
                batch_dir,
                fps=fps,
                width=480,
                start=batch_start,
                duration=batch_duration,
                job_id=job_id,
            )
            for index, frame_path in enumerate(frames):
                image = cv2.imread(str(frame_path))
                if image is None:
                    continue
                if not width:
                    height, width = image.shape[:2]
                timestamp = batch_start + index / fps
                faces = detector.detect(image)
                subject = saliency.subject_center(image)
                energy = _energy_at(energy_curve, timestamp)
                samples.append(FrameSample(time=timestamp, faces=faces, subject=subject, energy=energy))
                frame_path.unlink(missing_ok=True)
            try:
                batch_dir.rmdir()
            except OSError:
                pass
            elapsed += batch_duration
            if progress_cb:
                progress_cb(min(95.0, elapsed / max(duration, 0.01) * 95.0), "Tracking faces and subjects")
            if len(samples) >= total_frames:
                break
    finally:
        ffmpeg.cleanup_dir(frame_dir)
        detector.close()

    # ---- back into source coordinates -------------------------------------
    scale_x = (source_width or width) / max(1, width)
    scale_y = (source_height or height) / max(1, height)
    if abs(scale_x - 1.0) > 1e-6 or abs(scale_y - 1.0) > 1e-6:
        samples = [_scale_sample(sample, scale_x, scale_y) for sample in samples]
        log.debug("framing.scaled_samples", scale_x=round(scale_x, 4), scale_y=round(scale_y, 4))
    source_width = source_width or width
    source_height = source_height or height

    tracks = build_tracks(samples)
    dominant = select_dominant_track(tracks, samples)
    active_speaker_track = select_active_speaker(tracks, samples)
    focus_track = active_speaker_track or dominant

    keyframes, stats = plan_crop_path(
        samples,
        focus_track=focus_track,
        secondary_track=dominant if focus_track is not active_speaker_track else None,
        aspect_ratio=aspect_ratio,
        source_width=source_width or 1920,
        source_height=source_height or 1080,
        frame_count=fps,
    )

    faces_total = sum(len(s.faces) for s in samples)
    analysis = {
        "aspect_ratio": aspect_ratio,
        "sample_fps": round(fps, 3),
        "frames_analyzed": len(samples),
        "source_width": source_width,
        "source_height": source_height,
        "sampled_width": width,
        "sampled_height": height,
        "detector": detector.name,
        "detector_available": bool(detector.available),
        "faces_detected": faces_total,
        "faces_per_frame": round(faces_total / len(samples), 3) if samples else 0.0,
        "tracks": len(tracks),
        "dominant_track_id": dominant.id if dominant else None,
        "active_speaker_track_id": active_speaker_track.id if active_speaker_track else None,
        "focus_track_id": focus_track.id if focus_track else None,
        "framing_strategy": stats["strategy"],
        "tracking_confidence": stats["confidence"],
        "multi_speaker": bool(stats["strategy"].startswith("multiple")),
        "keyframes": keyframes,
        "notes": stats["notes"],
    }
    log.info(
        "framing.analyzed",
        detector=detector.name,
        frames=len(samples),
        faces=faces_total,
        tracks=len(tracks),
        strategy=stats["strategy"],
    )
    return analysis


def build_tracks(samples: Sequence[FrameSample]) -> list[Track]:
    """Greedy IoU/centroid tracker - adequate for framing, dependency-free."""
    tracks: list[Track] = []
    open_tracks: list[Track] = []
    next_id = 1
    for sample in samples:
        unmatched_open = list(open_tracks)
        for box in sorted(sample.faces, key=lambda b: -b.area):
            best: Track | None = None
            best_score = 0.0
            for track in unmatched_open:
                last = track.last_box
                if last is None:
                    continue
                iou = last.iou(box)
                distance = math.dist(last.center, box.center) / max(1.0, math.hypot(box.w, box.h))
                score = iou * 1.5 + max(0.0, 1.0 - distance)
                if (iou >= IOU_MATCH or distance < 1.2) and score > best_score:
                    best, best_score = track, score
            if best is None:
                track = Track(id=next_id)
                next_id += 1
                tracks.append(track)
                open_tracks.append(track)
                track.boxes.append((sample.time, box))
            else:
                best.boxes.append((sample.time, box))
                unmatched_open = [t for t in unmatched_open if t is not best]
        open_tracks = [t for t in open_tracks if sample.time - t.last_time <= MAX_TRACK_AGE / max(0.1, _sample_interval(samples))]
    return tracks


def _sample_interval(samples: Sequence[FrameSample]) -> float:
    if len(samples) < 2:
        return 0.5
    return max(0.01, (samples[-1].time - samples[0].time) / (len(samples) - 1))


def select_dominant_track(tracks: Sequence[Track], samples: Sequence[FrameSample]) -> Track | None:
    if not tracks:
        return None
    duration = samples[-1].time if samples else 1.0
    return max(tracks, key=lambda t: t.score * (1.0 + (t.hits * _sample_interval(samples)) / max(duration, 0.1)))


def select_active_speaker(tracks: Sequence[Track], samples: Sequence[FrameSample]) -> Track | None:
    """Correlate each track with measured speech energy: the face that is on
    screen and moving while someone is talking is the active speaker."""
    if not tracks:
        return None
    energy_by_time = {round(s.time, 2): s.energy for s in samples}
    if not any(energy_by_time.values()):
        return None

    def speech_score(track: Track) -> float:
        total = 0.0
        for time, box in track.boxes:
            energy = energy_by_time.get(round(time, 2), 0.0)
            total += energy * (1.0 + math.log1p(box.area))
        return total

    ranked = sorted(tracks, key=speech_score, reverse=True)
    best = ranked[0]
    if speech_score(best) <= 0:
        return None
    # Require a meaningful margin over the runner-up, otherwise we are guessing.
    if len(ranked) > 1 and speech_score(best) < speech_score(ranked[1]) * 1.15:
        return best if best.hits >= max(3, ranked[1].hits) else None
    return best


# ----------------------------------------------------------------- crop plan ---
def _scale_sample(sample: FrameSample, scale_x: float, scale_y: float) -> FrameSample:
    """Map a sample measured on a downscaled frame into source pixels."""
    faces = [
        FaceBox(
            x=int(round(face.x * scale_x)),
            y=int(round(face.y * scale_y)),
            w=max(1, int(round(face.w * scale_x))),
            h=max(1, int(round(face.h * scale_y))),
            confidence=face.confidence,
        )
        for face in sample.faces
    ]
    return FrameSample(time=sample.time, faces=faces, subject=sample.subject, energy=sample.energy)


def plan_crop_path(
    samples: Sequence[FrameSample],
    *,
    focus_track: Track | None,
    secondary_track: Track | None,
    aspect_ratio: str,
    source_width: int,
    source_height: int,
    frame_count: float = 2.0,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Convert tracked positions into a smoothed crop window over time."""
    ratio = ASPECTS.get(aspect_ratio, 9 / 16)
    crop_height = source_height
    crop_width = int(round(crop_height * ratio))
    if crop_width > source_width:
        crop_width = source_width
        crop_height = int(round(crop_width / ratio))
    if crop_height > source_height:
        crop_height = source_height
        crop_width = int(round(crop_height * ratio))

    max_width = source_width
    notes: list[str] = []
    raw_path: list[dict[str, Any]] = []
    face_frames = 0
    subject_frames = 0
    for sample in samples:
        box = focus_track.box_at(sample.time) if focus_track else None
        confidence = 0.0
        if box is not None:
            face_frames += 1
            centre_x = box.center[0]
            face_height = box.h
            # Keep the whole head plus some torso: scale the crop so the face is
            # roughly 34% of the frame height, clamped to sane bounds.
            desired_scale = max(MIN_CROP_SCALE, min(1.0, 0.34 * source_height / max(1.0, face_height)))
            desired_width = min(max_width, int(round(source_width * desired_scale)))
            desired_width = max(crop_width, desired_width)
            confidence = 0.9
            centre_y = box.y + box.h * HEADROOM
        else:
            cx_ratio, cy_ratio, saliency_confidence = sample.subject
            if saliency_confidence > 0.05:
                subject_frames += 1
                centre_x = cx_ratio * source_width
                centre_y = cy_ratio * source_height
                desired_width = crop_width
                confidence = 0.5 * saliency_confidence + 0.2
            else:
                centre_x = source_width / 2.0
                centre_y = source_height / 2.0
                desired_width = crop_width
                confidence = 0.15
        desired_width = max(crop_width, min(max_width, desired_width))
        desired_height = int(round(desired_width / ratio)) if ratio > 0 else source_height
        if desired_height > source_height:
            desired_height = source_height
            desired_width = int(round(desired_height * ratio))
        raw_path.append(
            {
                "time": round(sample.time, 3),
                "center_x": centre_x,
                "center_y": centre_y,
                "width": desired_width,
                "height": desired_height,
                "confidence": round(confidence, 3),
                "source": "face" if box is not None else ("subject" if confidence > 0.3 else "center"),
            }
        )

    if not raw_path:
        raw_path = [
            {
                "time": 0.0,
                "center_x": source_width / 2,
                "center_y": source_height / 2,
                "width": crop_width,
                "height": crop_height,
                "confidence": 0.0,
                "source": "center",
            }
        ]

    smoothed = _smooth_path(raw_path, source_width=source_width, source_height=source_height)
    keyframes = _compress_keyframes(smoothed, limit=DEFAULT_KEYFRAME_LIMIT)

    faces_ratio = face_frames / max(1, len(raw_path))
    if focus_track and secondary_track and focus_track.id != secondary_track.id:
        strategy = "multiple_faces" if faces_ratio > 0.35 else "active_speaker"
        if secondary_track.hits >= focus_track.hits * 0.6:
            notes.append("Two persistent faces detected; framing follows the dominant speaker.")
    elif faces_ratio >= 0.5:
        strategy = "active_speaker"
    elif faces_ratio >= 0.15:
        strategy = "detected_face"
    elif subject_frames / max(1, len(raw_path)) >= 0.4:
        strategy = "main_subject"
    else:
        strategy = "center_crop"
        notes.append("No faces or stable subject found; using a centre crop.")

    confidence = round(sum(k["confidence"] for k in smoothed) / max(1, len(smoothed)), 3)
    return keyframes, {
        "strategy": strategy,
        "confidence": confidence,
        "notes": notes,
    }


def _smooth_path(
    path: Sequence[dict[str, Any]], *, source_width: int, source_height: int
) -> list[dict[str, Any]]:
    """Dead-zone + velocity-limited smoothing so pans are deliberate."""
    if not path:
        return []
    interval = max(0.05, path[1]["time"] - path[0]["time"]) if len(path) > 1 else 0.5
    result: list[dict[str, Any]] = []
    current_x = path[0]["center_x"]
    current_width = path[0]["width"]
    max_velocity = MAX_PAN_PX_PER_SECOND_RATIO * current_width * interval
    max_zoom_velocity = current_width * 0.35 * interval
    for point in path:
        target_x = point["center_x"]
        target_width = max(current_width * 0.8, min(current_width * 1.25, point["width"]))
        dead_zone = DEAD_ZONE * current_width
        delta = target_x - current_x
        if abs(delta) > dead_zone:
            desired = delta - math.copysign(dead_zone, delta)
            current_x += max(-max_velocity, min(max_velocity, desired))
        width_delta = target_width - current_width
        current_width += max(-max_zoom_velocity, min(max_zoom_velocity, width_delta))
        half = current_width / 2.0
        center_x = max(half, min(source_width - half, current_x))
        current_x = center_x
        height = min(source_height, int(round(current_width / _ratio_of(point, source_width, source_height))))
        result.append({**point, "center_x": center_x, "width": current_width, "height": height})
    return result


def _ratio_of(point: dict[str, Any], source_width: int, source_height: int) -> float:
    width = max(1.0, float(point.get("width") or source_width))
    height = max(1.0, float(point.get("height") or source_height))
    return width / height


def _compress_keyframes(path: Sequence[dict[str, Any]], *, limit: int) -> list[dict[str, Any]]:
    """Reduce to at most `limit` keyframes, always keeping the endpoints."""
    if len(path) <= limit:
        return [dict(point) for point in path]
    step = len(path) / float(limit)
    selected = [path[min(len(path) - 1, int(round(index * step)))] for index in range(limit)]
    selected[-1] = path[-1]
    return [dict(point) for point in selected]


def _energy_at(energy_curve: tuple[list[float], list[float]] | None, time: float) -> float:
    if not energy_curve:
        return 0.0
    times, values = energy_curve
    if not times:
        return 0.0
    index = min(len(times) - 1, max(0, int(time / max(1e-6, times[1] - times[0])) if len(times) > 1 else 0))
    return float(values[index])


# --------------------------------------------------------------- ffmpeg expr ---
def crop_expression(
    keyframes: Sequence[dict[str, Any]],
    *,
    clip_start: float,
    source_width: int,
    source_height: int,
) -> tuple[int, int, str, str]:
    """Build (width, height, x_expr, y_expr) for FFmpeg's `crop` filter.

    The expressions are piecewise-linear in `t` (clip-relative seconds), so the
    window pans smoothly between keyframes instead of jumping.
    """
    if not keyframes:
        return source_width // 2, source_height, "0", "0"
    kf = [k for k in keyframes if k["time"] >= clip_start - 0.001] or list(keyframes)
    times = [max(0.0, float(k["time"]) - clip_start) for k in kf]
    width = int(round(float(kf[0]["width"])))
    height = int(round(float(kf[0]["height"])))
    xs = [max(0.0, float(k["center_x"]) - width / 2.0) for k in kf]
    ys = [max(0.0, float(k["center_y"]) - height / 2.0) for k in kf]
    # Clamp inside the source frame
    xs = [min(x, max(0.0, source_width - width)) for x in xs]
    ys = [min(y, max(0.0, source_height - height)) for y in ys]

    if all(abs(x - xs[0]) < 1.5 for x in xs):
        return width, height, f"{xs[0]:.1f}", f"{ys[0]:.1f}"

    x_expr = _piecewise(times, xs)
    if all(abs(y - ys[0]) < 1.5 for y in ys):
        y_expr = f"{ys[0]:.1f}"
    else:
        y_expr = _piecewise(times, ys)
    return width, height, x_expr, y_expr


def _piecewise(times: Sequence[float], values: Sequence[float]) -> str:
    """Nested if() chain evaluating a piecewise-linear function of `t`."""
    if len(times) == 1:
        return f"{values[0]:.1f}"
    expression = f"{values[-1]:.1f}"
    for index in range(len(times) - 2, -1, -1):
        t0, t1 = times[index], times[index + 1]
        v0, v1 = values[index], values[index + 1]
        span = max(1e-3, t1 - t0)
        slope = (v1 - v0) / span
        segment = f"({v0:.1f}+({slope:.5f})*(t-{t0:.3f}))"
        expression = f"if(lt(t,{t1:.3f}),{segment},{expression})"
    return expression


def static_crop_for(aspect_ratio: str, source_width: int, source_height: int, focus_x: float = 0.5) -> tuple[int, int, int, int]:
    """Centre (or horizontally offset) crop used when a user overrides framing."""
    ratio = ASPECTS.get(aspect_ratio, 9 / 16)
    crop_height = source_height
    crop_width = int(round(crop_height * ratio))
    if crop_width > source_width:
        crop_width = source_width
        crop_height = int(round(crop_width / ratio))
    x = int(round((source_width - crop_width) * max(0.0, min(1.0, focus_x))))
    y = max(0, (source_height - crop_height) // 2)
    return crop_width, crop_height, x, y


def framing_from_spec(clip_crop: dict[str, Any] | None) -> dict[str, Any]:
    """Normalise a user crop override into a plan the renderer understands."""
    crop = dict(clip_crop or {})
    mode = str(crop.get("mode") or "auto")
    return {
        "mode": mode,  # auto | center | manual | track
        "focus_x": float(crop.get("focus_x", 0.5) or 0.5),
        "focus_y": float(crop.get("focus_y", 0.5) or 0.5),
        "zoom": float(crop.get("zoom", 1.0) or 1.0),
        "keyframes": crop.get("keyframes") or [],
        "strategy": crop.get("strategy") or "",
    }
