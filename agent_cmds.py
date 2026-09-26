"""
YACINEDEV Agent Commands — Manus-style slash features (single-user Railway worker)
Endpoints are mounted from app.py under /cmd/*
"""
from __future__ import annotations

import concurrent.futures
import hashlib
import json
import os
import re
import shutil
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from flask import Blueprint, jsonify, request

bp = Blueprint("agent_cmds", __name__)

# Injected from app.py
WORKROOT: Path = Path("/tmp/yd_sandbox")
API_KEY_CHECK = None  # not needed; app.before_request already guards


def _session(name: str = "default") -> str:
    return re.sub(r"[^a-zA-Z0-9_-]", "_", str(name or "default"))[:64] or "default"


def _sess_dir(session: str) -> Path:
    p = (WORKROOT / _session(session) / "_agent").resolve()
    p.mkdir(parents=True, exist_ok=True)
    return p


def _read_json(path: Path, default: Any):
    try:
        if path.is_file():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        pass
    return default


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _body() -> dict:
    return request.get_json(silent=True) or {}


# ───────────────── /think ─────────────────

@bp.post("/think")
def cmd_think():
    """Store or toggle chain-of-thought for a session."""
    data = _body()
    session = _session(data.get("session"))
    d = _sess_dir(session)
    state_path = d / "think.json"
    state = _read_json(state_path, {"enabled": True, "entries": []})

    action = str(data.get("action") or "get").lower()
    if action in {"on", "enable"}:
        state["enabled"] = True
    elif action in {"off", "disable"}:
        state["enabled"] = False
    elif action == "append":
        text = str(data.get("text") or data.get("thought") or "").strip()
        if text:
            state.setdefault("entries", []).append({
                "t": time.time(),
                "text": text[:8000],
            })
            state["entries"] = state["entries"][-50:]
    elif action == "clear":
        state["entries"] = []
    elif action == "set":
        if "enabled" in data:
            state["enabled"] = bool(data["enabled"])

    _write_json(state_path, state)
    return jsonify(ok=True, command="/think", session=session, enabled=state.get("enabled", True),
                   entries=state.get("entries", [])[-20:], count=len(state.get("entries", [])))


# ───────────────── /plan ─────────────────

def _heuristic_plan(goal: str) -> list[dict]:
    """Lightweight planner without external LLM — splits goal into actionable steps."""
    g = goal.strip()
    steps = []
    low = g.lower()

    steps.append({"id": 1, "title": "فهم الهدف", "detail": g[:300], "status": "pending"})

    if re.search(r"بحث|search|research|مصادر|news|أخبار", low):
        steps.append({"id": 2, "title": "جمع مصادر", "detail": "بحث متوازي عبر /wide-research", "status": "pending"})
        steps.append({"id": 3, "title": "تلخيص النتائج", "detail": "دمج وإزالة التكرار", "status": "pending"})
    if re.search(r"كود|code|python|برنامج|script|موقع|html|app", low):
        steps.append({"id": 2 if len(steps) == 1 else len(steps) + 1, "title": "تهيئة Sandbox", "detail": "مجلد جلسة معزول", "status": "pending"})
        steps.append({"id": len(steps) + 1, "title": "تنفيذ/بناء", "detail": "تشغيل كود أو إنشاء ملفات", "status": "pending"})
        steps.append({"id": len(steps) + 1, "title": "اختبار النتيجة", "detail": "التحقق من المخرجات", "status": "pending"})
    if re.search(r"نشر|deploy|استضاف", low):
        steps.append({"id": len(steps) + 1, "title": "تجهيز الملفات للنشر", "detail": "جمع المخرجات", "status": "pending"})
        steps.append({"id": len(steps) + 1, "title": "نشر", "detail": "/deploy", "status": "pending"})
    if re.search(r"طقس|weather", low):
        steps.append({"id": len(steps) + 1, "title": "جلب بيانات الطقس", "detail": "أداة weather", "status": "pending"})
        steps.append({"id": len(steps) + 1, "title": "عرض منظم", "detail": "تلخيص للمستخدم", "status": "pending"})

    if len(steps) == 1:
        steps.append({"id": 2, "title": "تنفيذ المهمة", "detail": g[:200], "status": "pending"})
        steps.append({"id": 3, "title": "التحقق والتسليم", "detail": "مراجعة الناتج النهائي", "status": "pending"})

    # renumber
    for i, s in enumerate(steps, 1):
        s["id"] = i
    return steps


@bp.post("/plan")
def cmd_plan():
    data = _body()
    session = _session(data.get("session"))
    d = _sess_dir(session)
    path = d / "plan.json"
    action = str(data.get("action") or "create").lower()

    if action == "get":
        plan = _read_json(path, None)
        if not plan:
            return jsonify(ok=False, error="لا توجد خطة"), 404
        return jsonify(ok=True, command="/plan", plan=plan)

    if action == "update_step":
        plan = _read_json(path, None)
        if not plan:
            return jsonify(ok=False, error="لا توجد خطة"), 404
        sid = int(data.get("step_id") or 0)
        status = str(data.get("status") or "done")
        for s in plan.get("steps", []):
            if s.get("id") == sid:
                s["status"] = status
                if data.get("note"):
                    s["note"] = str(data["note"])[:500]
        _write_json(path, plan)
        return jsonify(ok=True, command="/plan", plan=plan)

    if action == "clear":
        if path.exists():
            path.unlink()
        return jsonify(ok=True, command="/plan", cleared=True)

    goal = str(data.get("goal") or data.get("task") or data.get("message") or "").strip()
    if not goal:
        return jsonify(ok=False, error="goal مطلوب"), 400

    steps = data.get("steps")
    if not isinstance(steps, list) or not steps:
        steps = _heuristic_plan(goal)
    else:
        norm = []
        for i, s in enumerate(steps, 1):
            if isinstance(s, str):
                norm.append({"id": i, "title": s, "detail": "", "status": "pending"})
            elif isinstance(s, dict):
                norm.append({
                    "id": int(s.get("id") or i),
                    "title": str(s.get("title") or s.get("name") or f"خطوة {i}"),
                    "detail": str(s.get("detail") or s.get("description") or ""),
                    "status": str(s.get("status") or "pending"),
                })
        steps = norm

    plan = {
        "goal": goal,
        "created_at": time.time(),
        "status": "ready",
        "steps": steps,
    }
    _write_json(path, plan)
    # also log replay event
    _append_replay(session, {"type": "plan", "goal": goal, "steps": len(steps)})
    return jsonify(ok=True, command="/plan", plan=plan)


# ───────────────── /wide-research ─────────────────

