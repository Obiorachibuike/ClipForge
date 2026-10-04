"""Acoustic feature extraction and lightweight speaker separation.

Everything here is computed from decoded PCM with numpy: mel-filterbank
energies, MFCCs, spectral centroid/rolloff, RMS and zero-crossing rate. Speaker
turns come from k-means clustering over those features, which is a genuine (if
simple) diarisation method - the UI labels it as "acoustic clustering" and it is
only used when a provider has not already supplied speaker labels.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

from app.core.logging import get_logger
from app.services.media import ffmpeg

log = get_logger(__name__)

SAMPLE_RATE = 16000
FRAME_SECONDS = 0.025
HOP_SECONDS = 0.010
MEL_BANDS = 26
MFCC_COUNT = 13


# A single voice clustered into k-means groups is not two speakers: claiming
# multiple speakers requires a real silhouette and a real share per cluster.
MIN_SILHOUETTE = 0.42
MIN_CLUSTER_SHARE = 0.18


@dataclass
class FrameFeatures:
    times: list[float]
    rms: list[float]
    zcr: list[float]
    centroid: list[float]
    rolloff: list[float]
    mfcc: list[list[float]]
    voiced: list[bool]


def mel_filterbank(sample_rate: int, fft_size: int, bands: int = MEL_BANDS) -> list[list[float]]:
    def hz_to_mel(hz: float) -> float:
        return 2595.0 * math.log10(1.0 + hz / 700.0)

    def mel_to_hz(mel: float) -> float:
        return 700.0 * (10 ** (mel / 2595.0) - 1.0)

    low, high = hz_to_mel(50.0), hz_to_mel(sample_rate / 2.0)
    points = [mel_to_hz(low + (high - low) * index / (bands + 1)) for index in range(bands + 2)]
    bins = [int(math.floor(point / (sample_rate / 2.0) * (fft_size / 2 + 1))) for point in points]
    filters: list[list[float]] = []
    for index in range(1, bands + 1):
        left, center, right = bins[index - 1], bins[index], bins[index + 1]
        if center <= left or right <= center:
            filters.append([0.0] * (fft_size // 2 + 1))
            continue
        row = [0.0] * (fft_size // 2 + 1)
        for k in range(left, center):
            if 0 <= k < len(row):
                row[k] = (k - left) / (center - left)
        for k in range(center, right):
            if 0 <= k < len(row):
                row[k] = (right - k) / (right - center)
        filters.append(row)
    return filters


def dct_matrix(size: int, coefficients: int) -> list[list[float]]:
    rows: list[list[float]] = []
    for i in range(coefficients):
        row = [math.cos(math.pi * i * (2 * j + 1) / (2 * size)) for j in range(size)]
        norm = math.sqrt(2.0 / size) if i > 0 else math.sqrt(1.0 / size)
        rows.append([value * norm for value in row])
    return rows


def extract_features(
    audio_source: Path,
    *,
    start: float | None = None,
    duration: float | None = None,
    job_id: str | None = None,
    max_seconds: float | None = None,
) -> FrameFeatures:
    """Compute frame-level acoustic features from a real audio stream."""
    import numpy as np

    samples = ffmpeg.decode_pcm(
        audio_source,
        sample_rate=SAMPLE_RATE,
        channels=1,
        start=start,
        duration=duration,
        job_id=job_id,
    )
    if samples.size == 0:
        return FrameFeatures([], [], [], [], [], [], [])

    if max_seconds:
        samples = samples[: int(max_seconds * SAMPLE_RATE)]

    frame_length = int(FRAME_SECONDS * SAMPLE_RATE)
    hop_length = int(HOP_SECONDS * SAMPLE_RATE)
    frame_count = max(1, 1 + (samples.size - frame_length) // hop_length)
    if frame_count < 1 or samples.size < frame_length:
        padded = np.zeros(frame_length, dtype="float32")
        padded[: samples.size] = samples
        samples = padded
        frame_count = 1

    # Build the frame matrix once (stride trick keeps this fast for long audio).
    itemsize = samples.itemsize
    frames = np.lib.stride_tricks.sliding_window_view(samples, frame_length)[::hop_length]
    if frames.shape[0] > frame_count:
        frames = frames[:frame_count]
    window = np.hanning(frame_length).astype("float32")
    windowed = frames * window

    rms = np.sqrt(np.mean(np.square(frames), axis=1) + 1e-12)
    zcr = np.mean(np.abs(np.diff(np.sign(frames), axis=1)) > 0, axis=1)

    fft_size = 512
    spectrum = np.abs(np.fft.rfft(windowed, n=fft_size, axis=1)) + 1e-10
    freqs = np.fft.rfftfreq(fft_size, 1.0 / SAMPLE_RATE)
    magnitude_sum = spectrum.sum(axis=1) + 1e-10
    centroid = (spectrum * freqs).sum(axis=1) / magnitude_sum
    cumulative = np.cumsum(spectrum, axis=1)
    rolloff_index = np.argmax(cumulative >= (cumulative[:, -1:] * 0.85), axis=1)
    rolloff = freqs[rolloff_index]

    filters = np.array(mel_filterbank(SAMPLE_RATE, fft_size, MEL_BANDS), dtype="float32")
    mel_energies = np.log(spectrum @ filters.T + 1e-8)
    dct = np.array(dct_matrix(MEL_BANDS, MFCC_COUNT), dtype="float32")
    mfcc = mel_energies @ dct.T

    noise_floor = float(np.percentile(rms, 35)) if rms.size else 0.0
    threshold = max(0.004, noise_floor * 1.9)
    voiced = rms > threshold

    times = (np.arange(len(rms)) * HOP_SECONDS).tolist()
    return FrameFeatures(
        times=[round(float(t), 3) for t in times],
        rms=[round(float(v), 5) for v in rms],
        zcr=[round(float(v), 5) for v in zcr],
        centroid=[round(float(v), 1) for v in centroid],
        rolloff=[round(float(v), 1) for v in rolloff],
        mfcc=[[round(float(v), 3) for v in row] for row in mfcc],
        voiced=[bool(v) for v in voiced],
    )


# ------------------------------------------------------------ diarisation ---
@dataclass
class SpeakerTurn:
    start: float
    end: float
    speaker: str


def cluster_speakers(
    features: FrameFeatures,
    *,
    max_speakers: int = 3,
    min_turn_seconds: float = 1.2,
) -> tuple[list[SpeakerTurn], int, dict[str, Any]]:
    """K-means over voiced frames -> speaker turns.

    Returns (turns, speaker_count, diagnostics). When the audio does not
    separate cleanly we return a single speaker rather than inventing turns.
    """
    import numpy as np

    if not features.times or not features.mfcc:
        return [], 0, {"method": "none", "reason": "no audio frames"}

    matrix = np.array(features.mfcc, dtype="float32")
    voiced = np.array(features.voiced, dtype=bool)
    if voiced.sum() < 20:
        return [], 0, {"method": "none", "reason": "too little voiced audio"}

    # Normalise features (mean/variance per MFCC dimension, skipping c0 energy).
    usable = matrix[:, 1:]
    mean = usable[voiced].mean(axis=0)
    std = usable[voiced].std(axis=0) + 1e-6
    normalised = (usable - mean) / std
    data = normalised[voiced]

    best: dict[str, Any] | None = None
    for k in range(2, max_speakers + 1):
        labels, centers, inertia = _kmeans(data, k)
        separation = _cluster_separation(centers)
        share = min(float(np.mean(labels == index)) for index in range(k))
        silhouette = _silhouette(data, labels)
        score = silhouette + min(separation, 3.0) * 0.05
        entry = {
            "k": k,
            "labels": labels,
            "centers": centers,
            "inertia": inertia,
            "score": score,
            "separation": separation,
            "silhouette": silhouette,
            "smallest_share": share,
        }
        if best is None or score > best["score"]:
            best = entry

    if best is None:
        return [], 0, {"method": "none", "reason": "clustering failed"}

    # Claiming multiple speakers must be earned. A single voice (or a voice with
    # one delivery style) clusters into sub-groups that are not speakers, so we
    # require a real silhouette - and a real share of the audio - per cluster.
    credible = (
        best["k"] > 1
        and best["silhouette"] >= MIN_SILHOUETTE
        and best["smallest_share"] >= MIN_CLUSTER_SHARE
        and best["separation"] >= 0.75
    )
    diagnostics_extra = {
        "silhouette": round(float(best["silhouette"]), 3),
        "smallest_share": round(float(best["smallest_share"]), 3),
        "thresholds": {"silhouette": MIN_SILHOUETTE, "share": MIN_CLUSTER_SHARE, "separation": 0.75},
    }
    if not credible:
        speaker_count = 1
        frame_labels = np.zeros(len(features.times), dtype=int)
        diagnostics_extra["note"] = (
            "Audio did not separate into distinct voices; reporting a single speaker "
            "rather than inventing turns."
        )
    else:
        speaker_count = int(best["k"])
        frame_labels = np.full(len(features.times), -1, dtype=int)
        frame_labels[voiced] = best["labels"]

    turns = _labels_to_turns(features.times, frame_labels, min_turn_seconds=min_turn_seconds)
    diagnostics = {
        "method": "acoustic-clustering",
        "features": "mfcc13+rms+zcr",
        "requested_max_speakers": max_speakers,
        "speakers": speaker_count,
        "separation": round(float(best["separation"]), 3),
        "voiced_frames": int(voiced.sum()),
        "total_frames": len(features.times),
        **diagnostics_extra,
    }
    return turns, speaker_count, diagnostics


def _kmeans(data, k: int, iterations: int = 40, seed: int = 42):  # noqa: ANN001, ANN202
    import numpy as np

    rng = np.random.default_rng(seed)
    # k-means++ initialisation
    centers = [data[rng.integers(0, len(data))]]
    for _ in range(k - 1):
        distances = np.min(
            np.stack([np.sum((data - center) ** 2, axis=1) for center in centers], axis=1), axis=1
        )
        total = distances.sum()
        probabilities = distances / total if total > 0 else np.ones(len(data)) / len(data)
        centers.append(data[rng.choice(len(data), p=probabilities)])
    centers_array = np.array(centers, dtype="float32")

    labels = np.zeros(len(data), dtype=int)
    for _ in range(iterations):
        distances = np.stack([np.sum((data - center) ** 2, axis=1) for center in centers_array], axis=1)
        new_labels = np.argmin(distances, axis=1)
        if np.array_equal(new_labels, labels):
            break
        labels = new_labels
        for index in range(k):
            members = data[labels == index]
            if len(members):
                centers_array[index] = members.mean(axis=0)
    inertia = float(
        sum(
            np.sum((data[labels == index] - centers_array[index]) ** 2)
            for index in range(k)
            if (labels == index).any()
        )
    )
    return labels, centers_array, inertia


def _silhouette(data, labels, *, sample_size: int = 1500, seed: int = 7) -> float:  # noqa: ANN001
    """Mean silhouette coefficient, computed on a subsample (O(n^2) otherwise).

    This is what separates "two voices" from "one voice that varied".
    """
    import numpy as np

    if len(data) < 2 * len(set(labels.tolist())):
        return 0.0
    rng = np.random.default_rng(seed)
    count = min(sample_size, len(data))
    index = rng.choice(len(data), count, replace=False)
    sample = data[index]
    sample_labels = labels[index]
    distances = np.linalg.norm(sample[:, None, :] - sample[None, :, :], axis=2)
    scores = np.zeros(count, dtype=float)
    for position in range(count):
        own = sample_labels == sample_labels[position]
        own[position] = False
        if not own.any():
            continue
        a = float(distances[position, own].mean())
        b = float("inf")
        for other in set(sample_labels.tolist()):
            if other == sample_labels[position]:
                continue
            mask = sample_labels == other
            if mask.any():
                b = min(b, float(distances[position, mask].mean()))
        if b == float("inf"):
            continue
        scores[position] = (b - a) / max(a, b) if max(a, b) > 0 else 0.0
    return float(scores.mean())


def _cluster_separation(centers) -> float:  # noqa: ANN001
    import numpy as np

    if len(centers) < 2:
        return 10.0
    minimum = float("inf")
    for i in range(len(centers)):
        for j in range(i + 1, len(centers)):
            distance = float(np.linalg.norm(centers[i] - centers[j]))
            minimum = min(minimum, distance)
    return minimum if minimum != float("inf") else 0.0


def _labels_to_turns(times: Sequence[float], labels, *, min_turn_seconds: float) -> list[SpeakerTurn]:  # noqa: ANN001
    turns: list[SpeakerTurn] = []
    if not len(times) or not len(labels):
        return turns
    current = int(labels[0])
    start = float(times[0])
    for index in range(1, len(times)):
        label = int(labels[index])
        if label != current:
            end = float(times[index])
            if end - start >= min_turn_seconds:
                turns.append(SpeakerTurn(start=start, end=end, speaker=_name(current)))
            elif turns:
                turns[-1] = SpeakerTurn(start=turns[-1].start, end=end, speaker=turns[-1].speaker)
            start = float(times[index])
            current = label
    # Merge consecutive turns with the same speaker
    merged: list[SpeakerTurn] = []
    for turn in turns:
        if merged and merged[-1].speaker == turn.speaker:
            merged[-1] = SpeakerTurn(start=merged[-1].start, end=turn.end, speaker=turn.speaker)
        else:
            merged.append(turn)
    return merged


def _name(label: int) -> str:
    if label < 0:
        return ""
    return f"Speaker {label + 1}"


def speaker_at(turns: Sequence[SpeakerTurn], time: float) -> str:
    for turn in turns:
        if turn.start <= time <= turn.end:
            return turn.speaker
    return ""


def assign_speakers(words: Sequence[Any], turns: Sequence[SpeakerTurn]) -> None:
    """Attach a speaker label to each word (based on its midpoint)."""
    if not turns:
        for word in words:
            if not getattr(word, "speaker", ""):
                word.speaker = "Speaker 1"
        return
    for word in words:
        midpoint = (word.start + word.end) / 2.0
        label = speaker_at(turns, midpoint)
        word.speaker = label or (getattr(word, "speaker", "") or "")


def segment_acoustics(
    features: FrameFeatures,
    start: float,
    end: float,
) -> dict[str, float]:
    """Energy/variance/pause statistics for a transcript segment."""
    if not features.times:
        return {"energy": 0.0, "energy_variance": 0.0, "speech_rate": 0.0, "centroid": 0.0}
    import numpy as np

    times = np.array(features.times)
    mask = (times >= start) & (times < max(start + 0.01, end))
    if not mask.any():
        return {"energy": 0.0, "energy_variance": 0.0, "speech_rate": 0.0, "centroid": 0.0}
    rms = np.array(features.rms)[mask]
    centroid = np.array(features.centroid)[mask]
    return {
        "energy": float(rms.mean()),
        "energy_variance": float(rms.std()),
        "centroid": float(centroid.mean()),
        "voiced_ratio": float(np.mean(np.array(features.voiced)[mask])),
    }
