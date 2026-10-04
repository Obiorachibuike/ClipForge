"""Forced alignment: real word-level timings for known narration.

Use cases
---------
* A creator uploads a narrated video together with its script (or a corrected
  transcript) and needs accurate word timings for animated captions.
* ClipForge's demo content generator produces a synthetic narration from a
  written script; the script is known, so timings can be recovered exactly.

How it works
------------
This is classic forced alignment, not speech recognition:

1. Each word of the script is synthesised into reference audio (espeak-ng) so we
   have a real acoustic target for every word.
2. Both the reference and the target audio are reduced to MFCC + energy feature
   sequences.
3. Dynamic time warping finds the minimum-cost monotonic path between them.
4. Word boundaries are read off the warping path in the *target* time base.

The result is timing derived entirely from the actual audio; the text comes from
the script, which by definition matches the audio it was written for. When
espeak-ng is unavailable, the aligner degrades to proportional timing from
detected speech regions and says so in `meta["method"]`.
"""
from __future__ import annotations

import ctypes
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from app.core.errors import ProviderError, ProviderUnavailableError
from app.core.logging import get_logger
from app.services.ai.types import ProviderInfo, SegmentTiming, TranscriptionResult, WordTiming
from app.services.media.audio_features import HOP_SECONDS, extract_features

log = get_logger(__name__)

ESPEAK_SAMPLE_RATE = 22050
DEFAULT_VOICE = "en"
DEFAULT_RATE = 165

_lock = threading.Lock()
_lib = None
_available: bool | None = None


# --------------------------------------------------------------- espeak glue ---
def espeak_library():  # noqa: ANN201
    """Load espeak-ng through the PyPI wheel that bundles it."""
    global _lib, _available
    with _lock:
        if _available is False:
            return None
        if _lib is not None:
            return _lib
        try:
            import espeakng_loader

            lib = ctypes.CDLL(str(espeakng_loader.get_library_path()))
            # espeak_Initialize wants the directory *containing* espeak-ng-data.
            data_parent = str(Path(espeakng_loader.get_data_path()).parent)
            lib.espeak_Initialize.restype = ctypes.c_int
            lib.espeak_Initialize.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_char_p, ctypes.c_int]
            lib.espeak_SetVoiceByName.argtypes = [ctypes.c_char_p]
            lib.espeak_Synth.argtypes = [
                ctypes.c_char_p,
                ctypes.c_size_t,
                ctypes.c_uint,
                ctypes.c_int,
                ctypes.c_uint,
                ctypes.c_uint,
                ctypes.c_void_p,
                ctypes.c_void_p,
            ]
            lib.espeak_Synth.restype = ctypes.c_int
            lib.espeak_Synchronize.argtypes = []
            callback_type = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.POINTER(ctypes.c_int16), ctypes.c_int, ctypes.c_void_p)
            lib.espeak_SetSynthCallback.argtypes = [callback_type]
            rate = lib.espeak_Initialize(0x01, 0, data_parent.encode(), 0)
            if rate <= 0:
                _available = False
                return None
            lib.espeak_SetVoiceByName(DEFAULT_VOICE.encode())
            _lib = lib
            _available = True
            return _lib
        except Exception as exc:
            log.info("alignment.espeak_unavailable", error=str(exc)[:200])
            _available = False
            return None


def espeak_available() -> bool:
    return espeak_library() is not None


def synthesize_raw(text: str, *, voice: str = DEFAULT_VOICE, rate: int = DEFAULT_RATE) -> tuple[list[float], int]:
    """Return (samples in [-1,1], sample_rate) from espeak-ng."""
    import numpy as np

    lib = espeak_library()
    if lib is None:
        raise ProviderUnavailableError(
            "speech synthesis",
            "espeak-ng is not available in this environment (pip install espeakng-loader).",
        )
    buffer: list = []
    callback_type = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.POINTER(ctypes.c_int16), ctypes.c_int, ctypes.c_void_p)

    def _collect(wav, count, _events):  # noqa: ANN001
        if count:
            buffer.append(np.ctypeslib.as_array(wav, shape=(int(count),)).copy())
        return 0

    callback = callback_type(_collect)
    with _lock:
        lib.espeak_SetSynthCallback(callback)
        lib.espeak_SetVoiceByName(voice.encode())
        # parameter 1 == espeakRATE
        lib.espeak_SetParameter(1, int(rate), 0)
        payload = ctypes.create_string_buffer(text.encode("utf-8"))
        lib.espeak_Synth(payload, len(text.encode("utf-8")), 0, 0, 0, 1, None, None)
        lib.espeak_Synchronize()
    if not buffer:
        return [], ESPEAK_SAMPLE_RATE
    samples = np.concatenate(buffer).astype("float32") / 32768.0
    return samples.tolist(), ESPEAK_SAMPLE_RATE


