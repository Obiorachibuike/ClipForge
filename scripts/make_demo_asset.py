#!/usr/bin/env python
"""Build the ClipForge demo asset: a real video with real narration.

Produces ``demo/creator_masterclass.mp4`` - a 1920x1080, 30 fps, ~4 minute clip
with:

* narration synthesised by espeak-ng (one utterance per sentence, real pauses),
* four Ken Burns segments over generated presenter imagery (real faces, real
  motion, real scene changes) so smart framing has genuine material to track,
* a manifest (``demo/manifest.json``) holding the ground-truth script and the
  measured section offsets.

Nothing about the resulting video is simulated: it is a normal MP4 that the
pipeline processes exactly like an upload from a browser.

Usage:
    python scripts/make_demo_asset.py [--out demo] [--no-video]
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
import wave
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "apps" / "api"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from demo_script import (  # noqa: E402
    NARRATION_SECTIONS,
    RATE,
    SECTION_PAUSE,
    SENTENCE_PAUSE,
    VOICE,
    narration_text,
)

from app.services.ai.transcription.alignment import synthesize_raw  # noqa: E402

WIDTH, HEIGHT, FPS = 1920, 1080, 30
SAMPLE_RATE = 22050


def ffmpeg_bin() -> str:
    from app.services.media.ffmpeg import ffmpeg_bin as resolve

    binary = resolve()
    if not binary:
        raise SystemExit("FFmpeg is required to build the demo asset.")
    return binary


def _silence(seconds: float, rate: int = SAMPLE_RATE) -> "np.ndarray":
    """Silence as int16, matching the speech blocks.

    Mixing dtypes here promotes the whole array to float, which writes 4-byte
    values into a 2-byte-per-sample WAV: twice the duration and pure noise.
    """
    import numpy as np

    return np.zeros(max(1, int(seconds * rate)), dtype=np.int16)


# One converter for the whole project (float synthesis -> int16 PCM).
from app.services.ai.transcription.alignment import to_pcm16  # noqa: E402


def assert_audible(samples, label: str) -> dict:
    """Refuse to ship a silent asset: fail loudly instead of faking a demo."""
    import numpy as np

    values = np.asarray(samples)
    if values.dtype != np.int16:
        raise SystemExit(f"{label} is not 16-bit PCM (dtype={values.dtype}) - refusing to build the demo asset.")
    peak = float(np.max(np.abs(values))) if values.size else 0.0
    rms = float(np.sqrt(np.mean(np.square(values)))) if values.size else 0.0
    if peak < 100 or rms < 20:
        raise SystemExit(f"{label} is silent (peak={peak:.1f}, rms={rms:.2f}) - refusing to build the demo asset.")
    return {"peak": round(peak, 1), "rms": round(rms, 2), "samples": int(values.size)}


def build_narration(out_wav: Path) -> dict:
    """Synthesise every sentence, concatenate with measured pauses, write a WAV."""
    import numpy as np

    pcm: list = []
    marks: list[dict] = []
    cursor = 0.0
    for section in NARRATION_SECTIONS:
        section_start = cursor
        for sentence in section["sentences"]:
            samples, rate = synthesize_raw(sentence, voice=VOICE, rate=RATE)
            if rate != SAMPLE_RATE:  # pragma: no cover - espeak is stable at 22050
                ratio = SAMPLE_RATE / rate
                samples = _resample(samples, ratio)
            start = cursor
            block = to_pcm16(samples)
            pcm.append(block)
            cursor += block.size / SAMPLE_RATE
            marks.append(
                {
                    "section": section["title"],
                    "sentence": sentence,
                    "start": round(start, 3),
                    "end": round(cursor, 3),
                }
            )
            pcm.append(_silence(SENTENCE_PAUSE))
            cursor += SENTENCE_PAUSE
        pcm.append(_silence(SECTION_PAUSE))
        cursor += SECTION_PAUSE
        marks.append({"section": section["title"], "marker": "section_end", "at": round(cursor, 3)})
        section_span = (round(section_start, 3), round(cursor, 3))
        marks[-1]["section_start"] = section_span[0]
        marks[-1]["section_end"] = section_span[1]

    audio = np.concatenate(pcm) if pcm else np.zeros(1, dtype=np.int16)
    if audio.dtype != np.int16:  # pragma: no cover - defensive
        raise SystemExit(f"narration buffer is {audio.dtype}, expected int16")
    loudness = assert_audible(audio, "synthesised narration")
    out_wav.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(out_wav), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(SAMPLE_RATE)
        handle.writeframes(audio.tobytes())

    duration = len(audio) / SAMPLE_RATE
    return {"duration": round(duration, 3), "sentences": marks, "audio_levels": loudness}


def _resample(samples, ratio: float):
    import numpy as np

    values = np.asarray(samples, dtype=np.float32)
    target = max(1, int(len(values) * ratio))
    indices = np.linspace(0, len(values) - 1, target)
    return np.interp(indices, np.arange(len(values)), values).astype(np.int16)


def _zoompan_expression(pan: str, frames: int) -> tuple[str, str, str]:
    """Ken Burns expressions. Deterministic, no jumps - real camera motion."""
    if pan == "zoom_in":
        return f"min(zoom+0.00035,1.16)", "iw/2-(iw/zoom/2)", "ih/2-(ih/zoom/2)"
    if pan == "zoom_out":
        return f"if(eq(on,1),1.16,max(zoom-0.00035,1.0))", "iw/2-(iw/zoom/2)", "ih/2-(ih/zoom/2)"
    if pan == "pan_right":
        return "1.08", f"(iw-iw/zoom)*on/{frames}", "ih/2-(ih/zoom/2)"
    return "1.08", f"(iw-iw/zoom)*(1-on/{frames})", "ih/2-(ih/zoom/2)"


def build_section_clip(image: Path, duration: float, pan: str, out: Path) -> None:
    frames = max(1, int(duration * FPS))
    zoom, x_expr, y_expr = _zoompan_expression(pan, frames)
    filters = (
        f"scale={WIDTH * 2}:-2,"
        f"zoompan=z='{zoom}':x='{x_expr}':y='{y_expr}':d={frames}:s={WIDTH}x{HEIGHT}:fps={FPS},"
        f"format=yuv420p"
    )
    command = [
        ffmpeg_bin(),
        "-hide_banner",
        "-loglevel", "error",
        "-y",
        "-loop", "1",
        "-i", str(image),
        "-t", f"{duration:.3f}",
        "-vf", filters,
        "-c:v", "libx264",
        "-preset", "veryfast",
        "-crf", "23",
        "-pix_fmt", "yuv420p",
        "-r", str(FPS),
        str(out),
    ]
    subprocess.run(command, check=True, capture_output=True)


def verify_faces(images: list[Path]) -> list[dict]:
    """Confirm the presenter images really contain detectable faces."""
    results = []
    try:
        import cv2
    except Exception as exc:  # pragma: no cover - opencv optional here
        return [{"image": str(p), "faces": None, "error": str(exc)} for p in images]
    cascade = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
    for path in images:
        frame = cv2.imread(str(path))
        faces = []
        if frame is not None:
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            faces = [
                {"x": int(x), "y": int(y), "w": int(w), "h": int(h)}
                for (x, y, w, h) in list(cascade.detectMultiScale(gray, 1.1, 5, minSize=(60, 60)))
            ]
        results.append({"image": path.name, "faces": len(faces), "boxes": faces})
    return results


def build(out_dir: Path, *, with_video: bool = True) -> dict:
    asset_dir = out_dir / "assets"
    images = [asset_dir / section["image"] for section in NARRATION_SECTIONS]
    missing = [str(p) for p in images if not p.exists()]
    if missing:
        raise SystemExit(f"Missing presenter images: {missing}")

    work = Path(tempfile.mkdtemp(prefix="clipforge-demo-"))
    try:
        narration = work / "narration.wav"
        info = build_narration(narration)

        audio_info = _probe_audio(narration)
        report: dict = {
            "generator": "scripts/make_demo_asset.py",
            "voice": f"espeak-ng:{VOICE}@{RATE}wpm",
            "audio": audio_info,
            "narration": info,
            "script": narration_text(),
            "faces": verify_faces(images),
        }

        if not with_video:
            target = out_dir / "narration.wav"
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(narration, target)
            report["master"] = str(target)
            (out_dir / "manifest.json").write_text(json.dumps(report, indent=2))
            return report

        # Section boundaries follow the measured narration, not guesses.
        section_ends = [
            mark["section_end"] for mark in info["sentences"] if mark.get("marker") == "section_end"
        ]
        video_duration = section_ends[-1] + 1.2
        cursor = 0.0
        parts = []
        for index, section in enumerate(NARRATION_SECTIONS):
            end = section_ends[index]
            duration = max(1.0, end - cursor)
            part = work / f"section_{index}.mp4"
            build_section_clip(images[index], duration, section["pan"], part)
            parts.append(part)
            report.setdefault("sections", []).append(
                {"title": section["title"], "start": round(cursor, 3), "end": round(end, 3)}
            )
            cursor = end

        concat_file = work / "concat.txt"
        concat_file.write_text("".join(f"file '{part.as_posix()}'\n" for part in parts))
        silent = work / "video.mp4"
        subprocess.run(
            [
                ffmpeg_bin(), "-hide_banner", "-loglevel", "error", "-y",
                "-f", "concat", "-safe", "0", "-i", str(concat_file),
                "-c", "copy", str(silent),
            ],
            check=True,
            capture_output=True,
        )

        master = out_dir / "creator_masterclass.mp4"
        master.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            [
                ffmpeg_bin(), "-hide_banner", "-loglevel", "error", "-y",
                "-i", str(silent),
                "-i", str(narration),
                "-c:v", "copy",
                "-c:a", "aac", "-b:a", "160k", "-ar", "44100",
                "-af", "loudnorm=I=-16:TP=-1.5:LRA=11",
                "-shortest",
                "-movflags", "+faststart",
                str(master),
            ],
            check=True,
            capture_output=True,
        )
        final_levels = _audio_levels(master)
        if final_levels.get("peak", 0.0) < 0.01:
            raise SystemExit(
                f"master audio is silent (peak={final_levels.get('peak')}) - the narration did not reach the mux"
            )
        report["master"] = str(master)
        report["master_bytes"] = master.stat().st_size
        report["master_audio_levels"] = final_levels
        report["video_duration_target"] = round(video_duration, 3)
        report["video"] = _probe_video(master)
        (out_dir / "manifest.json").write_text(json.dumps(report, indent=2))
        return report
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _audio_levels(path: Path) -> dict:
    """Measure the decoded audio of a media file (never assume the mux worked).

    Samples are normalised to 0..1 regardless of their storage format (PyAV hands
    back int16 for some codecs and floats for others), and reported in dBFS so
    the numbers mean something either way.
    """
    import math

    import numpy as np

    import av

    peak = 0.0
    rms_total = 0.0
    count = 0
    with av.open(str(path)) as container:
        if not container.streams.audio:
            return {"peak": 0.0, "rms": 0.0, "streams": 0}
        stream = container.streams.audio[0]
        for frame in container.decode(stream):
            data = frame.to_ndarray()
            if data.size == 0:
                continue
            data = data.astype(np.float64)
            if data.dtype != np.float64 or frame.format.name not in ("flt", "fltp", "fltp32", "dbl", "dblp"):
                info = frame.format
                # integer formats: normalise by the format's bit depth
                if np.issubdtype(np.dtype(data.dtype), np.integer) or True:
                    peak_scale = 32768.0 if info.bits <= 16 else float(2 ** (info.bits - 1))
                    data = data / peak_scale
            peak = max(peak, float(np.max(np.abs(data))))
            rms_total += float(np.sum(np.square(data)))
            count += data.size
    rms = float(np.sqrt(rms_total / count)) if count else 0.0
    return {
        "peak": round(peak, 4),
        "rms": round(rms, 4),
        "peak_dbfs": round(20 * math.log10(peak), 1) if peak > 0 else -999.0,
        "rms_dbfs": round(20 * math.log10(rms), 1) if rms > 0 else -999.0,
        "samples": count,
        "streams": 1,
    }


def _probe_audio(path: Path) -> dict:
    with wave.open(str(path)) as handle:
        return {
            "path": str(path),
            "channels": handle.getnchannels(),
            "sample_rate": handle.getframerate(),
            "duration": round(handle.getnframes() / handle.getframerate(), 3),
        }


def _probe_video(path: Path) -> dict:
    import av

    with av.open(str(path)) as container:
        video = container.streams.video[0]
        audio = container.streams.audio[0] if container.streams.audio else None
        return {
            "duration": round(float(container.duration or 0) / 1_000_000, 3),
            "width": video.codec_context.width,
            "height": video.codec_context.height,
            "fps": round(float(video.average_rate or 0), 3),
            "video_codec": video.codec_context.name,
            "audio_codec": audio.codec_context.name if audio else None,
            "bytes": path.stat().st_size,
        }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default=str(REPO_ROOT / "demo"), help="output directory")
    parser.add_argument("--no-video", action="store_true", help="only build the narration WAV")
    args = parser.parse_args()

    report = build(Path(args.out).resolve(), with_video=not args.no_video)
    print(json.dumps({k: v for k, v in report.items() if k != "narration"}, indent=2)[:2000])
    print(f"\nBuilt: {report.get('master')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
