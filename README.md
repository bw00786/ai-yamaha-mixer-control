# MGX AI Mixer

AI-assisted mixing companion for the **Yamaha MGX16** digital console.
Python (FastAPI) backend + React/Vite frontend.

## What the MGX16 can and cannot do (read this first)

The MGX16 exposes a **22-in / 22-out USB MAIN audio interface (32-bit/96 kHz)**
plus a 2×2 USB SUB port — so a computer can *hear every channel* of the desk.
However, the MGX series does **not** publish a remote-control protocol: there
is no documented way for third-party software to move its faders or EQ (unlike
Yamaha's DM3/TF/CL/QL consoles, which speak RCP over TCP). Yamaha has announced
future remote operation via Elgato Stream Deck, but no open API so far.

This tool has two control modes. **Advisory** (default): the AI writes a
move sheet, you apply it on the desk. **Software-DSP takeover**: the AI's
corrections are executed in real time on the USB return path — see below.

Advisory signal flow:

```
MGX16 ──USB MAIN (22ch)──▶ Python backend ──WebSocket──▶ React UI
        multitrack audio     analysis + AI               meters, collisions,
                                                          "move sheet"
                     human applies the moves on the console
```

The AI listens, diagnoses (clipping, mud, masking, over-compression,
loudness), and writes a prioritized move sheet — you keep your hands on the
faders. A `MixerBridge` abstraction (`backend/app/mixer/bridge.py`) is the
single extension point if Yamaha ships a protocol, or if you swap in a DM3/TF.

## Software-DSP takeover (real control, today)

The USB MAIN is bidirectional (22 out AND 22 in), so the Mac can sit inside
the signal path as a giant outboard processor:

```
desk channel ──USB──▶ Python DSP (HPF → 4-band parametric EQ → comp → trim)
                             │ ~5–11 ms round trip @48 kHz / 256-sample blocks
desk channel input ◀──USB────┘   (patch channel source to USB on the MGX)
```

Click **ENGAGE** in the Software DSP panel, then **ANALYZE + APPLY**: the AI's
moves are translated into filter/compressor parameters and take effect
immediately — no hands needed. Every parameter is hard-clamped (trim −12/+6 dB,
EQ ±8/+6 dB, ratio ≤ 8:1) and a brick-wall ceiling on the return path means a
bad move can never clip the desk. The panel shows exactly what's running per
channel, with one-click clear; BYPASS hands back the untouched signal
instantly. To hear the processing you must patch the MGX channel inputs to USB
(user guide: Input Patch). Pan moves stay advisory — software panning would
fight the console's bus routing.

If you hear dropouts, raise `blocksize` in `audio/capture.py` from 256 to 512
(adds ~5 ms of latency, halves the CPU deadline pressure).

## Sunday-morning quick start (for volunteer operators)

1. Plug the MGX16's **USB TO HOST (MAIN)** into the Mac. Start the backend
   and open the app (whoever set this up can make both a double-click
   script). Top bar should read **MGX USB LIVE**.
2. Click **ENGAGE** in the Software DSP panel. On the desk, channel inputs
   should already be patched to USB (one-time setup).
3. Click **ARMED** on the **Feedback guard** AND the **Mix keeper**.
   That's it — feedback rings are caught and notched automatically, and the
   usual mix problems (hot channels, low-end mud, instruments masking each
   other) are corrected gently on their own. Everything either one does is
   listed in its log.
4. During soundcheck, press **ANALYZE + APPLY** once while the band plays —
   the AI cleans up mud, clipping, and masking on its own.
5. Anything sounds wrong? Type it: *"the pastor's mic is muddy"*, *"more
   reverb on the choir"*. To undo anything, the DSP panel shows every active
   correction with a ✕, and **BYPASS** instantly returns the desk untouched.

## Feedback killer

Say **"channel 2 has feedback, fix it"** (or "the vox is ringing / squealing /
howling"). The system grabs the last ~1.6 s of that channel, looks for the
feedback fingerprint — one very narrow spectral peak that dominates the
spectrum (≥25 dB over the median) and *stays at the same frequency* across
consecutive windows (music moves; a ring doesn't) — then drops a deep,
surgical notch exactly there (−18 dB, Q 14, ~1/10 octave wide). Verified:
kills the ring by 18 dB while a tone one octave away loses 0.07 dB. Up to 4
notches per channel; a re-ring near an existing notch re-centers it instead
of stacking. Notches live separately from your musical EQ and show in the
DSP panel as `NOTCH 2.4k Hz`.

Two honest limits: say it *while it's ringing* (the detector needs to hear
it; if the ring is gone it will tell you), and a perfectly steady synth/organ
note can mimic the fingerprint — the detector requires strong dominance to
minimize that, but don't hunt feedback during a drone chord. Feedback in a
monitor path that never passes through the computer can't be caught here.

## Mix keeper (autonomous mix-quality mode)

Arm the **Mix keeper** and the mix is checked every ~2 seconds, Sunday to
Sunday, with no operator skill required. Same trust architecture as the
feedback guard:

- **Deterministic rules only** in the autonomous loop — no LLM, no internet
  required, identical behavior every week.
- **Persistence before action**: a problem must hold for 2-3 consecutive
  analysis cycles (4-6 s). A single loud note or momentary collision never
  triggers anything.
- **Gentle, bounded corrections**: hot channel (headroom < 3 dB) -> trim
  −2 dB at a time (10 s cooldown); bright source carrying low-end mud ->
  engage HPF 100 Hz (one-shot); persistent masking (score ≥ 0.7) -> −2 dB on
  the *quieter* channel in the collision band (30 s cooldown). All moves
  pass the same hard clamps as everything else, so corrections stay bounded
  over an arbitrarily long service.
- **Global limit of 4 actions/minute** — the mix never churns.
- **The master fader stays human**: an overall-too-loud mix produces a
  logged advisory ("consider lowering the master fader"), never an
  automatic level change.
- Full audit log in the panel; every action visible in the DSP panel and
  one click to undo; disarming or bypassing stops everything instantly.

For a fully hands-off Sunday: ENGAGE + arm both the Feedback guard and the
Mix keeper. The on-demand tools (ANALYZE + APPLY, the command bar) remain
available on top at any time.

## Automatic feedback guard

Arm the **Feedback guard** and every active channel is scanned ~3× per
second. A confirmed ring is notched automatically about a second after it
starts — usually before it blooms into a full squeal. Built to be trusted
unattended:

- **Harmonic rejection**: organ pipes, pianos, voices and pads all carry a
  harmonic series (energy at 2× and 3× the fundamental); a feedback ring is
  a pure sinusoid. Any peak with audible harmonics is treated as music and
  never touched — verified in tests with a synthetic organ chord that the
  guard refuses while catching a ring on the adjacent channel.
- **Two-scan confirmation** (~0.6 s) before any action; transients can't
  trigger it.
- **Source attribution**: one ring in the room is heard by every open mic.
  The guard notches only the channel hearing it strongest — the mic driving
  the loop — and logs the bystanders ("also heard on VOCALS, PIANO") instead
  of carving the same hole into four channels.
- **Per-channel 4 s cooldown and a global limit of 6 auto-notches/minute** —
  a pathological signal cannot machine-gun notches into your mix.
- **Full audit trail**: every catch is logged (time, channel, frequency) in
  the guard panel, every notch is visible in the DSP panel and one click to
  remove, and disarming or bypassing stops all autonomous action instantly.
- Detection runs off the real-time audio thread (a full 22-channel sweep is
  ~25 ms); audio never glitches while it hunts.

Channels can be excluded from the guard via
`POST /api/autoguard {"excluded": [4]}` — e.g. keep it away from a keyboard
channel that plays long pure drones.

## Talk to the mix (natural-language effects)

With the DSP engaged, type plain English into the **Talk to the mix** bar:

- "more reverb on the vocals" / "a lot more reverb on vox" / "no reverb on vox"
- "give the guitar a slapback echo and make the kick punchier"
- "the keys are muddy" · "brighter overheads" · "snare louder"
- "more echo on channel 5"

Commands are interpreted by Claude (claude-sonnet-5) when an API key is
configured; a clause-aware rule parser handles the common phrasings offline
(it knows studio-speak: "vocals" finds a channel named VOX, "guitar" finds GTR).
Effects run per channel in the real-time chain:
HPF → EQ → comp → **reverb** (Freeverb comb/allpass network) → **delay** → trim,
all on circular delay lines (O(N) per block). Wet levels clamp at 60%,
delay feedback at 0.6 — nothing can run away into self-oscillation.

If you put reverb+delay on very many channels at once and hear dropouts,
raise `blocksize` in `audio/capture.py` to 512.

## Learning over time (RAG memory)

The mixer remembers. Every proposed move — whether it came from the LLM
advisor, the deterministic rule engine, or an autonomous AutoGuard/Mix-keeper
action — is logged to Postgres, and so is the operator's response (approve,
reject, or modify). Over a season of services this builds a per-channel,
per-issue track record:

```
VOX  — HPF 100 Hz        approved 21/23  (91%)  -> trusted
GTR  — eq_cut low-mid    approved 2/9    (22%)  -> avoid
KEYS — trim -2 dB        approved 6/6    (100%) -> trusted
```

That history is fed back into both advice engines as **retrieval-augmented
context (RAG)**:

- **LLM advisor**: a short natural-language summary of what has historically
  been approved/rejected for the currently active channels is injected into
  the system prompt ("Learned history for this room"). The model leans into
  moves that worked here before and de-prioritizes ones repeatedly rejected —
  unless the current snapshot shows a clearly different problem.
- **Rule engine**: the same history biases priority deterministically —
  trusted moves bubble up, repeatedly-rejected moves sink and get flagged
  ("often rejected here — double-check").

Nothing is ever silently dropped and none of this overrides the hard DSP
clamps — RAG only shapes *which* moves get suggested and how confidently; the
human still sees and decides everything. A move/issue pair needs at least
**3 observations** before it influences anything (below that it's just
"learning"); ≥70% approval reads as *trusted*, ≤30% as *avoid*.

Autonomous actions (Feedback guard, Mix keeper) already gate on persistence
and cooldowns before firing, so a completed auto-action counts as an implicit
*approved* observation — but it's logged as its own row (`source=autoguard` /
`automix`) so the audit trail stays honest about AI-only vs. human-approved.

