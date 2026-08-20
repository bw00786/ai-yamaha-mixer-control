"""Permanent per-channel presets.

Corrections a given room needs every session (a stubborn feedback node, a mic
that always carries low-end rumble) belong here, applied once at startup so the
AI doesn't re-derive them each time. Protected notches survive `reset` and are
skipped by AutoGuard, so the learning log stays free of repeat catches on known
problem frequencies.
"""
from __future__ import annotations

from .controller import DSPController

# channel -> {"hpf": freq_hz | None, "notches": [freq_hz, ...]}
# CH 2 is this room's fixed feedback node (~8.4 kHz ring, dominance up to 51 dB)
# and always carries low-end rumble — pin both corrections permanently.
CHANNEL_PRESETS: dict[int, dict] = {
    2: {"hpf": 100.0, "notches": [8446.0]},
}


def apply_presets(dsp: DSPController) -> None:
    """(Re)apply every channel preset to its DSP chain. Idempotent — notches
    re-center rather than stack, so calling this after a reset is safe."""
    for ch, preset in CHANNEL_PRESETS.items():
        if not (1 <= ch <= dsp.n_channels):
            continue
        chain = dsp.chains[ch - 1]
        hpf = preset.get("hpf")
        if hpf:
            chain.set_hpf(True, hpf)
        for freq in preset.get("notches", []):
            chain.set_notch(freq, protected=True)