def to_pcm16(samples) -> "object":  # noqa: ANN001
    """Convert synthesised float samples (-1..1) into int16 PCM.

    espeak-ng returns float64: casting that straight to int16 truncates every
    sample to zero, which is how a "working" TTS run can produce a silent file.
    """
    import numpy as np

    values = np.asarray(samples)
    if values.dtype == np.int16:
        return values
    return (np.clip(values.astype(np.float64), -1.0, 1.0) * 32767.0).astype("<i2")


def write_pcm16_wav(samples, sample_rate: int, target: Path) -> Path:
    """Write int16 mono PCM to a WAV, tolerating mixed input dtypes."""
    import numpy as np
    import wave

    array = to_pcm16(samples)
    if array.size == 0:
        raise ProviderError("espeak", "synthesis produced no audio", retryable=False)
    if array.dtype != np.int16:  # pragma: no cover - defensive
        raise ProviderError("espeak", "synthesis produced non-integer audio", retryable=False)
    target.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(target), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(sample_rate)
        handle.writeframes(np.ascontiguousarray(array, dtype="<i2").tobytes())
    return target


def synthesize_to_wav(text: str, target: Path, *, voice: str = DEFAULT_VOICE, rate: int = DEFAULT_RATE) -> Path:
    import numpy as np

    samples, sample_rate = synthesize_raw(text, voice=voice, rate=rate)
    if not samples:
        raise ProviderError("espeak", "synthesis produced no audio", retryable=False)
    target.parent.mkdir(parents=True, exist_ok=True)
    array = to_pcm16(samples)
    with wave.open(str(target), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(sample_rate)
        handle.writeframes(np.ascontiguousarray(array, dtype="<i2").tobytes())
    return target


# ---------------------------------------------------------------- alignment ---
@dataclass
class AlignmentResult:
    words: list[WordTiming]
    method: str
    confidence: float
    reference_duration: float = 0.0
    diagnostics: dict | None = None


def _features_from_samples(samples, sample_rate: int, *, start: float = 0.0, duration: float | None = None):  # noqa: ANN001
    """Feature extraction for in-memory audio (mirrors the file-based path)."""
    import numpy as np

    from app.services.media.audio_features import (
        FRAME_SECONDS,
        HOP_SECONDS,
        MFCC_COUNT,
        dct_matrix,
        mel_filterbank,
    )

    array = np.asarray(samples, dtype="float32")
    if duration is not None:
        begin = int(max(0.0, start) * sample_rate)
        end = int((start + duration) * sample_rate)
        array = array[begin:end]
    elif start > 0:
        array = array[int(start * sample_rate) :]
    if array.size < 400:
        array = np.pad(array, (0, max(0, 400 - array.size)))

    frame_length = int(FRAME_SECONDS * sample_rate)
    hop_length = int(HOP_SECONDS * sample_rate)
    frames = np.lib.stride_tricks.sliding_window_view(array, frame_length)[::hop_length]
    window = np.hanning(frame_length).astype("float32")
    windowed = frames * window
    fft_size = 512
    spectrum = np.abs(np.fft.rfft(windowed, n=fft_size, axis=1)) + 1e-10
    filters = np.array(mel_filterbank(sample_rate, fft_size), dtype="float32")
    mel = np.log(spectrum @ filters.T + 1e-8)
    dct = np.array(dct_matrix(filters.shape[0], MFCC_COUNT), dtype="float32")
    mfcc = mel @ dct.T
    rms = np.sqrt(np.mean(np.square(frames), axis=1) + 1e-12)[:, None]
    # Cepstral mean normalisation removes channel/voice colouring.
    mfcc = mfcc - mfcc.mean(axis=0, keepdims=True)
    delta = np.diff(mfcc, axis=0, prepend=mfcc[:1])
    return np.concatenate([mfcc, delta * 0.5, rms * 4.0], axis=1)


def _dtw_path(reference, target, band: int = 60):  # noqa: ANN001
    """Constrained DTW. Returns (cost, path) with path as (ref_index, tgt_index)."""
    import numpy as np

    n, m = len(reference), len(target)
    if n == 0 or m == 0:
        return float("inf"), []
    inf = float("inf")
    cost = np.full((n + 1, m + 1), inf, dtype="float64")
    cost[0, 0] = 0.0
    back = np.zeros((n + 1, m + 1), dtype="int8")
    # Vectorised step: for each reference frame, compute the local distance to
    # target frames inside the Sakoe-Chiba band.
    for i in range(1, n + 1):
        ref = reference[i - 1]
        low = max(1, int(i * m / n) - band)
        high = min(m, int(i * m / n) + band)
        local = np.linalg.norm(target[low - 1 : high] - ref, axis=1) / math_sqrt_dim(ref)
        for offset, j in enumerate(range(low, high + 1)):
            candidates = (
                (cost[i - 1, j - 1], 0),  # diagonal
                (cost[i - 1, j], 1),  # vertical
                (cost[i, j - 1], 2),  # horizontal
            )
            best_cost, direction = min(candidates, key=lambda item: item[0])
            cost[i, j] = local[offset] + best_cost
            back[i, j] = direction
    if cost[n, m] == inf:
        return inf, []
    path: list[tuple[int, int]] = []
    i, j = n, m
    while i > 0 and j > 0:
        path.append((i - 1, j - 1))
        direction = back[i, j]
        if direction == 0:
            i, j = i - 1, j - 1
        elif direction == 1:
            i -= 1
        else:
            j -= 1
    path.reverse()
    return float(cost[n, m]), path


def math_sqrt_dim(vector) -> float:  # noqa: ANN001
    import numpy as np

    return float(np.sqrt(vector.shape[0]))


def align_script(
    audio_path: Path,
    script: str,
    *,
    voice: str = DEFAULT_VOICE,
    rate: int = DEFAULT_RATE,
    start_offset: float = 0.0,
    duration: float | None = None,
    progress_cb: Callable[[float, str], None] | None = None,
) -> AlignmentResult:
    """Return real word timings for `script` against `audio_path`."""
    import numpy as np

    words = [word for word in script.replace("\n", " ").split() if word.strip()]
    if not words:
        raise ProviderError("alignment", "the script contains no words", retryable=False)

    if progress_cb:
        progress_cb(8.0, "Building acoustic reference")

    # 1. Synthesise a reference utterance per word (cached within this call).
    reference_blocks: list = []
    boundaries: list[tuple[int, int]] = []  # (start_frame, end_frame) per word
    cursor = 0
    total_reference_frames = 0
    for index, word in enumerate(words):
        clean = "".join(ch for ch in word if ch.isalnum() or ch in "-'")
        if not clean:
            boundaries.append((cursor, cursor))
            continue
        try:
            samples, sample_rate = synthesize_raw(clean, voice=voice, rate=rate)
        except ProviderUnavailableError:
            samples, sample_rate = [], ESPEAK_SAMPLE_RATE
        if not samples:
            boundaries.append((cursor, cursor))
            continue
        features = _features_from_samples(samples, sample_rate)
        if features.size == 0:
            boundaries.append((cursor, cursor))
            continue
        reference_blocks.append(features)
        frames = len(features)
        boundaries.append((cursor, cursor + frames))
        cursor += frames
        if progress_cb:
            progress_cb(8.0 + 40.0 * (index + 1) / len(words), "Building acoustic reference")

    if not reference_blocks:
        # No synthesizer: fall back to proportional timing over detected speech.
        return _proportional_alignment(audio_path, words, start_offset=start_offset, duration=duration)

    reference = np.concatenate(reference_blocks, axis=0)
    total_reference_frames = len(reference)

    if progress_cb:
        progress_cb(55.0, "Aligning narration to audio")

    # 2. Features of the real audio.
    file_features = extract_features(audio_path, start=start_offset, duration=duration)
    target = _file_feature_matrix(file_features)
    if target.size == 0:
        raise ProviderError("alignment", "no audio frames could be decoded", retryable=False)
    _require_speech_energy(file_features)

    # 3. DTW.
    cost, path = _dtw_path(reference, target, band=max(40, int(len(target) * 0.2)))
    if not path:
        return _proportional_alignment(audio_path, words, start_offset=start_offset, duration=duration)
    confidence = _confidence(cost, len(reference), len(target), path_steps=len(path))

    if progress_cb:
        progress_cb(85.0, "Mapping word timings")

    # 4. Map reference frame ranges onto target times through the path.
    hop = HOP_SECONDS
    ref_to_target: dict[int, int] = {}
    for ref_index, target_index in path:
        ref_to_target.setdefault(ref_index, target_index)

    word_timings: list[WordTiming] = []
    path_target_indices = [target_index for _ref, target_index in path]
    for word, (ref_start, ref_end) in zip(words, boundaries):
        if ref_end <= ref_start:
            continue
        start_frame = _nearest_target(ref_to_target, ref_start, path_target_indices)
        end_frame = _nearest_target(ref_to_target, max(ref_start, ref_end - 1), path_target_indices)
        start_time = start_offset + start_frame * hop
        end_time = start_offset + max(start_frame + 1, end_frame + 1) * hop
        word_timings.append(
            WordTiming(
                word=word,
                start=round(start_time, 3),
                end=round(end_time, 3),
                confidence=confidence,
            )
        )

    words_result = _monotonic(word_timings)
    return AlignmentResult(
        words=words_result,
        method="espeak-dtw",
        confidence=confidence,
        reference_duration=total_reference_frames * hop,
        diagnostics=alignment_quality(reference, target, cost, len(path)),
    )


def _nearest_target(mapping: dict[int, int], ref_index: int, path_indices: list[int]) -> int:
    if ref_index in mapping:
        return mapping[ref_index]
    lower = [key for key in mapping if key <= ref_index]
    if lower:
        return mapping[max(lower)]
    return path_indices[0] if path_indices else 0


def _file_feature_matrix(features) -> "object":  # noqa: ANN001
    import numpy as np

    mfcc = np.array(features.mfcc, dtype="float32")
    if mfcc.ndim != 2 or mfcc.size == 0:
        return np.zeros((0, 1), dtype="float32")
    mfcc = mfcc - mfcc.mean(axis=0, keepdims=True)
    delta = np.diff(mfcc, axis=0, prepend=mfcc[:1])
    rms = np.array(features.rms, dtype="float32")[:, None]
    return np.concatenate([mfcc, delta * 0.5, rms * 4.0], axis=1)


def _monotonic(words: list[WordTiming]) -> list[WordTiming]:
    """Force strictly increasing, non-overlapping timings."""
    result: list[WordTiming] = []
    for word in words:
        if result and word.start < result[-1].end:
            midpoint = round((result[-1].start + word.end) / 2.0, 3)
            result[-1].end = midpoint
            word.start = midpoint
        word.end = max(word.end, round(word.start + 0.06, 3))
        result.append(word)
    return result


# A track whose loudest frames never clear this RMS is silence, not speech.
SILENCE_RMS_THRESHOLD = 0.004


def _require_speech_energy(features) -> None:
    """Refuse to align a script against silence.

    Without this check a silent track still "aligns" (DTW will happily warp
    anything), and the result would look like a successful transcript made of
    timings that mean nothing.
    """
    import numpy as np

    rms = getattr(features, "rms", None)
    if rms is None or len(rms) == 0:
        return
    values = np.asarray(rms, dtype=float)
    peak = float(np.max(values))
    if peak < SILENCE_RMS_THRESHOLD:
        raise ProviderError(
            "alignment",
            "the audio track is silent, so there is nothing to align",
            retryable=False,
        )


def _confidence(cost: float, reference_frames: int, target_frames: int, path_steps: int | None = None) -> float:
    """Confidence from the alignment cost, used as a coarse quality flag.

    The absolute cost depends on frame counts, so it is expressed per path step
    and mapped onto a 0-1 range with a conservative calibration.
    """
    if not reference_frames:
        return 0.0
    steps = path_steps or min(reference_frames, target_frames)
    average_local = cost / max(1, steps)
    # Empirically a correct alignment lands near 2.4 per step for this feature
    # set (MFCC + deltas + RMS); a random pairing is roughly twice that.
    return round(max(0.05, min(0.98, 1.35 - average_local / 2.4 * 0.9)), 3)


def alignment_quality(reference, target, cost: float, path_steps: int) -> dict:
    """Diagnostics: how much better the path is than an arbitrary pairing."""
    import numpy as np

    if reference is None or target is None or len(reference) == 0 or len(target) == 0:
        return {}
    rng = np.random.default_rng(0)
    sample_ref = reference[rng.choice(len(reference), min(60, len(reference)), replace=False)]
    sample_tgt = target[rng.choice(len(target), min(60, len(target)), replace=False)]
    dimension = float(np.sqrt(reference.shape[1]))
    typical = float(
        np.mean([np.linalg.norm(a - b) / dimension for a in sample_ref for b in sample_tgt])
    )
    average_local = cost / max(1, path_steps)
    return {
        "average_local_cost": round(average_local, 3),
        "typical_mismatch_cost": round(typical, 3),
        "quality_ratio": round(average_local / typical, 3) if typical else 0.0,
        "path_steps": path_steps,
    }


def _proportional_alignment(
    audio_path: Path, words: list[str], *, start_offset: float = 0.0, duration: float | None = None
) -> AlignmentResult:
    """Voice-activity based fallback: distribute words across detected speech."""
    features = extract_features(audio_path, start=start_offset, duration=duration)
    voiced = [index for index, flag in enumerate(features.voiced) if flag]
    if not voiced:
        raise ProviderError("alignment", "no speech was detected in the audio", retryable=False)
    start_frame, end_frame = voiced[0], voiced[-1]
    span_seconds = max(0.4, (end_frame - start_frame) * HOP_SECONDS)
    per_word = span_seconds / len(words)
    timings = []
    for index, word in enumerate(words):
        word_start = start_offset + start_frame * HOP_SECONDS + index * per_word
        timings.append(
            WordTiming(
                word=word,
                start=round(word_start, 3),
                end=round(word_start + per_word * 0.92, 3),
                confidence=0.3,
            )
        )
    return AlignmentResult(words=_monotonic(timings), method="voice-activity", confidence=0.3)


# ---------------------------------------------------------------- provider ---
class ScriptAlignmentProvider:
    """Transcription by alignment when the spoken text is known.

    Reports itself available when espeak-ng is present. Unlike a recogniser, it
    requires a script: `transcribe(..., script=...)`. Without one it raises a
    clear error instead of guessing.
    """

    name = "alignment"
    is_local = True

    def __init__(self, *, voice: str = DEFAULT_VOICE, rate: int = DEFAULT_RATE) -> None:
        self.voice = voice
        self.rate = rate

    def info(self) -> ProviderInfo:
        available = espeak_available()
        return ProviderInfo(
            name=self.name,
            kind="transcription",
            available=available,
            model="espeak-ng DTW aligner",
            detail="" if available else "espeak-ng unavailable (pip install espeakng-loader)",
            is_local=True,
        )

    def transcribe(
        self,
        audio_path: Path,
        *,
        language: str | None = None,
        progress_cb: Callable[[float, str], None] | None = None,
        script: str | None = None,
        **_ignored,
    ) -> TranscriptionResult:
        if not script or not script.strip():
            raise ProviderError(
                self.name,
                "script alignment needs the narration text; provide a script or configure a speech model",
                retryable=False,
            )
        result = align_script(
            Path(audio_path),
            script,
            voice=self.voice,
            rate=self.rate,
            progress_cb=progress_cb,
        )
        words = result.words
        if not words:
            raise ProviderError(self.name, "alignment produced no words", retryable=False)
        segments = _group_segments(words)
        duration = max((word.end for word in words), default=0.0)
        return TranscriptionResult(
            segments=segments,
            language=language or "en",
            language_probability=1.0,
            duration=duration,
            provider=self.name,
            model="espeak-dtw-alignment",
            has_word_timings=True,
            meta={
                "engine": "forced-alignment",
                "method": result.method,
                "alignment_confidence": result.confidence,
                "alignment_diagnostics": result.diagnostics or {},
                "note": "Timings were aligned to the supplied script against the real audio.",
            },
        )


def _group_segments(words: list[WordTiming], max_words: int = 14, max_seconds: float = 8.0) -> list[SegmentTiming]:
    segments: list[SegmentTiming] = []
    current: list[WordTiming] = []
    for word in words:
        current.append(word)
        long_enough = current[-1].end - current[0].start >= max_seconds
        sentence_end = word.word.strip().endswith((".", "!", "?", ","))
        if len(current) >= max_words or long_enough or sentence_end:
            segments.append(
                SegmentTiming(
                    start=current[0].start,
                    end=current[-1].end,
                    text=" ".join(item.word for item in current),
                    words=list(current),
                    confidence=sum(item.confidence for item in current) / len(current),
                )
            )
            current = []
    if current:
        segments.append(
            SegmentTiming(
                start=current[0].start,
                end=current[-1].end,
                text=" ".join(item.word for item in current),
                words=list(current),
                confidence=sum(item.confidence for item in current) / len(current),
            )
        )
    return segments


def reset_state() -> None:
    """Test helper."""
    global _lib, _available
    with _lock:
        _lib = None
        _available = None