def _fetch_url(url: str, timeout: int = 12) -> dict:
    try:
        if not url.startswith("http"):
            url = "https://" + url
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "YACINEDEV-ResearchBot/1.0"},
            method="GET",
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read(120_000)
            ctype = resp.headers.get("Content-Type", "")
            text = raw.decode("utf-8", errors="replace")
            # strip tags lightly
            text = re.sub(r"(?is)<script[^>]*>.*?</script>", " ", text)
            text = re.sub(r"(?is)<style[^>]*>.*?</style>", " ", text)
            text = re.sub(r"(?is)<[^>]+>", " ", text)
            text = re.sub(r"\s+", " ", text).strip()
            title_m = re.search(r"(?is)<title[^>]*>(.*?)</title>", raw.decode("utf-8", errors="replace"))
            title = re.sub(r"\s+", " ", title_m.group(1)).strip() if title_m else url
            return {
                "ok": True,
                "url": url,
                "title": title[:200],
                "snippet": text[:600],
                "content_type": ctype,
            }
    except Exception as e:
        return {"ok": False, "url": url, "error": str(e)[:200]}


def _ddg_links(query: str, n: int = 8) -> list[str]:
    """Best-effort DuckDuckGo HTML links (no API key)."""
    try:
        q = urllib.parse.quote_plus(query)
        url = f"https://html.duckduckgo.com/html/?q={q}"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 YDResearch/1.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            html = resp.read().decode("utf-8", errors="replace")
        links = re.findall(r'uddg=([^&"]+)', html)
        out = []
        for u in links:
            u = urllib.parse.unquote(u)
            if u.startswith("http") and u not in out:
                out.append(u)
            if len(out) >= n:
                break
        return out
    except Exception:
        return []


@bp.post("/wide-research")
def cmd_wide_research():
    data = _body()
    session = _session(data.get("session"))
    query = str(data.get("query") or data.get("q") or "").strip()
    urls = data.get("urls") if isinstance(data.get("urls"), list) else []
    max_sources = max(1, min(int(data.get("max_sources") or 12), 24))

    if not query and not urls:
        return jsonify(ok=False, error="query أو urls مطلوب"), 400

    if query and not urls:
        urls = _ddg_links(query, max_sources)

    urls = [str(u).strip() for u in urls if str(u).strip()][:max_sources]
    if not urls:
        return jsonify(ok=False, error="لم يُعثر على مصادر — مرّر urls يدوياً أو غيّر الاستعلام"), 404

    results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(8, len(urls))) as ex:
        futs = {ex.submit(_fetch_url, u): u for u in urls}
        for fut in concurrent.futures.as_completed(futs):
            results.append(fut.result())

    results.sort(key=lambda r: (0 if r.get("ok") else 1))
    payload = {
        "query": query,
        "sources": results,
        "ok_count": sum(1 for r in results if r.get("ok")),
        "total": len(results),
        "t": time.time(),
    }
    out = _sess_dir(session) / "research.json"
    _write_json(out, payload)
    _append_replay(session, {"type": "wide_research", "query": query, "total": len(results)})
    return jsonify(ok=True, command="/wide-research", **payload)


# ───────────────── /agent-mode ─────────────────

@bp.post("/agent-mode")
def cmd_agent_mode():
    data = _body()
    session = _session(data.get("session"))
    path = _sess_dir(session) / "agent_mode.json"
    action = str(data.get("action") or "status").lower()
    state = _read_json(path, {
        "active": False,
        "max_steps": 20,
        "steps_done": 0,
        "started_at": None,
        "goal": "",
        "log": [],
    })

    if action == "start":
        state = {
            "active": True,
            "max_steps": max(1, min(int(data.get("max_steps") or 20), 50)),
            "steps_done": 0,
            "started_at": time.time(),
            "goal": str(data.get("goal") or state.get("goal") or ""),
            "log": [],
        }
        _append_replay(session, {"type": "agent_mode_start", "goal": state["goal"]})
    elif action == "stop":
        state["active"] = False
        state["stopped_at"] = time.time()
        _append_replay(session, {"type": "agent_mode_stop"})
    elif action == "tick":
        if not state.get("active"):
            return jsonify(ok=False, error="agent-mode غير مفعّل", state=state), 400
        state["steps_done"] = int(state.get("steps_done") or 0) + 1
        note = str(data.get("note") or "")[:300]
        state.setdefault("log", []).append({"t": time.time(), "step": state["steps_done"], "note": note})
        state["log"] = state["log"][-40:]
        if state["steps_done"] >= int(state.get("max_steps") or 20):
            state["active"] = False
            state["finished_reason"] = "max_steps"
    elif action == "status":
        pass
    else:
        return jsonify(ok=False, error="action: start|stop|tick|status"), 400

    _write_json(path, state)
    return jsonify(ok=True, command="/agent-mode", state=state)


# ───────────────── /take-over ─────────────────

@bp.post("/take-over")
def cmd_take_over():
    data = _body()
    session = _session(data.get("session"))
    path = _sess_dir(session) / "takeover.json"
    action = str(data.get("action") or "status").lower()
    state = _read_json(path, {
        "awaiting": False,
        "prompt": "",
        "human_input": None,
        "requested_at": None,
        "resolved_at": None,
    })

    if action in {"request", "pause"}:
        state = {
            "awaiting": True,
            "prompt": str(data.get("prompt") or data.get("message") or "مطلوب تدخل بشري"),
            "kind": str(data.get("kind") or "input"),  # input|sms|captcha|decision
            "human_input": None,
            "requested_at": time.time(),
            "resolved_at": None,
        }
        _append_replay(session, {"type": "take_over_request", "prompt": state["prompt"]})
    elif action in {"resume", "submit"}:
        state["human_input"] = str(data.get("input") or data.get("text") or "")
        state["awaiting"] = False
        state["resolved_at"] = time.time()
        _append_replay(session, {"type": "take_over_resume"})
    elif action == "status":
        pass
    else:
        return jsonify(ok=False, error="action: request|resume|status"), 400

    _write_json(path, state)
    return jsonify(ok=True, command="/take-over", state=state)


# ───────────────── /sandbox ─────────────────

@bp.post("/sandbox")
def cmd_sandbox():
    """Ensure isolated session workspace (uses same WORKROOT as shell/code)."""
    data = _body()
    session = _session(data.get("session"))
    action = str(data.get("action") or "info").lower()
    root = (WORKROOT / session).resolve()
    root.mkdir(parents=True, exist_ok=True)
    agent = _sess_dir(session)

    if action == "reset":
        if root.exists():
            shutil.rmtree(root, ignore_errors=True)
        root.mkdir(parents=True, exist_ok=True)
        agent.mkdir(parents=True, exist_ok=True)
        _append_replay(session, {"type": "sandbox_reset"})
        return jsonify(ok=True, command="/sandbox", session=session, path=str(root), reset=True)

    if action == "write":
        rel = str(data.get("path") or "").strip().lstrip("/")
        if not rel or ".." in rel:
            return jsonify(ok=False, error="path غير صالح"), 400
        content = str(data.get("content") or "")
        p = (root / rel).resolve()
        if root not in p.parents and p != root:
            return jsonify(ok=False, error="خارج الـ sandbox"), 400
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        return jsonify(ok=True, command="/sandbox", path=rel, size=len(content.encode("utf-8")))

    if action == "list":
        items = []
        for p in sorted(root.rglob("*")):
            if "_agent" in p.parts:
                continue
            rel = str(p.relative_to(root))
            items.append({"path": rel, "type": "dir" if p.is_dir() else "file",
                          "size": p.stat().st_size if p.is_file() else 0})
            if len(items) >= 300:
                break
        return jsonify(ok=True, command="/sandbox", session=session, items=items)

    # info
    files = sum(1 for p in root.rglob("*") if p.is_file() and "_agent" not in p.parts)
    return jsonify(ok=True, command="/sandbox", session=session, path=str(root), files=files,
                   note="استخدم /shell أو /code/run مع session نفسه للتنفيذ داخل العزل")


