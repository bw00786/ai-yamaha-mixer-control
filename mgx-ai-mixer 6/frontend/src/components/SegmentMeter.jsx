import React from "react";

// LED-segment meter, drawn like console hardware: discrete segments,
// green -> amber -> red, with a peak-hold segment.
const SEGMENTS = 18;
const FLOOR = -60; // dB at bottom segment

function segFor(db) {
  if (db <= FLOOR) return 0;
  return Math.min(SEGMENTS, Math.round(((db - FLOOR) / -FLOOR) * SEGMENTS));
}

export default function SegmentMeter({ rms, peak, clip }) {
  const lit = segFor(rms);
  const peakSeg = segFor(peak);
  const cells = [];
  for (let i = SEGMENTS - 1; i >= 0; i--) {
    const zone = i >= SEGMENTS - 2 ? "red" : i >= SEGMENTS - 6 ? "amber" : "green";
    const on = i < lit;
    const hold = i === peakSeg - 1 && !on;
    cells.push(
      <span
        key={i}
        className={`seg seg-${zone} ${on ? "on" : ""} ${hold ? "hold" : ""}`}
      />
    );
  }
  return (
    <div className={`meter ${clip ? "clipping" : ""}`} title={`${rms} dBFS`}>
      {cells}
    </div>
  );
}
