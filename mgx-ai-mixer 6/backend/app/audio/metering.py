"""Fast per-channel metering + LUFS loudness for the stereo mix."""
from __future__ import annotations

import numpy as np

from ..models import ChannelMeter

try:
    import pyloudnorm as pyln
except ImportError:
    pyln = None

EPS = 1e-10


def db(x: float) -> float:
    return float(20.0 * np.log10(max(x, EPS)))


def meters(audio: np.ndarray, names) -> list[ChannelMeter]:
    """audio: (channels, samples) — the last ~100ms is plenty."""
    out = []
    for ch in range(audio.shape[0]):
        x = audio[ch]
        peak = float(np.max(np.abs(x))) if x.size else 0.0
        rms = float(np.sqrt(np.mean(x ** 2))) if x.size else 0.0
        out.append(ChannelMeter(
            channel=ch + 1,
            name=names(ch + 1),
            peak_db=round(db(peak), 1),
            rms_db=round(db(rms), 1),
            clip=peak >= 0.999,
        ))
    return out


def mix_lufs(stereo: np.ndarray, samplerate: int) -> tuple[float | None, float | None]:
    """Integrated + short-term LUFS of a (2, samples) stereo buffer."""
    if pyln is None or stereo.shape[1] < samplerate:
        return None, None
    meter = pyln.Meter(samplerate)
    data = stereo.T  # pyloudnorm wants (samples, channels)
    try:
        integrated = meter.integrated_loudness(data)
        short = meter.integrated_loudness(data[-3 * samplerate:])
        # pyloudnorm returns -inf for silence (e.g. unpatched channels on
        # real hardware); that isn't valid JSON, so map it to None instead
        # of breaking the WebSocket payload on the frontend.
        integrated = round(float(integrated), 1) if np.isfinite(integrated) else None
        short = round(float(short), 1) if np.isfinite(short) else None
        return integrated, short
    except Exception:
        return None, None