# ───────────────── /replay ─────────────────

def _append_replay(session: str, event: dict) -> None:
    path = _sess_dir(session) / "replay.jsonl"
    event = dict(event)
    event["t"] = event.get("t") or time.time()
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(event, ensure_ascii=False) + "\n")


@bp.post("/replay")
def cmd_replay():
    data = _body()
    session = _session(data.get("session"))
    path = _sess_dir(session) / "replay.jsonl"
    action = str(data.get("action") or "list").lower()

    if action == "append":
        ev = data.get("event") if isinstance(data.get("event"), dict) else {
            "type": str(data.get("type") or "note"),
            "text": str(data.get("text") or "")[:2000],
        }
        _append_replay(session, ev)
        return jsonify(ok=True, command="/replay", appended=True)

    if action == "clear":
        if path.exists():
            path.unlink()
        return jsonify(ok=True, command="/replay", cleared=True)

    events = []
    if path.is_file():
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except Exception:
                continue
    limit = max(1, min(int(data.get("limit") or 200), 500))
    return jsonify(ok=True, command="/replay", session=session, events=events[-limit:], count=len(events))


# ───────────────── /deploy ─────────────────

@bp.post("/deploy")
def cmd_deploy():
    """
    Package session files as static site.
    Optional: NETLIFY_AUTH_TOKEN + NETLIFY_SITE_ID for real deploy.
    """
    data = _body()
    session = _session(data.get("session"))
    root = (WORKROOT / session).resolve()
    root.mkdir(parents=True, exist_ok=True)

    # ensure index.html exists if missing
    index = root / "index.html"
    if not index.is_file():
        title = str(data.get("title") or "YACINEDEV Deploy")
        body = str(data.get("html") or f"<h1>{title}</h1><p>Deployed from agent session.</p>")
        index.write_text(
            f"<!DOCTYPE html><html lang='ar' dir='rtl'><head><meta charset='utf-8'>"
            f"<meta name='viewport' content='width=device-width,initial-scale=1'>"
            f"<title>{title}</title></head><body>{body}</body></html>",
            encoding="utf-8",
        )

    files = [str(p.relative_to(root)) for p in root.rglob("*")
             if p.is_file() and "_agent" not in p.parts][:200]

    result = {
        "ok": True,
        "command": "/deploy",
        "session": session,
        "local_path": str(root),
        "files": files,
        "live_url": None,
        "note": "الملفات جاهزة في الـ sandbox",
    }

    token = os.environ.get("NETLIFY_AUTH_TOKEN") or ""
    site_id = os.environ.get("NETLIFY_SITE_ID") or str(data.get("site_id") or "")
    if token and site_id and data.get("publish"):
        # Minimal: zip not implemented without extra deps — report ready for manual
        result["note"] = "توكن Netlify موجود — ارفع المجلد يدوياً أو أضف zip deploy لاحقاً"
        result["netlify_configured"] = True
    elif data.get("publish"):
        result["note"] = "اضبط NETLIFY_AUTH_TOKEN و NETLIFY_SITE_ID للنشر التلقائي"

    _append_replay(session, {"type": "deploy", "files": len(files)})
    _write_json(_sess_dir(session) / "deploy.json", result)
    return jsonify(result)


# ───────────────── /skill ─────────────────

def _skills_dir() -> Path:
    p = WORKROOT / "_skills"
    p.mkdir(parents=True, exist_ok=True)
    return p


@bp.post("/skill")
def cmd_skill():
    data = _body()
    action = str(data.get("action") or "list").lower()
    root = _skills_dir()

    if action == "list":
        items = []
        for p in sorted(root.glob("*.json")):
            try:
                meta = json.loads(p.read_text(encoding="utf-8"))
                items.append({"name": p.stem, "description": meta.get("description", ""),
                              "steps": len(meta.get("steps") or [])})
            except Exception:
                items.append({"name": p.stem})
        return jsonify(ok=True, command="/skill", skills=items)

    name = re.sub(r"[^a-zA-Z0-9_\-]", "", str(data.get("name") or "").lower())[:64]

    if action == "save":
        if not name:
            return jsonify(ok=False, error="name مطلوب"), 400
        skill = {
            "name": name,
            "description": str(data.get("description") or "")[:300],
            "steps": data.get("steps") if isinstance(data.get("steps"), list) else [],
            "prompt": str(data.get("prompt") or "")[:4000],
            "tools": data.get("tools") if isinstance(data.get("tools"), list) else [],
            "saved_at": time.time(),
        }
        _write_json(root / f"{name}.json", skill)
        return jsonify(ok=True, command="/skill", saved=name, skill=skill)

    if action == "get":
        if not name:
            return jsonify(ok=False, error="name مطلوب"), 400
        p = root / f"{name}.json"
        if not p.is_file():
            return jsonify(ok=False, error="المهارة غير موجودة"), 404
        return jsonify(ok=True, command="/skill", skill=json.loads(p.read_text(encoding="utf-8")))

    if action == "delete":
        if not name:
            return jsonify(ok=False, error="name مطلوب"), 400
        p = root / f"{name}.json"
        p.unlink(missing_ok=True)
        return jsonify(ok=True, command="/skill", deleted=name)

    return jsonify(ok=False, error="action: list|save|get|delete"), 400


# ───────────────── /connect ─────────────────

