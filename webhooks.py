"""
YACINEDEV webhooks -- إرسال أحداث المنصة لمنصات خارجية.
"""
from __future__ import annotations
import json
import os
import re
import threading
import time
import urllib.request
import urllib.error
from pathlib import Path
from flask import Blueprint, jsonify, request

bp = Blueprint("webhooks", __name__)

STORE_PATH = Path(os.environ.get("WEBHOOKS_STORE_PATH", "/tmp/yd_sandbox/webhooks.json"))
_lock = threading.RLock()


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


def _fmt_discord(event: str, payload: dict) -> dict:
    return {"content": f"**{event}**\n```json\n{json.dumps(payload, ensure_ascii=False, indent=2)[:1800]}\n```"}


def _fmt_slack(event: str, payload: dict) -> dict:
    return {"text": f"*{event}*\n```{json.dumps(payload, ensure_ascii=False, indent=2)[:2800]}```"}


def _fmt_generic(event: str, payload: dict) -> dict:
    return {"event": event, "payload": payload, "ts": time.time()}


FORMATTERS = {"discord": _fmt_discord, "slack": _fmt_slack, "github": _fmt_generic,
              "gitlab": _fmt_generic, "generic": _fmt_generic}


def _post(url: str, body: dict, timeout: int = 10) -> tuple[bool, str]:
    req = urllib.request.Request(
        url, data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return 200 <= resp.status < 300, f"HTTP {resp.status}"
    except urllib.error.HTTPError as e:
        return False, f"HTTP {e.code}: {e.read().decode('utf-8', 'replace')[:300]}"
    except Exception as e:
        return False, str(e)


def fire(event: str, payload: dict) -> list[dict]:
    """ينادى من agent_runtime.py عند انتهاء/فشل مهمة، أو من cron.py، أو أي كود آخر."""
    results = []
    with _lock:
        hooks = _load()
    for hook_id, hook in hooks.items():
        if not hook.get("enabled", True):
            continue
        events = hook.get("events") or ["*"]
        if "*" not in events and event not in events:
            continue
        fmt = FORMATTERS.get(hook.get("kind", "generic"), _fmt_generic)
        ok, info = _post(hook["url"], fmt(event, payload))
        results.append({"id": hook_id, "ok": ok, "info": info})
    return results


@bp.get("/webhooks")
def list_webhooks():
    with _lock:
        hooks = _load()
    safe = {k: {**v, "url": v["url"][:50] + "…"} for k, v in hooks.items()}
    return jsonify(ok=True, webhooks=safe)


@bp.post("/webhooks")
def create_webhook():
    data = request.get_json(silent=True) or {}
    url = str(data.get("url") or "").strip()
    kind = str(data.get("kind") or "generic").lower()
    events = data.get("events") or ["*"]
    if not url.startswith(("http://", "https://")):
        return jsonify(ok=False, error="url غير صالح"), 400
    if kind not in FORMATTERS:
        return jsonify(ok=False, error=f"kind يجب أن يكون أحد: {list(FORMATTERS)}"), 400
    hook_id = re.sub(r"[^a-zA-Z0-9_\-]", "", str(data.get("id") or f"hook_{int(time.time())}"))[:64]
    with _lock:
        hooks = _load()
        hooks[hook_id] = {"url": url, "kind": kind, "events": events, "enabled": True, "created_at": time.time()}
        _save(hooks)
    return jsonify(ok=True, id=hook_id)


@bp.delete("/webhooks/<hook_id>")
def delete_webhook(hook_id: str):
    hook_id = re.sub(r"[^a-zA-Z0-9_\-]", "", hook_id)
    with _lock:
        hooks = _load()
        if hook_id not in hooks:
            return jsonify(ok=False, error="غير موجود"), 404
        del hooks[hook_id]
        _save(hooks)
    return jsonify(ok=True, deleted=hook_id)


@bp.post("/webhooks/<hook_id>/test")
def test_webhook(hook_id: str):
    hook_id = re.sub(r"[^a-zA-Z0-9_\-]", "", hook_id)
    with _lock:
        hooks = _load()
    hook = hooks.get(hook_id)
    if not hook:
        return jsonify(ok=False, error="غير موجود"), 404
    fmt = FORMATTERS.get(hook.get("kind", "generic"), _fmt_generic)
    ok, info = _post(hook["url"], fmt("test", {"message": "رسالة اختبار من YACINEDEV"}))
    return jsonify(ok=ok, info=info)
