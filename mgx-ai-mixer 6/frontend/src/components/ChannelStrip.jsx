import React from "react";
import SegmentMeter from "./SegmentMeter.jsx";

export default function ChannelStrip({ meter, analysis, flags, dspActive }) {
  const active = analysis?.active;
  const headroom = analysis?.headroom_db;
  return (
    <div className={`strip ${active ? "active" : ""} ${meter.clip ? "strip-clip" : ""}`}>
      {flags > 0 && <div className="flag" title="AI has suggestions for this channel">{flags}</div>}
      <div className="ch-num">
        {String(meter.channel).padStart(2, "0")}
        {dspActive && <span className="dsp-pip" title="Software DSP active on this channel" />}
      </div>
      <SegmentMeter rms={meter.rms_db} peak={meter.peak_db} clip={meter.clip} />
      <div className="readout">
        <span className="peak">{meter.peak_db <= -90 ? "−∞" : meter.peak_db}</span>
        {active && headroom != null && headroom < 4 && (
          <span className="warn">HOT</span>
        )}
      </div>
      <div className="scribble">{meter.name}</div>
    </div>
  );
}