@bp.post("/connect")
def cmd_connect():
    """Store service tokens for sole-user agent (not for multi-tenant)."""
    data = _body()
    session = _session(data.get("session") or "default")
    path = _sess_dir(session) / "connections.json"
    store = _read_json(path, {})
    action = str(data.get("action") or "list").lower()
    service = re.sub(r"[^a-z0-9_\-]", "", str(data.get("service") or "").lower())[:32]

    if action == "list":
        safe = {k: {"linked": True, "label": v.get("label") or k} for k, v in store.items()}
        return jsonify(ok=True, command="/connect", connections=safe)

    if action == "set":
        if not service:
            return jsonify(ok=False, error="service مطلوب (github|google|slack|...)"), 400
        token = str(data.get("token") or data.get("api_key") or "")
        if not token:
            return jsonify(ok=False, error="token مطلوب"), 400
        # store hashed hint + token (sole user trust model)
        store[service] = {
            "token": token,
            "label": str(data.get("label") or service),
            "hint": hashlib.sha256(token.encode()).hexdigest()[:12],
            "saved_at": time.time(),
        }
        _write_json(path, store)
        return jsonify(ok=True, command="/connect", service=service, linked=True)

    if action == "get":
        if service not in store:
            return jsonify(ok=False, error="غير مربوط"), 404
        # return token only to authenticated API caller (already gated)
        return jsonify(ok=True, command="/connect", service=service, connection=store[service])

    if action == "delete":
        store.pop(service, None)
        _write_json(path, store)
        return jsonify(ok=True, command="/connect", deleted=service)

    return jsonify(ok=False, error="action: list|set|get|delete"), 400



# ───────────────── /dream ─────────────────

@bp.post("/dream")
def cmd_dream():
    """Background long-horizon job (survives on Railway worker, not shared hosting)."""
    data = _body()
    session = _session(data.get("session") or "dream")
    path = _sess_dir(session) / "dream.json"
    action = str(data.get("action") or "start").lower()
    state = _read_json(path, {
        "active": False, "goal": "", "deadline_s": 0, "started_at": None,
        "notify_on": "complete", "milestones": [], "report": None, "status": "idle",
    })

    if action == "start":
        goal = str(data.get("goal") or "").strip()
        if not goal:
            return jsonify(ok=False, error="goal مطلوب"), 400
        deadline = str(data.get("deadline") or "8h")
        # parse 8h / 30m / 2d
        m = re.match(r"^(\d+)\s*([hmsd])$", deadline.strip().lower())
        mult = {"s": 1, "m": 60, "h": 3600, "d": 86400}
        deadline_s = int(m.group(1)) * mult.get(m.group(2), 3600) if m else 8 * 3600
        state = {
            "active": True,
            "goal": goal,
            "deadline_s": deadline_s,
            "deadline_at": time.time() + deadline_s,
            "started_at": time.time(),
            "notify_on": str(data.get("notify_on") or "complete"),
            "milestones": [{"t": time.time(), "text": "بدأ الحلم / الخطة الخلفية"}],
            "report": None,
            "status": "running",
            "plan": _heuristic_plan(goal),
        }
        _append_replay(session, {"type": "dream_start", "goal": goal})
    elif action == "milestone":
        if not state.get("active"):
            return jsonify(ok=False, error="لا يوجد dream نشط"), 400
        state.setdefault("milestones", []).append({
            "t": time.time(), "text": str(data.get("text") or data.get("note") or "")[:500]
        })
    elif action == "complete":
        state["active"] = False
        state["status"] = "complete"
        state["report"] = data.get("report") or {
            "summary": str(data.get("summary") or "اكتمل"),
            "finished_at": time.time(),
        }
        state.setdefault("milestones", []).append({"t": time.time(), "text": "اكتمل"})
        _append_replay(session, {"type": "dream_complete"})
    elif action == "status":
        if state.get("deadline_at") and time.time() > state["deadline_at"] and state.get("active"):
            state["status"] = "deadline_passed"
    elif action == "stop":
        state["active"] = False
        state["status"] = "stopped"
    else:
        return jsonify(ok=False, error="action: start|milestone|complete|status|stop"), 400

    _write_json(path, state)
    return jsonify(ok=True, command="/dream", state=state)


# ───────────────── /evolve ─────────────────

@bp.post("/evolve")
def cmd_evolve():
    """Analyze gaps and optionally register a new tool (require_approval by default)."""
    data = _body()
    session = _session(data.get("session") or "default")
    path = _sess_dir(session) / "evolve.json"
    action = str(data.get("action") or "analyze").lower()

    if action == "analyze":
        # scan replay + skills for patterns
        replay_path = _sess_dir(session) / "replay.jsonl"
        events = []
        if replay_path.is_file():
            for line in replay_path.read_text(encoding="utf-8", errors="replace").splitlines()[-200:]:
                try:
                    events.append(json.loads(line))
                except Exception:
                    pass
        types = {}
        for e in events:
            t = str(e.get("type") or "other")
            types[t] = types.get(t, 0) + 1
        gaps = []
        if types.get("wide_research", 0) == 0:
            gaps.append({"gap": "research", "suggestion": "إضافة بحث تلقائي للمواضيع المتكررة"})
        if types.get("deploy", 0) == 0:
            gaps.append({"gap": "deploy", "suggestion": "مهارة نشر للمشاريع المتكررة"})
        proposal = {
            "analyzed_events": len(events),
            "type_counts": types,
            "gaps": gaps,
            "auto_create_tools": bool(data.get("auto_create_tools")),
            "require_approval": data.get("require_approval", True),
            "proposed_tools": [],
        }
        if gaps and data.get("auto_create_tools"):
            for g in gaps[:3]:
                proposal["proposed_tools"].append({
                    "name": f"auto_{g['gap']}",
                    "description": g["suggestion"],
                    "status": "pending_approval" if proposal["require_approval"] else "ready",
                })
        _write_json(path, proposal)
        return jsonify(ok=True, command="/evolve", proposal=proposal)

    if action == "approve":
        name = re.sub(r"[^a-z0-9_\-]", "", str(data.get("name") or "").lower())
        source = str(data.get("source") or "")
        if not name:
            return jsonify(ok=False, error="name مطلوب"), 400
        if not source:
            source = (
                "def run(args):\n"
                "    return {'ok': True, 'echo': args, 'note': 'evolved tool stub'}\n"
            )
        # write to tools dir via WORKROOT parent convention — use /tmp/yd_python_tools if exists
        tools = Path(os.environ.get("PYTHON_TOOLS_DIR", "/tmp/yd_python_tools"))
        tools.mkdir(parents=True, exist_ok=True)
        (tools / f"{name}.py").write_text(source, encoding="utf-8")
        (tools / f"{name}.meta.json").write_text(
            json.dumps({"name": name, "description": str(data.get("description") or "evolved")}, ensure_ascii=False),
            encoding="utf-8",
        )
        return jsonify(ok=True, command="/evolve", approved=name)

    if action == "status":
        return jsonify(ok=True, command="/evolve", state=_read_json(path, {}))

    return jsonify(ok=False, error="action: analyze|approve|status"), 400


# ───────────────── /mirror ─────────────────

