"""Real-time send effects: algorithmic reverb + feedback delay.

Built on circular delay lines with vectorized block reads/writes — O(N) per
block regardless of delay length. (A naive lfilter comb is O(N·D) because the
IIR coefficient array is dense; at 1600-tap comb delays that's ~35x over the
real-time budget for 22 channels. Circular buffers fix it.)

Reverb: Freeverb topology — 8 parallel feedback combs into 2 series
allpasses, one-pole low-pass on the wet path standing in for damping.
Delay: single feedback comb, echoes wet-mixed against dry.
"""
from __future__ import annotations

import numpy as np
from scipy.signal import lfilter

# Freeverb comb/allpass delays (samples @ 44.1k, scaled at runtime)
_COMB_44K = [1116, 1188, 1277, 1356, 1422, 1491, 1557, 1617]
_ALLPASS_44K = [556, 441]

CLAMP_FX = {
    "reverb_wet": (0.0, 0.6),
    "reverb_size": (0.1, 1.0),
    "delay_wet": (0.0, 0.6),
    "delay_time_ms": (50.0, 800.0),
    "delay_feedback": (0.0, 0.6),
}


def clamp_fx(key: str, v: float) -> float:
    lo, hi = CLAMP_FX[key]
    return float(min(hi, max(lo, v)))


class _FeedbackComb:
    """Freeverb comb: out = buf[i]; buf[i] = x + g*out. Block-vectorized."""

    def __init__(self, delay: int, g: float):
        self.D = max(2, delay)
        self.g = g
        self.buf = np.zeros(self.D)
        self.i = 0

    def process(self, x: np.ndarray) -> np.ndarray:
        out = np.empty_like(x)
        n, pos = len(x), 0
        while pos < n:
            k = min(n - pos, self.D - self.i)
            seg = slice(self.i, self.i + k)
            d = self.buf[seg]
            out[pos:pos + k] = d
            self.buf[seg] = x[pos:pos + k] + self.g * d
            self.i = (self.i + k) % self.D
            pos += k
        return out


class _Allpass:
    """Freeverb allpass: out = -x + buf[i]; buf[i] = x + g*buf[i]."""

    def __init__(self, delay: int, g: float = 0.5):
        self.D = max(2, delay)
        self.g = g
        self.buf = np.zeros(self.D)
        self.i = 0

    def process(self, x: np.ndarray) -> np.ndarray:
        out = np.empty_like(x)
        n, pos = len(x), 0
        while pos < n:
            k = min(n - pos, self.D - self.i)
            seg = slice(self.i, self.i + k)
            d = self.buf[seg]
            out[pos:pos + k] = -x[pos:pos + k] + d
            self.buf[seg] = x[pos:pos + k] + self.g * d
            self.i = (self.i + k) % self.D
            pos += k
        return out


class Reverb:
    def __init__(self, fs: float):
        self.fs = fs
        self.on = False
        self.wet = 0.25
        self.size = 0.5          # 0..1 -> comb feedback 0.75..0.92
        self._build()
        # one-pole LP on the wet path ≈ damping
        self._lp_b, self._lp_a = np.array([0.35]), np.array([1.0, -0.65])
        self._lp_zi = np.zeros(1)

    def _build(self):
        scale = self.fs / 44100.0
        g = 0.75 + 0.17 * self.size
        self.combs = [_FeedbackComb(int(d * scale), g) for d in _COMB_44K]
        self.allpasses = [_Allpass(int(d * scale)) for d in _ALLPASS_44K]

    def set(self, wet: float | None = None, size: float | None = None):
        if wet is not None:
            self.wet = clamp_fx("reverb_wet", wet)
        if size is not None:
            self.size = clamp_fx("reverb_size", size)
            self._build()          # re-tune comb feedback (tails reset)
        self.on = self.wet > 0.005

    def process(self, x: np.ndarray) -> np.ndarray:
        if not self.on:
            return x
        wet = self.combs[0].process(x)
        for c in self.combs[1:]:
            wet = wet + c.process(x)
        wet /= len(self.combs)
        for ap in self.allpasses:
            wet = ap.process(wet)
        wet, self._lp_zi = lfilter(self._lp_b, self._lp_a, wet, zi=self._lp_zi)
        return (1.0 - self.wet) * x + self.wet * wet * 1.5

    def state(self) -> dict:
        return {"on": self.on, "wet": round(self.wet, 2),
                "size": round(self.size, 2)}


class Delay:
    def __init__(self, fs: float):
        self.fs = fs
        self.on = False
        self.wet = 0.25
        self.time_ms = 320.0
        self.feedback = 0.35
        self._build()

    def _build(self):
        d = max(2, int(self.fs * self.time_ms / 1000.0))
        self._comb = _FeedbackComb(d, self.feedback)

    def set(self, wet: float | None = None, time_ms: float | None = None,
            feedback: float | None = None):
        rebuild = False
        if wet is not None:
            self.wet = clamp_fx("delay_wet", wet)
        if time_ms is not None:
            self.time_ms = clamp_fx("delay_time_ms", time_ms); rebuild = True
        if feedback is not None:
            self.feedback = clamp_fx("delay_feedback", feedback); rebuild = True
        if rebuild:
            self._build()
        self.on = self.wet > 0.005

    def process(self, x: np.ndarray) -> np.ndarray:
        if not self.on:
            return x
        echoes = self._comb.process(x)   # delayed + regenerating tail only
        return x + self.wet * echoes

    def state(self) -> dict:
        return {"on": self.on, "wet": round(self.wet, 2),
                "time_ms": round(self.time_ms, 0),
                "feedback": round(self.feedback, 2)}
