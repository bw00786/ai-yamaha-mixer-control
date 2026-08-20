"""AutoMixer ("Mix keeper") — autonomous mix-quality corrections.

Watches the continuously-computed MixSnapshot (every ~2 s) and fixes the
common problems a volunteer operator would miss, using the same trust
architecture as the feedback AutoGuard:

  * Deterministic rules only — no LLM in the autonomous loop. Sunday
    behavior is predictable, explainable, and works with no internet.
  * Persistence before action: a problem must be present in N consecutive
    analysis cycles (4-6 s) before anything is touched. Transients,
    single loud notes, and momentary collisions never trigger.
  * Gentle increments with per-issue cooldowns: -2 dB at a time, never
    -6 at once; a channel/issue pair can only be acted on again after its
    cooldown. All parameters pass the chain's hard clamps, so cumulative
    corrections are bounded no matter how long the service runs.
  * Global rate limit (4 actions/min): the mix must never feel like it's
    churning under the operator.
  * Full audit trail, streamed to the UI; every action is visible in the
    DSP panel and one click to undo. Disabling or bypassing stops
    everything instantly.

What it watches and does:
  HOT      headroom < 3 dB, 2 cycles      -> trim -2 dB        (10 s cooldown)
  MUD      bright source w/ low-end energy,
           3 cycles                        -> engage HPF 100 Hz (one-shot)
  MASKING  collision score >= 0.7, 3 cycles-> -2 dB on the quieter channel
                                              in the collision band (30 s)
  LOUD MIX short-term LUFS > -12, 3 cycles -> log-only advisory (master
                                              level belongs to the human)
"""
from __future__ import annotations

import time
from collections import deque

from ..models import MixSnapshot
from .controller import DSPController, BAND_CENTERS

PERSIST = {"hot": 2, "mud": 3, "mask": 3, "loud": 3}
COOLDOWN_S = {"hot": 10.0, "mask": 30.0, "loud": 60.0}
GLOBAL_LIMIT = 4          # actions per rolling minute
MASK_SCORE = 0.70


def _band_center(label: str) -> float | None:
    low = label.lower()
    for key, center in sorted(BAND_CENTERS.items(), key=lambda kv: -len(kv[0])):
        if key in low:
            return center
    return None


class AutoMixer:
    def __init__(self, dsp: DSPController):
        self.dsp = dsp
        self.enabled = False
        self._counts: dict[tuple, int] = {}
        self._last: dict[tuple, float] = {}
        self._recent: deque[float] = deque(maxlen=GLOBAL_LIMIT)
        self.events: deque[dict] = deque(maxlen=20)

    def configure(self, enabled: bool | None = None):
        if enabled is not None:
            self.enabled = bool(enabled)
            if not self.enabled:
                self._counts.clear()

    # ------------------------------------------------------------ plumbing
    def _bump(self, key: tuple, condition: bool) -> bool:
        """Track persistence. True when the condition has held long enough."""
        if not condition:
            self._counts.pop(key, None)
            return False
        n = self._counts.get(key, 0) + 1
        self._counts[key] = n
        return n >= PERSIST[key[0]]

    def _may_act(self, key: tuple, now: float,
                 count_global: bool = True) -> bool:
        cd = COOLDOWN_S.get(key[0], 0.0)
        if now - self._last.get(key, -1e9) < cd:
            return False
        # log-only advisories don't touch audio, so they don't compete with
        # real actions for the global rate budget
        if count_global and len(self._recent) == GLOBAL_LIMIT \
                and now - self._recent[0] < 60.0:
            return False
        return True

    def _record(self, key: tuple, now: float, name: str,
                action: str, reason: str, log_only: bool = False) -> dict:
        self._last[key] = now
        if not log_only:
            self._recent.append(now)
        self._counts.pop(key, None)
        event = {"time": time.strftime("%H:%M:%S"), "name": name,
                 "action": action, "reason": reason, "advisory": log_only}
        self.events.appendleft(event)
        return event

    # ---------------------------------------------------------------- scan
    def scan(self, snap: MixSnapshot, name_of) -> list[dict]:
        """Call with each fresh analysis snapshot (~every 2 s), off the
        real-time thread. Returns new actions."""
        if not self.enabled or self.dsp.master_bypass:
            self._counts.clear()
            return []

        now = time.monotonic()
        actions: list[dict] = []

        for c in snap.channels:
            ch = c.channel
            if ch > self.dsp.n_channels:
                continue
            chain = self.dsp.chains[ch - 1]
            if not c.active:
                for issue in ("hot", "mud"):
                    self._counts.pop((issue, ch), None)
                continue

            # HOT: persistent clipping risk -> gentle trim
            key = ("hot", ch)
            if self._bump(key, c.headroom_db < 3.0) and self._may_act(key, now):
                chain.set_trim(-2.0)
                actions.append(self._record(
                    key, now, name_of(ch), "trim -2 dB",
                    f"headroom {c.headroom_db:.0f} dB - clipping risk"))

            # MUD: bright source carrying low-end energy -> HPF (one-shot)
            key = ("mud", ch)
            muddy = (c.centroid_hz > 800 and len(c.band_energy) > 1
                     and c.band_energy[1] > 0.25 and not chain.hpf_on)
            if self._bump(key, muddy) and self._may_act(key, now):
                chain.set_hpf(True, 100.0)
                actions.append(self._record(
                    key, now, name_of(ch), "HPF 100 Hz",
                    "persistent low-end mud on a bright source"))

        # MASKING: persistent strong collision -> small cut on the quieter side
        for p in snap.masking[:3]:
            if p.a > self.dsp.n_channels or p.b > self.dsp.n_channels:
                continue
            a = snap.channels[p.a - 1]
            b = snap.channels[p.b - 1]
            key = ("mask", min(p.a, p.b), max(p.a, p.b))
            if self._bump(key, p.score >= MASK_SCORE) and self._may_act(key, now):
                quieter = a if a.rms_db < b.rms_db else b
                other = b if quieter is a else a
                center = _band_center(p.band)
                if center is None:
                    continue
                self.dsp.chains[quieter.channel - 1].set_eq_band(center, -2.0)
                actions.append(self._record(
                    key, now, name_of(quieter.channel),
                    f"-2 dB @ {center:.0f} Hz",
                    f"masking with {name_of(other.channel)} in {p.band}"))

        # LOUD MIX: advisory only — the master level belongs to the human
        key = ("loud",)
        if snap.lufs_s is not None:
            if self._bump(key, snap.lufs_s > -12.0) and \
                    self._may_act(key, now, count_global=False):
                actions.append(self._record(
                    key, now, "MIX", "advisory",
                    f"mix running hot ({snap.lufs_s} LUFS) - consider "
                    "lowering the master fader", log_only=True))

        return actions

    def state(self) -> dict:
        return {
            "enabled": self.enabled,
            "armed": self.enabled and not self.dsp.master_bypass,
            "events": list(self.events),
        }