@bp.post("/mirror")
def cmd_mirror():
    data = _body()
    session = _session(data.get("session") or "mirror")
    path = _sess_dir(session) / "mirror.json"
    action = str(data.get("action") or "status").lower()
    state = _read_json(path, {
        "active": False, "samples": [], "style_notes": "", "confidence": 0.0,
        "threshold": 0.85,
    })

    if action == "train":
        text = str(data.get("text") or data.get("content") or "").strip()
        sources = data.get("sources") if isinstance(data.get("sources"), list) else []
        if text:
            state.setdefault("samples", []).append({"t": time.time(), "text": text[:2000]})
            state["samples"] = state["samples"][-100:]
        if sources:
            state["sources"] = sources
        # naive confidence from sample count
        n = len(state.get("samples") or [])
        state["confidence"] = min(0.99, 0.4 + n * 0.02)
        state["style_notes"] = str(data.get("style_notes") or state.get("style_notes") or "")
        state["threshold"] = float(data.get("confidence_threshold") or state.get("threshold") or 0.85)
    elif action == "activate":
        state["active"] = True
        state["activated_at"] = time.time()
    elif action == "deactivate":
        state["active"] = False
    elif action == "review":
        pass
    elif action == "status":
        pass
    else:
        return jsonify(ok=False, error="action: train|activate|deactivate|review|status"), 400

    _write_json(path, state)
    return jsonify(ok=True, command="/mirror", state={
        **state,
        "samples": state.get("samples", [])[-5:],
        "sample_count": len(state.get("samples") or []),
    })


# ───────────────── /swarm ─────────────────

@bp.post("/swarm")
def cmd_swarm():
    data = _body()
    session = _session(data.get("session") or "swarm")
    mission = str(data.get("mission") or data.get("goal") or "").strip()
    n = max(1, min(int(data.get("agents") or 5), 50))
    strategy = str(data.get("strategy") or "split")
    if not mission:
        return jsonify(ok=False, error="mission مطلوب"), 400

    # Create virtual worker slots (execution is coordinated by PHP/LLM calling back)
    workers = []
    for i in range(n):
        workers.append({
            "id": f"w{i+1}",
            "status": "pending",
            "slice": f"جزء {i+1}/{n} من: {mission[:120]}",
            "result": None,
        })
    state = {
        "mission": mission,
        "agents": n,
        "strategy": strategy,
        "merge_strategy": str(data.get("merge_strategy") or "concat"),
        "budget": data.get("budget") or {},
        "workers": workers,
        "started_at": time.time(),
        "status": "launched",
    }
    path = _sess_dir(session) / "swarm.json"
    _write_json(path, state)
    _append_replay(session, {"type": "swarm", "agents": n, "mission": mission[:200]})
    return jsonify(ok=True, command="/swarm", state=state)


@bp.post("/swarm/report")
def cmd_swarm_report():
    data = _body()
    session = _session(data.get("session") or "swarm")
    path = _sess_dir(session) / "swarm.json"
    state = _read_json(path, None)
    if not state:
        return jsonify(ok=False, error="لا يوجد swarm"), 404
    wid = str(data.get("worker_id") or "")
    for w in state.get("workers") or []:
        if w.get("id") == wid:
            w["status"] = str(data.get("status") or "done")
            w["result"] = data.get("result")
            break
    done = sum(1 for w in state.get("workers") or [] if w.get("status") == "done")
    total = len(state.get("workers") or [])
    if done >= total and total:
        state["status"] = "merged"
        state["merged_at"] = time.time()
    _write_json(path, state)
    return jsonify(ok=True, command="/swarm", done=done, total=total, state=state)


# ───────────────── /time-travel ─────────────────

@bp.post("/time-travel")
def cmd_time_travel():
    data = _body()
    session = _session(data.get("session") or "default")
    action = str(data.get("action") or "checkpoint").lower()
    root = (WORKROOT / session).resolve()
    root.mkdir(parents=True, exist_ok=True)
    snap_root = _sess_dir(session) / "snapshots"
    snap_root.mkdir(parents=True, exist_ok=True)

    if action == "checkpoint":
        name = re.sub(r"[^a-zA-Z0-9_\-]", "", str(data.get("name") or time.strftime("%Y%m%d_%H%M%S")))[:40]
        dest = snap_root / name
        if dest.exists():
            shutil.rmtree(dest, ignore_errors=True)
        # copy workspace excluding _agent/snapshots recursion
        dest.mkdir(parents=True, exist_ok=True)
        for p in root.iterdir():
            if p.name == "_agent":
                continue
            target = dest / p.name
            if p.is_dir():
                shutil.copytree(p, target, dirs_exist_ok=True)
            else:
                shutil.copy2(p, target)
        meta = {"name": name, "t": time.time(), "files": sum(1 for _ in dest.rglob("*") if _.is_file())}
        _write_json(snap_root / f"{name}.meta.json", meta)
        _append_replay(session, {"type": "checkpoint", "name": name})
        return jsonify(ok=True, command="/time-travel", checkpoint=meta)

    if action == "list":
        items = []
        for m in sorted(snap_root.glob("*.meta.json")):
            items.append(_read_json(m, {"name": m.stem}))
        return jsonify(ok=True, command="/time-travel", checkpoints=items)

    if action == "rewind":
        name = re.sub(r"[^a-zA-Z0-9_\-]", "", str(data.get("to") or data.get("name") or ""))
        src = snap_root / name
        if not src.is_dir():
            return jsonify(ok=False, error="checkpoint غير موجود"), 404
        # clear non-agent files then restore
        for p in list(root.iterdir()):
            if p.name == "_agent":
                continue
            if p.is_dir():
                shutil.rmtree(p, ignore_errors=True)
            else:
                p.unlink(missing_ok=True)
        for p in src.iterdir():
            target = root / p.name
            if p.is_dir():
                shutil.copytree(p, target, dirs_exist_ok=True)
            else:
                shutil.copy2(p, target)
        _append_replay(session, {"type": "rewind", "name": name})
        return jsonify(ok=True, command="/time-travel", rewound=name)

    if action == "branch":
        name = re.sub(r"[^a-zA-Z0-9_\-]", "", str(data.get("branch_name") or "branch"))[:40]
        new_session = _session(f"{session}_{name}")
        dest = WORKROOT / new_session
        if dest.exists():
            shutil.rmtree(dest, ignore_errors=True)
        shutil.copytree(root, dest, ignore=shutil.ignore_patterns("_agent"))
        return jsonify(ok=True, command="/time-travel", branch=new_session)

    if action == "diff":
        name = re.sub(r"[^a-zA-Z0-9_\-]", "", str(data.get("name") or ""))
        src = snap_root / name
        if not src.is_dir():
            return jsonify(ok=False, error="checkpoint غير موجود"), 404
        snap_files = {str(p.relative_to(src)) for p in src.rglob("*") if p.is_file()}
        cur_files = {str(p.relative_to(root)) for p in root.rglob("*") if p.is_file() and "_agent" not in p.parts}
        return jsonify(ok=True, command="/time-travel",
                       added=sorted(cur_files - snap_files)[:100],
                       removed=sorted(snap_files - cur_files)[:100])

    return jsonify(ok=False, error="action: checkpoint|list|rewind|branch|diff"), 400


# ───────────────── /bet ─────────────────

