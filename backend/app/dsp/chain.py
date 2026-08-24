"""Real-time per-channel DSP chain.

Signal path per channel:
  HPF -> parametric EQ (up to 4 bands) -> compressor -> reverb -> delay -> trim

Everything is block-based and fully vectorized (scipy.sosfilt with persistent
state, one-pole envelope follower via lfilter), so 22 channels at 48 kHz /
256-sample blocks costs well under a millisecond per block on a laptop.

RBJ biquad designs; all parameters hot-swappable between blocks.
"""
from __future__ import annotations

import math
import threading
import numpy as np
from scipy.signal import sosfilt, sosfilt_zi, lfilter, lfilter_zi

from .effects import Reverb, Delay

MAX_EQ_BANDS = 4
MAX_NOTCHES = 4
NOTCH_GAIN_DB = -18.0
NOTCH_Q = 14.0

# Safety clamps — nothing the AI (or a bug) does can exceed these.
CLAMP = {
    "trim_db": (-12.0, 6.0),
    "eq_gain_db": (-8.0, 6.0),
    "eq_freq": (40.0, 16000.0),
    "eq_q": (0.4, 6.0),
    "hpf_freq": (40.0, 400.0),
    "notch_freq": (60.0, 14000.0),
    "comp_threshold_db": (-40.0, 0.0),
    "comp_ratio": (1.0, 8.0),
    "comp_makeup_db": (0.0, 6.0),
}


def clamp(key: str, v: float) -> float:
    lo, hi = CLAMP[key]
    return float(min(hi, max(lo, v)))


# ------------------------------------------------------------ biquad design
def _hpf_sos(freq: float, fs: float, q: float = 0.707) -> np.ndarray:
    w0 = 2 * math.pi * freq / fs
    cw, sw = math.cos(w0), math.sin(w0)
    alpha = sw / (2 * q)
    b0, b1, b2 = (1 + cw) / 2, -(1 + cw), (1 + cw) / 2
    a0, a1, a2 = 1 + alpha, -2 * cw, 1 - alpha
    return np.array([[b0 / a0, b1 / a0, b2 / a0, 1.0, a1 / a0, a2 / a0]])


def _peak_sos(freq: float, gain_db: float, q: float, fs: float) -> np.ndarray:
    A = 10 ** (gain_db / 40)
    w0 = 2 * math.pi * freq / fs
    cw, sw = math.cos(w0), math.sin(w0)
    alpha = sw / (2 * q)
    b0, b1, b2 = 1 + alpha * A, -2 * cw, 1 - alpha * A
    a0, a1, a2 = 1 + alpha / A, -2 * cw, 1 - alpha / A
    return np.array([[b0 / a0, b1 / a0, b2 / a0, 1.0, a1 / a0, a2 / a0]])


class _Biquad:
    """One stateful biquad section that can be redesigned on the fly."""

    def __init__(self):
        self.sos = None
        self.zi = None

    def set(self, sos: np.ndarray | None):
        if sos is None:
            self.sos, self.zi = None, None
            return
        if (self.sos is None or sos.shape != self.sos.shape
                or not np.allclose(sos, self.sos)):
            self.sos = sos
            self.zi = sosfilt_zi(sos) * 0.0

    def process(self, x: np.ndarray) -> np.ndarray:
        if self.sos is None:
            return x
        y, self.zi = sosfilt(self.sos, x, zi=self.zi)
        return y


class Compressor:
    """Feed-forward compressor with a vectorized one-pole envelope follower."""

    def __init__(self, fs: float):
        self.fs = fs
        self.on = False
        self.threshold_db = -18.0
        self.ratio = 3.0
        self.attack_ms = 10.0
        self.release_ms = 120.0
        self.makeup_db = 0.0
        self._env_state = np.zeros(1)

    def process(self, x: np.ndarray) -> np.ndarray:
        if not self.on:
            return x
        a = 1.0 - math.exp(-1.0 / (self.fs * self.attack_ms / 1000.0))
        rect = np.abs(x)
        # one-pole smoother: y[n] = (1-a)*y[n-1] + a*|x[n]|
        env, self._env_state = lfilter([a], [1.0, -(1.0 - a)], rect,
                                       zi=self._env_state)
        env_db = 20.0 * np.log10(np.maximum(env, 1e-8))
        over = env_db - self.threshold_db
        gain_db = np.where(over > 0.0, -over * (1.0 - 1.0 / self.ratio), 0.0)
        gain_db += self.makeup_db
        return x * (10.0 ** (gain_db / 20.0))

    def state(self) -> dict:
        return {"on": self.on, "threshold_db": self.threshold_db,
                "ratio": self.ratio, "makeup_db": self.makeup_db}


