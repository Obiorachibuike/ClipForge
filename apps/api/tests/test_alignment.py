"""Script alignment: synthesis and timing conversion must be real, not assumed."""
from __future__ import annotations

import wave

import pytest
from app.core.errors import ProviderUnavailableError


def test_synthesize_to_wav_writes_real_pcm(tmp_path):
    """Regression: the function used `wave` without importing it (NameError)."""
    pytest.importorskip("espeakng")
    from app.services.ai.transcription.alignment import synthesize_to_wav

    try:
        path = synthesize_to_wav("One two three four five.", tmp_path / "reference.wav", rate=170)
    except ProviderUnavailableError:
        pytest.skip("espeak-ng is not available in this environment")

    assert path.exists() and path.stat().st_size > 44
    with wave.open(str(path), "rb") as handle:
        assert handle.getnchannels() == 1
        assert handle.getsampwidth() == 2
        assert handle.getframerate() > 0
        assert handle.getnframes() > 0