@bp.post("/bet")
def cmd_bet():
    data = _body()
    session = _session(data.get("session") or "default")
    objective = str(data.get("objective") or data.get("goal") or "").strip()
    if not objective:
        return jsonify(ok=False, error="objective مطلوب"), 400
    constraints = data.get("constraints") if isinstance(data.get("constraints"), dict) else {}
    depth = max(1, min(int(data.get("fallback_depth") or 3), 5))
    # Simple decision tree without true Monte Carlo
    paths = []
    for i in range(depth):
        paths.append({
            "id": f"P{i+1}",
            "label": f"مسار {i+1}",
            "success_p": round(max(0.15, 0.75 - i * 0.15), 2),
            "plan": _heuristic_plan(f"{objective} (مسار {i+1})"),
            "constraints": constraints,
        })
    paths.sort(key=lambda x: -x["success_p"])
    result = {
        "objective": objective,
        "best": paths[0],
        "alternatives": paths[1:],
        "iterations_note": "تقدير هيكلي — يمكن للـ LLM تحسين الاحتمالات لاحقاً",
        "t": time.time(),
    }
    _write_json(_sess_dir(session) / "bet.json", result)
    return jsonify(ok=True, command="/bet", result=result)


# ───────────────── /hive-mind ─────────────────

@bp.post("/hive-mind")
def cmd_hive_mind():
    data = _body()
    ns = re.sub(r"[^a-zA-Z0-9_\-]", "", str(data.get("namespace") or "global"))[:40] or "global"
    root = WORKROOT / "_hive" / ns
    root.mkdir(parents=True, exist_ok=True)
    store_path = root / "memory.jsonl"
    action = str(data.get("action") or "query").lower()

    if action == "share":
        content = data.get("content")
        if content is None:
            content = {"value": str(data.get("value") or data.get("text") or "")}
        entry = {
            "t": time.time(),
            "content": content,
            "share_with": data.get("share_with") if isinstance(data.get("share_with"), list) else [],
            "from_session": _session(data.get("session") or "default"),
        }
        with store_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        return jsonify(ok=True, command="/hive-mind", shared=True, namespace=ns)

    if action in {"query", "sync"}:
        q = str(data.get("q") or data.get("query") or "").lower()
        items = []
        if store_path.is_file():
            for line in store_path.read_text(encoding="utf-8", errors="replace").splitlines()[-300:]:
                try:
                    e = json.loads(line)
                except Exception:
                    continue
                if not q or q in json.dumps(e, ensure_ascii=False).lower():
                    items.append(e)
        return jsonify(ok=True, command="/hive-mind", namespace=ns, items=items[-50:], count=len(items))

    return jsonify(ok=False, error="action: share|query|sync"), 400


# ───────────────── /fs ─────────────────

@bp.post("/fs")
def cmd_fs():
    data = _body()
    session = _session(data.get("session") or "default")
    root = (WORKROOT / session).resolve()
    root.mkdir(parents=True, exist_ok=True)
    action = str(data.get("action") or "tree").lower()
    rel = str(data.get("path") or ".").lstrip("/")

    def safe(p: str) -> Path:
        target = (root / p).resolve()
        if target != root and root not in target.parents:
            raise ValueError("مسار خارج الـ sandbox")
        return target

    try:
        if action == "tree":
            items = []
            base = safe(rel if rel != "." else "")
            if not base.exists():
                return jsonify(ok=False, error="المسار غير موجود"), 404
            for p in sorted(base.rglob("*")):
                if "_agent" in p.parts:
                    continue
                items.append({
                    "path": str(p.relative_to(root)),
                    "type": "dir" if p.is_dir() else "file",
                    "size": p.stat().st_size if p.is_file() else 0,
                })
                if len(items) >= 400:
                    break
            return jsonify(ok=True, command="/fs", action=action, items=items)

        if action == "read":
            p = safe(rel)
            if not p.is_file():
                return jsonify(ok=False, error="ملف غير موجود"), 404
            raw = p.read_text(encoding="utf-8", errors="replace")
            off = max(0, int(data.get("offset") or 0))
            lim = data.get("limit")
            chunk = raw[off:]
            if lim is not None:
                chunk = chunk[: max(0, int(lim))]
            return jsonify(ok=True, command="/fs", content=chunk, size=len(raw), offset=off)

        if action == "write":
            p = safe(rel)
            p.parent.mkdir(parents=True, exist_ok=True)
            content = str(data.get("content") or "")
            p.write_text(content, encoding="utf-8")
            return jsonify(ok=True, command="/fs", path=rel, size=len(content.encode("utf-8")))

        if action == "edit":
            p = safe(rel)
            if not p.is_file():
                return jsonify(ok=False, error="ملف غير موجود"), 404
            text = p.read_text(encoding="utf-8", errors="replace")
            if data.get("find") is not None:
                text = text.replace(str(data.get("find")), str(data.get("replace") or ""))
            if data.get("insert") is not None and data.get("line") is not None:
                lines = text.splitlines(keepends=True)
                idx = max(0, min(int(data["line"]), len(lines)))
                lines.insert(idx, str(data["insert"]) + ("\n" if not str(data["insert"]).endswith("\n") else ""))
                text = "".join(lines)
            if data.get("delete_line") is not None:
                lines = text.splitlines(keepends=True)
                di = int(data["delete_line"])
                if 0 <= di < len(lines):
                    lines.pop(di)
                text = "".join(lines)
            p.write_text(text, encoding="utf-8")
            return jsonify(ok=True, command="/fs", path=rel, size=len(text.encode("utf-8")))

        if action == "search":
            pattern = str(data.get("pattern") or "")
            if not pattern:
                return jsonify(ok=False, error="pattern مطلوب"), 400
            rx = re.compile(pattern)
            hits = []
            base = safe(rel if rel not in {".", ""} else "")
            for p in base.rglob("*"):
                if not p.is_file() or "_agent" in p.parts:
                    continue
                if p.stat().st_size > 2_000_000:
                    continue
                try:
                    content = p.read_text(encoding="utf-8", errors="replace")
                except Exception:
                    continue
                for i, line in enumerate(content.splitlines()):
                    if rx.search(line):
                        hits.append({"path": str(p.relative_to(root)), "line": i + 1, "text": line[:300]})
                        if len(hits) >= 100:
                            break
                if len(hits) >= 100:
                    break
            return jsonify(ok=True, command="/fs", hits=hits)

        if action in {"move", "copy"}:
            to = str(data.get("to") or "").lstrip("/")
            src, dst = safe(rel), safe(to)
            if action == "move":
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(src), str(dst))
            else:
                dst.parent.mkdir(parents=True, exist_ok=True)
                if src.is_dir():
                    shutil.copytree(src, dst, dirs_exist_ok=True)
                else:
                    shutil.copy2(src, dst)
            return jsonify(ok=True, command="/fs", action=action, from_=rel, to=to)

        if action == "compress":
            import zipfile
            to = str(data.get("to") or f"{rel.rstrip('/')}.zip").lstrip("/")
            src, dst = safe(rel), safe(to)
            with zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED) as z:
                if src.is_file():
                    z.write(src, src.name)
                else:
                    for f in src.rglob("*"):
                        if f.is_file():
                            z.write(f, str(f.relative_to(src)))
            return jsonify(ok=True, command="/fs", archive=to, size=dst.stat().st_size)

        if action == "convert":
            fmt = str(data.get("format") or "md").lower()
            p = safe(rel)
            if not p.is_file():
                return jsonify(ok=False, error="ملف غير موجود"), 404
            raw = p.read_text(encoding="utf-8", errors="replace")
            out_rel = str(data.get("to") or (rel + "." + fmt)).lstrip("/")
            out = safe(out_rel)
            if fmt in {"md", "txt", "html"}:
                if fmt == "html" and not raw.lstrip().startswith("<"):
                    raw = f"<pre>{raw}</pre>"
                out.write_text(raw, encoding="utf-8")
            elif fmt == "json" and rel.endswith(".csv"):
                import csv
                from io import StringIO
                rows = list(csv.DictReader(StringIO(raw)))
                out.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
            else:
                out.write_text(raw, encoding="utf-8")
            return jsonify(ok=True, command="/fs", converted=out_rel)

        return jsonify(ok=False, error="action غير مدعوم"), 400
    except Exception as e:
        return jsonify(ok=False, error=str(e)), 400