The learned table and recent activity are surfaced in the UI's **Learning**
panel and over `GET /api/memory`. Operator decisions are recorded via
`POST /api/moves/{id}/decision {"decision": "approved|rejected|modified"}`.

Memory is **entirely optional**: if no Postgres is reachable the whole module
degrades to a harmless no-op — the advisor still works, it just doesn't
remember across sessions. Point it at a database with `MGX_MEMORY_DB_URL`
(default `postgresql://postgres:abc123@127.0.0.1:5432/mgx_mixer`); the schema
is created automatically on first connect.

## Features

- Real-time LED-segment metering for all USB channels (~12 Hz over WebSocket)
- Continuous analysis: per-channel spectral fingerprint, headroom, crest
  factor, spectral centroid, short-term/integrated LUFS of the mix
- Masking detection: pairwise spectral-collision scoring between
  simultaneously active channels, localized to the offending band
- Software-DSP takeover: per-channel HPF, 4-band parametric EQ (RBJ
  biquads), compressor, and trim, applied on the USB return path with global
  bypass and per-channel clear — verified by a measured test suite (filters
  hit their designed dB targets; 22 fully-loaded channels process in ~4.8 ms
  per 5.3 ms block)
- Natural-language mix commands ("more reverb on the vocals") with per-
  channel reverb and delay, LLM-interpreted with an offline rule fallback
