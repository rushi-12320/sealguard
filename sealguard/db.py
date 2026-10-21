import json
import os
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone

DB_PATH = os.environ.get("SEALGUARD_DB", os.path.join(os.path.dirname(__file__), "..", "data", "sealguard.db"))
_lock = threading.RLock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS captures(
  id TEXT PRIMARY KEY, ts TEXT, source TEXT, verdict TEXT,
  defect_type TEXT, gt_locations TEXT, gt_source TEXT, gt_by TEXT, gt_note TEXT, not_inspectable INTEGER DEFAULT 0);
CREATE INDEX IF NOT EXISTS ix_captures_ts ON captures(ts);
CREATE TABLE IF NOT EXISTS images(
  capture_id TEXT, uv TEXT, region TEXT, score REAL, threshold REAL, decision TEXT,
  exposure TEXT, sat_pct REAL, mean_brightness REAL, PRIMARY KEY(capture_id, uv, region));
CREATE INDEX IF NOT EXISTS ix_images_uv ON images(uv);
CREATE TABLE IF NOT EXISTS thresholds(
  version INTEGER, uv TEXT, value REAL, created_at TEXT, created_by TEXT, proposal_id TEXT, note TEXT,
  PRIMARY KEY(version, uv));
CREATE TABLE IF NOT EXISTS proposals(
  id TEXT PRIMARY KEY, created_at TEXT, kind TEXT, uv TEXT, title TEXT, payload TEXT, evidence TEXT,
  critic TEXT, status TEXT, decided_by TEXT, decided_at TEXT, decision_note TEXT, run_id TEXT);
CREATE TABLE IF NOT EXISTS agent_runs(
  id TEXT PRIMARY KEY, kind TEXT, started_at TEXT, finished_at TEXT, status TEXT, steps TEXT,
  input_hash TEXT, output_hash TEXT, summary TEXT, params TEXT, replay_of TEXT);
CREATE TABLE IF NOT EXISTS ledger(
  seq INTEGER PRIMARY KEY, ts TEXT, event_type TEXT, actor TEXT, target TEXT, payload TEXT,
  prev_hash TEXT, entry_hash TEXT);
CREATE TABLE IF NOT EXISTS guard_log(
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, actor TEXT, action TEXT, target TEXT,
  decision TEXT, reasons TEXT, run_id TEXT);
CREATE TABLE IF NOT EXISTS incidents(
  id TEXT PRIMARY KEY, created_at TEXT, kind TEXT, uv TEXT, severity TEXT, title TEXT, detail TEXT,
  evidence TEXT, status TEXT, run_id TEXT);
CREATE TABLE IF NOT EXISTS reports(
  id TEXT PRIMARY KEY, created_at TEXT, kind TEXT, title TEXT, body TEXT, citations TEXT, audit TEXT, run_id TEXT);
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT);
"""


def now_iso():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def connect(path=None):
    path = path or DB_PATH
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    return conn


@contextmanager
def tx(conn):
    """Serialised write transaction."""
    with _lock:
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise


def rows(conn, sql, args=()):
    return [dict(r) for r in conn.execute(sql, args).fetchall()]


def one(conn, sql, args=()):
    r = conn.execute(sql, args).fetchone()
    return dict(r) if r else None


def get_setting(conn, key, default=None):
    r = one(conn, "SELECT value FROM settings WHERE key=?", (key,))
    return json.loads(r["value"]) if r else default


def set_setting(conn, key, value):
    conn.execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                 (key, json.dumps(value)))


def active_thresholds(conn):
    r = one(conn, "SELECT MAX(version) v FROM thresholds")
    if not r or r["v"] is None:
        return 0, {}
    v = r["v"]
    return v, {x["uv"]: x["value"] for x in rows(conn, "SELECT uv,value FROM thresholds WHERE version=?", (v,))}


def thresholds_at(conn, version):
    return {x["uv"]: x["value"] for x in rows(conn, "SELECT uv,value FROM thresholds WHERE version=?", (version,))}
