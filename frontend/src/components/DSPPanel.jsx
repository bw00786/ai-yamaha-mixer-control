import React from "react";

function chainSummary(c) {
  const parts = [];
  if (c.trim_db && Math.abs(c.trim_db) > 0.05) parts.push(`trim ${c.trim_db > 0 ? "+" : ""}${c.trim_db} dB`);
  if (c.hpf?.on) parts.push(`HPF ${Math.round(c.hpf.freq)} Hz`);
  (c.eq || []).forEach((b) =>
    parts.push(`${b.gain_db > 0 ? "+" : ""}${b.gain_db} dB @ ${Math.round(b.freq)} Hz`));
  if (c.comp?.on) parts.push(`comp ${c.comp.ratio}:1 @ ${Math.round(c.comp.threshold_db)} dB`);
  (c.notches || []).forEach((n) =>
    parts.push(`NOTCH ${n.freq >= 1000 ? (n.freq/1000).toFixed(1) + "k" : Math.round(n.freq)} Hz`));
  if (c.reverb?.on) parts.push(`reverb ${Math.round(c.reverb.wet * 100)}%`);
  if (c.delay?.on) parts.push(`delay ${Math.round(c.delay.time_ms)} ms ${Math.round(c.delay.wet * 100)}%`);
  return parts.join(" · ");
}

export default function DSPPanel({ dsp, names, onEngage, onReset }) {
  const engaged = dsp?.engaged;
  const active = Object.entries(dsp?.channels || {});
  return (
    <section className={`panel dsp-panel ${engaged ? "engaged" : ""}`}>
      <div className="dsp-head">
        <h2 className="panel-title">Software DSP</h2>
        <button
          className={`engage ${engaged ? "on" : ""}`}
          onClick={() => onEngage(!engaged)}
          aria-pressed={engaged}
        >
          {engaged ? "ENGAGED" : "BYPASSED"}
        </button>
      </div>
      {!engaged && (
        <p className="panel-hint">
          Bypassed: audio passes through untouched. Engage to run corrections
          on the USB return path — patch the desk's channel inputs to USB to
          hear them.
        </p>
      )}
      {engaged && active.length === 0 && (
        <p className="panel-hint">
          Engaged, no processing yet. Apply AI moves or analyze with
          auto-apply.
        </p>
      )}
      {active.length > 0 && (
        <ul className="dsp-list">
          {active.map(([ch, c]) => (
            <li key={ch}>
              <span className="dsp-ch">{names?.[ch - 1]?.name || `CH ${ch}`}</span>
              <span className="dsp-chain">{chainSummary(c)}</span>
              <button className="dsp-clear" onClick={() => onReset(Number(ch))}
                      title="Clear this channel's processing">✕</button>
            </li>
          ))}
        </ul>
      )}
      {active.length > 0 && (
        <button className="dsp-reset-all" onClick={() => onReset(null)}>
          Clear all processing
        </button>
      )}
    </section>
  );
}
