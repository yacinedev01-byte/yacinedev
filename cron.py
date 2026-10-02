"""
YACINEDEV cron -- مهام مجدولة تعمل داخل thread خلفي واحد ضمن نفس حاوية Railway.
لا تحتاج أي خدمة خارجية. تُفحص كل TICK_SECONDS.
"""
from __future__ import annotations
import json
import os
import re
import threading
import time
import traceback
import urllib.request
from pathlib import Path
from flask import Blueprint, jsonify, request

bp = Blueprint("cron", __name__)

STORE_PATH = Path(os.environ.get("CRON_STORE_PATH", "/tmp/yd_sandbox/cron.json"))
_lock = threading.RLock()
_started = False
TICK_SECONDS = 30


def _load() -> dict:
    if not STORE_PATH.is_file():
        return {}
    try:
        return json.loads(STORE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save(data: dict) -> None:
    STORE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STORE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(STORE_PATH)


def _run_target(job: dict) -> dict:
    kind = job.get("target_kind", "url")
    base = os.environ.get("AGENT_SELF_URL") or "http://127.0.0.1:8080"
    key = os.environ.get("SHELL_API_KEY") or os.environ.get("PYTHON_API_KEY") or ""
    try:
        if kind == "url":
            req = urllib.request.Request(job["target_url"], method=job.get("method", "GET"))
            with urllib.request.urlopen(req, timeout=30) as resp:
                return {"ok": True, "status": resp.status}
        if kind == "cmd":
            body = json.dumps(job.get("payload") or {}).encode("utf-8")
            req = urllib.request.Request(
                base.rstrip("/") + "/cmd/" + job["cmd"], data=body,
                headers={"Content-Type": "application/json", "X-Api-Key": key}, method="POST",
            )
            with urllib.request.urlopen(req, timeout=60) as resp:
                return {"ok": True, "status": resp.status, "body": json.loads(resp.read().decode("utf-8"))}
        if kind == "job":
            body = json.dumps({"message": job.get("message", "")}).encode("utf-8")
            req = urllib.request.Request(
                base.rstrip("/") + "/agent/jobs", data=body,
                headers={"Content-Type": "application/json", "X-Api-Key": key}, method="POST",
            )
            with urllib.request.urlopen(req, timeout=30) as resp:
                return {"ok": True, "body": json.loads(resp.read().decode("utf-8"))}
        return {"ok": False, "error": f"target_kind غير معروف: {kind}"}
    except Exception as e:
        return {"ok": False, "error": str(e), "trace": traceback.format_exc()[-1000:]}


def _tick() -> None:
    while True:
        time.sleep(TICK_SECONDS)
        now = time.time()
        with _lock:
            jobs = _load()
            changed = False
            for job_id, job in jobs.items():
                if not job.get("enabled", True) or now < job.get("next_run", 0):
                    continue
                result = _run_target(job)
                job["last_run"] = now
                job["last_result"] = result
                job["next_run"] = now + max(60, int(job.get("interval_seconds", 3600)))
                changed = True
                try:
                    import webhooks as _webhooks
                    _webhooks.fire("cron.ran", {"job_id": job_id, "name": job.get("name"), "result": result})
                except Exception:
                    pass
            if changed:
                _save(jobs)


def start_background() -> None:
    global _started
    with _lock:
        if _started:
            return
        _started = True
    threading.Thread(target=_tick, daemon=True, name="yd-cron").start()


@bp.get("/cron")
def list_cron():
    with _lock:
        return jsonify(ok=True, jobs=_load())


@bp.post("/cron")
def create_cron():
    data = request.get_json(silent=True) or {}
    name = str(data.get("name") or "").strip()
    interval = int(data.get("interval_seconds") or 3600)
    target_kind = str(data.get("target_kind") or "url")
    if not name or interval < 60:
        return jsonify(ok=False, error="name مطلوب و interval_seconds >= 60"), 400
    job_id = re.sub(r"[^a-zA-Z0-9_\-]", "", str(data.get("id") or f"cron_{int(time.time())}"))[:64]
    job = {
        "id": job_id, "name": name, "enabled": True, "interval_seconds": interval,
        "target_kind": target_kind, "target_url": data.get("target_url"), "method": data.get("method", "GET"),
        "cmd": data.get("cmd"), "payload": data.get("payload"), "message": data.get("message"),
        "created_at": time.time(), "next_run": time.time() + 5, "last_run": None, "last_result": None,
    }
    with _lock:
        jobs = _load()
        jobs[job_id] = job
        _save(jobs)
    return jsonify(ok=True, id=job_id)


@bp.post("/cron/<job_id>/toggle")
def toggle_cron(job_id: str):
    job_id = re.sub(r"[^a-zA-Z0-9_\-]", "", job_id)
    with _lock:
        jobs = _load()
        if job_id not in jobs:
            return jsonify(ok=False, error="غير موجود"), 404
        jobs[job_id]["enabled"] = not jobs[job_id].get("enabled", True)
        _save(jobs)
        return jsonify(ok=True, enabled=jobs[job_id]["enabled"])


@bp.delete("/cron/<job_id>")
def delete_cron(job_id: str):
    job_id = re.sub(r"[^a-zA-Z0-9_\-]", "", job_id)
    with _lock:
        jobs = _load()
        if job_id not in jobs:
            return jsonify(ok=False, error="غير موجود"), 404
        del jobs[job_id]
        _save(jobs)
    return jsonify(ok=True, deleted=job_id)
