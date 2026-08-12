"""Natural-language mix commander.

Turns free text like "more reverb on the vocals" or "add a slap delay to the
guitar and make the kick punchier" into structured effect operations, then
executes them through the DSPController's clamped setters.

Two interpreters, same pattern as the advisor:
  1. Qwen3.6 (any OpenAI-compatible endpoint) — rich language understanding.
  2. Regex fallback — handles the common phrasings offline.

LLM proposes, deterministic code decides: ops are schema-validated and every
parameter passes through the same hard clamps as everything else.
"""
from __future__ import annotations

import json
import os
import re
from typing import Literal, Optional

from pydantic import BaseModel

from ..dsp.controller import DSPController
from ..dsp.feedback import detect_feedback

MODEL = os.environ.get("QWEN_MODEL", "qwen3.6")
BASE_URL = os.environ.get("QWEN_BASE_URL", "http://localhost:11434/v1")
API_KEY = os.environ.get("QWEN_API_KEY", "ollama")

_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL)


class EffectOp(BaseModel):
    channel: int
    effect: Literal["reverb", "delay", "eq", "hpf", "comp", "trim",
                    "feedback_fix"]
    # reverb: wet (0-0.6), size (0-1) | delay: wet, time_ms, feedback
    # eq: freq, gain_db | hpf: freq or off | comp: threshold_db, ratio | trim: delta_db
    params: dict = {}


class CommandResult(BaseModel):
    understood: str = ""
    ops: list[EffectOp] = []
    source: Literal["llm", "rules"] = "rules"
    errors: list[str] = []


SYSTEM = """You translate a sound engineer's natural-language request into mix
operations. You get the request plus a channel list [{channel, name}]. Respond
ONLY with JSON: {"understood": str, "ops": [{"channel": int,
"effect": "reverb|delay|eq|hpf|comp|trim", "params": {...}}]}.
Param keys: reverb {wet 0-0.6, size 0-1}; delay {wet 0-0.6, time_ms 50-800,
feedback 0-0.6}; eq {freq, gain_db -8..6}; hpf {freq 40-400} or {off: true};
comp {threshold_db, ratio}; trim {delta_db}; feedback_fix {} (use for any
complaint about feedback, ringing, squealing, howling, or whistling — the
system finds and notches the frequency itself).
"more/a bit" => wet +0.1, "a lot" => +0.2, "less" => -0.1, "no/remove" => wet 0.
Match channels by name; if a named source has no channel, skip it and say so
in "understood". No markdown, no prose outside JSON."""


def interpret(text: str, channels: list[dict], dsp: DSPController,
              audio_provider=None, samplerate: float = 48000.0) -> CommandResult:
    """audio_provider: callable(channel_1based) -> recent 1-D audio array,
    used by the feedback killer to find the ringing frequency."""
    try:
        result = _llm_interpret(text, channels)
    except Exception:
        result = _rule_interpret(text, channels, dsp)
    _execute(result, dsp, audio_provider, samplerate)
    return result


# ------------------------------------------------------------------ execute
def _execute(result: CommandResult, dsp: DSPController,
             audio_provider=None, samplerate: float = 48000.0):
    extra: list[str] = []
    for op in result.ops:
        ch = op.channel - 1
        if not (0 <= ch < dsp.n_channels):
            result.errors.append(f"channel {op.channel} out of range")
            continue
        chain = dsp.chains[ch]
        p = op.params
        try:
            if op.effect == "reverb":
                chain.set_reverb(wet=p.get("wet"), size=p.get("size"))
            elif op.effect == "delay":
                chain.set_delay(wet=p.get("wet"), time_ms=p.get("time_ms"),
                                feedback=p.get("feedback"))
            elif op.effect == "eq":
                chain.set_eq_band(float(p["freq"]), float(p["gain_db"]))
            elif op.effect == "hpf":
                if p.get("off"):
                    chain.set_hpf(False)
                else:
                    chain.set_hpf(True, float(p.get("freq", 100)))
            elif op.effect == "comp":
                chain.set_comp(True, threshold_db=p.get("threshold_db"),
                               ratio=p.get("ratio"))
            elif op.effect == "trim":
                chain.set_trim(float(p.get("delta_db", 0)))
            elif op.effect == "feedback_fix":
                if audio_provider is None:
                    result.errors.append(
                        f"ch {op.channel}: no audio available to hunt feedback")
                    continue
                hit = detect_feedback(audio_provider(op.channel), samplerate)
                if hit is None:
                    extra.append(
                        f"no sustained ring found on ch {op.channel} right now "
                        "— say it again while it's ringing")
                else:
                    f = chain.set_notch(hit["freq"])
                    extra.append(
                        f"notched {f:.0f} Hz on ch {op.channel} "
                        f"(peak was {hit['dominance_db']:.0f} dB over the mix)")
        except Exception as e:
            result.errors.append(f"ch {op.channel} {op.effect}: {e}")
    if extra:
        result.understood = "; ".join(
            ([result.understood] if result.understood else []) + extra)


