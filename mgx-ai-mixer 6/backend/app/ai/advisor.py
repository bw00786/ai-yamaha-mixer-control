"""AI mix advisor — Anthropic Claude edition.

Turns a MixSnapshot into concrete, prioritized moves an operator can apply
on the MGX16 (fader, EQ, HPF, comp, pan). Two engines:

  1. LLM engine  — Claude (claude-sonnet-5) via the Anthropic API.
  2. Rule engine — deterministic heuristics; always available, also the
                   safety fallback if the LLM call fails.

Configure with env vars:
  ANTHROPIC_API_KEY   your Anthropic API key (required for the LLM engine)
  CLAUDE_MODEL        model id, defaults to claude-sonnet-5

Design principle (LLM proposes, deterministic code decides): every LLM move
is validated against the schema and clamped before it reaches the UI. With no
API key the advisor silently falls back to the deterministic rule engine.
"""
from __future__ import annotations

import json
import os

from ..models import AdvisorResponse, MixMove, MixSnapshot
from ..memory.store import MemoryStore

MODEL = os.environ.get("CLAUDE_MODEL", "claude-sonnet-5")
API_KEY = os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("CLAUDE_API_KEY")

SYSTEM = """You are a live-sound mixing engineer assisting an operator on a
Yamaha MGX16 digital console. You receive a JSON snapshot of the current mix
(per-channel levels, spectral bands, masking pairs, loudness). Respond ONLY
with JSON: {"summary": str, "moves": [{"channel": int, "channel_name": str,
"action": "fader|eq_cut|eq_boost|hpf|pan|comp|gain", "param": str,
"amount": str, "reason": str, "priority": 1-5}]}.
Max 6 moves. Be conservative: cuts before boosts, 2-4 dB EQ moves,
prioritize clip prevention and masking. No markdown, no prose outside JSON.

You may also be given a "Learned history for this room" section below,
built from past Sundays' operator approvals/rejections of your own past
suggestions. Treat it as retrieval-augmented context: lean into moves noted
as historically approved, and avoid or de-prioritize moves noted as
historically rejected for that channel, unless the current snapshot shows a
clear, different problem."""


def suggest(snapshot: MixSnapshot, memory: MemoryStore | None = None) -> AdvisorResponse:
    try:
        return _llm_suggest(snapshot, memory)
    except Exception:
        return _rule_suggest(snapshot, memory)


# ---------------------------------------------------------------------- LLM
def _llm_suggest(snapshot: MixSnapshot, memory: MemoryStore | None) -> AdvisorResponse:
    from anthropic import Anthropic
    client = Anthropic(api_key=API_KEY)

    state = snapshot.model_dump()
    # trim inactive channels to keep the prompt small
    state["channels"] = [c for c in state["channels"] if c["active"]]

    system = SYSTEM
    if memory is not None:
        names = [c["name"] for c in state["channels"] if c.get("name")]
        history = memory.channel_memory_text(names)
        if history:
            system = f"{SYSTEM}\n\nLearned history for this room:\n{history}"

    resp = client.messages.create(
        model=MODEL,
        max_tokens=1600,
        temperature=0.3,
        system=system,
        messages=[
            {"role": "user", "content": json.dumps(state)},
        ],
    )
    text = "".join(b.text for b in resp.content
                   if getattr(b, "type", None) == "text").strip()
    # tolerate a stray markdown fence around the JSON
    text = text.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    data = json.loads(text)

    moves = []
    for m in data.get("moves", [])[:6]:
        try:
            moves.append(MixMove(**m))          # schema validation gate
        except Exception:
            continue
    return AdvisorResponse(summary=str(data.get("summary", ""))[:400],
                           moves=moves, source="llm")


# -------------------------------------------------------------------- rules
def _rule_suggest(snapshot: MixSnapshot, memory: MemoryStore | None = None) -> AdvisorResponse:
    moves: list[MixMove] = []
    notes: list[str] = []

    for c in snapshot.channels:
        if not c.active:
            continue
        # 1. Clip / headroom protection — always first
        if c.headroom_db < 3.0:
            moves.append(MixMove(
                channel=c.channel, channel_name=c.name, action="gain",
                param="input gain", amount="-4 dB",
                reason=f"only {c.headroom_db:.0f} dB headroom — clipping risk",
                priority=1))
        # 2. Low-frequency housekeeping on clearly non-bass sources
        if c.centroid_hz > 800 and c.band_energy and c.band_energy[1] > 0.25:
            moves.append(MixMove(
                channel=c.channel, channel_name=c.name, action="hpf",
                param="100 Hz", amount="engage",
                reason="high-centroid source carrying low-end mud",
                priority=2))
        # 3. Over-compressed / flat dynamics hint
        if 0 < c.crest_db < 6:
            moves.append(MixMove(
                channel=c.channel, channel_name=c.name, action="comp",
                param="threshold", amount="raise / reduce ratio",
                reason=f"crest factor {c.crest_db:.0f} dB — sounds squashed",
                priority=4))

    # 4. Masking → cut the lower-priority channel in the collision band
    for p in snapshot.masking[:3]:
        a = snapshot.channels[p.a - 1]
        b = snapshot.channels[p.b - 1]
        quieter = a if a.rms_db < b.rms_db else b
        other = b if quieter is a else a
        moves.append(MixMove(
            channel=quieter.channel, channel_name=quieter.name,
            action="eq_cut", param=p.band, amount="-3 dB",
            reason=f"masking with {other.name} (score {p.score})",
            priority=2))
        notes.append(f"{a.name}/{b.name} colliding in {p.band}")

    # 5. Overall loudness sanity
    if snapshot.lufs_s is not None and snapshot.lufs_s > -12:
        notes.append(f"mix running hot at {snapshot.lufs_s} LUFS short-term")

    # 6. Bias with learned history (RAG over past approvals/rejections):
    # bump trusted moves earlier, push repeatedly-rejected ones later and
    # flag them so the operator can see why. Nothing is ever silently
    # dropped — the human still sees and decides everything.
    if memory is not None:
        for m in moves:
            verdict = memory.bias_for(m.channel_name, m.action, m.param)
            if verdict == "trusted":
                m.priority = max(1, m.priority - 1)
                m.reason = f"{m.reason} (usually approved here)"
            elif verdict == "avoid":
                m.priority = min(5, m.priority + 2)
                m.reason = f"{m.reason} (often rejected here — double-check)"

    moves.sort(key=lambda m: m.priority)
    summary = ("Mix looks clean." if not moves else
               f"{len(moves)} suggested moves. " + "; ".join(notes[:3]))
    return AdvisorResponse(summary=summary, moves=moves[:6], source="rules")
