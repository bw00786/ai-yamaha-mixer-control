import React from "react";

export default function MaskingList({ masking, channels }) {
  const name = (n) => channels[n - 1]?.name || `CH ${n}`;
  return (
    <section className="panel">
      <h2 className="panel-title">Frequency collisions</h2>
      {masking.length === 0 ? (
        <p className="panel-hint">No significant masking detected.</p>
      ) : (
        <ul className="collisions">
          {masking.map((p, i) => (
            <li key={i}>
              <span className="pair">{name(p.a)} × {name(p.b)}</span>
              <span className="band">{p.band}</span>
              <span className="score-track">
                <span className="score-fill" style={{ width: `${p.score * 100}%` }} />
              </span>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
