// WebSocket + REST client for the MGX AI Mixer backend.

export function connectStream({ onMeters, onAnalysis, onDsp, onStatusChange }) {
  let ws;
  let closedByUser = false;

  function open() {
    const proto = location.protocol === "https:" ? "wss" : "ws";
    ws = new WebSocket(`${proto}://${location.host}/ws`);
    ws.onopen = () => onStatusChange?.("live");
    ws.onmessage = (ev) => {
      const msg = JSON.parse(ev.data);
      if (msg.type === "meters") onMeters?.(msg.data);
      if (msg.type === "analysis") onAnalysis?.(msg.data);
      if (msg.type === "dsp") onDsp?.(msg.data);
    };
    ws.onclose = () => {
      onStatusChange?.("offline");
      if (!closedByUser) setTimeout(open, 1500); // auto-reconnect
    };
  }
  open();
  return () => { closedByUser = true; ws?.close(); };
}

export async function fetchStatus() {
  const r = await fetch("/api/status");
  return r.json();
}

export async function requestAdvice() {
  const r = await fetch("/api/advise", { method: "POST" });
  if (!r.ok) throw new Error(`advise failed: ${r.status}`);
  return r.json();
}

export async function requestAdviceAndApply() {
  const r = await fetch("/api/advise?apply=true", { method: "POST" });
  if (!r.ok) throw new Error(`advise failed: ${r.status}`);
  return r.json();
}

export async function applyMove(move) {
  const r = await fetch("/api/moves/apply", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(move),
  });
  return r.json();
}

export async function recordDecision(suggestionId, decision, note = "") {
  const r = await fetch(`/api/moves/${suggestionId}/decision`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ decision, note }),
  });
  return r.json();
}

export async function fetchMemory() {
  const r = await fetch("/api/memory");
  return r.json();
}

export async function setDspEngaged(engage) {
  const r = await fetch("/api/dsp/engage", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ engage }),
  });
  return r.json();
}

export async function resetDsp(channel) {
  const r = await fetch("/api/dsp/reset", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ channel }),
  });
  return r.json();
}

export async function sendCommand(text) {
  const r = await fetch("/api/command", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ text }),
  });
  return r.json();
}

export async function setAutoGuard(enabled) {
  const r = await fetch("/api/autoguard", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ enabled }),
  });
  return r.json();
}

export async function setAutoMix(enabled) {
  const r = await fetch("/api/automix", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ enabled }),
  });
  return r.json();
}
