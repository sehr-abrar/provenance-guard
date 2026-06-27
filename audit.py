"""Structured audit log for Provenance Guard (SQLite).

Every attribution decision and every appeal is recorded as a row here. The
schema is forward-compatible: M3 fills the LLM/decision columns, M4 adds the
stylometric + combined scores, M5 adds appeal rows that reference a decision.
"""

import json
import sqlite3
from datetime import datetime, timezone

DB_PATH = "provenance.sqlite"


def _connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    """Create the audit_log table if it doesn't exist."""
    with _connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS audit_log (
                id                INTEGER PRIMARY KEY AUTOINCREMENT,
                entry_type        TEXT NOT NULL,          -- 'decision' | 'appeal'
                content_id        TEXT NOT NULL,
                creator_id        TEXT,
                timestamp         TEXT NOT NULL,
                attribution       TEXT,                   -- likely_human | uncertain | likely_ai
                confidence        REAL,
                llm_score         REAL,
                stylometric_score REAL,                   -- filled in M4
                combined_score    REAL,                   -- filled in M4
                status            TEXT,                   -- classified | under_review
                label             TEXT,
                reasoning         TEXT,                   -- appeal reasoning (M5)
                details           TEXT                    -- JSON: rationale, metrics, etc.
            )
            """
        )


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def log_decision(content_id, creator_id, attribution, confidence, llm_score,
                 status="classified", label=None, stylometric_score=None,
                 combined_score=None, details=None):
    """Append a classification decision to the audit log."""
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO audit_log (entry_type, content_id, creator_id, timestamp,
                attribution, confidence, llm_score, stylometric_score,
                combined_score, status, label, details)
            VALUES ('decision', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (content_id, creator_id, _now(), attribution, confidence, llm_score,
             stylometric_score, combined_score, status, label,
             json.dumps(details) if details is not None else None),
        )


def log_appeal(content_id, reasoning, details=None):
    """Append an appeal entry and flip the original decision to under_review.

    Returns the original decision row (dict) if found, else None.
    """
    with _connect() as conn:
        original = conn.execute(
            "SELECT * FROM audit_log WHERE content_id = ? AND entry_type = 'decision' "
            "ORDER BY id DESC LIMIT 1",
            (content_id,),
        ).fetchone()
        if original is None:
            return None
        conn.execute(
            "UPDATE audit_log SET status = 'under_review' "
            "WHERE content_id = ? AND entry_type = 'decision'",
            (content_id,),
        )
        conn.execute(
            """
            INSERT INTO audit_log (entry_type, content_id, creator_id, timestamp,
                attribution, confidence, llm_score, stylometric_score,
                combined_score, status, label, reasoning, details)
            VALUES ('appeal', ?, ?, ?, ?, ?, ?, ?, ?, 'under_review', ?, ?, ?)
            """,
            (content_id, original["creator_id"], _now(), original["attribution"],
             original["confidence"], original["llm_score"],
             original["stylometric_score"], original["combined_score"],
             original["label"], reasoning,
             json.dumps(details) if details is not None else None),
        )
        return dict(original)


def get_log(limit=50):
    """Return the most recent audit-log entries (newest first) as dicts."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    entries = []
    for r in rows:
        d = dict(r)
        if d.get("details"):
            try:
                d["details"] = json.loads(d["details"])
            except (TypeError, ValueError):
                pass
        entries.append(d)
    return entries


def get_appeals():
    """Return submissions currently under review (the reviewer queue)."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM audit_log WHERE entry_type = 'appeal' ORDER BY id DESC"
        ).fetchall()
    return [dict(r) for r in rows]
