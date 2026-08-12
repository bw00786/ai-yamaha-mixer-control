import React, { useState } from "react";

export default function CommandBar({ engaged, onCommand }) {
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [last, setLast] = useState(null);

  async function send() {
    const t = text.trim();
    if (!t || busy) return;
    setBusy(true);
    try {
      const r = await onCommand(t);
      setLast(r);
      if (!r.errors?.length) setText("");
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className={`panel command ${engaged ? "" : "disabled"}`}>
      <h2 className="panel-title">Talk to the mix</h2>
      <div className="cmd-row">
        <input
          className="cmd-input"
          value={text}
          disabled={!engaged || busy}
          placeholder={engaged
            ? 'e.g. "more reverb on the vocals", "slapback echo on the guitar"'
            : "Engage the DSP to use commands"}
          onChange={(e) => setText(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && send()}
        />
        <button className="cmd-send" onClick={send} disabled={!engaged || busy}>
          {busy ? "…" : "DO IT"}
        </button>
      </div>
      {last && (
        <div className={`cmd-result ${last.errors?.length ? "err" : ""}`}>
          {last.understood}
          {last.errors?.length > 0 && ` — ${last.errors.join("; ")}`}
          <span className="cmd-src"> ({last.source === "llm" ? "Qwen3.6" : "rules"})</span>
        </div>
      )}
    </section>
  );
}