# ---------------------------------------------------------------------- LLM
def _llm_interpret(text: str, channels: list[dict]) -> CommandResult:
    from openai import OpenAI
    client = OpenAI(base_url=BASE_URL, api_key=API_KEY)
    resp = client.chat.completions.create(
        model=MODEL, max_tokens=900, temperature=0.2,
        messages=[
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": json.dumps(
                {"request": text, "channels": channels})},
        ],
    )
    out = resp.choices[0].message.content or ""
    out = _THINK_RE.sub("", out).strip()
    out = out.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    data = json.loads(out)
    ops = []
    for o in data.get("ops", [])[:10]:
        try:
            ops.append(EffectOp(**o))
        except Exception:
            continue
    return CommandResult(understood=str(data.get("understood", ""))[:300],
                         ops=ops, source="llm")


# -------------------------------------------------------------------- rules
_MORE = re.compile(r"\b(more|add|some|bit|little|touch)\b", re.I)
_LOTS = re.compile(r"\b(a lot|lots|much more|heavy|drench|soak)\b", re.I)
_LESS = re.compile(r"\b(less|reduce|lower|too much)\b", re.I)
_OFF = re.compile(r"\b(no|remove|kill|off|dry)\b", re.I)


def _delta(text: str) -> float:
    if _OFF.search(text):
        return -1.0        # sentinel: turn off
    if _LOTS.search(text):
        return 0.2
    if _LESS.search(text):
        return -0.1
    return 0.1             # default "more"


# Common studio-speak -> scribble-strip abbreviations
_ALIASES: dict[str, set[str]] = {
    "vocals": {"vox", "vocal", "voc", "lead"},
    "vocal": {"vox", "voc", "lead"},
    "voice": {"vox", "voc"},
    "singer": {"vox", "voc", "lead"},
    "guitar": {"gtr", "git", "gt"},
    "keyboard": {"keys", "kbd", "key", "synth"},
    "keys": {"kbd", "key", "synth"},
    "drums": {"drm", "kit"},
    "kick": {"kck", "bd"},
    "snare": {"snr", "sn"},
    "overheads": {"oh", "ohl", "ohr"},
    "bass": {"bs", "bass"},
}


def _find_channels(text: str, channels: list[dict]) -> list[int]:
    low = text.lower()
    words = re.findall(r"[a-z]+", low)
    hits = []
    for c in channels:
        name = (c.get("name") or "").lower().strip()
        if not name or name.startswith("ch "):
            continue
        matched = name in low
        if not matched:
            for w in words:
                if len(w) < 3:
                    continue
                stems = {w} | _ALIASES.get(w, set())
                if any(name.startswith(s[:4]) or s.startswith(name)
                       for s in stems if len(s) >= 2):
                    matched = True
                    break
        if matched:
            hits.append(c["channel"])
    for m in re.finditer(r"\bch(?:annel)?\s*(\d+)\b", low):
        hits.append(int(m.group(1)))
    return sorted(set(hits))


