import React from "react";

export default function AutoMixPanel({ automix, engaged, onToggle }) {
  const armed = automix?.armed;
  const enabled = automix?.enabled;
  return (
    <section className={`panel guard ${armed ? "armed" : ""}`}>
      <div className="dsp-head">
        <h2 className="panel-title">Mix keeper</h2>
        <button
          className={`engage ${enabled ? "on" : ""}`}
          onClick={() => onToggle(!enabled)}
          disabled={!engaged && !enabled}
          aria-pressed={enabled}
          title={engaged ? "" : "Engage the DSP first"}
        >
          {armed ? "ARMED" : enabled ? "WAITING FOR DSP" : "OFF"}
        </button>
      </div>
      {!enabled && (
        <p className="panel-hint">
          When armed, the mix is checked every couple of seconds for the
          usual problems — channels running too hot, low-end mud, instruments
          masking each other. Fixes are small (−2 dB at a time), only applied
          when a problem persists, and every one is logged here and undoable
          in the DSP panel.
        </p>
      )}
      {armed && (automix?.events?.length ?? 0) === 0 && (
        <p className="panel-hint">Watching. Mix looks healthy.</p>
      )}
      {(automix?.events?.length ?? 0) > 0 && (
        <ul className="guard-log">
          {automix.events.map((e, i) => (
            <li key={i}>
              <span className="g-time">{e.time}</span>
              <span className="g-what">
                {e.name}: <b>{e.action}</b>
                <span className="g-bleed"> — {e.reason}</span>
              </span>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
