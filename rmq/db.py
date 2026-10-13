"""SQLite storage: agents (with hashed keys), tasks and an event log."""
from __future__ import annotations

import hashlib
import secrets
import sqlite3
import threading
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS agents (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    key_hash TEXT NOT NULL UNIQUE,
    key_prefix TEXT NOT NULL,
    weight REAL NOT NULL,
    max_priority INTEGER NOT NULL,
    max_running INTEGER NOT NULL,
    max_queued INTEGER NOT NULL,
    usage REAL NOT NULL DEFAULT 0,
    enabled INTEGER NOT NULL DEFAULT 1,
    builtin INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    note TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY,
    agent_id INTEGER NOT NULL REFERENCES agents(id),
    name TEXT NOT NULL,
    argv TEXT,
    command TEXT,
    cwd TEXT NOT NULL,
    env TEXT,
    cpu REAL NOT NULL,
    ram_gb REAL NOT NULL,
    vram_gb REAL NOT NULL,
    time_s REAL NOT NULL,
    time_limit_s REAL,
    priority INTEGER NOT NULL,
    preempt TEXT NOT NULL,
    state TEXT NOT NULL,
    state_reason TEXT NOT NULL DEFAULT '',
    exit_code INTEGER,
    submitted_at REAL NOT NULL,
    queued_since REAL NOT NULL,
    started_at REAL,
    last_start_at REAL,
    finished_at REAL,
    run_s REAL NOT NULL DEFAULT 0,
    restarts INTEGER NOT NULL DEFAULT 0,
    job_name TEXT,
    pid INTEGER,
    pid_ctime REAL,
    suspended_pids TEXT,
    resume_ram_gb REAL NOT NULL DEFAULT 0,
    peak_ram_gb REAL NOT NULL DEFAULT 0,
    peak_vram_gb REAL NOT NULL DEFAULT 0,
    cpu_s REAL NOT NULL DEFAULT 0,
    uses_gpu INTEGER NOT NULL DEFAULT 0,
    stop_requested_at REAL,
    origin_node TEXT,
    origin_task INTEGER,
    remote_node TEXT,
    remote_id INTEGER,
    remote_status TEXT,
    net_spec TEXT
);
CREATE INDEX IF NOT EXISTS tasks_state ON tasks(state);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY,
    ts REAL NOT NULL,
    task_id INTEGER,
    agent_id INTEGER,
    kind TEXT NOT NULL,
    message TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS events_ts ON events(ts);
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""

ACTIVE_STATES = ("queued", "running", "suspended")
FINAL_STATES = ("done", "failed", "cancelled", "timeout", "lost")

TASK_FIELDS = {
    "agent_id", "name", "argv", "command", "cwd", "env", "cpu", "ram_gb", "vram_gb", "time_s", "time_limit_s",
    "priority", "preempt", "state", "state_reason", "exit_code", "submitted_at", "queued_since", "started_at",
    "last_start_at", "finished_at", "run_s", "restarts", "job_name", "pid", "pid_ctime", "suspended_pids",
    "resume_ram_gb", "peak_ram_gb", "peak_vram_gb", "cpu_s", "uses_gpu", "stop_requested_at",
    "origin_node", "origin_task", "remote_node", "remote_id", "remote_status", "net_spec", "origin_net",
}
# columns added after the first release: (table, column, declaration)
ADDED_COLUMNS = [("tasks", "stop_requested_at", "REAL"),
                 # a task from another PC of the network (hosted here) and a task sent to another PC ("remote")
                 ("tasks", "origin_node", "TEXT"), ("tasks", "origin_task", "INTEGER"),
                 ("tasks", "remote_node", "TEXT"), ("tasks", "remote_id", "INTEGER"),
                 ("tasks", "remote_status", "TEXT"), ("tasks", "net_spec", "TEXT"),
                 # which network a hosted task came over: "home" (one's own PC) or "ext" (other people)
                 ("tasks", "origin_net", "TEXT")]
AGENT_FIELDS = {"name", "weight", "max_priority", "max_running", "max_queued", "usage", "enabled", "note"}


