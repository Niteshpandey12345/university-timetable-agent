"""
SQLite persistence shared by every MCP server process, the API and the agent.

Because each MCP server is a separate OS process, state cannot live in Python
singletons (the v1 design). Everything durable -- the ingested dataset, every
solution version, pins, what-if constraints and the MCP audit trail -- lives
here. WAL mode allows concurrent readers with one writer.
"""
from __future__ import annotations

import json
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from ttagent.config import DB_PATH, ensure_dirs

SCHEMA = """
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT, updated REAL);
CREATE TABLE IF NOT EXISTS solutions (
    id TEXT PRIMARY KEY, created REAL, label TEXT, method TEXT, status TEXT, parent TEXT,
    options TEXT, assignments TEXT, unplaced TEXT, stats TEXT, validation TEXT, metrics TEXT,
    published INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS pins (id INTEGER PRIMARY KEY AUTOINCREMENT, token INTEGER, day TEXT,
    start INTEGER, room TEXT, note TEXT, created REAL);
CREATE TABLE IF NOT EXISTS unavailability (id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT, code TEXT,
    day TEXT, slot INTEGER, note TEXT, created REAL);
CREATE TABLE IF NOT EXISTS audit (id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, server TEXT, tool TEXT,
    args TEXT, summary TEXT, ms REAL, ok INTEGER);
"""


class Store:
    def __init__(self, path: str | Path | None = None) -> None:
        ensure_dirs()
        self.path = Path(path or DB_PATH)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.conn() as c:
            c.executescript(SCHEMA)

    @contextmanager
    def conn(self) -> Iterator[sqlite3.Connection]:
        c = sqlite3.connect(self.path, timeout=30)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        try:
            yield c
            c.commit()
        finally:
            c.close()

    # ------------------------------------------------------------ kv
    def put(self, key: str, value: Any) -> None:
        with self.conn() as c:
            c.execute("INSERT OR REPLACE INTO kv VALUES (?,?,?)", (key, json.dumps(value, default=str), time.time()))

    def get(self, key: str, default: Any = None) -> Any:
        with self.conn() as c:
            row = c.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return json.loads(row["value"]) if row else default

    def updated(self, key: str) -> float:
        with self.conn() as c:
            row = c.execute("SELECT updated FROM kv WHERE key=?", (key,)).fetchone()
        return row["updated"] if row else 0.0

    # ------------------------------------------------------------ solutions
    def save_solution(self, res: dict[str, Any], label: str = "", parent: str | None = None) -> str:
        sid = res.get("id") or f"S{time.strftime('%m%d%H%M%S')}-{uuid.uuid4().hex[:4]}"
        with self.conn() as c:
            c.execute("INSERT OR REPLACE INTO solutions (id, created, label, method, status, parent, options, assignments,"
                      " unplaced, stats, validation, metrics, published) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                      (sid, time.time(), label, res.get("method"), res.get("status"), parent,
                       json.dumps(res.get("options", {}), default=str), json.dumps(res.get("assignments", [])),
                       json.dumps(res.get("unplaced", [])), json.dumps(res.get("stats", {}) | {"repair": res.get("repair")}, default=str),
                       json.dumps(res.get("validation", {}), default=str), json.dumps(res.get("metrics", {}), default=str), 0))
        return sid

    def update_solution(self, sid: str, **fields: Any) -> None:
        sets, vals = [], []
        for k, v in fields.items():
            sets.append(f"{k}=?")
            vals.append(json.dumps(v, default=str) if isinstance(v, (dict, list)) else v)
        with self.conn() as c:
            c.execute(f"UPDATE solutions SET {', '.join(sets)} WHERE id=?", (*vals, sid))

    def load_solution(self, sid: str) -> dict[str, Any] | None:
        with self.conn() as c:
            if sid in ("latest", "current"):
                row = c.execute("SELECT * FROM solutions ORDER BY created DESC LIMIT 1").fetchone()
            elif sid == "published_version":
                row = c.execute("SELECT * FROM solutions WHERE published=1 ORDER BY created DESC LIMIT 1").fetchone()
            else:
                row = c.execute("SELECT * FROM solutions WHERE id=?", (sid,)).fetchone()
        if not row:
            return None
        d = dict(row)
        for k in ("options", "assignments", "unplaced", "stats", "validation", "metrics"):
            d[k] = json.loads(d[k]) if d[k] else {}
        return d

    def list_solutions(self, limit: int = 20) -> list[dict[str, Any]]:
        with self.conn() as c:
            rows = c.execute("SELECT id, created, label, method, status, parent, published, validation, metrics, stats "
                             "FROM solutions ORDER BY created DESC LIMIT ?", (limit,)).fetchall()
        out = []
        for r in rows:
            v, m = json.loads(r["validation"] or "{}"), json.loads(r["metrics"] or "{}")
            out.append({"id": r["id"], "created": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(r["created"])),
                        "label": r["label"], "method": r["method"], "status": r["status"], "parent": r["parent"],
                        "published": bool(r["published"]), "valid": v.get("valid"),
                        "coverage_pct": (v.get("allocation") or {}).get("coverage_pct"),
                        "hard_violations": v.get("hard_violation_total"), "soft_penalty": m.get("soft_penalty")})
        return out

    def mark_published(self, sid: str) -> None:
        with self.conn() as c:
            c.execute("UPDATE solutions SET published=0")
            c.execute("UPDATE solutions SET published=1 WHERE id=?", (sid,))

    # ------------------------------------------------------------ pins / unavailability
    def add_pin(self, token: int, day: str, start: int, room: str | None, note: str = "") -> int:
        with self.conn() as c:
            cur = c.execute("INSERT INTO pins (token, day, start, room, note, created) VALUES (?,?,?,?,?,?)",
                            (token, day, start, room, note, time.time()))
            return cur.lastrowid

    def pins(self) -> list[dict[str, Any]]:
        with self.conn() as c:
            return [dict(r) for r in c.execute("SELECT * FROM pins ORDER BY id")]

    def remove_pin(self, pin_id: int) -> bool:
        with self.conn() as c:
            return c.execute("DELETE FROM pins WHERE id=?", (pin_id,)).rowcount > 0

    def add_unavailability(self, kind: str, code: str, day: str, slot: int | None, note: str = "") -> int:
        with self.conn() as c:
            cur = c.execute("INSERT INTO unavailability (kind, code, day, slot, note, created) VALUES (?,?,?,?,?,?)",
                            (kind, code, day, slot, note, time.time()))
            return cur.lastrowid

    def unavailability(self) -> list[dict[str, Any]]:
        with self.conn() as c:
            return [dict(r) for r in c.execute("SELECT * FROM unavailability ORDER BY id")]

    def remove_unavailability(self, uid: int) -> bool:
        with self.conn() as c:
            return c.execute("DELETE FROM unavailability WHERE id=?", (uid,)).rowcount > 0

    # ------------------------------------------------------------ audit
    def audit(self, server: str, tool: str, args: dict, summary: str, ms: float, ok: bool) -> None:
        with self.conn() as c:
            c.execute("INSERT INTO audit (ts, server, tool, args, summary, ms, ok) VALUES (?,?,?,?,?,?,?)",
                      (time.time(), server, tool, json.dumps(args, default=str)[:2000], summary[:2000], ms, int(ok)))

    def audit_tail(self, n: int = 50) -> list[dict[str, Any]]:
        with self.conn() as c:
            rows = c.execute("SELECT * FROM audit ORDER BY id DESC LIMIT ?", (n,)).fetchall()
        return [dict(r) | {"ts": time.strftime("%H:%M:%S", time.localtime(r["ts"]))} for r in rows]
