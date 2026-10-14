"""The store: one SQLite file holding all ilk2 state (docs/02 §2.3).

Rules it enforces:
- I1/I2: state changes only through `transition`, a compare-and-set on the row's version,
  journalled in `transitions` with actor, reason and previous version.
- Transactions are short and do no I/O besides SQLite. Busy timeout 10 s, 3 retries, then
  StoreBusy (§2.11.1).
- Every event carries the batch's consumer reference (§2.8).
"""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from . import clock

BUSY_TIMEOUT_MS = 10_000
BUSY_RETRIES = 3

STATES: dict[str, set[str]] = {
    "batches": {"queued", "active", "shipped", "blocked", "parked", "cancelled"},
    "plans": {"pending", "active", "shipped", "blocked", "cancelled"},
    "steps": {"pending", "accepting", "accepted", "blocked", "cancelled"},
    "runs": {"starting", "running", "ended"},
    "attempts": {"running", "done"},
}
TERMINAL = {
    "batches": {"shipped", "cancelled"},
    "runs": {"ended"},
}

NEEDS_HUMAN_REASONS = {
    "attempts", "crash-loop", "dependency", "setup", "base-unmeasured", "acceptance-mismatch",
    "unkillable", "quota", "api", "consumer-lease-expired", "health",
    # Not in the design's list (§2.11.3): a step gate that becomes permanently unmeasured
    # after the base measured fine (S4 when the runner disappears mid-batch).
    "unmeasured",
    # A V1 master claimed for an ilk2-owned project that ilk2 cannot accept (docs/04 §4.3).
    "plan-rejected",
    # `ilk2 apply`: a requested commit does not apply to a step's worktree.
    "apply-failed",
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS projects (
  id TEXT PRIMARY KEY, repo_root TEXT NOT NULL, default_branch TEXT NOT NULL,
  config TEXT NOT NULL, created_at REAL NOT NULL, version INTEGER NOT NULL DEFAULT 1);
CREATE TABLE IF NOT EXISTS batches (
  id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id),
  base_commit TEXT NOT NULL, branch TEXT NOT NULL, execution_root TEXT NOT NULL,
  priority INTEGER NOT NULL DEFAULT 0, consumer_ref TEXT,
  status TEXT NOT NULL, terminal_reason TEXT, blocked_reason TEXT,
  snapshot TEXT NOT NULL, originals TEXT NOT NULL,
  run_lease TEXT, control TEXT, created_at REAL NOT NULL, version INTEGER NOT NULL DEFAULT 1);
CREATE TABLE IF NOT EXISTS plans (
  id TEXT PRIMARY KEY, batch_id TEXT NOT NULL REFERENCES batches(id), slug TEXT NOT NULL,
  ord INTEGER NOT NULL, depends_on TEXT NOT NULL, status TEXT NOT NULL,
  blocked_reason TEXT, verification INTEGER NOT NULL DEFAULT 0, repair_of_step_id TEXT,
  version INTEGER NOT NULL DEFAULT 1);
CREATE TABLE IF NOT EXISTS steps (
  id TEXT PRIMARY KEY, plan_id TEXT NOT NULL REFERENCES plans(id),
  batch_id TEXT NOT NULL REFERENCES batches(id), idx INTEGER NOT NULL,
  title TEXT NOT NULL, instructions TEXT NOT NULL, gates TEXT NOT NULL,
  expected_reds TEXT NOT NULL, scope TEXT NOT NULL, must_pass TEXT NOT NULL DEFAULT '[]',
  status TEXT NOT NULL, blocked_reason TEXT, accepted_commit TEXT,
  attempt_count INTEGER NOT NULL DEFAULT 0, restart_count INTEGER NOT NULL DEFAULT 0,
  accepting TEXT, version INTEGER NOT NULL DEFAULT 1);