class ChannelChain:
    """Complete processing chain for one channel. Thread-safe parameter swaps."""

    def __init__(self, fs: float):
        self.fs = fs
        self._lock = threading.Lock()
        self.bypass = False
        self.trim_db = 0.0
        self.hpf_on = False
        self.hpf_freq = 100.0
        # eq bands: list of dicts {freq, gain_db, q, on}
        self.eq: list[dict] = []
        # surgical feedback notches: list of dicts {freq}
        self.notches: list[dict] = []
        # HPF + all EQ bands cascade into ONE sosfilt call (5 stages of
        # per-call overhead collapse into one)
        self._filters = _Biquad()
        self.comp = Compressor(fs)
        self.reverb = Reverb(fs)
        self.delay = Delay(fs)

    # ----------------------------------------------------------- parameters
    def set_hpf(self, on: bool, freq: float | None = None):
        with self._lock:
            self.hpf_on = on
            if freq is not None:
                self.hpf_freq = clamp("hpf_freq", freq)
            self._rebuild_filters()

    def set_eq_band(self, freq: float, gain_db: float, q: float = 1.4) -> bool:
        """Add a band, or adjust the existing band nearest in frequency
        (within a third of an octave). Cumulative gains are clamped.

        Returns True if a parameter actually changed; False when the band was
        already at the clamp, so callers can avoid logging phantom actions."""
        freq = clamp("eq_freq", freq)
        gain_db = clamp("eq_gain_db", gain_db)
        q = clamp("eq_q", q)
        with self._lock:
            for band in self.eq:
                if abs(math.log2(band["freq"] / freq)) < 0.33:
                    new_gain = clamp("eq_gain_db", band["gain_db"] + gain_db)
                    if new_gain == band["gain_db"] and q == band["q"]:
                        return False
                    band["gain_db"] = new_gain
                    band["q"] = q
                    self._rebuild_filters()
                    return True
            if len(self.eq) >= MAX_EQ_BANDS:
                self.eq.pop(0)  # oldest band makes room
            self.eq.append({"freq": freq, "gain_db": gain_db, "q": q, "on": True})
            self._rebuild_filters()
            return True

    def set_notch(self, freq: float, protected: bool = False) -> float:
        """Drop a deep, tight notch (feedback killer). Notches within a sixth
        of an octave of an existing one re-center it instead of stacking.
        A protected notch survives reset and is left alone by AutoGuard.
        Returns the frequency actually notched."""
        freq = clamp("notch_freq", freq)
        with self._lock:
            for n in self.notches:
                if abs(math.log2(n["freq"] / freq)) < 1 / 6:
                    n["freq"] = freq
                    if protected:
                        n["protected"] = True
                    self._rebuild_filters()
                    return freq
            if len(self.notches) >= MAX_NOTCHES:
                self.notches.pop(0)
            self.notches.append({"freq": freq, "protected": protected})
            self._rebuild_filters()
            return freq

    def has_protected_notch(self, freq: float) -> bool:
        """True if a protected (preset) notch already covers this frequency,
        within a sixth of an octave."""
        with self._lock:
            for n in self.notches:
                if n.get("protected") and abs(math.log2(n["freq"] / freq)) < 1 / 6:
                    return True
        return False

    def clear_notches(self):
        with self._lock:
            self.notches = []
            self._rebuild_filters()

    def clear_eq(self):
        with self._lock:
            self.eq = []
            self._rebuild_filters()

    def _rebuild_filters(self):
        sections = []
        if self.hpf_on:
            sections.append(_hpf_sos(self.hpf_freq, self.fs))
        for b in self.eq:
            if b["on"] and abs(b["gain_db"]) > 0.1:
                sections.append(_peak_sos(b["freq"], b["gain_db"], b["q"], self.fs))
        for n in self.notches:
            sections.append(_peak_sos(n["freq"], NOTCH_GAIN_DB, NOTCH_Q, self.fs))
        self._filters.set(np.vstack(sections) if sections else None)

    def set_trim(self, delta_db: float):
        with self._lock:
            self.trim_db = clamp("trim_db", self.trim_db + delta_db)

    def set_reverb(self, wet: float | None = None, size: float | None = None):
        with self._lock:
            self.reverb.set(wet=wet, size=size)

    def set_delay(self, wet: float | None = None, time_ms: float | None = None,
                  feedback: float | None = None):
        with self._lock:
            self.delay.set(wet=wet, time_ms=time_ms, feedback=feedback)

    def set_comp(self, on: bool, threshold_db: float | None = None,
                 ratio: float | None = None, makeup_db: float | None = None):
        with self._lock:
            self.comp.on = on
            if threshold_db is not None:
                self.comp.threshold_db = clamp("comp_threshold_db", threshold_db)
            if ratio is not None:
                self.comp.ratio = clamp("comp_ratio", ratio)
            if makeup_db is not None:
                self.comp.makeup_db = clamp("comp_makeup_db", makeup_db)

    def reset(self):
        with self._lock:
            self.trim_db = 0.0
            self.hpf_on = False
            self.eq = []
            # permanent presets survive a reset
            self.notches = [n for n in self.notches if n.get("protected")]
            self._rebuild_filters()
            self.comp.on = False
            self.reverb.set(wet=0.0)
            self.delay.set(wet=0.0)
            self.bypass = False

    # ------------------------------------------------------------- process
    def process(self, x: np.ndarray) -> np.ndarray:
        with self._lock:
            if self.bypass:
                return x
            y = self._filters.process(x)
            y = self.comp.process(y)
            y = self.reverb.process(y)
            y = self.delay.process(y)
            if abs(self.trim_db) > 0.01:
                y = y * (10.0 ** (self.trim_db / 20.0))
            return y

    @property
    def is_active(self) -> bool:
        return (not self.bypass) and (
            self.hpf_on or bool(self.eq) or bool(self.notches) or self.comp.on
            or self.reverb.on or self.delay.on
            or abs(self.trim_db) > 0.01)

    def state(self) -> dict:
        return {
            "bypass": self.bypass,
            "trim_db": round(self.trim_db, 1),
            "hpf": {"on": self.hpf_on, "freq": self.hpf_freq},
            "eq": [dict(b) for b in self.eq],
            "notches": [dict(n) for n in self.notches],
            "comp": self.comp.state(),
            "reverb": self.reverb.state(),
            "delay": self.delay.state(),
            "active": self.is_active,
        }