# ───────────────── /py  (persistent REPL) ─────────────────

_py_sessions: dict[str, dict] = {}

@bp.post("/py")
def cmd_py():
    data = _body()
    session = _session(data.get("session") or "py")
    code = str(data.get("code") or "")
    if not code:
        return jsonify(ok=False, error="code مطلوب"), 400
    timeout = max(1, min(int(data.get("timeout") or 30), 120))
    # packages: best-effort note (Railway may lack net/pip policy)
    packages = data.get("packages") if isinstance(data.get("packages"), list) else []

    ns = _py_sessions.get(session)
    if ns is None:
        ns = {"__name__": "__main__"}
        _py_sessions[session] = ns

    workdir = WORKROOT / session
    workdir.mkdir(parents=True, exist_ok=True)
    src = workdir / "__yd_repl.py"
    # wrap to capture last expr if possible
    src.write_text(code, encoding="utf-8")
    import subprocess, sys
    try:
        proc = subprocess.Popen(
            [sys.executable, "-I", str(src)],
            cwd=str(workdir),
            env={"PATH": os.environ.get("PATH", ""), "HOME": str(workdir), "LANG": "C.UTF-8"},
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            start_new_session=True,
        )
        try:
            out, err = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            out, err = proc.communicate()
            return jsonify(ok=False, error="timeout", stdout=out[-50000:], stderr=err[-20000:]), 200
        return jsonify(
            ok=proc.returncode == 0,
            command="/py",
            stdout=(out or "")[-100000:],
            stderr=(err or "")[-20000:],
            exit_code=proc.returncode,
            packages_requested=packages,
            note="state بين الاستدعاءات عبر ملفات الجلسة؛ لـ REPL كامل داخل process استخدم نفس session مع ملفات",
        )
    finally:
        src.unlink(missing_ok=True)


# ───────────────── /js ─────────────────

@bp.post("/js")
def cmd_js():
    data = _body()
    session = _session(data.get("session") or "js")
    code = str(data.get("code") or "")
    if not code:
        return jsonify(ok=False, error="code مطلوب"), 400
    timeout = max(1, min(int(data.get("timeout") or 30), 60))
    workdir = WORKROOT / session
    workdir.mkdir(parents=True, exist_ok=True)
    src = workdir / "__yd_run.js"
    src.write_text(code, encoding="utf-8")
    import shutil as _sh, subprocess
    node = _sh.which("node")
    if not node:
        return jsonify(ok=False, error="Node.js غير مثبت على الـ worker — أضفه في Dockerfile إن لزم"), 501
    try:
        proc = subprocess.Popen(
            [node, str(src)], cwd=str(workdir),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            start_new_session=True,
        )
        try:
            out, err = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            out, err = proc.communicate()
            return jsonify(ok=False, error="timeout", stdout=out, stderr=err), 200
        return jsonify(ok=proc.returncode == 0, command="/js", stdout=out[-100000:], stderr=err[-20000:], exit_code=proc.returncode)
    finally:
        src.unlink(missing_ok=True)


# ───────────────── /docker ─────────────────

@bp.post("/docker")
def cmd_docker():
    import shutil as _sh, subprocess
    if not _sh.which("docker"):
        return jsonify(
            ok=False,
            error="Docker غير متاح داخل هذا الـ container (Railway). استخدم /sandbox + /shell بدلاً منه.",
            command="/docker",
            available=False,
        ), 501
    data = _body()
    action = str(data.get("action") or "run").lower()
    # minimal passthrough for sole-user hosts that have docker socket
    image = str(data.get("image") or "")
    command = str(data.get("command") or "")
    if action == "run" and image:
        cmd = ["docker", "run", "--rm"]
        for k, v in (data.get("env") or {}).items():
            cmd.extend(["-e", f"{k}={v}"])
        cmd.append(image)
        if command:
            cmd.extend(["bash", "-lc", command])
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=min(int(data.get("timeout") or 60), 120))
            return jsonify(ok=r.returncode == 0, stdout=r.stdout[-50000:], stderr=r.stderr[-20000:], exit_code=r.returncode)
        except Exception as e:
            return jsonify(ok=False, error=str(e)), 500
    return jsonify(ok=False, error="docker محدود على Railway — راجع /sandbox"), 400


# ───────────────── /sql ─────────────────

@bp.post("/sql")
def cmd_sql():
    data = _body()
    engine = str(data.get("engine") or "sqlite").lower()
    action = str(data.get("action") or "query").lower()
    query = str(data.get("query") or "")
    session = _session(data.get("session") or "default")

    if engine != "sqlite":
        return jsonify(ok=False, error="حالياً sqlite فقط على هذا الـ worker (postgres/mysql لاحقاً)"), 501

    import sqlite3
    db_path = data.get("connection") or f"sqlite:///{WORKROOT / session / 'app.db'}"
    if db_path.startswith("sqlite:///"):
        file_path = db_path.replace("sqlite:///", "", 1)
        if not file_path.startswith("/"):
            file_path = str((WORKROOT / session / file_path).resolve())
    else:
        file_path = str((WORKROOT / session / "app.db").resolve())

    # stay inside workroot
    fp = Path(file_path).resolve()
    sess_root = (WORKROOT / session).resolve()
    if sess_root not in fp.parents and fp != sess_root and WORKROOT.resolve() not in fp.parents:
        # allow under WORKROOT
        if WORKROOT.resolve() not in fp.parents and fp.parent != WORKROOT.resolve():
            return jsonify(ok=False, error="مسار DB خارج المساحة"), 400

    fp.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(fp))
    conn.row_factory = sqlite3.Row
    try:
        cur = conn.cursor()
        params = data.get("params") if isinstance(data.get("params"), list) else []
        if action == "schema":
            rows = cur.execute("SELECT name, sql FROM sqlite_master WHERE type='table'").fetchall()
            return jsonify(ok=True, command="/sql", tables=[dict(r) for r in rows])
        if not query:
            return jsonify(ok=False, error="query مطلوب"), 400
        cur.execute(query, params)
        if action == "query" or query.strip().lower().startswith("select"):
            rows = [dict(r) for r in cur.fetchall()]
            return jsonify(ok=True, command="/sql", rows=rows, count=len(rows))
        conn.commit()
        return jsonify(ok=True, command="/sql", rowcount=cur.rowcount)
    except Exception as e:
        return jsonify(ok=False, error=str(e)), 400
    finally:
        conn.close()


