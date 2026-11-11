"""SQLite storage for the bridge.

Nothing in this module knows about MCP. The server only calls the public methods
of `Store`, so a hosted PostgreSQL version can replace it by implementing the
same methods (see README, "Moving to PostgreSQL").
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

SCHEMA_VERSION = 1
SENSITIVE_ACTIONS = (
    "spend_money",
    "publish_or_deploy",
    "send_external_message",
    "delete_important_data",
    "modify_production",
    "merge_protected_branch",
)
MEMORY_KINDS = ("note", "fact", "decision", "context")
TASK_STATUSES = ("awaiting_approval", "open", "claimed", "in_progress", "blocked", "done", "cancelled")
LEASED = "('claimed', 'in_progress', 'blocked')"  # statuses where an agent holds a lease
FINISHED = "('done', 'cancelled')"
# What the lease holder may move a task to, by current status.
TRANSITIONS = {
    "claimed": {"in_progress", "blocked", "open", "cancelled"},
    "in_progress": {"in_progress", "blocked", "done", "open", "cancelled"},
    "blocked": {"in_progress", "blocked", "open", "cancelled"},
}
TABLES = ("projects", "agents", "memories", "tasks", "handovers", "approvals", "audit_log")

SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL UNIQUE COLLATE NOCASE,
    description TEXT NOT NULL DEFAULT '',
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS agents (
    id TEXT PRIMARY KEY,
    capabilities TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS memories (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id),
    kind TEXT NOT NULL,
    key TEXT,
    content TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    tags TEXT NOT NULL DEFAULT '[]',
    version INTEGER NOT NULL DEFAULT 1,
    supersedes TEXT REFERENCES memories(id),
    is_latest INTEGER NOT NULL DEFAULT 1,
    agent_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS memories_project ON memories(project_id, is_latest, kind);
CREATE UNIQUE INDEX IF NOT EXISTS memories_latest_key ON memories(project_id, key) WHERE key IS NOT NULL AND is_latest = 1;
CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id),
    parent_id TEXT REFERENCES tasks(id),
    title TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    priority INTEGER NOT NULL,
    required_capability TEXT,
    sensitive_actions TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL,
    assigned_to TEXT,
    lease_expires_at TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    progress TEXT NOT NULL DEFAULT '',
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS tasks_queue ON tasks(status, priority, created_at);
CREATE INDEX IF NOT EXISTS tasks_parent ON tasks(parent_id);
CREATE TABLE IF NOT EXISTS handovers (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id),
    task_id TEXT REFERENCES tasks(id),
    from_agent TEXT NOT NULL,
    to_agent TEXT,
    summary TEXT NOT NULL,
    next_steps TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS approvals (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id),
    task_id TEXT NOT NULL REFERENCES tasks(id),
    action TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    requested_by TEXT NOT NULL,
    decided_by TEXT,
    reason TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS audit_log (
    id TEXT PRIMARY KEY,
    actor TEXT NOT NULL,
    action TEXT NOT NULL,
    project_id TEXT,
    task_id TEXT,
    detail TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS audit_actor ON audit_log(actor, created_at);
CREATE INDEX IF NOT EXISTS audit_project ON audit_log(project_id, created_at);
"""


class BridgeError(Exception):
    """An expected refusal (unknown id, lease conflict, missing approval). Safe to show to agents."""