CREATE TABLE IF NOT EXISTS runs (
  id TEXT PRIMARY KEY, batch_id TEXT NOT NULL REFERENCES batches(id),
  status TEXT NOT NULL, terminal_reason TEXT, pid INTEGER, pgid INTEGER,
  proc_start TEXT, boot_time INTEGER, release TEXT, host TEXT,
  phase TEXT, phase_started_mono REAL, last_event_mono REAL, spawned_mono REAL,
  created_at REAL NOT NULL, ended_at REAL, version INTEGER NOT NULL DEFAULT 1);
CREATE TABLE IF NOT EXISTS attempts (
  id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(id),
  step_id TEXT NOT NULL REFERENCES steps(id), n INTEGER NOT NULL,
  session_id TEXT, status TEXT NOT NULL, outcome TEXT, reason TEXT,
  timings TEXT NOT NULL DEFAULT '{}', evidence TEXT, parent_commit TEXT,
  created_at REAL NOT NULL, ended_at REAL, version INTEGER NOT NULL DEFAULT 1);
CREATE TABLE IF NOT EXISTS gate_results (
  id INTEGER PRIMARY KEY AUTOINCREMENT, attempt_id TEXT NOT NULL REFERENCES attempts(id),
  gate TEXT NOT NULL, spec_version TEXT NOT NULL, outcome TEXT NOT NULL,
  new_failures TEXT NOT NULL, details TEXT NOT NULL, at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS base_measurements (
  batch_id TEXT NOT NULL, gate TEXT NOT NULL, spec_version TEXT NOT NULL,
  commit_sha TEXT NOT NULL, outcome TEXT NOT NULL, failures TEXT NOT NULL, at REAL NOT NULL,
  PRIMARY KEY (batch_id, gate, spec_version, commit_sha));
CREATE TABLE IF NOT EXISTS transitions (
  id INTEGER PRIMARY KEY AUTOINCREMENT, entity TEXT NOT NULL, entity_id TEXT NOT NULL,
  from_state TEXT, to_state TEXT NOT NULL, actor TEXT NOT NULL, reason TEXT NOT NULL,
  prev_version INTEGER, at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS events (
  cursor INTEGER PRIMARY KEY AUTOINCREMENT, batch_id TEXT, run_id TEXT, kind TEXT NOT NULL,
  payload TEXT NOT NULL, consumer_ref TEXT, at REAL NOT NULL, mono REAL NOT NULL);
CREATE TABLE IF NOT EXISTS needs_human (
  id TEXT PRIMARY KEY, batch_id TEXT, project_id TEXT, reason TEXT NOT NULL,
  detail TEXT NOT NULL, clear_command TEXT NOT NULL, consumer_ref TEXT,
  state TEXT NOT NULL, created_at REAL NOT NULL, last_notified_at REAL,
  notify_count INTEGER NOT NULL DEFAULT 0, acked_by TEXT, acked_at REAL, cleared_at REAL);
CREATE TABLE IF NOT EXISTS refusals (
  id INTEGER PRIMARY KEY AUTOINCREMENT, batch_id TEXT, reason TEXT NOT NULL,
  detail TEXT NOT NULL, clearing_action TEXT NOT NULL, clearing_actor TEXT NOT NULL,
  opened_at REAL NOT NULL, cleared_at REAL);
CREATE TABLE IF NOT EXISTS root_leases (
  root TEXT PRIMARY KEY, holder TEXT NOT NULL, owner TEXT, generation INTEGER NOT NULL,
  expiry_wall REAL, expected_head TEXT, accepted_head TEXT, updated_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS amendments (
  id INTEGER PRIMARY KEY AUTOINCREMENT, batch_id TEXT NOT NULL REFERENCES batches(id),
  actor TEXT NOT NULL, reason TEXT NOT NULL, ops TEXT NOT NULL,
  batch_version_before INTEGER NOT NULL, batch_version_after INTEGER NOT NULL, at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS probe_cache (
  batch_id TEXT NOT NULL, commit_sha TEXT NOT NULL, spec_version TEXT NOT NULL,
  test_id TEXT NOT NULL, failing INTEGER NOT NULL, fingerprint TEXT, at REAL NOT NULL,
  PRIMARY KEY (batch_id, commit_sha, spec_version, test_id));
CREATE TABLE IF NOT EXISTS repairs (
  batch_id TEXT NOT NULL, test_id TEXT NOT NULL, fingerprint TEXT NOT NULL,
  edge_clean TEXT NOT NULL, edge_failing TEXT NOT NULL, plan_id TEXT NOT NULL, at REAL NOT NULL,
  PRIMARY KEY (batch_id, test_id, fingerprint, edge_clean, edge_failing));
CREATE TABLE IF NOT EXISTS supervisor (
  id INTEGER PRIMARY KEY CHECK (id = 1), pid INTEGER, proc_start TEXT, boot_time INTEGER,
  release TEXT, tick_wall REAL, tick_mono REAL, started_wall REAL, kickstarts TEXT);
CREATE TABLE IF NOT EXISTS rsi_candidates (
  id TEXT PRIMARY KEY, source TEXT NOT NULL, title TEXT NOT NULL, evidence TEXT NOT NULL,
  hits INTEGER NOT NULL, score REAL NOT NULL, status TEXT NOT NULL, batch_id TEXT, note TEXT,
  first_seen REAL NOT NULL, last_seen REAL NOT NULL, planned_at REAL,
  version INTEGER NOT NULL DEFAULT 1);
CREATE INDEX IF NOT EXISTS events_batch ON events(batch_id, cursor);
CREATE INDEX IF NOT EXISTS steps_plan ON steps(plan_id, idx);
CREATE INDEX IF NOT EXISTS runs_status ON runs(status);
"""


class StoreError(Exception):
    pass


class Conflict(StoreError):
    """A compare-and-set lost: the row's version or state was not the expected one."""


class CursorExpired(StoreError):
    """An events cursor older than the retention: the consumer re-reads status (§2.8)."""


class StoreBusy(StoreError):
    """The store stayed locked past the busy timeout and retries (§2.11.1)."""


def new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def dumps(v: Any) -> str:
    return json.dumps(v, sort_keys=True, default=str)


class Store:
    def __init__(self, path: Path | str, busy_timeout_ms: int = BUSY_TIMEOUT_MS,
                 retries: int = BUSY_RETRIES):
        self.path = str(path)
        self.retries = retries
        self.conn = sqlite3.connect(self.path, timeout=busy_timeout_ms / 1000,
                                    isolation_level=None, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute(f"PRAGMA busy_timeout = {busy_timeout_ms}")
        self.conn.execute("PRAGMA foreign_keys = ON")
        self._with_retry(lambda: self.conn.execute("PRAGMA journal_mode = WAL"))
        self._in_tx = False
        self.migrate()

    def close(self) -> None:
        self.conn.close()

    # -- transactions -------------------------------------------------------------------

    def _with_retry(self, fn):
        for i in range(self.retries + 1):
            try:
                return fn()
            except sqlite3.OperationalError as e:
                if "locked" not in str(e) and "busy" not in str(e):
                    raise
                if i == self.retries:
                    raise StoreBusy(str(e)) from e
                time.sleep(0.05 * (i + 1))

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        """BEGIN IMMEDIATE ... COMMIT. Nested use joins the outer transaction."""
        if self._in_tx:
            yield self.conn
            return
        self._with_retry(lambda: self.conn.execute("BEGIN IMMEDIATE"))
        self._in_tx = True
        try:
            yield self.conn
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise
        else:
            self._with_retry(lambda: self.conn.execute("COMMIT"))
        finally:
            self._in_tx = False

    def migrate(self) -> None:
        with self.tx() as c:
            for stmt in SCHEMA.split(";"):
                if stmt.strip():
                    c.execute(stmt)
            c.execute("INSERT OR IGNORE INTO meta(key, value) VALUES ('schema_version', '1')")
            # Added columns (ALTER TABLE only adds; old releases ignore them).
            for table, col, decl in (("runs", "plan_id", "TEXT"),):  # lanes (docs/06, Oct 14)
                have = {r[1] for r in c.execute(f"PRAGMA table_info({table})")}
                if col not in have:
                    c.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
            c.execute("INSERT OR IGNORE INTO supervisor(id) VALUES (1)")

    # -- reads --------------------------------------------------------------------------

    def one(self, sql: str, *args) -> sqlite3.Row | None:
        return self.conn.execute(sql, args).fetchone()

    def all(self, sql: str, *args) -> list[sqlite3.Row]:
        return self.conn.execute(sql, args).fetchall()

    def get(self, table: str, id_: str) -> sqlite3.Row:
        row = self.one(f"SELECT * FROM {table} WHERE id = ?", id_)
        if row is None:
            raise StoreError(f"{table} {id_} not found")
        return row

    # -- writes -------------------------------------------------------------------------

    def insert(self, table: str, row: dict) -> None:
        cols = ", ".join(row)
        marks = ", ".join("?" for _ in row)
        with self.tx() as c:
            c.execute(f"INSERT INTO {table} ({cols}) VALUES ({marks})", tuple(row.values()))

    def transition(self, table: str, id_: str, to: str, *, actor: str, reason: str,
                   expect_from: set[str] | str | None = None,
                   expect_version: int | None = None, fields: dict | None = None) -> int:
        """Compare-and-set a row's status. Returns the new version. Raises Conflict."""
        if to not in STATES[table]:
            raise StoreError(f"{table}: unknown state {to!r}")
        if isinstance(expect_from, str):
            expect_from = {expect_from}
        with self.tx() as c:
            row = c.execute(f"SELECT status, version FROM {table} WHERE id = ?", (id_,)).fetchone()
            if row is None:
                raise Conflict(f"{table} {id_} not found")
            if expect_from is not None and row["status"] not in expect_from:
                raise Conflict(f"{table} {id_} is {row['status']}, expected {sorted(expect_from)}")
            if expect_version is not None and row["version"] != expect_version:
                raise Conflict(f"{table} {id_} version {row['version']} != {expect_version}")
            sets = {"status": to, **(fields or {})}
            assigns = ", ".join(f"{k} = ?" for k in sets)
            cur = c.execute(
                f"UPDATE {table} SET {assigns}, version = version + 1 WHERE id = ? AND version = ?",
                (*sets.values(), id_, row["version"]))
            if cur.rowcount != 1:
                raise Conflict(f"{table} {id_} changed concurrently")
            c.execute("INSERT INTO transitions(entity, entity_id, from_state, to_state, actor, "
                      "reason, prev_version, at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                      (table, id_, row["status"], to, actor, reason, row["version"], clock.wall()))
            if table == "batches" and to == "blocked" and row["status"] != "blocked":
                # One event per block, whichever path blocked it (docs/06 "Self-resolution").
                self.event("batch.blocked", {"reason": reason, "actor": actor,
                                             "blocked_reason": (fields or {}).get("blocked_reason")},
                           batch_id=id_)
            return row["version"] + 1

    def update(self, table: str, id_: str, fields: dict, *, actor: str, reason: str) -> None:
        """Change non-status fields, journalled, with no CAS on status."""
        assigns = ", ".join(f"{k} = ?" for k in fields)
        with self.tx() as c:
            row = c.execute(f"SELECT version FROM {table} WHERE id = ?", (id_,)).fetchone()
            if row is None:
                raise StoreError(f"{table} {id_} not found")
            c.execute(f"UPDATE {table} SET {assigns}, version = version + 1 WHERE id = ?",
                      (*fields.values(), id_))
            c.execute("INSERT INTO transitions(entity, entity_id, from_state, to_state, actor, "
                      "reason, prev_version, at) VALUES (?, ?, NULL, ?, ?, ?, ?, ?)",
                      (table, id_, "update:" + ",".join(fields), actor, reason, row["version"],
                       clock.wall()))

    # -- events -------------------------------------------------------------------------

    def event(self, kind: str, payload: dict | None = None, *, batch_id: str | None = None,
              run_id: str | None = None) -> int:
        with self.tx() as c:
            ref = None
            if batch_id:
                r = c.execute("SELECT b.consumer_ref, p.config FROM batches b JOIN projects p ON "
                              "p.id = b.project_id WHERE b.id = ?", (batch_id,)).fetchone()
                ref = r["consumer_ref"] if r else None
                if r and '"shadow_of": "' in (r["config"] or ""):  # docs/04 §4.2: marked shadow
                    payload = {**(payload or {}), "shadow": True}
            cur = c.execute("INSERT INTO events(batch_id, run_id, kind, payload, consumer_ref, at, "
                            "mono) VALUES (?, ?, ?, ?, ?, ?, ?)",
                            (batch_id, run_id, kind, dumps(payload or {}), ref, clock.wall(),
                             clock.mono()))
            if run_id:
                c.execute("UPDATE runs SET last_event_mono = ? WHERE id = ?", (clock.mono(), run_id))
            return cur.lastrowid

    def prune_events(self, keep_s: float) -> int:
        """Delete events older than keep_s and remember the newest pruned cursor (§2.8)."""
        with self.tx() as c:
            row = c.execute("SELECT MAX(cursor) m, COUNT(*) n FROM events WHERE at < ?",
                            (clock.wall() - keep_s,)).fetchone()
            if not row["n"]:
                return 0
            c.execute("DELETE FROM events WHERE cursor <= ?", (row["m"],))
            c.execute("INSERT INTO meta(key, value) VALUES ('events_pruned_through', ?) "
                      "ON CONFLICT(key) DO UPDATE SET value = MAX(CAST(value AS INTEGER), excluded.value)",
                      (str(row["m"]),))
            return row["n"]

    def events_since(self, cursor: int, batch_id: str | None = None, limit: int = 1000) -> list[dict]:
        if cursor:
            pruned = self.one("SELECT value FROM meta WHERE key = 'events_pruned_through'")
            if pruned and cursor < int(pruned["value"]):
                raise CursorExpired(f"cursor {cursor} is older than the retained events "
                                    f"(pruned through {pruned['value']}); re-read `ilk2 status`")
        sql = "SELECT * FROM events WHERE cursor > ?"
        args: list = [cursor]
        if batch_id:
            sql += " AND batch_id = ?"
            args.append(batch_id)
        sql += " ORDER BY cursor LIMIT ?"
        args.append(limit)
        return [dict(r) | {"payload": json.loads(r["payload"])} for r in self.all(sql, *args)]

    # -- needs_human ----------------------------------------------------------------------

    def open_needs_human(self, reason: str, detail: str, clear_command: str, *,
                         batch_id: str | None = None, project_id: str | None = None,
                         consumer_ref: str | None = None) -> str:
        """One open row per (batch, reason): re-entry into the same state does not duplicate.
        A row with no batch is keyed by its consumer ref too: two rejected V1 masters are two
        rows (Oct 14: 14a's rejection merged into 14b's open row and went unseen)."""
        if reason not in NEEDS_HUMAN_REASONS:
            raise StoreError(f"unknown needs_human reason {reason!r}")
        with self.tx() as c:
            existing = c.execute("SELECT id FROM needs_human WHERE state = 'open' AND reason = ? "
                                 "AND batch_id IS ?" + ("" if batch_id else " AND consumer_ref IS ?"),
                                 (reason, batch_id) if batch_id else (reason, batch_id, consumer_ref)
                                 ).fetchone()
            if existing:
                return existing["id"]
            ref = consumer_ref
            if batch_id:
                r = c.execute("SELECT consumer_ref, project_id FROM batches WHERE id = ?",
                              (batch_id,)).fetchone()
                if r:
                    ref, project_id = r["consumer_ref"], project_id or r["project_id"]
            nid = new_id("nh")
            c.execute("INSERT INTO needs_human(id, batch_id, project_id, reason, detail, "
                      "clear_command, consumer_ref, state, created_at) VALUES "
                      "(?, ?, ?, ?, ?, ?, ?, 'open', ?)",
                      (nid, batch_id, project_id, reason, detail, clear_command, ref, clock.wall()))
            self.event("needs_human", {"id": nid, "reason": reason, "detail": detail,
                                       "clear_command": clear_command}, batch_id=batch_id)
            return nid

    def ack(self, nid: str, by: str) -> None:
        with self.tx() as c:
            cur = c.execute("UPDATE needs_human SET state = 'acked', acked_by = ?, acked_at = ? "
                            "WHERE id = ? AND state = 'open'", (by, clock.wall(), nid))
            if cur.rowcount != 1:
                raise Conflict(f"needs_human {nid} is not open")

    def clear_needs_human(self, batch_id: str, reasons: set[str] | None = None) -> int:
        with self.tx() as c:
            rows = c.execute("SELECT id, reason FROM needs_human WHERE batch_id = ? AND state = 'open'",
                             (batch_id,)).fetchall()
            n = 0
            for r in rows:
                if reasons is None or r["reason"] in reasons:
                    c.execute("UPDATE needs_human SET state = 'cleared', cleared_at = ? WHERE id = ?",
                              (clock.wall(), r["id"]))
                    n += 1
            return n

    def open_human_rows(self, batch_id: str | None = None) -> list[sqlite3.Row]:
        if batch_id:
            return self.all("SELECT * FROM needs_human WHERE state = 'open' AND batch_id = ?", batch_id)
        return self.all("SELECT * FROM needs_human WHERE state = 'open' ORDER BY created_at")

    # -- refusals (I10) -------------------------------------------------------------------

    def refuse(self, batch_id: str | None, reason: str, detail: str, clearing_action: str,
               clearing_actor: str) -> int:
        """Open a refusal row, or keep the open one for (batch, reason)."""
        if clearing_actor not in {"supervisor", "driver", "human"}:
            raise StoreError(f"bad clearing actor {clearing_actor!r}")
        with self.tx() as c:
            r = c.execute("SELECT id FROM refusals WHERE cleared_at IS NULL AND reason = ? AND "
                          "batch_id IS ?", (reason, batch_id)).fetchone()
            if r:  # keep the open row (its age is the refused time) but show the current detail
                c.execute("UPDATE refusals SET detail = ? WHERE id = ?", (detail, r["id"]))
                return r["id"]
            cur = c.execute("INSERT INTO refusals(batch_id, reason, detail, clearing_action, "
                            "clearing_actor, opened_at) VALUES (?, ?, ?, ?, ?, ?)",
                            (batch_id, reason, detail, clearing_action, clearing_actor, clock.wall()))
            return cur.lastrowid

    def clear_refusal(self, batch_id: str | None, reason: str) -> bool:
        with self.tx() as c:
            cur = c.execute("UPDATE refusals SET cleared_at = ? WHERE cleared_at IS NULL AND "
                            "reason = ? AND batch_id IS ?", (clock.wall(), reason, batch_id))
            return cur.rowcount > 0

    def open_refusals(self, batch_id: str | None = None) -> list[sqlite3.Row]:
        if batch_id is None:
            return self.all("SELECT * FROM refusals WHERE cleared_at IS NULL")
        return self.all("SELECT * FROM refusals WHERE cleared_at IS NULL AND batch_id IS ?", batch_id)

    def refused_hours(self) -> dict[str, float]:
        out: dict[str, float] = {}
        now = clock.wall()
        for r in self.all("SELECT reason, opened_at, cleared_at FROM refusals"):
            out[r["reason"]] = out.get(r["reason"], 0.0) + ((r["cleared_at"] or now) - r["opened_at"]) / 3600
        return out
