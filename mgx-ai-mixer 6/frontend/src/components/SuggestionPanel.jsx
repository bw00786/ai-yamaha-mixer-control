import React from "react";

const ACTION_LABEL = {
  fader: "FADER", eq_cut: "EQ −", eq_boost: "EQ +", hpf: "HPF",
  pan: "PAN", comp: "COMP", gain: "GAIN",
};

export default function SuggestionPanel({ advice, thinking, done, engaged, onApply, onToggle, onDecide }) {
  return (
    <section className="panel">
      <h2 className="panel-title">AI move sheet</h2>
      {!advice && !thinking && (
        <p className="panel-hint">
          Press <b>Analyze mix</b> to get concrete moves — the AI listens to
          the last few seconds and writes what to change on the desk. You stay
          on the faders; nothing is changed automatically.
        </p>
      )}
      {thinking && <p className="panel-hint">Listening to the mix…</p>}
      {advice && (
        <>
          <p className="summary">{advice.summary}</p>
          <ol className="moves">
            {advice.moves.map((m, i) => (
              <li key={i} className={done.has(i) ? "done" : ""}>
                <button className="check" onClick={() => onToggle(i)}
                        aria-label={done.has(i) ? "Mark not done" : "Mark done"}>
                  {done.has(i) ? "✓" : ""}
                </button>
                <div>
                  <div className="move-line">
                    <span className={`chip chip-${m.action}`}>{ACTION_LABEL[m.action] || m.action}</span>
                    <b>{m.instruction || `${m.channel_name}: ${m.param} ${m.amount}`}</b>
                  </div>
                  <div className="move-reason">{m.reason}</div>
                  {m.applied && <div className="move-applied">applied in DSP — {m.detail}</div>}
                  {!m.applied && engaged && (
                    <button className="apply-one" onClick={() => onApply(i)}>
                      Apply in DSP
                    </button>
                  )}
                  {m.suggestion_id != null && !m.decision && (
                    <div className="decide-row">
                      <button className="decide approve" title="This was a good suggestion — remember it"
                              onClick={() => onDecide(i, "approved")}>✓ good call</button>
                      <button className="decide reject" title="Not useful here — remember to avoid this"
                              onClick={() => onDecide(i, "rejected")}>✕ not this room</button>
                    </div>
                  )}
                  {m.decision && (
                    <div className={`decide-noted decide-${m.decision}`}>
                      {m.decision === "approved" ? "noted: good call — the AI will remember" :
                       m.decision === "rejected" ? "noted: avoided next time" : "noted"}
                    </div>
                  )}
                </div>
              </li>
            ))}
          </ol>
          <div className="source">engine: {advice.source === "llm" ? "Qwen3.6" : "rule-based"}</div>
        </>
      )}
    </section>
  );
}