def default_db_path() -> Path:
    env = os.environ.get("KTV_BRIDGE_DB")
    return Path(env) if env else Path(__file__).resolve().parent.parent / "data" / "bridge.db"


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def _row(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    d = dict(row)
    for k in ("capabilities", "tags", "sensitive_actions", "detail"):
        if isinstance(d.get(k), str):
            d[k] = json.loads(d[k])
    return d


def _like(term: str) -> str:
    return "%" + term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


class Store:
    def __init__(self, path: str | Path, lease_seconds: int | None = None,
                 clock: Callable[[], datetime] | None = None):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lease_seconds = lease_seconds or int(os.environ.get("KTV_BRIDGE_LEASE_SECONDS", "600"))
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._lock = threading.RLock()  # one connection, possibly used from worker threads
        self.db = sqlite3.connect(self.path, timeout=15, isolation_level=None, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")  # lets several server processes share the file
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.executescript(SCHEMA)
        self.db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")

    def close(self) -> None:
        self.db.close()

    # ---- helpers -------------------------------------------------------

    def _ts(self, seconds: float = 0) -> str:
        # Fixed-width UTC strings, so string comparison is time comparison.
        return (self._clock() + timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")

    @contextmanager
    def _tx(self):
        # BEGIN IMMEDIATE takes SQLite's write lock up front, so a read-then-update
        # (like claiming a task) cannot interleave with another process.
        with self._lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                yield self.db
            except BaseException:
                self.db.execute("ROLLBACK")
                raise
            self.db.execute("COMMIT")

    def _one(self, db: sqlite3.Connection, sql: str, *args: Any) -> dict[str, Any] | None:
        return _row(db.execute(sql, args).fetchone())

    def _all(self, db: sqlite3.Connection, sql: str, *args: Any) -> list[dict[str, Any]]:
        return [_row(r) for r in db.execute(sql, args).fetchall()]

    def _project(self, db, ref: str) -> dict[str, Any]:
        p = self._one(db, "SELECT * FROM projects WHERE id = ? OR name = ?", ref, ref)
        if not p:
            raise BridgeError(f"Unknown project {ref!r}. Use list_projects or create_project.")
        return p

    def _task(self, db, task_id: str) -> dict[str, Any]:
        t = self._one(db, "SELECT * FROM tasks WHERE id = ?", task_id)
        if not t:
            raise BridgeError(f"Unknown task {task_id!r}.")
        return t

    def _touch(self, db, agent_id: str, capabilities: list[str] | None = None) -> dict[str, Any]:
        now = self._ts()
        db.execute(
            "INSERT INTO agents (id, capabilities, created_at, updated_at, last_seen_at) VALUES (?, '[]', ?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET last_seen_at = excluded.last_seen_at",
            (agent_id, now, now, now),
        )
        if capabilities is not None:
            db.execute("UPDATE agents SET capabilities = ?, updated_at = ? WHERE id = ?",
                       (json.dumps(sorted(set(capabilities))), now, agent_id))
        return self._one(db, "SELECT * FROM agents WHERE id = ?", agent_id)

    def _audit(self, db, actor: str, action: str, project_id: str | None = None,
               task_id: str | None = None, **detail: Any) -> None:
        db.execute(
            "INSERT INTO audit_log (id, actor, action, project_id, task_id, detail, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (_id("evt"), actor, action, project_id, task_id, json.dumps(detail), self._ts()),
        )

    def _recover_expired(self, db) -> None:
        now = self._ts()
        for t in self._all(db, f"SELECT * FROM tasks WHERE status IN {LEASED} AND lease_expires_at < ?", now):
            db.execute("UPDATE tasks SET status = 'open', assigned_to = NULL, lease_expires_at = NULL, updated_at = ? "
                       "WHERE id = ?", (now, t["id"]))
            self._audit(db, "system", "lease_expired", t["project_id"], t["id"],
                        previous_agent=t["assigned_to"], previous_status=t["status"])

    def _handovers(self, db, task_id: str, limit: int = 5) -> list[dict[str, Any]]:
        return self._all(db, "SELECT * FROM handovers WHERE task_id = ? ORDER BY created_at DESC, rowid DESC LIMIT ?", task_id, limit)

    def _insert_task(self, db, project_id: str, agent_id: str, title: str, description: str, priority: int,
                     required_capability: str | None, parent_id: str | None,
                     sensitive_actions: list[str]) -> dict[str, Any]:
        bad = set(sensitive_actions) - set(SENSITIVE_ACTIONS)
        if bad:
            raise BridgeError(f"Unknown sensitive actions: {sorted(bad)}")
        if parent_id and self._task(db, parent_id)["project_id"] != project_id:
            raise BridgeError("parent_task_id belongs to a different project.")
        now, tid = self._ts(), _id("task")
        actions = sorted(set(sensitive_actions))
        db.execute(
            "INSERT INTO tasks (id, project_id, parent_id, title, description, priority, required_capability, "
            "sensitive_actions, status, created_by, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (tid, project_id, parent_id, title, description, priority, required_capability, json.dumps(actions),
             "awaiting_approval" if actions else "open", agent_id, now, now),
        )
        for action in actions:
            db.execute(
                "INSERT INTO approvals (id, project_id, task_id, action, requested_by, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)", (_id("appr"), project_id, tid, action, agent_id, now, now))
        self._audit(db, agent_id, "task_created", project_id, tid, title=title, parent_id=parent_id,
                    sensitive_actions=actions)
        return self._task(db, tid)

    # ---- projects & memory --------------------------------------------

    def health(self) -> dict[str, Any]:
        with self._tx() as db:
            counts = {t: db.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in TABLES}
        return {"status": "ok", "db_path": str(self.path), "schema_version": SCHEMA_VERSION,
                "lease_seconds": self.lease_seconds, "server_time": self._ts(), "counts": counts}

    def list_projects(self) -> dict[str, Any]:
        with self._tx() as db:
            return {"projects": self._all(db, (
                f"SELECT p.*, (SELECT COUNT(*) FROM tasks t WHERE t.project_id = p.id AND t.status NOT IN {FINISHED}) "
                "AS unfinished_tasks FROM projects p ORDER BY p.name"))}

    def create_project(self, agent_id: str, name: str, description: str = "") -> dict[str, Any]:
        with self._tx() as db:
            self._touch(db, agent_id)
            existing = self._one(db, "SELECT * FROM projects WHERE name = ?", name)
            if existing:
                return {"project": existing, "created": False}
            now, pid = self._ts(), _id("proj")
            db.execute("INSERT INTO projects (id, name, description, created_by, created_at, updated_at) "
                       "VALUES (?, ?, ?, ?, ?, ?)", (pid, name, description, agent_id, now, now))
            self._audit(db, agent_id, "project_created", pid, name=name)
            return {"project": self._project(db, pid), "created": True}

    def save_memory(self, agent_id: str, project: str, content: str, kind: str = "note",
                    key: str | None = None, tags: list[str] | None = None) -> dict[str, Any]:
        if kind not in MEMORY_KINDS:
            raise BridgeError(f"kind must be one of {MEMORY_KINDS}")
        digest = hashlib.sha256(" ".join(content.split()).lower().encode()).hexdigest()
        with self._tx() as db:
            self._touch(db, agent_id)
            p = self._project(db, project)
            dup = self._one(db, "SELECT * FROM memories WHERE project_id = ? AND content_hash = ? AND is_latest = 1 "
                                "AND key IS ?", p["id"], digest, key)
            if dup:
                self._audit(db, agent_id, "memory_duplicate", p["id"], memory_id=dup["id"])
                return {"memory": dup, "duplicate": True}
            prev = key and self._one(db, "SELECT * FROM memories WHERE project_id = ? AND key = ? AND is_latest = 1",
                                     p["id"], key)
            now, mid = self._ts(), _id("mem")
            if prev:
                db.execute("UPDATE memories SET is_latest = 0, updated_at = ? WHERE id = ?", (now, prev["id"]))
            db.execute(
                "INSERT INTO memories (id, project_id, kind, key, content, content_hash, tags, version, supersedes, "
                "agent_id, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (mid, p["id"], kind, key, content, digest, json.dumps(sorted(set(tags or []))),
                 prev["version"] + 1 if prev else 1, prev["id"] if prev else None, agent_id, now, now))
            self._audit(db, agent_id, "memory_saved", p["id"], memory_id=mid, kind=kind, key=key)
            return {"memory": self._one(db, "SELECT * FROM memories WHERE id = ?", mid), "duplicate": False}

    def search_memory(self, query: str, project: str | None = None, kind: str | None = None, limit: int = 20,
                      include_history: bool = False) -> dict[str, Any]:
        # shortcut: LIKE scan over all words; switch to FTS5 / Postgres full-text past ~100k memories.
        where, args = [], []
        for term in query.split():
            where.append("(m.content LIKE ? ESCAPE '\\' OR m.key LIKE ? ESCAPE '\\' OR m.tags LIKE ? ESCAPE '\\')")
            args += [_like(term)] * 3
        with self._tx() as db:
            if project:
                where.append("m.project_id = ?")
                args.append(self._project(db, project)["id"])
            if kind:
                where.append("m.kind = ?")
                args.append(kind)
            if not include_history:
                where.append("m.is_latest = 1")
            sql = ("SELECT m.*, p.name AS project_name FROM memories m JOIN projects p ON p.id = m.project_id "
                   f"WHERE {' AND '.join(where) or '1'} ORDER BY m.created_at DESC, m.rowid DESC LIMIT ?")
            return {"results": self._all(db, sql, *args, limit)}

    def project_context(self, project: str, limit: int = 10) -> dict[str, Any]:
        with self._tx() as db:
            self._recover_expired(db)
            p = self._project(db, project)
            pid = p["id"]
            latest = "SELECT * FROM memories WHERE project_id = ? AND is_latest = 1 AND kind {} ORDER BY created_at DESC, rowid DESC LIMIT ?"
            return {
                "project": p,
                "context": self._all(db, latest.format("= 'context'"), pid, limit),
                "decisions": self._all(db, latest.format("= 'decision'"), pid, limit),
                "recent_memories": self._all(db, latest.format("IN ('note', 'fact')"), pid, limit),
                "task_counts": {r["status"]: r["n"] for r in self._all(
                    db, "SELECT status, COUNT(*) AS n FROM tasks WHERE project_id = ? GROUP BY status", pid)},
                "unfinished_tasks": self._all(
                    db, f"SELECT * FROM tasks WHERE project_id = ? AND status NOT IN {FINISHED} "
                        "ORDER BY priority, created_at, rowid LIMIT ?", pid, limit * 5),
                "recent_handovers": self._all(
                    db, "SELECT * FROM handovers WHERE project_id = ? ORDER BY created_at DESC, rowid DESC LIMIT ?", pid, limit),
                "pending_approvals": self._all(
                    db, "SELECT * FROM approvals WHERE project_id = ? AND status = 'pending' ORDER BY created_at, rowid", pid),
            }

    # ---- tasks ---------------------------------------------------------

    def create_task(self, agent_id: str, project: str, title: str, description: str = "", priority: int = 3,
                    required_capability: str | None = None, parent_task_id: str | None = None,
                    sensitive_actions: list[str] | None = None) -> dict[str, Any]:
        with self._tx() as db:
            self._touch(db, agent_id)
            p = self._project(db, project)
            task = self._insert_task(db, p["id"], agent_id, title, description, priority, required_capability,
                                     parent_task_id, sensitive_actions or [])
            approvals = self._all(db, "SELECT * FROM approvals WHERE task_id = ?", task["id"])
            return {"task": task, "approvals": approvals}

    def list_tasks(self, project: str | None = None, status: str | None = None, agent_id: str | None = None,
                   limit: int = 50) -> dict[str, Any]:
        where, args = [], []
        with self._tx() as db:
            self._recover_expired(db)
            if project:
                where.append("project_id = ?")
                args.append(self._project(db, project)["id"])
            if status:
                where.append("status = ?")
                args.append(status)
            if agent_id:
                where.append("assigned_to = ?")
                args.append(agent_id)
            sql = f"SELECT * FROM tasks WHERE {' AND '.join(where) or '1'} ORDER BY priority, created_at, rowid LIMIT ?"
            return {"tasks": self._all(db, sql, *args, limit)}

    def claim_next_task(self, agent_id: str, capabilities: list[str] | None = None, project: str | None = None,
                        lease_seconds: int | None = None) -> dict[str, Any]:
        lease = lease_seconds or self.lease_seconds
        with self._tx() as db:
            caps = self._touch(db, agent_id, capabilities)["capabilities"]
            self._recover_expired(db)
            scope, args = "", []
            if project:
                scope = "AND t.project_id = ?"
                args.append(self._project(db, project)["id"])
            held = self._one(db, f"SELECT * FROM tasks t WHERE t.assigned_to = ? AND t.status IN {LEASED} {scope} "
                                 "ORDER BY t.priority, t.created_at, t.rowid LIMIT 1", agent_id, *args)
            if held:
                db.execute("UPDATE tasks SET lease_expires_at = ?, updated_at = ? WHERE id = ?",
                           (self._ts(lease), self._ts(), held["id"]))
                self._audit(db, agent_id, "task_resumed", held["project_id"], held["id"])
                return {"task": self._task(db, held["id"]), "resumed": True,
                        "handovers": self._handovers(db, held["id"]),
                        "message": "You already hold this task; finish it or release it (update_task status='open') "
                                   "before claiming another."}
            cap_sql = f"OR t.required_capability IN ({', '.join('?' * len(caps))})" if caps else ""
            task = self._one(db, (
                f"SELECT t.* FROM tasks t WHERE t.status = 'open' {scope} "
                f"AND (t.required_capability IS NULL {cap_sql}) "
                f"AND NOT EXISTS (SELECT 1 FROM tasks c WHERE c.parent_id = t.id AND c.status NOT IN {FINISHED}) "
                "ORDER BY t.priority, t.created_at, t.rowid LIMIT 1"), *args, *caps)
            if not task:
                return {"task": None, "resumed": False, "handovers": [],
                        "message": "No claimable task. Tasks may be taken, waiting on subtasks, waiting for human "
                                   "approval, or need a capability you did not list."}
            # The status guard is belt-and-braces; BEGIN IMMEDIATE already serialises claimers.
            cur = db.execute("UPDATE tasks SET status = 'claimed', assigned_to = ?, lease_expires_at = ?, "
                             "attempts = attempts + 1, updated_at = ? WHERE id = ? AND status = 'open'",
                             (agent_id, self._ts(lease), self._ts(), task["id"]))
            if cur.rowcount != 1:
                raise BridgeError("Task was claimed by another agent; try again.")
            self._audit(db, agent_id, "task_claimed", task["project_id"], task["id"])
            return {"task": self._task(db, task["id"]), "resumed": False,
                    "handovers": self._handovers(db, task["id"]),
                    "message": "Claimed. Call update_task with status='in_progress' when you start work, and "
                               "update it again before lease_expires_at to keep the lease."}

    def update_task(self, agent_id: str, task_id: str, status: str | None = None, progress_note: str | None = None,
                    lease_seconds: int | None = None) -> dict[str, Any]:
        lease = lease_seconds or self.lease_seconds
        with self._tx() as db:
            self._touch(db, agent_id)
            self._recover_expired(db)
            t = self._task(db, task_id)
            if t["assigned_to"] != agent_id or t["status"] not in TRANSITIONS:
                raise BridgeError(f"Task {task_id} is not leased to {agent_id} (status {t['status']}, holder "
                                  f"{t['assigned_to'] or 'none'}). Claim it with claim_next_task first.")
            new = status or t["status"]
            if new not in TRANSITIONS[t["status"]] and new != t["status"]:
                hint = " Set status='in_progress' first." if new == "done" else ""
                raise BridgeError(f"Cannot move task from {t['status']} to {new}.{hint}")
            if new == "done" and db.execute(
                    f"SELECT 1 FROM tasks WHERE parent_id = ? AND status NOT IN {FINISHED}", (task_id,)).fetchone():
                raise BridgeError("Task has unfinished subtasks.")
            now = self._ts()
            if new == "open":  # release
                assigned, expires = None, None
            elif new in ("done", "cancelled"):
                assigned, expires = agent_id, None
            else:
                assigned, expires = agent_id, self._ts(lease)
            db.execute("UPDATE tasks SET status = ?, assigned_to = ?, lease_expires_at = ?, progress = ?, "
                       "updated_at = ? WHERE id = ?",
                       (new, assigned, expires, progress_note if progress_note is not None else t["progress"],
                        now, task_id))
            self._audit(db, agent_id, "task_updated", t["project_id"], task_id, old_status=t["status"],
                        new_status=new, note=progress_note)
            return {"task": self._task(db, task_id)}

    def request_agent_help(self, agent_id: str, task_id: str, request: str, capability: str | None = None,
                           priority: int | None = None) -> dict[str, Any]:
        with self._tx() as db:
            self._touch(db, agent_id)
            t = self._task(db, task_id)
            if t["status"] in ("done", "cancelled"):
                raise BridgeError(f"Task {task_id} is already {t['status']}.")
            title = "Help: " + " ".join(request.split())[:80]
            help_task = self._insert_task(db, t["project_id"], agent_id, title, request, priority or t["priority"],
                                          capability, task_id, [])
            self._audit(db, agent_id, "help_requested", t["project_id"], task_id, help_task_id=help_task["id"],
                        capability=capability)
            return {"help_task": help_task}

    def submit_handover(self, agent_id: str, project: str, summary: str, next_steps: str = "",
                        task_id: str | None = None, to_agent: str | None = None) -> dict[str, Any]:
        with self._tx() as db:
            self._touch(db, agent_id)
            p = self._project(db, project)
            released = False
            if task_id:
                t = self._task(db, task_id)
                if t["project_id"] != p["id"]:
                    raise BridgeError("task_id belongs to a different project.")
                if t["assigned_to"] == agent_id and t["status"] in TRANSITIONS:
                    db.execute("UPDATE tasks SET status = 'open', assigned_to = NULL, lease_expires_at = NULL, "
                               "progress = ?, updated_at = ? WHERE id = ?", (summary, self._ts(), task_id))
                    released = True
            hid = _id("hand")
            db.execute("INSERT INTO handovers (id, project_id, task_id, from_agent, to_agent, summary, next_steps, "
                       "created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                       (hid, p["id"], task_id, agent_id, to_agent, summary, next_steps, self._ts()))
            self._audit(db, agent_id, "handover_submitted", p["id"], task_id, handover_id=hid, released=released,
                        to_agent=to_agent)
            return {"handover": self._one(db, "SELECT * FROM handovers WHERE id = ?", hid), "task_released": released}

    def agent_activity(self, agent_id: str | None = None, project: str | None = None,
                       limit: int = 50) -> dict[str, Any]:
        with self._tx() as db:
            self._recover_expired(db)
            agents = self._all(db, "SELECT * FROM agents WHERE ? IS NULL OR id = ? ORDER BY last_seen_at DESC",
                               agent_id, agent_id)
            for a in agents:
                a["active_tasks"] = self._all(db, f"SELECT id, project_id, title, status, lease_expires_at FROM tasks "
                                                  f"WHERE assigned_to = ? AND status IN {LEASED}", a["id"])
            pid = self._project(db, project)["id"] if project else None
            activity = self._all(db, "SELECT * FROM audit_log WHERE (? IS NULL OR actor = ?) AND "
                                     "(? IS NULL OR project_id = ?) ORDER BY created_at DESC, rowid DESC LIMIT ?",
                                 agent_id, agent_id, pid, pid, limit)
            return {"agents": agents, "activity": activity}

    def log_refusal(self, agent_id: str | None, operation: str, error: str, project: str | None = None,
                    task_id: str | None = None) -> None:
        """Audit a refused call. Its own transaction was rolled back, so this is the only trace of the attempt."""
        with self._tx() as db:
            row = task_id and self._one(db, "SELECT project_id FROM tasks WHERE id = ?", task_id)
            if not row and project:
                row = self._one(db, "SELECT id AS project_id FROM projects WHERE id = ? OR name = ?", project, project)
            self._audit(db, agent_id or "unknown", "refused", row["project_id"] if row else None, task_id,
                        operation=operation, error=error)

    # ---- approvals (human only: exposed through the CLI, never as an MCP tool) --

    def list_approvals(self, status: str = "pending") -> dict[str, Any]:
        with self._tx() as db:
            return {"approvals": self._all(
                db, "SELECT a.*, t.title AS task_title FROM approvals a JOIN tasks t ON t.id = a.task_id "
                    "WHERE a.status = ? ORDER BY a.created_at, a.rowid", status)}

    def decide_approval(self, approval_id: str, approve: bool, decided_by: str, reason: str = "") -> dict[str, Any]:
        with self._tx() as db:
            a = self._one(db, "SELECT * FROM approvals WHERE id = ?", approval_id)
            if not a:
                raise BridgeError(f"Unknown approval {approval_id!r}.")
            if a["status"] != "pending":
                raise BridgeError(f"Approval {approval_id} is already {a['status']}.")
            now, status = self._ts(), "approved" if approve else "rejected"
            db.execute("UPDATE approvals SET status = ?, decided_by = ?, reason = ?, updated_at = ? WHERE id = ?",
                       (status, decided_by, reason, now, approval_id))
            self._audit(db, f"human:{decided_by}", f"approval_{status}", a["project_id"], a["task_id"],
                        sensitive_action=a["action"], reason=reason)
            t = self._task(db, a["task_id"])
            if t["status"] == "awaiting_approval":
                if not approve:
                    db.execute("UPDATE tasks SET status = 'cancelled', progress = ?, updated_at = ? WHERE id = ?",
                               (f"{a['action']} rejected by {decided_by}: {reason}", now, t["id"]))
                elif not db.execute("SELECT 1 FROM approvals WHERE task_id = ? AND status != 'approved'",
                                    (t["id"],)).fetchone():
                    db.execute("UPDATE tasks SET status = 'open', updated_at = ? WHERE id = ?", (now, t["id"]))
            return {"approval": self._one(db, "SELECT * FROM approvals WHERE id = ?", approval_id),
                    "task": self._task(db, a["task_id"])}
