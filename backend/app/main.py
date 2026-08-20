"""MGX AI Mixer — FastAPI backend (with software-DSP takeover).

Run:  uvicorn app.main:app --reload --port 8000

WebSocket /ws streams:
  { "type": "meters",   "data": [ChannelMeter...] }        ~12 Hz
  { "type": "analysis", "data": MixSnapshot }               ~0.5 Hz
  { "type": "dsp",      "data": DSP state }                 on change + with analysis
REST:
  GET  /api/status
  POST /api/advise                 -> suggestions only (advisory)
  POST /api/advise?apply=true      -> suggestions applied to the DSP engine
  POST /api/moves/apply            -> apply a single move (body: MixMove)
  GET  /api/dsp                    -> full DSP state
  POST /api/dsp/engage             -> {"engage": bool}  master takeover on/off
  POST /api/dsp/reset              -> {"channel": int|null}  clear processing
  POST /api/command                -> {"text": "more reverb on the vocals"}
                                      NL command -> effect ops, executed
  POST /api/autoguard              -> {"enabled": bool, "excluded": [ch...]}
                                      autonomous feedback suppression
  POST /api/automix                -> {"enabled": bool}
                                      autonomous mix-quality keeper
  POST /api/channel-names          -> set friendly channel names
  POST /api/moves/{id}/decision    -> {"decision": "approved|rejected|modified"}
                                      record the operator's verdict on a past
                                      suggestion (learning signal)
  GET  /api/memory                 -> learned per-channel preferences +
                                      recent suggestion/decision history
"""
from __future__ import annotations

import asyncio
import json

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from .audio.capture import CaptureEngine
from .audio.metering import meters
from .analysis.engine import analyze
from .ai.advisor import suggest
from .ai.commander import interpret
from .dsp.controller import DSPController
from .dsp.autoguard import AutoGuard
from .dsp.automix import AutoMixer
from .dsp.presets import apply_presets
from .mixer.bridge import AdvisoryBridge, SoftwareDSPBridge
from .models import MixSnapshot, MixMove
from .memory.store import MemoryStore

app = FastAPI(title="MGX AI Mixer")
app.add_middleware(CORSMiddleware, allow_origins=["*"],
                   allow_methods=["*"], allow_headers=["*"])

engine = CaptureEngine()
dsp = DSPController(engine.n_channels, engine.samplerate)
apply_presets(dsp)                            # permanent per-channel corrections
guard = AutoGuard(dsp, engine.samplerate)
automix = AutoMixer(dsp)
engine.processor = dsp.process_block          # duplex takeover path
advisory = AdvisoryBridge()
software = SoftwareDSPBridge(dsp)
memory = MemoryStore()                        # Postgres-backed learning (no-op if unreachable)
latest_snapshot: MixSnapshot = MixSnapshot()
clients: set[WebSocket] = set()


@app.on_event("startup")
async def startup():
    engine.start()
    asyncio.create_task(_meter_loop())
    asyncio.create_task(_analysis_loop())
    asyncio.create_task(_autoguard_loop())


@app.on_event("shutdown")
async def shutdown():
    engine.stop()


def _json_default(o):
    raise TypeError(f"not JSON serializable: {type(o)}")


