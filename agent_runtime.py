"""
YACINEDEV durable agent runtime on Railway.
Thinking + reply generation run HERE (not on shared PHP hosting).
Client only starts a job and polls — closing the browser does not kill the job.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
import uuid
import traceback
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Optional

from flask import Blueprint, jsonify, request

bp = Blueprint("agent_runtime", __name__)

WORKROOT: Path = Path(os.environ.get("SHELL_WORKROOT", "/tmp/yd_sandbox"))
_jobs_lock = threading.RLock()
_threads: dict[str, threading.Thread] = {}

_PLATFORM_BRIDGED_TOOLS = {
    "weather", "qr", "zip", "time", "web-search", "search", "search-hub", "multi-search",
    "web-fetch", "open-url", "browser-open", "movies", "films", "movie", "wiki-images",
    "wikipedia", "wiki", "images", "calculator", "calc", "translator", "translate",
    "summarizer", "summarize", "unit-converter", "convert", "video", "videos", "youtube",
    "film-clip", "simple-image", "image-gen", "generate-image", "create-image", "browser-info",
    "browser-diagnostics", "workspace", "computer", "computer-workspace", "snablox",
    "workspace-ls", "snablox-ls", "workspace-read", "snablox-read", "workspace-write",
    "snablox-write", "workspace-unzip", "snablox-unzip", "workspace-mkdir", "snablox-mkdir",
    "workspace-rm", "snablox-rm", "workspace-info", "snablox-info", "workspace-tree",
    "snablox-tree", "tree", "workspace-find", "snablox-find", "find", "workspace-diff",
    "snablox-diff", "python", "python-run", "python-list", "python-register", "python-analyze",
    "javascript", "javascript-run", "javascript-analyze", "node", "node-run", "apk-plan",
    "apk-code", "apk-build", "apk-full", "apklab", "fb-video", "facebook-video", "facebook",
    "yt", "shell-exec", "run-command", "railway-cmd", "cmd",
}


def _is_platform_tool(tool: str) -> bool:
    return str(tool or "").strip().lower().replace("_", "-") in _PLATFORM_BRIDGED_TOOLS


def _tool_result_path(job_id: str, call_id: str) -> Path:
    safe_job = re.sub(r"[^a-zA-Z0-9_\-]", "", job_id)[:64]
    safe_call = re.sub(r"[^a-zA-Z0-9_\-]", "", call_id)[:64]
    d = _jobs_dir() / "_tool_results"
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{safe_job}_{safe_call}.json"


def _jobs_dir() -> Path:
    p = WORKROOT / "_agent_jobs"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _job_path(job_id: str) -> Path:
    safe = re.sub(r"[^a-zA-Z0-9_\-]", "", job_id)[:64]
    return _jobs_dir() / f"{safe}.json"


def _read_job(job_id: str) -> Optional[dict]:
    p = _job_path(job_id)
    if not p.is_file():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def _write_job(job: dict) -> None:
    p = _job_path(str(job["id"]))
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(job, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(p)


def _append_event(job: dict, kind: str, **payload) -> None:
    ev = {"t": time.time(), "kind": kind, **payload}
    job.setdefault("events", []).append(ev)
    # keep last 200 events
    if len(job["events"]) > 200:
        job["events"] = job["events"][-200:]
    if kind == "thinking":
        text = str(payload.get("text") or "")
        if text:
            job["thinking"] = (job.get("thinking") or "") + text
            if len(job["thinking"]) > 50000:
                job["thinking"] = job["thinking"][-50000:]
    if kind == "token":
        text = str(payload.get("text") or "")
        job["answer"] = (job.get("answer") or "") + text
    if kind == "answer":
        job["answer"] = str(payload.get("text") or job.get("answer") or "")
    _write_job(job)


def _llm_config() -> dict:
    """Prefer bundled Kimi web-session credentials; no Railway variables needed."""
    try:
        import kimi_chat

        if kimi_chat.ACCESS and kimi_chat.REFRESH:
            return {
                "api_key": "embedded-kimi-session",
                "base": "https://kimi.moonshot.cn",
                "model": "kimi",
                "mode": "kimi_session",
            }
    except Exception:
        pass

    """Fallback to OpenAI-compatible providers when the Kimi client is absent."""
    # Keep each provider's key paired with its own endpoint. Previously an
    # OPENAI_API_KEY without OPENAI_BASE_URL was sent to Moonshot by default.
    kimi_key = os.environ.get("KIMI_API_KEY") or os.environ.get("MOONSHOT_API_KEY")
    openai_key = os.environ.get("OPENAI_API_KEY")
    if kimi_key:
        api_key = kimi_key
        base = (
            os.environ.get("KIMI_BASE_URL")
            or os.environ.get("MOONSHOT_BASE_URL")
            or "https://api.moonshot.cn/v1"
        )
        model = os.environ.get("KIMI_MODEL") or os.environ.get("LLM_MODEL") or "moonshot-v1-auto"
    elif openai_key:
        api_key = openai_key
        base = os.environ.get("OPENAI_BASE_URL") or "https://api.openai.com/v1"
        model = os.environ.get("OPENAI_MODEL") or os.environ.get("LLM_MODEL") or "gpt-4o-mini"
    else:
        # Generic OpenAI-compatible providers should specify their endpoint.
        api_key = os.environ.get("LLM_API_KEY") or ""
        base = os.environ.get("LLM_BASE_URL") or "https://api.moonshot.cn/v1"
        model = os.environ.get("LLM_MODEL") or "moonshot-v1-auto"
    base = base.rstrip("/")
    return {"api_key": api_key, "base": base, "model": model}


def _llm_chat(messages: list, temperature: float = 0.4) -> str:
    cfg = _llm_config()
    if not cfg["api_key"]:
        raise RuntimeError(
            "لا يوجد مفتاح LLM على Railway. اضبط KIMI_API_KEY أو OPENAI_API_KEY في Variables."
        )
    if cfg.get("mode") == "kimi_session":
        import kimi_chat

        labels = {"system": "تعليمات النظام", "user": "المستخدم", "assistant": "المساعد"}
        transcript = "\n\n".join(
            f"[{labels.get(str(m.get('role') or ''), str(m.get('role') or 'رسالة'))}]\n"
            f"{m.get('content') or ''}"
            for m in messages
        )
        client = kimi_chat.Kimi()
        try:
            return client.ask(transcript, print_stream=False)
        except Exception as exc:
            raise RuntimeError(f"فشل اتصال Kimi بجلسة الويب: {exc}") from None
        finally:
            client.cleanup()

    url = cfg["base"] + "/chat/completions"
    body = {
        "model": cfg["model"],
        "messages": messages,
        "temperature": temperature,
    }
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer " + cfg["api_key"],
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        # Provider errors usually explain whether the key, account, or model
        # is wrong; include that detail but never log or return the key.
        detail = exc.read().decode("utf-8", errors="replace").strip()
        raise RuntimeError(
            f"مزود LLM رفض الطلب (HTTP {exc.code}) عند {cfg['base']}. "
            f"تحقق من أن المفتاح تابع لهذا المزوّد وأن النموذج مسموح. "
            f"تفاصيل المزوّد: {detail[:800]}"
        ) from None
    choices = data.get("choices") or []
    if not choices:
        raise RuntimeError("LLM بدون choices: " + json.dumps(data)[:400])
    msg = choices[0].get("message") or {}
    return str(msg.get("content") or "")


def _internal_cmd(cmd: str, payload: dict) -> dict:
    """Call local /cmd handlers in-process via agent_cmds when possible."""
    try:
        import agent_cmds  # noqa

        # Map to helper functions by simulating request context is hard;
        # use HTTP loopback to same service.
    except Exception:
        pass
    base = os.environ.get("AGENT_SELF_URL") or "http://127.0.0.1:8080"
    key = os.environ.get("SHELL_API_KEY") or os.environ.get("PYTHON_API_KEY") or ""
    path = f"/cmd/{cmd}"
    if cmd == "commands":
        method = "GET"
        data = None
    else:
        method = "POST"
        data = json.dumps(payload or {}).encode("utf-8")
    req = urllib.request.Request(
        base.rstrip("/") + path,
        data=data,
        headers={
            "Content-Type": "application/json",
            "X-Api-Key": key,
            "Accept": "application/json",
        },
        method=method,
    )
    try:
        with urllib.request.urlopen(req, timeout=90) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace")
        try:
            return json.loads(raw)
        except Exception:
            return {"ok": False, "error": raw[:500], "http": e.code}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def _run_shell(cmd: str, session: str) -> dict:
    base = os.environ.get("AGENT_SELF_URL") or "http://127.0.0.1:8080"
    key = os.environ.get("SHELL_API_KEY") or os.environ.get("PYTHON_API_KEY") or ""
    body = json.dumps({"cmd": cmd, "session": session, "timeout": 45}).encode("utf-8")
    req = urllib.request.Request(
        base.rstrip("/") + "/shell",
        data=body,
        headers={"Content-Type": "application/json", "X-Api-Key": key},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        return {"ok": False, "error": str(e)}


SYSTEM_PROMPT = """أنت وكيل YACINEDEV يعمل على سيرفر Railway الدائم.
أجب بالعربية الفصحى الواضحة ما لم يطلب المستخدم لغة أخرى.
قبل كل ACTION اكتب سطراً موجزاً بصيغة "تحديث للمستخدم: ..." يذكر الإجراء القادم وغايته العملية فقط، دون سرد التفكير الداخلي أو البدائل الخاصة.
عندما تحتاج أداة، أخرج سطراً واحداً بالشكل:
ACTION: tool_name
INPUT: {json}

