"""Persistent memory for the AI mixer — Postgres-backed learning over time.

The goal: every suggestion the AI makes (from the rule engine, the LLM
advisor, or an autonomous action from AutoGuard/AutoMixer) is logged. When
the human operator approves, rejects, or modifies it, that decision is
recorded too. Over a season of services (roughly 3-6 months / ~15-25
Sundays) this builds a per-channel, per-issue track record:

    VOX  — "HPF 100 Hz"        approved 21/23  (91%)  -> trusted
    GTR  — "eq_cut low-mid"    approved 2/9    (22%)  -> avoid suggesting
    KEYS — "trim -2 dB"        approved 6/6    (100%) -> trusted

That track record is:
  1. Surfaced to the operator as a "Learning" panel (what the AI has
     learned works / doesn't work for this room and this band).
  2. Fed back into the advisor as retrieval-augmented context (RAG): the
     LLM prompt is given a short summary of what has historically been
     approved/rejected for the currently active channels, and the rule
     engine biases priority (and can suppress repeatedly-rejected moves)
     using the same history. Nothing here overrides the hard DSP clamps —
     this only shapes *which* moves get suggested and how confidently.

No Postgres reachable => the whole module degrades to a harmless no-op
(the advisor still works, it just doesn't remember across sessions).
"""
from __future__ import annotations

import os
import threading
from datetime import datetime, timezone

try:
    import psycopg
    from psycopg_pool import ConnectionPool
except ImportError:                      # pragma: no cover - optional dep
    psycopg = None
    ConnectionPool = None