- Feedback killer: "channel 2 has feedback" → ring detection and a surgical
  notch at the exact frequency; plus an autonomous **Feedback guard** with
  harmonic rejection, two-scan confirmation, cooldowns, and a full audit log
- AI move sheet: press **Analyze** → concrete console moves
  (`gain −4 dB on CH03`, `cut 3 dB at 300 Hz on GTR`, `HPF 100 Hz on VOX`)
  with reasons and priorities, rendered as a checklist
- Two advice engines: **Claude** (`claude-sonnet-5` via the Anthropic API) or
  a deterministic rule engine — the LLM's output is schema-validated and
  clamped before it reaches the UI (LLM proposes, deterministic code decides)
- Learning memory (RAG): every suggestion and the operator's approve/reject/
  modify decision is logged to Postgres, building a per-channel track record
  that is fed back into both the LLM prompt and the rule engine to bias which
  moves get suggested — surfaced in a **Learning** panel, degrades to a no-op
  if no database is configured
- No hardware? The backend auto-falls back to an 8-channel simulator so the
  whole stack runs anywhere

## Setup

Backend (Python 3.11+):

```bash
cd backend
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
# Claude (Anthropic API) powers the advisor and command interpreter:
#      export ANTHROPIC_API_KEY=sk-ant-...
#      export CLAUDE_MODEL=claude-sonnet-5   # optional, this is the default
# If no API key is set, the advisor silently falls back to rules.

# Optional — learning memory (RAG). Point at any Postgres to persist the
# per-channel approve/reject track record across services; omit it and the
# learning module is a harmless no-op.
#   export MGX_MEMORY_DB_URL=postgresql://postgres:abc123@127.0.0.1:5432/mgx_mixer

uvicorn app.main:app --reload --port 8000
```

