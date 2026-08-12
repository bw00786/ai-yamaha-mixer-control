"""Feedback detection.

Acoustic feedback has an unmistakable spectral fingerprint: a single very
narrow peak that towers over the rest of the spectrum AND stays at the same
frequency over time (music moves; a ring doesn't). The detector checks both:

  1. Dominance  — peak power vs the spectrum's median (dB) and the fraction
                  of total energy concentrated around the peak.
  2. Persistence — the same peak frequency (±2%) in consecutive windows.

Frequency is refined with parabolic interpolation on the log spectrum, which
gets within a few Hz — plenty for a notch with a ~1/10-octave bandwidth.
"""
from __future__ import annotations

import numpy as np

MIN_FREQ = 80.0        # below this it's rumble, not feedback
MAX_FREQ = 12000.0
DOMINANCE_DB = 25.0    # peak must be this far above the median bin
ENERGY_FRACTION = 0.20 # peak neighborhood must hold this share of energy
NFFT = 8192


def _peak_of(x: np.ndarray, fs: float) -> tuple[float, float, float] | None:
    """Return (freq, dominance_db, energy_fraction) of the strongest
    narrow peak in one window, or None if the window is too quiet."""
    if len(x) < NFFT:
        return None
    seg = x[-NFFT:] * np.hanning(NFFT)
    spec = np.abs(np.fft.rfft(seg)) ** 2
    freqs = np.fft.rfftfreq(NFFT, 1.0 / fs)
    band = (freqs >= MIN_FREQ) & (freqs <= MAX_FREQ)
    if not np.any(band) or np.sum(spec[band]) < 1e-10:
        return None

    idx_band = np.flatnonzero(band)
    k = idx_band[np.argmax(spec[idx_band])]

    # parabolic interpolation on log power for sub-bin frequency
    if 1 <= k < len(spec) - 1:
        a, b, c = np.log(spec[k - 1] + 1e-20), np.log(spec[k] + 1e-20), \
                  np.log(spec[k + 1] + 1e-20)
        denom = a - 2 * b + c
        delta = 0.5 * (a - c) / denom if abs(denom) > 1e-12 else 0.0
        delta = float(np.clip(delta, -0.5, 0.5))
    else:
        delta = 0.0
    freq = float((k + delta) * fs / NFFT)

    med = float(np.median(spec[band])) + 1e-20
    dominance = 10.0 * np.log10(spec[k] / med)
    lo, hi = max(0, k - 3), min(len(spec), k + 4)
    fraction = float(np.sum(spec[lo:hi]) / np.sum(spec[band]))
    return freq, dominance, fraction


HARMONIC_REJECT_DB = -28.0   # 2nd/3rd harmonic within this of the peak => a note


def _harmonic_level_db(x: np.ndarray, fs: float, freq: float, mult: int) -> float:
    """Power at mult*freq relative to power at freq, in dB."""
    if mult * freq >= fs / 2 * 0.95:
        return -120.0
    seg = x[-NFFT:] * np.hanning(NFFT)
    spec = np.abs(np.fft.rfft(seg)) ** 2
    def p_at(f):
        k = int(round(f * NFFT / fs))
        lo, hi = max(0, k - 2), min(len(spec), k + 3)
        return float(np.max(spec[lo:hi])) + 1e-20
    return 10.0 * np.log10(p_at(mult * freq) / p_at(freq))


def detect_feedback(x: np.ndarray, fs: float,
                    dominance_db: float = DOMINANCE_DB,
                    energy_fraction: float = ENERGY_FRACTION,
                    reject_harmonics: bool = False) -> dict | None:
    """x: 1-D recent audio for one channel (>= ~0.7 s recommended).

    reject_harmonics: refuse peaks that carry significant 2nd or 3rd
    harmonics — organ pipes, pianos, voices and synth notes all do; a
    feedback ring is a pure sinusoid and doesn't. Enable for autonomous
    operation, where notching a musical note is worse than reacting a
    moment slower.

    Returns {"freq": Hz, "dominance_db": float} or None."""
    x = np.asarray(x, dtype=np.float64)
    if len(x) < 2 * NFFT:
        return None
    windows = [x[-NFFT:], x[-2 * NFFT:-NFFT]]
    if len(x) >= 3 * NFFT:
        windows.append(x[-3 * NFFT:-2 * NFFT])

    peaks = [_peak_of(w, fs) for w in windows]
    if any(p is None for p in peaks):
        return None

    freqs = [p[0] for p in peaks]
    ref = freqs[0]
    if any(abs(f - ref) / ref > 0.02 for f in freqs[1:]):
        return None                      # peak is moving: that's music
    if not all(p[1] >= dominance_db and p[2] >= energy_fraction
               for p in peaks):
        return None                      # strong but broad: also music

    freq = float(np.mean(freqs))
    if reject_harmonics:
        h2 = _harmonic_level_db(x, fs, freq, 2)
        h3 = _harmonic_level_db(x, fs, freq, 3)
        if max(h2, h3) > HARMONIC_REJECT_DB:
            return None                  # harmonic series present: a note

    return {"freq": round(freq, 1),
            "dominance_db": round(min(p[1] for p in peaks), 1)}
