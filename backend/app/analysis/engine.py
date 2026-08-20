"""Mix analysis: per-channel spectral fingerprints, masking detection,
gain-staging checks. Pure NumPy/SciPy — runs in a few ms per pass."""
from __future__ import annotations

import numpy as np
from scipy.signal import welch

from ..models import ChannelAnalysis, MaskingPair, MixSnapshot
from ..audio.metering import db, mix_lufs

# Analysis bands roughly matching how engineers talk about a mix
BANDS = [
    ("sub (20-60 Hz)", 20, 60),
    ("low (60-250 Hz)", 60, 250),
    ("low-mid (250-500 Hz)", 250, 500),
    ("mid (500-2k Hz)", 500, 2000),
    ("high-mid (2k-6k Hz)", 2000, 6000),
    ("high (6k-16k Hz)", 6000, 16000),
]

ACTIVITY_GATE_DB = -55.0     # channels quieter than this are ignored


def analyze(audio: np.ndarray, samplerate: int, names) -> MixSnapshot:
    """audio: (channels, samples), ~2-4 s window."""
    n_ch = audio.shape[0]
    channels: list[ChannelAnalysis] = []
    band_matrix = np.zeros((n_ch, len(BANDS)))

    for ch in range(n_ch):
        x = audio[ch]
        rms = float(np.sqrt(np.mean(x ** 2)))
        peak = float(np.max(np.abs(x))) if x.size else 0.0
        rms_db, peak_db = db(rms), db(peak)
        active = rms_db > ACTIVITY_GATE_DB

        centroid = 0.0
        bands = [0.0] * len(BANDS)
        if active:
            f, pxx = welch(x, fs=samplerate, nperseg=4096)
            total = float(np.sum(pxx)) or 1e-12
            centroid = float(np.sum(f * pxx) / total)
            for i, (_, lo, hi) in enumerate(BANDS):
                m = (f >= lo) & (f < hi)
                bands[i] = float(np.sum(pxx[m]) / total)
            band_matrix[ch] = bands

        channels.append(ChannelAnalysis(
            channel=ch + 1, name=names(ch + 1),
            rms_db=round(rms_db, 1), peak_db=round(peak_db, 1),
            crest_db=round(peak_db - rms_db, 1),
            centroid_hz=round(centroid, 0),
            band_energy=[round(b, 4) for b in bands],
            headroom_db=round(-peak_db, 1),
            active=active,
        ))

    masking = _masking_pairs(band_matrix, channels)
    stereo = audio[:2] if n_ch >= 2 else np.vstack([audio[0], audio[0]])
    lufs_i, lufs_s = mix_lufs(stereo, samplerate)
    return MixSnapshot(lufs_i=lufs_i, lufs_s=lufs_s,
                       channels=channels, masking=masking)


def _masking_pairs(band_matrix: np.ndarray,
                   channels: list[ChannelAnalysis]) -> list[MaskingPair]:
    """Score pairwise spectral collision between simultaneously active channels.

    Score = cosine similarity of band-energy fingerprints, weighted by how
    close the two channels are in level (near-equal levels mask hardest).
    """
    pairs: list[MaskingPair] = []
    active = [c for c in channels if c.active]
    for i, a in enumerate(active):
        for b in active[i + 1:]:
            va, vb = band_matrix[a.channel - 1], band_matrix[b.channel - 1]
            na, nb = np.linalg.norm(va), np.linalg.norm(vb)
            if na < 1e-9 or nb < 1e-9:
                continue
            sim = float(np.dot(va, vb) / (na * nb))
            level_gap = abs(a.rms_db - b.rms_db)
            weight = max(0.0, 1.0 - level_gap / 12.0)   # >12 dB apart: fine
            score = sim * weight
            if score > 0.55:
                overlap = va * vb
                band = BANDS[int(np.argmax(overlap))][0]
                pairs.append(MaskingPair(a=a.channel, b=b.channel,
                                         score=round(score, 2), band=band))
    pairs.sort(key=lambda p: -p.score)
    return pairs[:6]
