import React from "react";

const fmt = (f) => (f >= 1000 ? (f / 1000).toFixed(1) + " kHz" : Math.round(f) + " Hz");

export default function AutoGuardPanel({ guard, engaged, onToggle }) {
  const armed = guard?.armed;
  const enabled = guard?.enabled;
  return (
    <section className={`panel guard ${armed ? "armed" : ""}`}>
      <div className="dsp-head">
        <h2 className="panel-title">Feedback guard</h2>
        <button
          className={`engage ${enabled ? "on" : ""}`}
          onClick={() => onToggle(!enabled)}
          disabled={!engaged && !enabled}
          aria-pressed={enabled}
          title={engaged ? "" : "Engage the DSP first — the guard acts through it"}
        >
          {armed ? "ARMED" : enabled ? "WAITING FOR DSP" : "OFF"}
        </button>
      </div>
      {!enabled && (
        <p className="panel-hint">
          When armed, every channel is watched continuously. A confirmed ring
          is notched automatically within about a second — musical notes are
          recognized by their harmonics and never touched. Every action is
          logged below and undoable in the DSP panel.
        </p>
      )}
      {enabled && !armed && (
        <p className="panel-hint">Guard is on but the DSP is bypassed — engage
          the DSP so the guard can act.</p>
      )}
      {armed && (guard?.events?.length ?? 0) === 0 && (
        <p className="panel-hint">Watching. No feedback caught yet.</p>
      )}
      {(guard?.events?.length ?? 0) > 0 && (
        <ul className="guard-log">
          {guard.events.map((e, i) => (
            <li key={i}>
              <span className="g-time">{e.time}</span>
              <span className="g-what">
                {e.name}: notched <b>{fmt(e.freq)}</b>
                {e.also_heard_on?.length > 0 && (
                  <span className="g-bleed"> (also heard on {e.also_heard_on.join(", ")})</span>
                )}
              </span>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
