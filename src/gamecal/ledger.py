"""SQLite ledger: every run, every observation, every write we make.

This is the idempotency store, the drift detector, and the circuit breaker
state, so every job goes through here. Schema is created on open.
"""

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY,
    job TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL DEFAULT 'running',  -- running | ok | failed
    detail TEXT
);

-- What a source reported at a point in time. One row per (run, source, item).
CREATE TABLE IF NOT EXISTS observations (
    id INTEGER PRIMARY KEY,
    run_id INTEGER NOT NULL REFERENCES runs(id),
    source TEXT NOT NULL,          -- steam_wishlist | steam_library | igdb | ...
    external_id TEXT NOT NULL,     -- steam appid, igdb id, ...
    title TEXT,
    payload TEXT NOT NULL,         -- full JSON blob from the source
    observed_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_obs_source_ext ON observations(source, external_id);

-- Writes we performed against an external system (e.g. the calendar).
CREATE TABLE IF NOT EXISTS actions (
    id INTEGER PRIMARY KEY,
    run_id INTEGER NOT NULL REFERENCES runs(id),
    target TEXT NOT NULL,          -- e.g. gcal
    external_id TEXT NOT NULL,
    action TEXT NOT NULL,          -- create | update | delete
    payload TEXT NOT NULL,
    performed_at TEXT NOT NULL
);

-- Small key/value store: breaker counts, last-sync timestamps, oauth tokens.
CREATE TABLE IF NOT EXISTS kv (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- Items needing a human decision, surfaced by `report` (later: the digest site).
CREATE TABLE IF NOT EXISTS attention (
    id INTEGER PRIMARY KEY,
    created_at TEXT NOT NULL,
    kind TEXT NOT NULL,            -- gone_quiet | sync_failure | drift
    external_id TEXT,
    message TEXT NOT NULL,
    resolved_at TEXT
);
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Ledger:
    def __init__(self, path: Path, check_same_thread: bool = True):
        # check_same_thread=False for the web app: one shared connection
        # across uvicorn's threadpool. Single-user, low write volume.
        self.conn = sqlite3.connect(path, check_same_thread=check_same_thread)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)

    # -- runs ---------------------------------------------------------------

    def start_run(self, job: str) -> int:
        cur = self.conn.execute(
            "INSERT INTO runs (job, started_at) VALUES (?, ?)", (job, now())
        )
        self.conn.commit()
        return cur.lastrowid

    def finish_run(self, run_id: int, status: str, detail: str = "") -> None:
        self.conn.execute(
            "UPDATE runs SET finished_at = ?, status = ?, detail = ? WHERE id = ?",
            (now(), status, detail, run_id),
        )
        self.conn.commit()

    def recent_runs(self, limit: int = 20) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()

    # -- observations -------------------------------------------------------

    def record_observations(self, run_id: int, source: str, items: list[dict]) -> None:
        ts = now()
        self.conn.executemany(
            "INSERT INTO observations (run_id, source, external_id, title, payload, observed_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            [
                (run_id, source, str(i["external_id"]), i.get("title"), json.dumps(i), ts)
                for i in items
            ],
        )
        self.conn.commit()

    def latest_observations(self, source: str) -> dict[str, dict]:
        """Most recent observation per external_id for a source."""
        rows = self.conn.execute(
            """
            SELECT o.* FROM observations o
            JOIN (SELECT source, external_id, MAX(id) AS max_id
                  FROM observations WHERE source = ? GROUP BY external_id) m
            ON o.id = m.max_id
            """,
            (source,),
        ).fetchall()
        return {r["external_id"]: json.loads(r["payload"]) for r in rows}

    def run_observations(self, source: str) -> list[dict]:
        """All observations of `source` from the most recent run that recorded
        any. Use for sources that snapshot the full current state each run
        (igdb_release, igdb_game): stale keys from old runs drop out."""
        row = self.conn.execute(
            "SELECT MAX(run_id) AS r FROM observations WHERE source = ?", (source,)
        ).fetchone()
        if row["r"] is None:
            return []
        rows = self.conn.execute(
            "SELECT payload FROM observations WHERE source = ? AND run_id = ?",
            (source, row["r"]),
        ).fetchall()
        return [json.loads(r["payload"]) for r in rows]

    # -- actions ------------------------------------------------------------

    def record_actions(self, run_id: int, target: str, items: list[tuple[str, str, dict]]) -> None:
        """items: (external_id, action, payload)"""
        ts = now()
        self.conn.executemany(
            "INSERT INTO actions (run_id, target, external_id, action, payload, performed_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            [(run_id, target, ext, act, json.dumps(payload), ts) for ext, act, payload in items],
        )
        self.conn.commit()

    # -- kv / circuit breaker ----------------------------------------------

    def get(self, key: str, default: str | None = None) -> str | None:
        row = self.conn.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default

    def set(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO kv (key, value) VALUES (?, ?)"
            " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        self.conn.commit()

    def breaker_failures(self, job: str) -> int:
        return int(self.get(f"breaker:{job}", "0") or 0)

    def breaker_tripped(self, job: str, limit: int = 3) -> bool:
        return self.breaker_failures(job) >= limit

    def breaker_record(self, job: str, ok: bool) -> None:
        self.set(f"breaker:{job}", "0" if ok else str(self.breaker_failures(job) + 1))

    def breaker_reset(self, job: str) -> None:
        self.set(f"breaker:{job}", "0")

    # -- attention ----------------------------------------------------------

    def tracked_games(self) -> dict[str, dict]:
        """Current tracked set, by slug: the last releases-run snapshot, plus
        watch-sourced games added since (web Track button), minus watch games
        whose kv entry was removed."""
        games = {g["slug"]: g for g in self.run_observations("igdb_game")}
        for slug, g in self.latest_observations("igdb_game").items():
            if slug not in games and g.get("source") == "watch":
                games[slug] = g
        return {
            slug: g
            for slug, g in games.items()
            if g.get("source") != "watch" or self.get(f"watch:{slug}") is not None
        }

    def add_attention(self, kind: str, message: str, external_id: str | None = None) -> None:
        self.conn.execute(
            "INSERT INTO attention (created_at, kind, external_id, message) VALUES (?, ?, ?, ?)",
            (now(), kind, external_id, message),
        )
        self.conn.commit()

    def open_attention(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM attention WHERE resolved_at IS NULL ORDER BY id"
        ).fetchall()

    def resolve_attention(self, attention_id: int) -> None:
        self.conn.execute(
            "UPDATE attention SET resolved_at = ? WHERE id = ?", (now(), attention_id)
        )
        self.conn.commit()