Frontend:

```bash
cd frontend
npm install
npm run dev            # http://localhost:5173 (proxies /api and /ws to :8000)
```

Connect the MGX16's **USB TO HOST (MAIN)** port. On macOS it appears as a
core-audio device; the backend picks any input device whose name contains
"MGX". Name your channels via:

```bash
curl -X POST localhost:8000/api/channel-names \
  -H 'content-type: application/json' \
  -d '{"1": "KICK", "2": "SNARE", "3": "BASS", "4": "GTR", "5": "VOX"}'
```

## Repo layout

```
backend/app/
  audio/capture.py     USB capture + full-duplex DSP return path + simulator
  dsp/chain.py         per-channel HPF/EQ/comp/trim (stateful, vectorized)
  dsp/controller.py    all chains + AI-move → parameter translation, clamps
  dsp/effects.py       reverb + delay on O(N) circular delay lines
  dsp/feedback.py      feedback fingerprint detector (+ harmonic rejection)
  dsp/autoguard.py     autonomous guard: confirmation, cooldowns, audit log
  dsp/automix.py       mix keeper: persistence-gated auto corrections
  ai/commander.py      natural-language command → effect ops (LLM + rules)
  audio/metering.py    peak/RMS meters, LUFS via pyloudnorm
  analysis/engine.py   band energies, masking pairs, gain staging
  ai/advisor.py        Claude advisor + rule fallback, schema-gated, RAG-primed
  memory/store.py      Postgres learning store: logs moves + decisions, RAG
  mixer/bridge.py      control abstraction (advisory today; RCP stub)
  main.py              FastAPI + WebSocket broadcaster
frontend/src/
  App.jsx              console surface layout
  components/          ChannelStrip, SegmentMeter, SuggestionPanel, MaskingList
```

## Roadmap ideas

- Control via `YamahaRCPBridge` when pointed at a DM3/TF console
- Scene-aware advice (soundcheck vs. show), per-genre target curves
- Record-and-compare: A/B the mix before/after applying a move sheet
