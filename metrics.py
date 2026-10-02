"""
YACINEDEV metrics -- تسجيل تنفيذ الأدوات (SQLite، بدون اعتماديات خارجية).
"""
from __future__ import annotations
import os
import sqlite3
import threading
import time
from pathlib import Path
from flask import Blueprint, jsonify, request

bp = Blueprint("metrics", __name__)

DB_PATH = Path(os.environ.get("METRICS_DB_PATH", "/tmp/yd_sandbox/metrics.sqlite3"))
_lock = threading.RLock()


def _conn() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(str(DB_PATH), timeout=10, check_same_thread=False)
    c.execute(
        """CREATE TABLE IF NOT EXISTS tool_calls (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tool TEXT NOT NULL,
            ok INTEGER NOT NULL,
            duration_ms INTEGER NOT NULL,
            error TEXT,
            session TEXT,
            created_at REAL NOT NULL
        )"""
    )
    c.execute("CREATE INDEX IF NOT EXISTS idx_tc_tool ON tool_calls(tool)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_tc_created ON tool_calls(created_at)")
    return c


def record(tool: str, ok: bool, duration_ms: int, error: str | None = None, session: str = "") -> None:
    with _lock:
        try:
            c = _conn()
            c.execute(
                "INSERT INTO tool_calls (tool, ok, duration_ms, error, session, created_at) VALUES (?,?,?,?,?,?)",
                (tool, 1 if ok else 0, duration_ms, (error or "")[:2000], session, time.time()),
            )
            c.commit()
            c.close()
        except Exception:
            pass  # القياسات لا توقف أبداً تنفيذ الأداة الفعلي


@bp.get("/metrics")
def metrics_summary():
    window = float(request.args.get("hours", 24)) * 3600
    since = time.time() - window
    with _lock:
        c = _conn()
        rows = c.execute(
            """SELECT tool, COUNT(*), SUM(ok), AVG(duration_ms), MAX(duration_ms)
               FROM tool_calls WHERE created_at >= ? GROUP BY tool ORDER BY 2 DESC""",
            (since,),
        ).fetchall()
        c.close()
    items = []
    for tool, calls, ok_calls, avg_ms, max_ms in rows:
        ok_calls = ok_calls or 0
        items.append({
            "tool": tool, "calls": calls,
            "success_rate": round(ok_calls / calls, 3) if calls else 0,
            "fail_rate": round(1 - (ok_calls / calls), 3) if calls else 0,
            "avg_ms": round(avg_ms or 0, 1), "max_ms": max_ms or 0,
        })
    return jsonify(ok=True, window_hours=window / 3600, tools=items)


@bp.get("/metrics/recent")
def metrics_recent():
    limit = min(int(request.args.get("limit", 50)), 500)
    with _lock:
        c = _conn()
        rows = c.execute(
            "SELECT tool, ok, duration_ms, error, session, created_at FROM tool_calls ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        c.close()
    items = [{"tool": r[0], "ok": bool(r[1]), "duration_ms": r[2], "error": r[3], "session": r[4], "at": r[5]} for r in rows]
    return jsonify(ok=True, items=items)
