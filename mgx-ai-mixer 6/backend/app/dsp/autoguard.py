"""AutoGuard — autonomous feedback suppression.

Watches every active channel continuously and notches rings the moment they
are confirmed. Built for unattended trustworthiness (volunteer operators):

  * Strict detection: higher dominance threshold than the manual command,
    PLUS harmonic rejection — organ pipes, pianos, voices and pads carry a
    harmonic series; a feedback ring is a pure sinusoid. A peak with audible
    2nd/3rd harmonics is a note and is never touched.
  * Two-scan confirmation: the same frequency (±2%) must be detected in two
    consecutive sweeps (~0.6 s apart) before any notch is placed. A ring
    survives that easily; a transient never does.
  * Source attribution: one ring in the room is heard by EVERY open mic.
    When the same frequency shows up on several channels in one sweep, only
    the channel hearing it strongest — the mic actually driving the loop —
    is notched; bystander channels are left untouched (and logged).
  * Per-channel cooldown (4 s) and a global rate limit (6 notches/min):
    even a pathological signal can't machine-gun notches into the mix.
  * Full audit trail: every catch is logged with time, channel, frequency
    and dominance, streamed to the UI, and every notch is one click to undo.
  * Fail-safe: the guard only acts while the DSP is engaged; disarming or
    bypassing instantly stops all autonomous action. Detection runs off the
    real-time audio thread.
"""
from __future__ import annotations

import time
from collections import deque

import numpy as np

from .controller import DSPController
from .feedback import detect_feedback

SCAN_DOMINANCE_DB = 28.0     # stricter than manual (25)
SCAN_ENERGY_FRACTION = 0.25
CONFIRM_SCANS = 2
CHANNEL_COOLDOWN_S = 4.0
GLOBAL_LIMIT = 6             # max auto-notches per rolling minute
ACTIVITY_GATE_DB = -55.0


class AutoGuard:
    def __init__(self, dsp: DSPController, samplerate: float):
        self.dsp = dsp
        self.fs = samplerate
        self.enabled = False
        self.excluded: set[int] = set()
        self._pending: dict[int, dict] = {}      # ch -> {freq, count}
        self._last_notch: dict[int, float] = {}  # ch -> monotonic time
        self._recent: deque[float] = deque(maxlen=GLOBAL_LIMIT)
        self.events: deque[dict] = deque(maxlen=20)

    # ------------------------------------------------------------- control
    def configure(self, enabled: bool | None = None,
                  excluded: list[int] | None = None):
        if enabled is not None:
            self.enabled = bool(enabled)
            if not self.enabled:
                self._pending.clear()
        if excluded is not None:
            self.excluded = {int(c) for c in excluded}

    # ---------------------------------------------------------------- scan
    def scan(self, audio: np.ndarray, name_of) -> list[dict]:
        """audio: (channels, samples), >= ~1.05 s. Returns new catches.
        Call from a background task, never from the audio callback."""
        if not self.enabled or self.dsp.master_bypass:
            self._pending.clear()
            return []

        now = time.monotonic()
        catches: list[dict] = []

        # -- pass 1: detect on every eligible channel --------------------
        detections: list[tuple[int, dict, float]] = []   # (ch1, hit, level)
        for ch in range(min(audio.shape[0], self.dsp.n_channels)):
            ch1 = ch + 1
            if ch1 in self.excluded:
                continue
            x = audio[ch]
            rms = float(np.sqrt(np.mean(x ** 2)))
            if 20 * np.log10(max(rms, 1e-10)) < ACTIVITY_GATE_DB:
                self._pending.pop(ch1, None)
                continue

            hit = detect_feedback(
                x, self.fs,
                dominance_db=SCAN_DOMINANCE_DB,
                energy_fraction=SCAN_ENERGY_FRACTION,
                reject_harmonics=True,
            )
            if hit is None:
                self._pending.pop(ch1, None)
                continue
            detections.append((ch1, hit, rms))

        # -- pass 2: group same-frequency detections; strongest mic leads --
        # One acoustic ring enters every open mic. Attribute it to the
        # channel hearing it loudest (in-band level = rms here, all channels
        # dominated by the ring) and clear pending on the bystanders.
        groups: list[dict] = []
        for ch1, hit, rms in sorted(detections, key=lambda d: -d[2]):
            placed = False
            for g in groups:
                if abs(g["freq"] - hit["freq"]) / g["freq"] < 0.02:
                    g["others"].append(ch1)
                    placed = True
                    break
            if not placed:
                groups.append({"freq": hit["freq"], "lead": (ch1, hit),
                               "others": []})
        for g in groups:
            for other in g["others"]:
                self._pending.pop(other, None)

        # -- pass 3: confirmation / cooldown / notch on group leads -------
        for g in groups:
            ch1, hit = g["lead"]
            pend = self._pending.get(ch1)
            if pend and abs(pend["freq"] - hit["freq"]) / pend["freq"] < 0.02:
                pend["count"] += 1
                pend["freq"] = hit["freq"]
            else:
                self._pending[ch1] = {"freq": hit["freq"], "count": 1}
                continue

            if pend["count"] < CONFIRM_SCANS:
                continue
            if now - self._last_notch.get(ch1, -1e9) < CHANNEL_COOLDOWN_S:
                continue
            if len(self._recent) == GLOBAL_LIMIT and \
                    now - self._recent[0] < 60.0:
                continue                      # global rate limit reached

            freq = self.dsp.chains[ch1 - 1].set_notch(hit["freq"])
            self._last_notch[ch1] = now
            self._recent.append(now)
            self._pending.pop(ch1, None)
            event = {
                "time": time.strftime("%H:%M:%S"),
                "channel": ch1,
                "name": name_of(ch1),
                "freq": freq,
                "dominance_db": hit["dominance_db"],
                "also_heard_on": [name_of(c) for c in g["others"]],
            }
            self.events.appendleft(event)
            catches.append(event)

        return catches

    # ---------------------------------------------------------------- state
    def state(self) -> dict:
        return {
            "enabled": self.enabled,
            "armed": self.enabled and not self.dsp.master_bypass,
            "excluded": sorted(self.excluded),
            "events": list(self.events),
        }