إذا احتجت تنفيذ أكثر من أداة في نفس الخطوة ولا تعتمد نتيجة إحداها على الأخرى،
اكتب حتى 3 أزواج ACTION/INPUT متتالية مفصولة بسطر فارغ:
ACTION: tool_a
INPUT: {json}

ACTION: tool_b
INPUT: {json}
إذا كانت نتيجة أداة تعتمد على نتيجة أداة أخرى، نفّذ واحدة فقط وانتظر النتيجة أولاً.

الأدوات المتاحة:
- plan: {"goal":"..."}
- wide-research: {"query":"...","max_sources":8}
- http: {"method":"GET","url":"..."}
- scrape: {"url":"...","render":"static"}
- fs: {"action":"write|read|tree","path":"...","content":"..."}
- py: {"code":"..."}
- shell: {"cmd":"..."}
- sql: {"query":"..."}
- swarm: {"mission":"...","agents":5}
- think: {"action":"append","text":"..."}
- dream: {"goal":"...","deadline":"2h"}

عندما تنتهي ولا تحتاج أداة، أخرج الإجابة النهائية فقط بدون ACTION.
"""


def _parse_action(text: str) -> tuple[Optional[str], Optional[dict], str]:
    """Return (tool, args, remaining_text) -- يبقى للتوافق مع أي كود قديم يستدعيه."""
    m = re.search(
        r"(?:ACTION|Action):\s*([a-zA-Z0-9_\-]+)\s*\n\s*(?:INPUT|Action Input):\s*(\{[\s\S]*?\})(?:\s*$|\n)",
        text,
        re.IGNORECASE,
    )
    if not m:
        return None, None, text
    tool = m.group(1).strip().lower().replace("_", "-")
    try:
        args = json.loads(m.group(2))
    except Exception:
        args = {}
    rest = (text[: m.start()] + text[m.end() :]).strip()
    return tool, args if isinstance(args, dict) else {}, rest


_ACTIONS_RE = re.compile(
    r"(?:ACTION|Action):\s*([a-zA-Z0-9_\-]+)\s*\n\s*(?:INPUT|Action Input):\s*(\{[\s\S]*?\})(?=\n\s*\n(?:ACTION|Action):|\n\s*(?:ACTION|Action):|\s*$)",
    re.IGNORECASE,
)


def _parse_actions(text: str, max_actions: int = 3) -> tuple[list[dict], str]:
    """يستخرج حتى 3 أزواج ACTION/INPUT مستقلة من نفس الرد لتنفيذها بالتوازي.
    يُرجع (قائمة الأوامر, النص المتبقي بعد إزالتها)."""
    actions: list[dict] = []
    spans: list[tuple[int, int]] = []
    for m in _ACTIONS_RE.finditer(text):
        try:
            args = json.loads(m.group(2))
        except Exception:
            continue
        tool = m.group(1).strip().lower().replace("_", "-")
        actions.append({"tool": tool, "args": args if isinstance(args, dict) else {}})
        spans.append((m.start(), m.end()))
        if len(actions) >= max_actions:
            break

    if not actions:
        return [], text

    remaining = text
    for start, end in sorted(spans, reverse=True):
        remaining = remaining[:start] + remaining[end:]
    return actions, remaining.strip()


def _fire_webhook(event: str, job: dict) -> None:
    """يرسل حدث job.done / job.error / job.cancelled لأي webhook مسجّل (اختياري)."""
    try:
        import webhooks as _webhooks
    except Exception:
        return
    try:
        _webhooks.fire(event, {
            "job_id": job.get("id"), "session": job.get("session"), "chat_id": job.get("chat_id"),
            "status": job.get("status"), "answer": (job.get("answer") or "")[:500],
            "error": job.get("error"),
        })
    except Exception:
        pass


def _request_platform_tool(job_id: str, tool: str, args: dict, timeout: int = 180) -> dict:
    job = _read_job(job_id)
    if not job or not job.get("platform_bridge_enabled"):
        return {"ok": False, "error": "أدوات المنصة غير مفعّلة لهذه المهمة"}
    call_id = uuid.uuid4().hex
    job["pending_platform_tool"] = {
        "id": call_id,
        "tool": tool,
        "args": args if isinstance(args, dict) else {},
        "requested_at": time.time(),
    }
    _append_event(job, "platform_tool_call", tool=tool, args=args or {}, tool_call_id=call_id)
    deadline = time.monotonic() + max(10, timeout)
    result_path = _tool_result_path(job_id, call_id)
    while time.monotonic() < deadline:
        current = _read_job(job_id)
        if not current or current.get("cancel") or current.get("status") == "cancelled":
            if current:
                current["pending_platform_tool"] = None
                _write_job(current)
            return {"ok": False, "error": "أُلغيت المهمة قبل اكتمال أداة المنصة"}
        if result_path.is_file():
            try:
                payload = json.loads(result_path.read_text(encoding="utf-8"))
                result_path.unlink(missing_ok=True)
                current["pending_platform_tool"] = None
                _write_job(current)
                result = payload.get("result") if isinstance(payload, dict) else None
                return result if isinstance(result, dict) else {"ok": False, "error": "نتيجة أداة المنصة غير صالحة"}
            except Exception as exc:
                result_path.unlink(missing_ok=True)
                return {"ok": False, "error": "تعذر قراءة نتيجة أداة المنصة: " + str(exc)[:200]}
        time.sleep(0.25)
    current = _read_job(job_id)
    if current:
        current["pending_platform_tool"] = None
        _write_job(current)
    return {"ok": False, "error": "انتهت مهلة أداة المنصة؛ اترك تبويب المنصة مفتوحًا أثناء استخدام Snablox"}


def _progress_clean(value, limit: int = 220) -> str:
    if value is None:
        return ""
    text = str(value)
    text = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", text)
    text = re.sub(r"(?i)(authorization\s*[:=]\s*bearer\s+)[A-Za-z0-9._~+/=-]+", r"\1[محجوب]", text)
    text = re.sub(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]+", r"\1[محجوب]", text)
    text = re.sub(r"(?i)((?:x-api-key|api[_-]?key|access[_-]?token|refresh[_-]?token|password|secret)\s*[:=]\s*)[^\s,;]+", r"\1[محجوب]", text)
    text = re.sub(r"(?i)([?&](?:token|key|api_key|access_token|password)=)[^&#\s]+", r"\1[محجوب]", text)
    text = re.sub(r"\bsk-[A-Za-z0-9_-]{12,}\b", "[مفتاح محجوب]", text)
    text = re.sub(r"[\r\n\t]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


def _progress_input_summary(tool: str, args: Optional[dict]) -> str:
    args = args if isinstance(args, dict) else {}
    labels = {
        "cmd": "الأمر", "command": "الأمر", "code": "المقطع", "query": "الطلب",
        "goal": "الهدف", "url": "الرابط", "path": "المسار", "expr": "التعبير",
        "expression": "التعبير", "action": "الإجراء", "tool": "الأداة",
    }
    parts = []
    for key in ("action", "cmd", "command", "code", "goal", "query", "url", "path", "expr", "expression", "tool"):
        value = args.get(key)
        if value in (None, "", [], {}):
            continue
        clean = _progress_clean(value, 135)
        if clean:
            parts.append(f"{labels[key]}: {clean}")
        if len(parts) >= 2:
            break
    return "؛ ".join(parts)


def _progress_result_summary(result: dict) -> str:
    labels = {
        "summary": "الخلاصة", "message": "الرسالة", "title": "العنوان",
        "stdout": "المخرجات", "output": "المخرجات", "result": "النتيجة",
        "error": "الخطأ", "detail": "التفصيل", "stderr": "رسالة التنفيذ",
        "note": "ملاحظة", "path": "المسار", "query": "البحث", "plan": "الخطة",
    }
    priority = ("error", "stderr", "summary", "message", "detail", "stdout", "output", "result", "title", "note", "path", "query", "plan") if result.get("ok") is False else ("summary", "message", "title", "stdout", "output", "result", "error", "detail", "stderr", "note", "path", "query", "plan")
    for key in priority:
        value = result.get(key)
        if value in (None, "", [], {}):
            continue
        if isinstance(value, dict):
            safe = []
            for subkey in ("summary", "message", "title", "path", "count", "files_count", "status", "exit_code", "stdout", "stderr", "error"):
                subvalue = value.get(subkey)
                if subvalue not in (None, "", [], {}):
                    safe.append(f"{subkey}: {_progress_clean(subvalue, 70)}")
            text = "؛ ".join(safe) or "وصلت بيانات منظمة من الأداة"
        elif isinstance(value, (list, tuple)):
            text = "، ".join(_progress_clean(x, 55) for x in value[:3] if x not in (None, "", [], {}))
            if len(value) > 3:
                text += f"؛ وعناصر أخرى ({len(value)})"
        else:
            text = _progress_clean(value, 180)
        if text:
            detail = f"{labels[key]}: {text}"
            if result.get("exit_code") is not None:
                detail += f"؛ رمز الخروج: {result['exit_code']}"
            elif result.get("http") is not None:
                detail += f"؛ HTTP {result['http']}"
            return _progress_clean(detail, 205)
    if result.get("exit_code") is not None:
        return f"رمز الخروج: {result['exit_code']}"
    return "لم تُرجع الأداة ملخصًا نصيًا؛ سأتحقق من الحالة النهائية."


def _tool_progress_block(step: int, max_steps: int, tool: str, result: dict, args: Optional[dict] = None) -> str:
    result = result if isinstance(result, dict) else {"result": result}
    if result.get("ok") is True or result.get("exit_code") == 0:
        status = "نجح التنفيذ."
    elif result.get("ok") is False or (result.get("exit_code") is not None and result.get("exit_code") != 0):
        status = "تعثر التنفيذ؛ أراجع رسالة الخطأ أو أختار مسارًا بديلًا."
    else:
        status = "وصلت نتيجة الأداة؛ أتحقق من تفاصيلها."

    input_summary = _progress_input_summary(tool, args)
    tool_line = f"الأداة: {tool}"
    if input_summary:
        tool_line += f" — {input_summary}"
    next_step = (
        "أراجع المخرجات وأقدّم الخلاصة النهائية."
        if step >= max_steps
        else ("أفحص سبب التعثر قبل المتابعة." if result.get("ok") is False else "أستخدم هذه النتيجة في الخطوة التالية.")
    )
    return "\n".join([
        f"تقدم التنفيذ — الخطوة {step}/{max_steps}",
        tool_line,
        f"الحالة: {status}",
        f"ما تحقق: {_progress_result_summary(result)}",
        f"التالي: {next_step}",
    ]) + "\n"


def _initial_task_plan(message: str) -> str:
    goal = _progress_clean(message, 120) or "تنفيذ طلبك"
    low = str(message or "").lower()
    if any(word in low for word in ("ارفع", "نشر", "تحديث", "deploy", "upload", "رفع")):
        review = "أراجع الملفات والوجهة الحالية قبل إجراء التغيير."
        execute = "أطبّق التحديث مع الحفاظ على نسخة استعادة عند الحاجة."
    elif any(word in low for word in ("ابحث", "قارن", "تحقق", "افحص", "research", "search")):
        review = "أحدد ما يلزم فحصه وأجمع النتائج ذات الصلة."
        execute = "أتحقق من النتائج بمصدر أو اختبار مناسب."
    elif any(word in low for word in ("اكتب", "أنشئ", "عدّل", "أصلح", "برمج", "build", "fix", "create")):
        review = "أحدد موضع التعديل والأداة الأنسب للتنفيذ."
        execute = "أنفذ التغيير ثم أراجع أثره أو اختباره."
    else:
        review = "أفكك الطلب إلى خطوات عملية وأحدد الأداة المناسبة."
        execute = "أنفذ كل خطوة وأستند إلى نتيجة الأداة الفعلية."
    return "\n".join([
        "خطة المهمة:",
        f"1. الهدف: {goal}",
        f"2. المراجعة: {review}",
        f"3. التنفيذ والتحقق: {execute}",
        "4. الإكمال: ألخص ما تحقق وما بقي بوضوح.",
    ]) + "\n"


def _public_action_update(preface: str) -> str:
    match = re.search(r"(?im)^\s*(?:تحديث للمستخدم|سأفعل الآن|progress)\s*:\s*(.+)$", str(preface or ""))
    return _progress_clean(match.group(1), 180) if match else ""


def _execute_tool(tool: str, args: dict, session: str, job_id: Optional[str] = None) -> dict:
    args = dict(args or {})
    args.setdefault("session", session)
    if _is_platform_tool(tool):
        if not job_id:
            return {"ok": False, "error": "لا يوجد معرّف مهمة لجسر أدوات المنصة"}
        return _request_platform_tool(job_id, tool.replace("-", "_"), args)
    if tool in {"shell", "shell-exec", "run-command"}:
        return _run_shell(str(args.get("cmd") or args.get("command") or ""), session)
    # normalize aliases
    aliases = {
        "wide_research": "wide-research",
        "agent_mode": "agent-mode",
        "time_travel": "time-travel",
        "hive_mind": "hive-mind",
        "http_cmd": "http",
    }
    tool = aliases.get(tool.replace("-", "_"), tool)
    tool = tool.replace("_", "-")
    return _internal_cmd(tool, args)


def _worker(job_id: str, platform_context: str = "") -> None:
    job = _read_job(job_id)
    if not job:
        return
    try:
        job["status"] = "running"
        job["started_at"] = time.time()
        _write_job(job)

        message = str(job.get("message") or "")
        session = str(job.get("session") or job_id)
        max_steps = max(1, min(int(job.get("max_steps") or 8), 20))
        display = str(job.get("display_name") or "user")
        platform_context = platform_context or str(job.get("platform_context") or "")

        _append_event(job, "thinking", text=f"بدء المهمة على Railway (جلسة {session})…\n")
        job = _read_job(job_id) or job
        _append_event(job, "thinking", text=_initial_task_plan(message))
        job = _read_job(job_id) or job

        cfg = _llm_config()
        if not cfg["api_key"]:
            # Tool-only fallback: plan + optional research heuristic
            _append_event(
                job,
                "thinking",
                text="لا يوجد KIMI_API_KEY على Railway — وضع أدوات فقط (plan).\n",
            )
            job = _read_job(job_id) or job
            plan = _internal_cmd("plan", {"goal": message, "session": session})
            _append_event(job, "tool", tool="plan", result=plan)
            job = _read_job(job_id) or job
            _append_event(job, "thinking", text=_tool_progress_block(1, max_steps, "plan", plan, {"goal": message}))
            job = _read_job(job_id) or job
            answer = (
                "## نتيجة (وضع بدون LLM)\n\n"
                "تم حفظ الخطة على Railway. أضف متغير **KIMI_API_KEY** "
                "(أو OPENAI_API_KEY) في Railway Variables لتوليد رد كامل هنا.\n\n"
                f"```json\n{json.dumps(plan, ensure_ascii=False, indent=2)[:4000]}\n```"
            )
            job = _read_job(job_id) or job
            _append_event(job, "answer", text=answer)
            job = _read_job(job_id) or job
            job["status"] = "done"
            job["finished_at"] = time.time()
            job.pop("platform_context", None)
            _write_job(job)
            _fire_webhook("job.done", job)
            return

        system_prompt = SYSTEM_PROMPT
        if platform_context:
            system_prompt += (
                "\n\nتعليمات YACINEDEV الأصلية وأوصاف أدوات المنصة (مصدرها خادم المنصة الموثوق):\n"
                + platform_context[:30000]
                + "\n\nبروتوكول التنفيذ الملزم لهذا API: عند طلب أداة اكتب ACTION: tool_name ثم INPUT: {JSON}. "
                "يمكن استخدام أسماء الأدوات الموصوفة أعلاه؛ الأدوات الخاصة بالمنصة تُنفذ عبر جسر جلسة المستخدم إلى PHP/Snablox. "
                "لا تستخدم مساحة fs على Railway بدلاً من workspace/Snablox الخاصة بالمستخدم. "
                "قبل الأداة أرسل تحديثاً عملياً موجزاً بصيغة تحديث للمستخدم، وبعدها اعرض ملخصاً واقعياً لمدخل الأداة ونتيجتها وحالتها والخطوة التالية. "
                "استند إلى stdout/stderr أو المسار أو العدد عند توافرها، ولا تكتف بعبارة عامة مثل استُلمت المخرجات. "
                "لا تعرض التفكير الداخلي أو خطوات الاستدلال؛ اختم بالإجابة النهائية بالعربية."
            )

        messages = [
            {"role": "system", "content": system_prompt},
            {
                "role": "user",
                "content": f"المستخدم ({display}) يقول:\n{message}",
            },
        ]

        for step in range(max_steps):
            job = _read_job(job_id) or job
            if job.get("cancel"):
                job["status"] = "cancelled"
                job["finished_at"] = time.time()
                _write_job(job)
                _fire_webhook("job.cancelled", job)
                return

            _append_event(job, "thinking", text=f"\n— خطوة {step + 1}/{max_steps} —\n")
            job = _read_job(job_id) or job

            try:
                content = _llm_chat(messages)
            except Exception as e:
                _append_event(job, "error", text=str(e))
                job = _read_job(job_id) or job
                job["status"] = "error"
                job["error"] = str(e)
                job["finished_at"] = time.time()
                _write_job(job)
                _fire_webhook("job.error", job)
                return

            actions, preface = _parse_actions(content)
            if preface:
                public_update = _public_action_update(preface)
                if public_update:
                    _append_event(job, "thinking", text=f"\nتحديث للمستخدم: {public_update}\n")
                else:
                    _append_event(job, "thinking", text="تم تحديد الخطوة المناسبة، جارٍ تنفيذها.\n")
                job = _read_job(job_id) or job

            if not actions:
                # final answer
                final_answer = content.strip()
                final_match = re.search(r"(?:^|\n)\s*Final Answer\s*:\s*([\s\S]*)$", final_answer, re.IGNORECASE)
                if final_match:
                    final_answer = final_match.group(1).strip()
                _append_event(job, "answer", text=final_answer)
                job = _read_job(job_id) or job
                job["status"] = "done"
                job["finished_at"] = time.time()
                job.pop("platform_context", None)
                _write_job(job)
                _fire_webhook("job.done", job)
                return

            if len(actions) == 1:
                tool, args = actions[0]["tool"], actions[0]["args"]
                _append_event(job, "thinking", text=f"تنفيذ أداة: {tool}\n")
                job = _read_job(job_id) or job
                result = _execute_tool(tool, args or {}, session, job_id)
                _append_event(job, "tool", tool=tool, args=args, result=result)
                job = _read_job(job_id) or job
                _append_event(job, "thinking", text=_tool_progress_block(step + 1, max_steps, tool, result, args))
                job = _read_job(job_id) or job

                messages.append({"role": "assistant", "content": content})
                messages.append(
                    {
                        "role": "user",
                        "content": "نتيجة الأداة "
                        + tool
                        + ":\n"
                        + json.dumps(result, ensure_ascii=False)[:8000]
                        + "\n\nأكمل. إذا انتهيت أعطِ الإجابة النهائية فقط.",
                    }
                )
            else:
                # تنفيذ متوازٍ -- النموذج حدّد أن هذه الأدوات مستقلة عن بعضها
                names = ", ".join(a["tool"] for a in actions)
                _append_event(job, "thinking", text=f"تنفيذ {len(actions)} أدوات بالتوازي: {names}\n")
                job = _read_job(job_id) or job

                results: dict[str, dict] = {}
                if any(_is_platform_tool(a["tool"]) for a in actions):
                    # Platform tools share one authenticated browser session; serialize them.
                    for a in actions:
                        tname = a["tool"]
                        try:
                            results[tname] = _execute_tool(tname, a["args"] or {}, session, job_id)
                        except Exception as e:
                            results[tname] = {"ok": False, "error": str(e)}
                        job = _read_job(job_id) or job
                        _append_event(job, "tool", tool=tname, args=a["args"], result=results[tname])
                        job = _read_job(job_id) or job
                        _append_event(job, "thinking", text=_tool_progress_block(step + 1, max_steps, tname, results[tname], a["args"]))
                else:
                    with ThreadPoolExecutor(max_workers=3) as ex:
                        futures = {
                            ex.submit(_execute_tool, a["tool"], a["args"] or {}, session, job_id): a["tool"]
                            for a in actions
                        }
                        for fut in as_completed(futures):
                            tname = futures[fut]
                            try:
                                results[tname] = fut.result()
                            except Exception as e:
                                results[tname] = {"ok": False, "error": str(e)}

                    for a in actions:
                        _append_event(job, "tool", tool=a["tool"], args=a["args"], result=results.get(a["tool"]))
                        job = _read_job(job_id) or job
                        _append_event(job, "thinking", text=_tool_progress_block(step + 1, max_steps, a["tool"], results.get(a["tool"]) or {}, a["args"]))
                        job = _read_job(job_id) or job

                observation = "\n".join(
                    f"نتيجة الأداة {t}:\n{json.dumps(r, ensure_ascii=False)[:4000]}"
                    for t, r in results.items()
                )
                messages.append({"role": "assistant", "content": content})
                messages.append(
                    {
                        "role": "user",
                        "content": observation + "\n\nأكمل. إذا انتهيت أعطِ الإجابة النهائية فقط.",
                    }
                )

        job = _read_job(job_id) or job
        _append_event(job, "thinking", text="\nوصلنا للحد الأقصى من الخطوات — تلخيص نهائي.\n")
        job = _read_job(job_id) or job
        try:
            final = _llm_chat(
                messages
                + [
                    {
                        "role": "user",
                        "content": "أعطِ الإجابة النهائية المختصرة الآن بدون ACTION.",
                    }
                ]
            )
        except Exception as e:
            final = "تعذر التلخيص: " + str(e)
        job = _read_job(job_id) or job
        final_match = re.search(r"(?:^|\n)\s*Final Answer\s*:\s*([\s\S]*)$", final.strip(), re.IGNORECASE)
        if final_match:
            final = final_match.group(1).strip()
        _append_event(job, "answer", text=final)
        job = _read_job(job_id) or job
        job["status"] = "done"
        job["finished_at"] = time.time()
        job.pop("platform_context", None)
        _write_job(job)
        _fire_webhook("job.done", job)
    except Exception as e:
        job = _read_job(job_id) or {"id": job_id}
        job["status"] = "error"
        job["error"] = str(e)
        job["trace"] = traceback.format_exc()[-2000:]
        job["finished_at"] = time.time()
        job.pop("platform_context", None)
        _write_job(job)
        _fire_webhook("job.error", job)


def _start_thread(job_id: str, platform_context: str = "") -> None:
    with _jobs_lock:
        t = _threads.get(job_id)
        if t and t.is_alive():
            return
        th = threading.Thread(target=_worker, args=(job_id, platform_context), daemon=True, name=f"job-{job_id}")
        _threads[job_id] = th
        th.start()


@bp.post("/jobs")
def create_job():
    """
    Start durable agent job on Railway.
    Body: {message, session?, max_steps?, display_name?, chat_id?}
    Returns: {ok, job_id, status, poll_url}
    """
    data = request.get_json(silent=True) or {}
    message = str(data.get("message") or data.get("text") or "").strip()
    if not message:
        return jsonify(ok=False, error="message مطلوب"), 400

    job_id = str(data.get("job_id") or ("job_" + str(int(time.time())) + "_" + str(os.getpid()) + "_" + uuid.uuid4().hex[:8]))
    job_id = re.sub(r"[^a-zA-Z0-9_\-]", "", job_id)[:64]
    session = re.sub(r"[^a-zA-Z0-9_\-]", "", str(data.get("session") or job_id))[:64]
    platform_context = str(data.get("platform_context") or "")[:30000]
    platform_bridge_enabled = bool(data.get("platform_bridge")) and bool(platform_context)
    existing = _read_job(job_id)
    if existing and existing.get("status") in {"queued", "running"}:
        return jsonify(ok=False, error="job_id قيد الاستخدام بالفعل"), 409

    job = {
        "id": job_id,
        "status": "queued",
        "message": message,
        "session": session,
        "chat_id": data.get("chat_id"),
        "display_name": str(data.get("display_name") or "user")[:80],
        "max_steps": max(1, min(int(data.get("max_steps") or 8), 20)),
        "platform_context": platform_context,
        "platform_bridge_enabled": platform_bridge_enabled,
        "created_at": time.time(),
        "thinking": "",
        "answer": "",
        "events": [],
        "llm_configured": bool(_llm_config()["api_key"]),
    }
    _write_job(job)
    _start_thread(job_id, platform_context)
    return jsonify(
        ok=True,
        job_id=job_id,
        status="queued",
        poll_url=f"/agent/jobs/{job_id}",
        llm_configured=job["llm_configured"],
        note="المهمة تعمل على Railway حتى لو أغلقت المتصفح؛ أدوات مساحة Snablox تحتاج بقاء تبويب المنصة مفتوحًا.",
    )


@bp.get("/jobs/<job_id>")
def get_job(job_id: str):
    job = _read_job(job_id)
    if not job:
        return jsonify(ok=False, error="المهمة غير موجودة"), 404
    # resume thread if server restarted mid-job
    if job.get("status") in {"queued", "running"}:
        _start_thread(job_id, str(job.get("platform_context") or ""))
    public_job = dict(job)
    public_job.pop("platform_context", None)
    return jsonify(ok=True, job=public_job)


@bp.post("/jobs/<job_id>/tool-result")
def submit_platform_tool_result(job_id: str):
    job = _read_job(job_id)
    if not job:
        return jsonify(ok=False, error="المهمة غير موجودة"), 404
    pending = job.get("pending_platform_tool") or {}
    data = request.get_json(silent=True) or {}
    call_id = re.sub(r"[^a-zA-Z0-9_\-]", "", str(data.get("tool_call_id") or ""))[:64]
    if not call_id or call_id != str(pending.get("id") or ""):
        return jsonify(ok=False, error="طلب الأداة غير مطابق أو انتهت صلاحيته"), 409
    if job.get("status") not in {"running", "queued"}:
        return jsonify(ok=False, error="المهمة لم تعد نشطة"), 409
    result = data.get("result")
    if not isinstance(result, dict):
        result = {"ok": False, "error": "نتيجة الأداة ليست كائن JSON"}
    encoded = json.dumps({"result": result}, ensure_ascii=False).encode("utf-8")
    if len(encoded) > 1_000_000:
        return jsonify(ok=False, error="نتيجة الأداة أكبر من الحد المسموح"), 413
    path = _tool_result_path(job_id, call_id)
    if path.exists():
        return jsonify(ok=True, duplicate=True)
    tmp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    tmp.write_bytes(encoded)
    tmp.replace(path)
    return jsonify(ok=True, received=True)


@bp.get("/jobs")
def list_jobs():
    items = []
    for p in sorted(_jobs_dir().glob("*.json"), key=lambda x: x.stat().st_mtime, reverse=True)[:50]:
        try:
            j = json.loads(p.read_text(encoding="utf-8"))
            items.append(
                {
                    "id": j.get("id"),
                    "status": j.get("status"),
                    "message": str(j.get("message") or "")[:120],
                    "created_at": j.get("created_at"),
                    "finished_at": j.get("finished_at"),
                }
            )
        except Exception:
            continue
    return jsonify(ok=True, jobs=items)


@bp.post("/jobs/<job_id>/cancel")
def cancel_job(job_id: str):
    job = _read_job(job_id)
    if not job:
        return jsonify(ok=False, error="غير موجودة"), 404
    job["cancel"] = True
    if job.get("status") in {"queued", "running"}:
        job["status"] = "cancelled"
        job["finished_at"] = time.time()
        job.pop("platform_context", None)
        _write_job(job)
        _fire_webhook("job.cancelled", job)
    else:
        _write_job(job)
    return jsonify(ok=True, job_id=job_id, status=job.get("status"))


@bp.get("/health")
def agent_health():
    cfg = _llm_config()
    return jsonify(
        ok=True,
        service="yd-agent-runtime",
        llm_configured=bool(cfg["api_key"]),
        llm_base=cfg["base"],
        llm_model=cfg["model"],
        jobs_dir=str(_jobs_dir()),
    )
