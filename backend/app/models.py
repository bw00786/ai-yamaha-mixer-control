"""Shared data models for the MGX AI Mixer backend."""
from __future__ import annotations

from typing import Literal, Optional
from pydantic import BaseModel, Field


class ChannelMeter(BaseModel):
    channel: int                      # 1-based, matches MGX USB MAIN channel order
    name: str = ""
    peak_db: float = -90.0            # instantaneous peak, dBFS
    rms_db: float = -90.0             # short-term RMS, dBFS
    clip: bool = False


class ChannelAnalysis(BaseModel):
    channel: int
    name: str = ""
    rms_db: float = -90.0
    peak_db: float = -90.0
    crest_db: float = 0.0             # peak - rms (dynamics indicator)
    centroid_hz: float = 0.0          # spectral centroid
    band_energy: list[float] = []     # normalized energy in analysis bands
    headroom_db: float = 90.0         # distance of peak from 0 dBFS
    active: bool = False


class MaskingPair(BaseModel):
    a: int
    b: int
    score: float                      # 0..1, higher = more spectral collision
    band: str                         # e.g. "low-mid (250-500 Hz)"


class MixSnapshot(BaseModel):
    lufs_i: Optional[float] = None    # integrated loudness of the stereo mix
    lufs_s: Optional[float] = None    # short-term
    channels: list[ChannelAnalysis] = []
    masking: list[MaskingPair] = []


MoveAction = Literal["fader", "eq_cut", "eq_boost", "hpf", "pan", "comp", "gain"]


class MixMove(BaseModel):
    """One concrete, human-applicable move on the MGX16 console."""
    channel: int
    channel_name: str = ""
    action: MoveAction
    param: str = ""                   # e.g. "300 Hz", "L30", "-4 dB"
    amount: str = ""                  # e.g. "-3 dB", "ratio 3:1"
    reason: str = ""
    priority: int = Field(3, ge=1, le=5)   # 1 = do first


class AdvisorResponse(BaseModel):
    summary: str = ""
    moves: list[MixMove] = []
    source: Literal["llm", "rules"] = "rules"