def _sanitize(obj):
    """Recursively replace NaN/Infinity with None — the standard json module
    emits these as bare tokens (Infinity/-Infinity/NaN), which is not valid
    JSON and breaks strict JSON.parse() in the browser."""
    if isinstance(obj, float):
        return obj if obj == obj and obj not in (float("inf"), float("-inf")) else None
    if isinstance(obj, dict):
        return {k: _sanitize(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_sanitize(v) for v in obj]
    return obj


async def _broadcast(msg: dict):
    dead = []
    payload = json.dumps(_sanitize(msg), default=_json_default)
    for ws in clients:
        try:
            await ws.send_text(payload)
        except Exception:
            dead.append(ws)
    for ws in dead:
        clients.discard(ws)


async def _broadcast_dsp():
    data = dsp.state()
    data["autoguard"] = guard.state()
    data["automix"] = automix.state()
    await _broadcast({"type": "dsp", "data": data})


async def _meter_loop():
    while True:
        audio = engine.latest(0.1)
        data = [m.model_dump() for m in meters(audio, engine.name_of)]
        await _broadcast({"type": "meters", "data": data})
        await asyncio.sleep(1 / 12)


async def _analysis_loop():
    global latest_snapshot
    while True:
        audio = engine.latest(3.0)
        snap = await asyncio.to_thread(analyze, audio, engine.samplerate,
                                       engine.name_of)
        latest_snapshot = snap
        if automix.enabled and not dsp.master_bypass:
            actions = await asyncio.to_thread(automix.scan, snap, engine.name_of)
            for a in actions:
                if not a.get("advisory"):
                    await asyncio.to_thread(
                        memory.log_auto_action,
                        channel_name=a["name"], action="automix",
                        param=a["action"], amount=a["action"], reason=a["reason"],
                        source="automix")
        await _broadcast({"type": "analysis", "data": snap.model_dump()})
        await _broadcast_dsp()
        await asyncio.sleep(2.0)


async def _autoguard_loop():
    while True:
        if guard.enabled and not dsp.master_bypass:
            audio = engine.latest(1.1)
            catches = await asyncio.to_thread(guard.scan, audio,
                                              engine.name_of)
            for c in catches:
                await asyncio.to_thread(
                    memory.log_auto_action,
                    channel_name=c["name"], action="notch",
                    param=f"{c['freq']:.0f} Hz", amount=f"{c['freq']:.0f} Hz",
                    reason=f"feedback ring, dominance {c['dominance_db']:.0f} dB",
                    source="autoguard")
            if catches:
                await _broadcast_dsp()
        await asyncio.sleep(0.3)


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await ws.accept()
    clients.add(ws)
    try:
        first = dsp.state(); first["autoguard"] = guard.state()
        first["automix"] = automix.state()
        await ws.send_text(json.dumps(_sanitize({"type": "dsp", "data": first})))
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        clients.discard(ws)


@app.get("/api/status")
def status():
    return {
        "device": "simulated" if engine.simulated else "Yamaha MGX (USB MAIN)",
        "channels": engine.n_channels,
        "samplerate": engine.samplerate,
        "duplex": engine.duplex,
        "control_mode": "software-dsp" if not dsp.master_bypass else "advisory",
    }


@app.post("/api/advise")
async def advise(apply: bool = False):
    resp = await asyncio.to_thread(suggest, latest_snapshot, memory)
    bridge = software if apply else advisory
    enriched = []
    for m in resp.moves:
        result = bridge.apply(m)
        suggestion_id = await asyncio.to_thread(
            memory.log_suggestion, channel=m.channel, channel_name=m.channel_name,
            action=m.action, param=m.param, amount=m.amount, reason=m.reason,
            priority=m.priority, source=resp.source)
        # "Analyze + apply" is an explicit one-click approval by the
        # operator for every move it applies — record that immediately so
        # the learning loop doesn't wait on a decision that will never come
        # separately.
        if apply and result.get("applied") and suggestion_id is not None:
            await asyncio.to_thread(memory.record_decision, suggestion_id, "approved")
        e = m.model_dump()
        e["instruction"] = result["instruction"]
        e["applied"] = result.get("applied", False)
        e["detail"] = result.get("detail", "")
        e["suggestion_id"] = suggestion_id
        enriched.append(e)
    if apply:
        await _broadcast_dsp()
    return {"summary": resp.summary, "source": resp.source,
            "applied": apply, "moves": enriched}


class ApplyMoveBody(MixMove):
    suggestion_id: int | None = None


@app.post("/api/moves/apply")
async def apply_move(body: ApplyMoveBody):
    move = MixMove(**body.model_dump(exclude={"suggestion_id"}))
    result = software.apply(move)
    if body.suggestion_id is not None and result.get("applied"):
        await asyncio.to_thread(memory.record_decision, body.suggestion_id, "approved")
    await _broadcast_dsp()
    return result


class DecisionBody(BaseModel):
    decision: str    # "approved" | "rejected" | "modified"
    note: str = ""


@app.post("/api/moves/{suggestion_id}/decision")
async def move_decision(suggestion_id: int, body: DecisionBody):
    if body.decision not in ("approved", "rejected", "modified"):
        return {"ok": False, "detail": "decision must be approved|rejected|modified"}
    ok = await asyncio.to_thread(memory.record_decision, suggestion_id,
                                 body.decision, body.note)
    return {"ok": ok}


@app.get("/api/memory")
def memory_summary():
    """Learned per-channel preferences (approval rates) and recent activity,
    for the Learning panel — the AI's track record for this room over time."""
    return {
        "available": memory.available,
        "preferences": memory.summary(),
        "recent": memory.recent_activity(40),
    }


@app.get("/api/dsp")
def dsp_state():
    return dsp.state()


class EngageBody(BaseModel):
    engage: bool


@app.post("/api/dsp/engage")
async def dsp_engage(body: EngageBody):
    dsp.set_master_bypass(not body.engage)
    await _broadcast_dsp()
    return dsp.state()


class ResetBody(BaseModel):
    channel: int | None = None


@app.post("/api/dsp/reset")
async def dsp_reset(body: ResetBody):
    if body.channel:
        dsp.reset_channel(body.channel)
    else:
        dsp.reset_all()
    apply_presets(dsp)                        # restore permanent corrections
    await _broadcast_dsp()
    return dsp.state()


class CommandBody(BaseModel):
    text: str


@app.post("/api/command")
async def command(body: CommandBody):
    channels = [{"channel": i + 1, "name": engine.name_of(i + 1)}
                for i in range(engine.n_channels)]
    def _audio_for(ch: int):
        return engine.latest(1.6)[ch - 1]
    result = await asyncio.to_thread(interpret, body.text, channels, dsp,
                                     _audio_for, engine.samplerate)
    await _broadcast_dsp()
    return result.model_dump()


class AutoGuardBody(BaseModel):
    enabled: bool | None = None
    excluded: list[int] | None = None


@app.post("/api/autoguard")
async def autoguard(body: AutoGuardBody):
    guard.configure(enabled=body.enabled, excluded=body.excluded)
    await _broadcast_dsp()
    return guard.state()


class AutoMixBody(BaseModel):
    enabled: bool


@app.post("/api/automix")
async def automix_config(body: AutoMixBody):
    automix.configure(enabled=body.enabled)
    await _broadcast_dsp()
    return automix.state()


@app.post("/api/channel-names")
def channel_names(names: dict[int, str]):
    engine.channel_names.update({int(k): v for k, v in names.items()})
    return {"ok": True}