def _rule_interpret(text: str, channels: list[dict],
                    dsp: DSPController) -> CommandResult:
    # Clause-scoped: "slapback on the guitar and punch on the kick" must not
    # cross-apply. Split on conjunctions; a clause with effects but no channel
    # inherits the previous clause's channels ("more reverb on vox and less
    # delay too").
    clauses = [c.strip() for c in
               re.split(r"\band\b|\bthen\b|[,;.]", text.lower()) if c.strip()]
    any_channel = False
    ops: list[EffectOp] = []
    said: list[str] = []
    prev_chs: list[int] = []

    for clause in clauses:
        chs = _find_channels(clause, channels) or prev_chs
        if not chs:
            continue
        any_channel = True
        prev_chs = chs
        _clause_ops(clause, chs, dsp, ops, said)

    if not any_channel:
        return CommandResult(
            understood="Couldn't match a channel — name channels first "
                       "(e.g. VOX, KICK) or say 'channel 3'.",
            ops=[], source="rules",
            errors=["no channel matched"])
    if not ops:
        return CommandResult(
            understood="Matched channels but no known effect keyword "
                       "(try: reverb, delay/echo, brighter, muddy, punchier, "
                       "louder/quieter).",
            ops=[], source="rules", errors=["no effect matched"])
    return CommandResult(understood="; ".join(said), ops=ops, source="rules")


def _clause_ops(low: str, chs: list[int], dsp: DSPController,
                ops: list[EffectOp], said: list[str]):
    d = _delta(low)
    for ch in chs:
        chain = dsp.chains[ch - 1]
        if re.search(r"\b(feedback|feeding back|ringing|ring|squeal|howl|whistl)", low):
            ops.append(EffectOp(channel=ch, effect="feedback_fix", params={}))
            said.append(f"hunting feedback on ch {ch}")
        if re.search(r"\b(reverb|verb|wash|room|hall|space|wet)\b", low):
            base = chain.reverb.wet if chain.reverb.on else 0.0
            wet = 0.0 if d < -0.5 else max(0.0, base + d)
            ops.append(EffectOp(channel=ch, effect="reverb", params={"wet": wet}))
            said.append(f"reverb wet -> {wet:.2f} on ch {ch}")
        if re.search(r"\b(delay|echo|slap|slapback)\b", low):
            base = chain.delay.wet if chain.delay.on else 0.0
            wet = 0.0 if d < -0.5 else max(0.0, base + d)
            time_ms = 120.0 if "slap" in low else 320.0
            ops.append(EffectOp(channel=ch, effect="delay",
                                params={"wet": wet, "time_ms": time_ms}))
            said.append(f"delay wet -> {wet:.2f} on ch {ch}")
        if re.search(r"\b(bright|brighter|air|sparkle)\b", low):
            ops.append(EffectOp(channel=ch, effect="eq",
                                params={"freq": 8000, "gain_db": 2.0}))
            said.append(f"+2 dB @ 8 kHz on ch {ch}")
        if re.search(r"\b(dark|darker|dull|harsh|less bright)\b", low):
            ops.append(EffectOp(channel=ch, effect="eq",
                                params={"freq": 6000, "gain_db": -2.0}))
            said.append(f"-2 dB @ 6 kHz on ch {ch}")
        if re.search(r"\b(mud|muddy|boomy|boxy)\b", low):
            ops.append(EffectOp(channel=ch, effect="eq",
                                params={"freq": 300, "gain_db": -3.0}))
            said.append(f"-3 dB @ 300 Hz on ch {ch}")
        if re.search(r"\b(punch|punchy|punchier|tighter)\b", low):
            ops.append(EffectOp(channel=ch, effect="comp",
                                params={"threshold_db": -16, "ratio": 4.0}))
            said.append(f"comp 4:1 on ch {ch}")
        if re.search(r"\b(louder|up|boost the level)\b", low):
            ops.append(EffectOp(channel=ch, effect="trim", params={"delta_db": 2.0}))
            said.append(f"trim +2 dB on ch {ch}")
        if re.search(r"\b(quieter|down|too loud)\b", low):
            ops.append(EffectOp(channel=ch, effect="trim", params={"delta_db": -2.0}))
            said.append(f"trim -2 dB on ch {ch}")