# ───────────────── /http ─────────────────

@bp.post("/http")
def cmd_http():
    data = _body()
    method = str(data.get("method") or "GET").upper()
    url = str(data.get("url") or "").strip()
    if not url.startswith("http"):
        return jsonify(ok=False, error="url غير صالح"), 400
    headers = data.get("headers") if isinstance(data.get("headers"), dict) else {}
    headers = {str(k): str(v) for k, v in headers.items()}
    body = data.get("body")
    params = data.get("params") if isinstance(data.get("params"), dict) else None
    if params:
        url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
    auth = data.get("auth") if isinstance(data.get("auth"), dict) else None
    if auth:
        t = str(auth.get("type") or "").lower()
        val = str(auth.get("value") or "")
        if t == "bearer":
            headers["Authorization"] = f"Bearer {val}"
        elif t == "api_key":
            headers[str(auth.get("header") or "X-Api-Key")] = val

    attempts = max(1, min(int((data.get("retry") or {}).get("attempts") or 1), 5))
    last_err = None
    for i in range(attempts):
        try:
            raw_body = None
            if body is not None and method in {"POST", "PUT", "PATCH"}:
                if isinstance(body, (dict, list)):
                    raw_body = json.dumps(body).encode("utf-8")
                    headers.setdefault("Content-Type", "application/json")
                else:
                    raw_body = str(body).encode("utf-8")
            req = urllib.request.Request(url, data=raw_body, headers=headers, method=method)
            with urllib.request.urlopen(req, timeout=min(int(data.get("timeout") or 20), 60)) as resp:
                raw = resp.read(500_000)
                text = raw.decode("utf-8", errors="replace")
                parsed = None
                if str(data.get("parse") or "json") == "json":
                    try:
                        parsed = json.loads(text)
                    except Exception:
                        parsed = None
                return jsonify(
                    ok=True, command="/http", status=resp.status, headers=dict(resp.headers.items())[:20] if False else {},
                    text=text[:100000], json=parsed, attempt=i + 1,
                )
        except Exception as e:
            last_err = str(e)
            time.sleep(min(2 ** i, 5))
    return jsonify(ok=False, error=last_err or "فشل الطلب"), 502


# ───────────────── /scrape ─────────────────

@bp.post("/scrape")
def cmd_scrape():
    data = _body()
    url = str(data.get("url") or "").strip()
    if not url.startswith("http"):
        return jsonify(ok=False, error="url غير صالح"), 400
    render = str(data.get("render") or "static").lower()
    extract = data.get("extract") if isinstance(data.get("extract"), dict) else {}

    if render == "js":
        # use playwright if available via app helpers — fallback static
        try:
            from app import _browser_context, _safe_url  # type: ignore
            url = _safe_url(url)
            session, _, page = _browser_context(data.get("session") or "scrape")
            page.goto(url, wait_until="domcontentloaded", timeout=30000)
            for act in (data.get("actions") or []):
                if not isinstance(act, dict):
                    continue
                t = act.get("type")
                if t == "click" and act.get("selector"):
                    page.locator(str(act["selector"])).first.click(timeout=10000)
                elif t == "scroll":
                    page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                page.wait_for_timeout(300)
            html = page.content()
            text = page.locator("body").inner_text(timeout=5000) if page.locator("body").count() else ""
            shot = None
            if data.get("screenshot"):
                import base64
                shot = base64.b64encode(page.screenshot(type="png")).decode("ascii")
            fields = {}
            for k, sel in extract.items():
                try:
                    fields[k] = page.locator(str(sel).split("::")[0]).all_inner_texts()[:50]
                except Exception:
                    fields[k] = []
            return jsonify(ok=True, command="/scrape", url=page.url, text=text[:50000], fields=fields, screenshot=shot)
        except Exception as e:
            return jsonify(ok=False, error=f"js render فشل: {e}", fallback="static"), 200

    # static fetch
    r = _fetch_url(url)
    return jsonify(ok=r.get("ok"), command="/scrape", **r)



# ───────────────── catalog ─────────────────

@bp.get("/commands")
def cmd_list():
    return jsonify(ok=True, commands=[
        {"cmd": "/plan", "endpoint": "POST /cmd/plan"},
        {"cmd": "/wide-research", "endpoint": "POST /cmd/wide-research"},
        {"cmd": "/agent-mode", "endpoint": "POST /cmd/agent-mode"},
        {"cmd": "/take-over", "endpoint": "POST /cmd/take-over"},
        {"cmd": "/sandbox", "endpoint": "POST /cmd/sandbox"},
        {"cmd": "/replay", "endpoint": "POST /cmd/replay"},
        {"cmd": "/deploy", "endpoint": "POST /cmd/deploy"},
        {"cmd": "/skill", "endpoint": "POST /cmd/skill"},
        {"cmd": "/connect", "endpoint": "POST /cmd/connect"},
        {"cmd": "/think", "endpoint": "POST /cmd/think"},
        {"cmd": "/dream", "endpoint": "POST /cmd/dream"},
        {"cmd": "/evolve", "endpoint": "POST /cmd/evolve"},
        {"cmd": "/mirror", "endpoint": "POST /cmd/mirror"},
        {"cmd": "/swarm", "endpoint": "POST /cmd/swarm"},
        {"cmd": "/time-travel", "endpoint": "POST /cmd/time-travel"},
        {"cmd": "/bet", "endpoint": "POST /cmd/bet"},
        {"cmd": "/hive-mind", "endpoint": "POST /cmd/hive-mind"},
        {"cmd": "/fs", "endpoint": "POST /cmd/fs"},
        {"cmd": "/py", "endpoint": "POST /cmd/py"},
        {"cmd": "/js", "endpoint": "POST /cmd/js"},
        {"cmd": "/docker", "endpoint": "POST /cmd/docker"},
        {"cmd": "/sql", "endpoint": "POST /cmd/sql"},
        {"cmd": "/http", "endpoint": "POST /cmd/http"},
        {"cmd": "/scrape", "endpoint": "POST /cmd/scrape"},
    ])