def hash_key(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def new_key() -> str:
    return "rmq_" + secrets.token_urlsafe(24)


class DB:
    def __init__(self, path: Path):
        self._lock = threading.RLock()
        self._c = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None, timeout=10)
        self._c.row_factory = sqlite3.Row
        self._c.execute("PRAGMA journal_mode=WAL")
        self._c.execute("PRAGMA synchronous=NORMAL")
        self._c.executescript(SCHEMA)
        for table, column, decl in ADDED_COLUMNS:
            if column not in {r[1] for r in self._c.execute(f"PRAGMA table_info({table})")}:
                self._c.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")

    def _all(self, sql: str, args=()) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self._c.execute(sql, args).fetchall()]

    def _one(self, sql: str, args=()) -> dict | None:
        with self._lock:
            r = self._c.execute(sql, args).fetchone()
            return dict(r) if r else None

    def _exec(self, sql: str, args=()) -> sqlite3.Cursor:
        with self._lock:
            return self._c.execute(sql, args)

    # ------------------------------------------------------------ kv
    def kv_get(self, key: str) -> str | None:
        r = self._one("SELECT value FROM kv WHERE key=?", (key,))
        return r["value"] if r else None

    def kv_set(self, key: str, value: str) -> None:
        self._exec("INSERT INTO kv(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                   (key, value))

    # ------------------------------------------------------------ agents
    def agent_create(self, name: str, *, weight: float, max_priority: int, max_running: int,
                     max_queued: int, builtin: bool = False, note: str = "") -> tuple[dict, str]:
        key = new_key()
        cur = self._exec(
            "INSERT INTO agents(name, key_hash, key_prefix, weight, max_priority, max_running, max_queued,"
            " builtin, created_at, note) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (name, hash_key(key), key[:10], weight, max_priority, max_running, max_queued, int(builtin),
             time.time(), note))
        return self.agent(cur.lastrowid), key

    def agent(self, agent_id: int) -> dict | None:
        return self._one("SELECT * FROM agents WHERE id=?", (agent_id,))

    def agent_by_name(self, name: str) -> dict | None:
        return self._one("SELECT * FROM agents WHERE name=?", (name,))

    def agent_by_key(self, key: str) -> dict | None:
        return self._one("SELECT * FROM agents WHERE key_hash=?", (hash_key(key),))

    def agents(self) -> list[dict]:
        return self._all("SELECT * FROM agents ORDER BY builtin DESC, id")

    def agent_update(self, agent_id: int, **fields) -> None:
        bad = set(fields) - AGENT_FIELDS
        if bad:
            raise KeyError(bad)
        if fields:
            cols = ", ".join(f"{k}=?" for k in fields)
            self._exec(f"UPDATE agents SET {cols} WHERE id=?", (*fields.values(), agent_id))

    def agent_rotate_key(self, agent_id: int) -> str:
        key = new_key()
        self._exec("UPDATE agents SET key_hash=?, key_prefix=? WHERE id=?", (hash_key(key), key[:10], agent_id))
        return key

    # ------------------------------------------------------------ tasks
    def task_insert(self, **fields) -> int:
        bad = set(fields) - TASK_FIELDS
        if bad:
            raise KeyError(bad)
        cols = ", ".join(fields)
        marks = ", ".join("?" for _ in fields)
        return self._exec(f"INSERT INTO tasks({cols}) VALUES({marks})", tuple(fields.values())).lastrowid

    def task_update(self, task_id: int, **fields) -> None:
        bad = set(fields) - TASK_FIELDS
        if bad:
            raise KeyError(bad)
        if fields:
            cols = ", ".join(f"{k}=?" for k in fields)
            self._exec(f"UPDATE tasks SET {cols} WHERE id=?", (*fields.values(), task_id))

    def task(self, task_id: int) -> dict | None:
        return self._one("SELECT * FROM tasks WHERE id=?", (task_id,))

    def tasks_active(self) -> list[dict]:
        return self._all("SELECT * FROM tasks WHERE state IN ('queued','running','suspended') ORDER BY id")

    def tasks_recent(self, limit: int = 30) -> list[dict]:
        return self._all("SELECT * FROM tasks WHERE state NOT IN ('queued','running','suspended','remote')"
                         " ORDER BY finished_at DESC, id DESC LIMIT ?", (limit,))

    def tasks_remote(self) -> list[dict]:
        """Tasks this PC sent to other PCs of the network and still follows."""
        return self._all("SELECT * FROM tasks WHERE state='remote' ORDER BY id")

    def tasks_hosted(self) -> list[dict]:
        """Tasks other PCs of the network run here (any state)."""
        return self._all("SELECT * FROM tasks WHERE origin_node IS NOT NULL ORDER BY id")

    def tasks_list(self, *, state: str | None = None, agent_id: int | None = None, limit: int = 100) -> list[dict]:
        sql, args = "SELECT * FROM tasks WHERE 1=1", []
        if state == "active":
            sql += " AND state IN ('queued','running','suspended')"
        elif state:
            sql += " AND state=?"
            args.append(state)
        if agent_id is not None:
            sql += " AND agent_id=?"
            args.append(agent_id)
        sql += " ORDER BY id DESC LIMIT ?"
        args.append(limit)
        return self._all(sql, args)

    def tasks_overlapping(self, since: float, limit: int = 300) -> list[dict]:
        """Tasks alive at some point after `since`: still active, or finished after it (newest first)."""
        return self._all("SELECT * FROM tasks WHERE finished_at IS NULL OR finished_at >= ?"
                         " ORDER BY submitted_at DESC LIMIT ?", (since, limit))

    def count_queued(self, agent_id: int) -> int:
        r = self._one("SELECT COUNT(*) AS n FROM tasks WHERE agent_id=? AND state='queued'", (agent_id,))
        return r["n"] if r else 0

    def tasks_finished_before(self, ts: float) -> list[int]:
        return [r["id"] for r in self._all(
            "SELECT id FROM tasks WHERE state NOT IN ('queued','running','suspended','remote') AND finished_at < ?",
            (ts,))]

    def tasks_delete(self, ids: list[int]) -> None:
        with self._lock:
            self._c.executemany("DELETE FROM tasks WHERE id=?", [(i,) for i in ids])

    # ------------------------------------------------------------ events
    def event(self, kind: str, message: str, *, task_id: int | None = None, agent_id: int | None = None) -> None:
        self._exec("INSERT INTO events(ts, task_id, agent_id, kind, message) VALUES(?,?,?,?,?)",
                   (time.time(), task_id, agent_id, kind, message))

    _EVENTS = ("SELECT e.*, t.name AS task_name, a.name AS agent_name FROM events e "
               "LEFT JOIN tasks t ON t.id = e.task_id LEFT JOIN agents a ON a.id = COALESCE(e.agent_id, t.agent_id) ")

    def events(self, after_id: int = 0, limit: int = 200, task_id: int | None = None) -> list[dict]:
        """Newest first, each with the name of its task and of the agent (for the journal)."""
        if task_id is not None:
            return self._all(self._EVENTS + "WHERE e.task_id=? AND e.id>? ORDER BY e.id DESC LIMIT ?",
                             (task_id, after_id, limit))
        return self._all(self._EVENTS + "WHERE e.id>? ORDER BY e.id DESC LIMIT ?", (after_id, limit))

    def task_labels(self, ids) -> dict[str, dict]:
        """{task id: {"name", "agent"}} for the chart of resources by task."""
        ids = sorted(ids)[:500]
        if not ids:
            return {}
        rows = self._all("SELECT t.id, t.name, a.name AS agent FROM tasks t LEFT JOIN agents a ON a.id = t.agent_id "
                         f"WHERE t.id IN ({','.join('?' * len(ids))})", tuple(ids))
        return {str(r["id"]): {"name": r["name"], "agent": r["agent"]} for r in rows}

    def events_for_tasks(self, ids: list[int]) -> list[dict]:
        out: list[dict] = []
        for i in range(0, len(ids), 500):
            chunk = ids[i:i + 500]
            marks = ",".join("?" for _ in chunk)
            out += self._all(f"SELECT * FROM events WHERE task_id IN ({marks}) ORDER BY ts, id", chunk)
        return out

    def events_prune(self, before_ts: float) -> None:
        self._exec("DELETE FROM events WHERE ts < ?", (before_ts,))
