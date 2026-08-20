import React, { useEffect, useRef, useState } from "react";
import {
  connectStream, fetchStatus, requestAdvice, requestAdviceAndApply,
  applyMove, setDspEngaged, resetDsp, sendCommand, setAutoGuard, setAutoMix,
  recordDecision, fetchMemory,
} from "./api.js";
import ChannelStrip from "./components/ChannelStrip.jsx";
import SuggestionPanel from "./components/SuggestionPanel.jsx";
import MaskingList from "./components/MaskingList.jsx";
import DSPPanel from "./components/DSPPanel.jsx";
import CommandBar from "./components/CommandBar.jsx";
import AutoGuardPanel from "./components/AutoGuardPanel.jsx";
import AutoMixPanel from "./components/AutoMixPanel.jsx";
import LearningPanel from "./components/LearningPanel.jsx";

export default function App() {
  const [meters, setMeters] = useState([]);
  const [analysis, setAnalysis] = useState(null);
  const [status, setStatus] = useState(null);
  const [link, setLink] = useState("offline");
  const [advice, setAdvice] = useState(null);
  const [thinking, setThinking] = useState(false);
  const [done, setDone] = useState(new Set());
  const [dsp, setDsp] = useState(null);
  const [memory, setMemory] = useState(null);
  const meterRef = useRef([]);

  useEffect(() => {
    fetchStatus().then(setStatus).catch(() => {});
    const close = connectStream({
      onMeters: (m) => { meterRef.current = m; setMeters(m); },
      onAnalysis: setAnalysis,
      onDsp: setDsp,
      onStatusChange: setLink,
    });
    const refreshMemory = () => fetchMemory().then(setMemory).catch(() => {});
    refreshMemory();
    const iv = setInterval(refreshMemory, 30000);
    return () => { close(); clearInterval(iv); };
  }, []);

  async function askAI(autoApply) {
    setThinking(true);
    setDone(new Set());
    try {
      setAdvice(autoApply ? await requestAdviceAndApply() : await requestAdvice());
    } catch (e) {
      setAdvice({ summary: "Backend unreachable — is uvicorn running on :8000?", moves: [], source: "rules" });
    } finally {
      setThinking(false);
    }
  }

  async function applyOne(i) {
    const m = advice.moves[i];
    const result = await applyMove(m);
    setAdvice({
      ...advice,
      moves: advice.moves.map((mv, j) =>
        j === i ? { ...mv, applied: result.applied, detail: result.detail } : mv),
    });
    if (m.suggestion_id != null && result.applied) {
      fetchMemory().then(setMemory).catch(() => {});
    }
  }

  async function decideOne(i, decision) {
    const m = advice.moves[i];
    if (m.suggestion_id == null) return;
    await recordDecision(m.suggestion_id, decision);
    setAdvice({
      ...advice,
      moves: advice.moves.map((mv, j) => (j === i ? { ...mv, decision } : mv)),
    });
    fetchMemory().then(setMemory).catch(() => {});
  }

  const engaged = dsp?.engaged;
  const dspChannels = new Set(Object.keys(dsp?.channels || {}).map(Number));
  const flagged = new Map();
  advice?.moves?.forEach((m, i) => {
    if (!done.has(i) && !m.applied) flagged.set(m.channel, (flagged.get(m.channel) || 0) + 1);
  });

  return (
    <div className="deck">
      <header className="topbar">
        <div className="brand">
          <span className="brand-mgx">MGX</span>
          <span className="brand-sub">AI MIX ASSIST</span>
        </div>
        <div className="topbar-right">
          {analysis?.lufs_s != null && (
            <span className="lufs"><em>{analysis.lufs_s}</em> LUFS·S</span>
          )}
          <span className={`mode mode-${engaged ? "dsp" : "adv"}`}>
            {engaged ? "DSP TAKEOVER" : "ADVISORY"}
          </span>
          <span className={`link link-${link}`}>
            {link === "live"
              ? (status?.device === "simulated" ? "SIM SOURCE" : "MGX USB LIVE")
              : "LINK DOWN"}
          </span>
          <button className="ai-btn ghost" onClick={() => askAI(false)} disabled={thinking}>
            {thinking ? "LISTENING…" : "ANALYZE"}
          </button>
          <button className="ai-btn" onClick={() => askAI(true)}
                  disabled={thinking || !engaged}
                  title={engaged ? "Analyze and apply corrections in software"
                                 : "Engage the DSP first"}>
            ANALYZE + APPLY
          </button>
        </div>
      </header>

      <main className="surface">
        <section className="strips" aria-label="Channel strips">
          {meters.map((m) => (
            <ChannelStrip
              key={m.channel}
              meter={m}
              analysis={analysis?.channels?.[m.channel - 1]}
              flags={flagged.get(m.channel) || 0}
              dspActive={engaged && dspChannels.has(m.channel)}
            />
          ))}
          {meters.length === 0 && (
            <div className="empty">
              Waiting for audio. Start the backend, then connect the MGX16
              over USB (MAIN port) — or let the simulator feed test signals.
            </div>
          )}
        </section>

        <aside className="sidebar">
          <AutoGuardPanel
            guard={dsp?.autoguard}
            engaged={engaged}
            onToggle={async (v) => {
              const g = await setAutoGuard(v);
              setDsp({ ...dsp, autoguard: g });
            }}
          />
          <AutoMixPanel
            automix={dsp?.automix}
            engaged={engaged}
            onToggle={async (v) => {
              const m = await setAutoMix(v);
              setDsp({ ...dsp, automix: m });
            }}
          />
          <CommandBar
            engaged={engaged}
            onCommand={async (t) => await sendCommand(t)}
          />
          <DSPPanel
            dsp={dsp}
            names={analysis?.channels}
            onEngage={async (v) => setDsp(await setDspEngaged(v))}
            onReset={async (ch) => setDsp(await resetDsp(ch))}
          />
          <SuggestionPanel
            advice={advice}
            thinking={thinking}
            done={done}
            engaged={engaged}
            onApply={applyOne}
            onDecide={decideOne}
            onToggle={(i) => {
              const next = new Set(done);
              next.has(i) ? next.delete(i) : next.add(i);
              setDone(next);
            }}
          />
          <MaskingList
            masking={analysis?.masking || []}
            channels={analysis?.channels || []}
          />
          <LearningPanel memory={memory} />
        </aside>
      </main>
    </div>
  );
}