DATABASE_URL = os.environ.get(
    "MGX_MEMORY_DB_URL",
    "postgresql://postgres:abc123@127.0.0.1:5432/mgx_mixer",
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS suggestions (
    id            SERIAL PRIMARY KEY,
    ts            TIMESTAMPTZ NOT NULL DEFAULT now(),
    channel       INT,
    channel_name  TEXT NOT NULL,
    action        TEXT NOT NULL,
    param         TEXT DEFAULT '',
    amount        TEXT DEFAULT '',
    reason        TEXT DEFAULT '',
    priority      INT DEFAULT 3,
    source        TEXT NOT NULL,        -- llm | rules | automix | autoguard
    issue_key     TEXT NOT NULL,        -- e.g. "hpf:100 Hz", "eq_cut:low-mid"
    decision      TEXT,                 -- NULL=pending, approved | rejected | modified
    decided_ts    TIMESTAMPTZ,
    operator_note TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS channel_preferences (
    channel_name    TEXT NOT NULL,
    issue_key       TEXT NOT NULL,
    approved_count  INT NOT NULL DEFAULT 0,
    rejected_count  INT NOT NULL DEFAULT 0,
    modified_count  INT NOT NULL DEFAULT 0,
    last_amount     TEXT DEFAULT '',
    last_updated    TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (channel_name, issue_key)
);

CREATE INDEX IF NOT EXISTS idx_suggestions_channel ON suggestions (channel_name);
CREATE INDEX IF NOT EXISTS idx_suggestions_pending ON suggestions (decision) WHERE decision IS NULL;
"""

# Below this many observations for a channel/issue pair, we don't yet trust
# the signal enough to bias behavior — just keep collecting.
MIN_OBSERVATIONS = 3
TRUSTED_RATE = 0.70
AVOID_RATE = 0.30


def issue_key(action: str, param: str) -> str:
    """Collapse a move into a stable key so repeated similar suggestions
    (same action roughly the same place) accumulate one track record
    instead of each being a unique snowflake."""
    p = (param or "").strip().lower()
    return f"{action}:{p}" if p else action


class MemoryStore:
    """Thin wrapper around a psycopg connection pool. All methods are safe
    to call even if Postgres is unreachable — they log a warning once and
    become no-ops so the rest of the app is never blocked by this."""

    def __init__(self, url: str = DATABASE_URL):
        self.available = False
        self._pool = None
        self._warned = False
        self._lock = threading.Lock()
        if psycopg is None:
            return
        try:
            self._pool = ConnectionPool(url, min_size=1, max_size=4,
                                        open=True, timeout=3.0)
            with self._pool.connection() as conn:
                conn.execute(SCHEMA)
                conn.commit()
            self.available = True
        except Exception as e:                        # pragma: no cover
            self._pool = None
            print(f"[memory] Postgres unavailable, learning disabled: {e}")

    # ------------------------------------------------------------- helpers
    def _conn(self):
        return self._pool.connection()

    def _safe(self, fn, default=None):
        if not self.available:
            return default
        try:
            with self._lock:
                return fn()
        except Exception as e:                         # pragma: no cover
            if not self._warned:
                print(f"[memory] operation failed: {e}")
                self._warned = True
            return default

    # -------------------------------------------------------------- write
    def log_suggestion(self, *, channel: int, channel_name: str, action: str,
                        param: str, amount: str, reason: str, priority: int,
                        source: str) -> int | None:
        """Record a proposed move. Returns its id (used later to record the
        operator's decision), or None if memory is unavailable."""
        key = issue_key(action, param)

        def _do():
            with self._conn() as conn:
                row = conn.execute(
                    """INSERT INTO suggestions
                       (channel, channel_name, action, param, amount, reason,
                        priority, source, issue_key)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
                    (channel, channel_name, action, param, amount, reason,
                     priority, source, key)).fetchone()
                conn.commit()
                return row[0]
        return self._safe(_do)

    def record_decision(self, suggestion_id: int, decision: str,
                         note: str = "") -> bool:
        """decision: 'approved' | 'rejected' | 'modified'."""
        def _do():
            with self._conn() as conn:
                row = conn.execute(
                    """UPDATE suggestions SET decision=%s, decided_ts=now(),
                       operator_note=%s WHERE id=%s
                       RETURNING channel_name, issue_key, amount""",
                    (decision, note, suggestion_id)).fetchone()
                if row is None:
                    conn.commit()
                    return False
                channel_name, key, amount = row
                self._bump_preference(conn, channel_name, key, decision, amount)
                conn.commit()
                return True
        return self._safe(_do, False)

    def log_auto_action(self, *, channel_name: str, action: str, param: str,
                         amount: str, reason: str, source: str) -> None:
        """AutoGuard/AutoMixer already gate on persistence + cooldowns before
        acting, so a completed autonomous action counts as an implicit
        'approved' observation for learning — but it's still logged as its
        own row (source='automix'/'autoguard') so the audit trail and the
        approval-rate math both stay honest about what was AI-only vs.
        human-approved."""
        key = issue_key(action, param)

        def _do():
            with self._conn() as conn:
                conn.execute(
                    """INSERT INTO suggestions
                       (channel_name, action, param, amount, reason, source,
                        issue_key, decision, decided_ts)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,'approved', now())""",
                    (channel_name, action, param, amount, reason, source, key))
                self._bump_preference(conn, channel_name, key, "approved", amount)
                conn.commit()
        self._safe(_do)

    def _bump_preference(self, conn, channel_name: str, key: str,
                          decision: str, amount: str):
        col = {"approved": "approved_count", "rejected": "rejected_count",
               "modified": "modified_count"}.get(decision, "modified_count")
        conn.execute(
            f"""INSERT INTO channel_preferences
                (channel_name, issue_key, {col}, last_amount, last_updated)
                VALUES (%s,%s,1,%s, now())
                ON CONFLICT (channel_name, issue_key) DO UPDATE SET
                    {col} = channel_preferences.{col} + 1,
                    last_amount = EXCLUDED.last_amount,
                    last_updated = now()""",
            (channel_name, key, amount))

    # --------------------------------------------------------------- read
    def channel_memory_text(self, channel_names: list[str]) -> str:
        """Short natural-language summary for the given channels, meant to
        be dropped into the LLM system prompt as retrieval-augmented
        context. Empty string if there's nothing learned yet."""
        if not channel_names:
            return ""

        def _do():
            with self._conn() as conn:
                rows = conn.execute(
                    """SELECT channel_name, issue_key, approved_count,
                              rejected_count, last_amount
                       FROM channel_preferences
                       WHERE channel_name = ANY(%s)
                       ORDER BY channel_name""",
                    (channel_names,)).fetchall()
            lines = []
            for name, key, approved, rejected, last_amount in rows:
                total = approved + rejected
                if total < MIN_OBSERVATIONS:
                    continue
                rate = approved / total
                if rate >= TRUSTED_RATE:
                    verdict = f"historically approved ({approved}/{total}) — safe to suggest again"
                elif rate <= AVOID_RATE:
                    verdict = f"historically rejected ({rejected}/{total}) — avoid suggesting unless clearly needed"
                else:
                    verdict = f"mixed history ({approved}/{total} approved)"
                lines.append(f"- {name} {key} (last: {last_amount}): {verdict}")
            return "\n".join(lines)
        text = self._safe(_do, "")
        return text or ""

    def bias_for(self, channel_name: str, action: str, param: str) -> str | None:
        """Return 'trusted' | 'avoid' | 'mixed' | None (not enough data)
        for one channel/issue pair — used by the deterministic rule engine
        to reorder/suppress its own suggestions."""
        key = issue_key(action, param)

        def _do():
            with self._conn() as conn:
                row = conn.execute(
                    """SELECT approved_count, rejected_count
                       FROM channel_preferences
                       WHERE channel_name=%s AND issue_key=%s""",
                    (channel_name, key)).fetchone()
            if row is None:
                return None
            approved, rejected = row
            total = approved + rejected
            if total < MIN_OBSERVATIONS:
                return None
            rate = approved / total
            if rate >= TRUSTED_RATE:
                return "trusted"
            if rate <= AVOID_RATE:
                return "avoid"
            return "mixed"
        return self._safe(_do, None)

    def summary(self) -> list[dict]:
        """Full learned-preference table for the UI's Learning panel."""
        def _do():
            with self._conn() as conn:
                rows = conn.execute(
                    """SELECT channel_name, issue_key, approved_count,
                              rejected_count, modified_count, last_amount,
                              last_updated
                       FROM channel_preferences
                       ORDER BY (approved_count + rejected_count + modified_count) DESC
                       LIMIT 100""").fetchall()
            out = []
            for name, key, approved, rejected, modified, last_amount, updated in rows:
                total = approved + rejected
                rate = (approved / total) if total else None
                if total < MIN_OBSERVATIONS:
                    verdict = "learning"
                elif rate >= TRUSTED_RATE:
                    verdict = "trusted"
                elif rate <= AVOID_RATE:
                    verdict = "avoid"
                else:
                    verdict = "mixed"
                out.append({
                    "channel_name": name, "issue": key,
                    "approved": approved, "rejected": rejected,
                    "modified": modified, "last_amount": last_amount,
                    "updated": updated.isoformat() if isinstance(updated, datetime) else str(updated),
                    "verdict": verdict,
                })
            return out
        return self._safe(_do, [])

    def recent_activity(self, limit: int = 30) -> list[dict]:
        def _do():
            with self._conn() as conn:
                rows = conn.execute(
                    """SELECT ts, channel_name, action, param, amount, reason,
                              source, decision
                       FROM suggestions
                       ORDER BY ts DESC LIMIT %s""", (limit,)).fetchall()
            return [{
                "ts": ts.isoformat() if isinstance(ts, datetime) else str(ts),
                "channel_name": name, "action": action, "param": param,
                "amount": amount, "reason": reason, "source": source,
                "decision": decision or "pending",
            } for ts, name, action, param, amount, reason, source, decision in rows]
        return self._safe(_do, [])
