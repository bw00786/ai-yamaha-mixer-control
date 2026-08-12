"""DSPController — owns one ChannelChain per channel, processes audio blocks,
and translates AI MixMoves into concrete parameter changes.

This is the "takeover" layer: when the advisor's moves are applied here, the
correction happens in software on the USB return path instead of (or before)
the operator touching the desk. A global bypass hands the untouched signal
straight back at any moment.
"""
from __future__ import annotations

import re
import numpy as np

from ..models import MixMove
from .chain import ChannelChain

_FREQ_RE = re.compile(r"(\d+(?:\.\d+)?)\s*k?\s*hz", re.IGNORECASE)
_DB_RE = re.compile(r"(-?\d+(?:\.\d+)?)\s*d?b", re.IGNORECASE)
_RATIO_RE = re.compile(r"(\d+(?:\.\d+)?)\s*:\s*1")

# Center frequencies used when the AI names a band instead of a frequency
BAND_CENTERS = {
    "sub": 45.0, "low": 120.0, "low-mid": 350.0,
    "mid": 1000.0, "high-mid": 3500.0, "high": 9000.0,
}


def _parse_freq(text: str) -> float | None:
    # Band names win over embedded digits: "low-mid (250-500 Hz)" means the
    # band's center, not whichever edge the regex happens to grab.
    low = text.lower()
    for key, center in sorted(BAND_CENTERS.items(), key=lambda kv: -len(kv[0])):
        if key in low:
            return center
    m = _FREQ_RE.search(text)
    if m:
        v = float(m.group(1))
        if "k" in m.group(0).lower().replace("khz", "k"):
            v *= 1000.0
        return v
    return None


def _parse_db(text: str, default: float) -> float:
    m = _DB_RE.search(text)
    return float(m.group(1)) if m else default


class DSPController:
    def __init__(self, n_channels: int, samplerate: float):
        self.n_channels = n_channels
        self.samplerate = samplerate
        self.chains = [ChannelChain(samplerate) for _ in range(n_channels)]
        self.master_bypass = True   # start passive — takeover is opt-in

    # -------------------------------------------------------------- audio
    def process_block(self, block: np.ndarray) -> np.ndarray:
        """block: (channels, samples) float32 — returns processed copy."""
        if self.master_bypass:
            return block
        out = np.empty_like(block)
        for ch in range(min(block.shape[0], self.n_channels)):
            out[ch] = self.chains[ch].process(block[ch].astype(np.float64))
        # protective hard ceiling so a bad boost can't clip the return path
        np.clip(out, -0.985, 0.985, out=out)
        return out.astype(np.float32)

    # ------------------------------------------------------- move -> params
    def apply_move(self, move: MixMove) -> dict:
        """Translate one AI move into DSP changes. Returns what was done."""
        ch = move.channel - 1
        if not (0 <= ch < self.n_channels):
            return {"applied": False, "detail": "channel out of range"}
        chain = self.chains[ch]
        text = f"{move.param} {move.amount}"

        if move.action == "gain" or move.action == "fader":
            delta = _parse_db(text, -3.0)
            chain.set_trim(delta)
            return {"applied": True, "detail": f"trim {delta:+.1f} dB "
                    f"(now {chain.trim_db:+.1f} dB)"}

        if move.action == "hpf":
            freq = _parse_freq(text) or 100.0
            chain.set_hpf(True, freq)
            return {"applied": True, "detail": f"HPF on @ {chain.hpf_freq:.0f} Hz"}

        if move.action in ("eq_cut", "eq_boost"):
            freq = _parse_freq(text)
            if freq is None:
                return {"applied": False, "detail": "no frequency in move"}
            gain = _parse_db(move.amount, -3.0 if move.action == "eq_cut" else 2.0)
            if move.action == "eq_cut" and gain > 0:
                gain = -gain
            chain.set_eq_band(freq, gain)
            return {"applied": True, "detail": f"EQ {gain:+.1f} dB @ {freq:.0f} Hz"}

        if move.action == "comp":
            m = _RATIO_RE.search(text)
            ratio = float(m.group(1)) if m else 3.0
            thresh = _parse_db(move.param, -18.0)
            chain.set_comp(True, threshold_db=thresh, ratio=ratio)
            return {"applied": True,
                    "detail": f"comp on, thr {chain.comp.threshold_db:.0f} dB, "
                              f"ratio {chain.comp.ratio:.1f}:1"}

        if move.action == "pan":
            # pan stays on the desk — software panning would fight the
            # console's own bus routing
            return {"applied": False, "detail": "pan is applied on the console"}

        return {"applied": False, "detail": f"unknown action {move.action}"}

    # -------------------------------------------------------------- control
    def set_master_bypass(self, bypass: bool):
        self.master_bypass = bool(bypass)

    def reset_channel(self, channel: int):
        if 1 <= channel <= self.n_channels:
            self.chains[channel - 1].reset()

    def reset_all(self):
        for c in self.chains:
            c.reset()

    def state(self) -> dict:
        return {
            "master_bypass": self.master_bypass,
            "engaged": not self.master_bypass,
            "channels": {str(i + 1): c.state()
                         for i, c in enumerate(self.chains) if c.is_active},
        }
