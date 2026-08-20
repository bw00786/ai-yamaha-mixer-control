import React from "react";

const VERDICT_LABEL = {
  trusted: "TRUSTED", avoid: "AVOID", mixed: "MIXED", learning: "LEARNING",
};

export default function LearningPanel({ memory }) {
  if (!memory) return null;
  if (!memory.available) {
    return (
      <section className="panel learning">
        <h2 className="panel-title">Learning (this room)</h2>
        <p className="panel-hint">
          No memory database reachable — the AI still works, but it won't
          remember approvals/rejections across services. Set
          <code> MGX_MEMORY_DB_URL</code> to a Postgres instance to enable
          long-term learning.
        </p>
      </section>
    );
  }

  const prefs = memory.preferences || [];
  return (
    <section className="panel learning">
      <h2 className="panel-title">Learning (this room)</h2>
      {prefs.length === 0 && (
        <p className="panel-hint">
          No history yet. Every time you approve or reject a suggested move
          (or the automatic guards act), it's remembered here. Over a season
          of services (roughly 3-6 months) the AI learns which moves actually
          work for this room and this band, and leans into them.
        </p>
      )}
      {prefs.length > 0 && (
        <ul className="learn-list">
          {prefs.map((p, i) => {
            const total = p.approved + p.rejected;
            const rate = total ? Math.round((p.approved / total) * 100) : null;
            return (
              <li key={i}>
                <div className="learn-row">
                  <span className={`verdict verdict-${p.verdict}`}>{VERDICT_LABEL[p.verdict] || p.verdict}</span>
                  <b>{p.channel_name}</b>
                  <span className="learn-issue">{p.issue}</span>
                </div>
                <div className="learn-stats">
                  {rate != null ? `${rate}% approved (${p.approved}/${total})` : `${p.approved + p.rejected + p.modified} observations`}
                  {p.last_amount ? ` · last: ${p.last_amount}` : ""}
                </div>
              </li>
            );
          })}
        </ul>
      )}
    </section>
  );
}
