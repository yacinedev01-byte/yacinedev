"""
YACINEDEV Agent — Shell + Python + Browser + Manus-style commands on Railway
- POST /shell, /code/run, /workspace/*, /browser/*, /run, /register
- Manus commands under /cmd/* :
  /plan /wide-research /agent-mode /take-over /sandbox
  /replay /deploy /skill /connect /think
- GET  /cmd/commands  قائمة الأوامر
- POST /agent/jobs   مهمة تفكير+رد دائمة على Railway
- GET  /agent/jobs/<id> استطلاع النتيجة (لا تضيع عند إغلاق الصفحة)
- GET  /health
"""
from __future__ import annotations

import ast
import json
import os
import re
import shutil
import subprocess
import time
import traceback
import base64
import ipaddress
import socket
import threading
import signal
import resource
import difflib
from urllib.parse import urlparse

try:
    from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError
except Exception:
    sync_playwright = None
    PlaywrightTimeoutError = TimeoutError
from pathlib import Path
from typing import Any

from flask import Flask, request, jsonify

app = Flask(__name__)

API_KEY = os.environ.get("SHELL_API_KEY") or os.environ.get("PYTHON_API_KEY") or ""
WORKROOT = Path(os.environ.get("SHELL_WORKROOT", "/tmp/yd_sandbox"))
TOOLS_DIR = Path(os.environ.get("PYTHON_TOOLS_DIR", "/tmp/yd_python_tools"))
WORKROOT.mkdir(parents=True, exist_ok=True)
TOOLS_DIR.mkdir(parents=True, exist_ok=True)

# ── Manus-style agent commands ──
try:
    import agent_cmds
    agent_cmds.WORKROOT = WORKROOT
    app.register_blueprint(agent_cmds.bp, url_prefix="/cmd")
except Exception as _cmd_err:
    import sys
    print("agent_cmds load failed:", _cmd_err, file=sys.stderr)

# ── Durable agent jobs (thinking + reply on Railway) ──
try:
    import agent_runtime
    agent_runtime.WORKROOT = WORKROOT
    app.register_blueprint(agent_runtime.bp, url_prefix="/agent")
except Exception as _rt_err:
    import sys
    print("agent_runtime load failed:", _rt_err, file=sys.stderr)

# ── Metrics / Webhooks / Cron / Sandbox المتقدمة ──
for _mod_name in ("metrics", "webhooks", "cron", "sandbox"):
    try:
        _mod = __import__(_mod_name)
        app.register_blueprint(_mod.bp)
        if _mod_name == "cron":
            _mod.start_background()
    except Exception as _add_err:
        import sys
        print(f"{_mod_name} load failed:", _add_err, file=sys.stderr)


_browser_lock = threading.RLock()
_browser_runtime = None
_browser_contexts = {}

def _safe_url(url: str) -> str:
    parsed = urlparse(str(url).strip())
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("الرابط يجب أن يبدأ بـ http:// أو https://")
    if parsed.username or parsed.password:
        raise ValueError("بيانات الدخول داخل الرابط غير مسموح بها")
    try:
        if parsed.port not in {None, 80, 443}:
            raise ValueError("المنافذ غير القياسية غير مسموح بها")
    except ValueError as exc:
        if "port" in str(exc).lower():
            raise ValueError("منفذ الرابط غير صالح") from exc
        raise
    host = parsed.hostname.lower().rstrip(".")
    if host in {"localhost", "localhost.localdomain"} or host.endswith((".localhost", ".local", ".internal")):
        raise ValueError("الوصول إلى عناوين داخلية غير مسموح")
    try:
        addresses = socket.getaddrinfo(host, parsed.port or (443 if parsed.scheme == "https" else 80), type=socket.SOCK_STREAM)
        for item in addresses:
            ip = ipaddress.ip_address(item[4][0])
            if not ip.is_global:
                raise ValueError("الوصول إلى الشبكات الداخلية غير مسموح")
    except socket.gaierror as exc:
        raise ValueError(f"تعذر حل اسم النطاق: {exc}")
    return parsed.geturl()

def _browser_request_is_public(raw_url: str) -> bool:
    parsed = urlparse(str(raw_url))
    if parsed.scheme not in {"http", "https"}:
        return parsed.scheme in {"about", "data", "blob"}
    host = (parsed.hostname or "").lower().rstrip(".")
    if parsed.username or parsed.password or not host or host in {"localhost", "localhost.localdomain"} or host.endswith((".localhost", ".local", ".internal")):
        return False
    try:
        if parsed.port not in {None, 80, 443}:
            return False
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        return all(ipaddress.ip_address(item[4][0]).is_global for item in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM))
    except (OSError, ValueError):
        return False

def _route_browser_request(route) -> None:
    try:
        if _browser_request_is_public(route.request.url):
            route.continue_()
        else:
            route.abort("blockedbyclient")
    except Exception:
        route.abort("blockedbyclient")

def _browser_context(session: str):
    global _browser_runtime
    if sync_playwright is None:
        raise RuntimeError("Playwright غير مثبت على الخادم")
    session = re.sub(r"[^a-zA-Z0-9_-]", "_", str(session or "default"))[:64] or "default"
    with _browser_lock:
        if _browser_runtime is None:
            _browser_runtime = sync_playwright().start()
        ctx = _browser_contexts.get(session)
        if ctx is None:
            browser = _browser_runtime.chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
            ctx = browser.new_context(viewport={"width": 1280, "height": 900}, java_script_enabled=True)
            ctx.route("**/*", _route_browser_request)
            _browser_contexts[session] = ctx
        pages = ctx.pages
        page = pages[0] if pages else ctx.new_page()
        page.set_default_timeout(BROWSER_TIMEOUT_MS)
        return session, ctx, page

def _close_browser(session: str):
    session = re.sub(r"[^a-zA-Z0-9_-]", "_", str(session or "default"))[:64] or "default"
    with _browser_lock:
        ctx = _browser_contexts.pop(session, None)
        if ctx:
            ctx.close()

MAX_TIMEOUT = 120
MAX_OUTPUT = 200_000
MAX_SOURCE = 250_000
MAX_BROWSER_TEXT = 100_000
MAX_SCREENSHOT = 2_000_000
BROWSER_TIMEOUT_MS = 30_000

BLOCKED_SHELL = [
    r"\brm\s+-rf\s+/(\s|$)",
    r"\bmkfs\b",
    r"\bdd\s+if=.*of=/dev",
    r":\(\)\s*\{\s*:\|:&\s*\}\s*;\s*:",
    r"\bshutdown\b|\breboot\b",
    r"\bmount\b|\bumount\b|\binsmod\b|\brmmod\b|\bsystemctl\b|\bservice\s+",
    r"/proc/|/sys/|/dev/[^nullzero]",
]

# مكتبات/نداءات ممنوعة داخل كود الأدوات المسجّلة
BLOCKED_PY = re.compile(
    r"\b(?:import|from)\s+(?:os|sys|subprocess|socket|ctypes|shutil|pathlib|signal|resource|pty|multiprocessing|threading)\b"
    r"|\b(?:os|sys|subprocess|socket|ctypes|shutil)\s*\."
    r"|\b(?:eval|exec|compile|__import__)\s*\(",
    re.I,
)


def ok_auth() -> bool:
    if not API_KEY:
        return False
    return request.headers.get("X-Api-Key") == API_KEY


def _child_limits(timeout: int) -> None:
    """Apply best-effort Linux limits inside the Railway container."""
    try:
        resource.setrlimit(resource.RLIMIT_CPU, (timeout + 2, timeout + 2))
        resource.setrlimit(resource.RLIMIT_AS, (768 * 1024 * 1024, 768 * 1024 * 1024))
        resource.setrlimit(resource.RLIMIT_FSIZE, (50 * 1024 * 1024, 50 * 1024 * 1024))
        resource.setrlimit(resource.RLIMIT_NOFILE, (128, 128))
        resource.setrlimit(resource.RLIMIT_NPROC, (64, 64))
    except (OSError, ValueError):
        pass


def _kill_process_group(proc: subprocess.Popen) -> None:
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (OSError, ProcessLookupError):
        try:
            proc.kill()
        except OSError:
            pass


def _session_name(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_-]", "_", str(value or "default"))[:64] or "default"


def _session_path(session: str, name: str = "") -> Path:
    root = (WORKROOT / _session_name(session)).resolve()
    root.mkdir(parents=True, exist_ok=True)
    candidate = (root / name).resolve()
    if candidate != root and root not in candidate.parents:
        raise ValueError("مسار خارج مساحة الجلسة غير مسموح")
    return candidate


@app.before_request
def check_auth():
    if request.path == "/health":
        return None
    if not API_KEY:
        return jsonify(ok=False, error="SHELL_API_KEY / PYTHON_API_KEY غير مضبوط على السيرفر"), 500
    if not ok_auth():
        return jsonify(ok=False, error="مفتاح API غير صحيح"), 401


@app.get("/health")
def health():
    return jsonify(
        ok=True,
        service="yd-shell-python-agent",
        features=[
            "shell", "code_run_python", "python_tools", "python_run", "python_register",
            "workspace_list", "workspace_read", "workspace_write", "workspace_diff",
            "browser_navigate", "browser_read", "browser_click", "browser_type", "browser_screenshot",
            "cmd_plan", "cmd_wide_research", "cmd_agent_mode", "cmd_take_over", "cmd_sandbox",
            "cmd_replay", "cmd_deploy", "cmd_skill", "cmd_connect", "cmd_think",
            "cmd_dream", "cmd_evolve", "cmd_mirror", "cmd_swarm", "cmd_time_travel",
            "cmd_bet", "cmd_hive_mind", "cmd_fs", "cmd_py", "cmd_js", "cmd_docker",
            "cmd_sql", "cmd_http", "cmd_scrape",
            "agent_jobs",
        ],
    )


# ───────────────── Shell ─────────────────

@app.post("/shell")
def shell():
    data = request.get_json(silent=True) or {}
    cmd = str(data.get("cmd") or "").strip()
    if not cmd:
        return jsonify(ok=False, error="cmd مطلوب"), 400

    for pat in BLOCKED_SHELL:
        if re.search(pat, cmd, re.I):
            return jsonify(ok=False, error=f"أمر مرفوض لأسباب أمان: {pat}"), 400

    timeout = max(1, min(int(data.get("timeout") or 30), MAX_TIMEOUT))
    session = re.sub(r"[^a-zA-Z0-9_-]", "_", str(data.get("session") or "default"))[:64] or "default"
    workdir = WORKROOT / session
    workdir.mkdir(parents=True, exist_ok=True)

    started = time.time()
    try:
        clean_env = {
            k: v for k, v in os.environ.items()
            if k in {"PATH", "LANG", "LC_ALL", "HOME", "TMPDIR"}
        }
        clean_env["HOME"] = str(workdir)
        proc = subprocess.Popen(
            ["/bin/bash", "-lc", cmd], cwd=str(workdir), env=clean_env,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            start_new_session=True, preexec_fn=lambda: _child_limits(timeout),
        )
        try:
            stdout, stderr = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            _kill_process_group(proc)
            stdout, stderr = proc.communicate()
            return jsonify(ok=False, error=f"انتهت المهلة بعد {timeout} ثانية",
                           stdout=(stdout or str(exc.stdout or ""))[-MAX_OUTPUT:],
                           stderr=(stderr or str(exc.stderr or ""))[-MAX_OUTPUT:], timed_out=True), 200
        return jsonify(
            ok=proc.returncode == 0,
            exit_code=proc.returncode,
            stdout=(stdout or "")[-MAX_OUTPUT:],
            stderr=(stderr or "")[-MAX_OUTPUT:],
            duration_s=round(time.time() - started, 2),
            workdir=str(workdir),
        )
    except Exception as e:
        return jsonify(ok=False, error=str(e)), 500


@app.post("/code/run")
def code_run():
    data = request.get_json(silent=True) or {}
    language = str(data.get("language") or "python").lower()
    source = str(data.get("source") or "")
    if language not in {"python", "py"}:
        return jsonify(ok=False, error="حالياً اللغة المدعومة هي Python فقط"), 400
    if not source or len(source) > MAX_SOURCE:
        return jsonify(ok=False, error="source فارغ أو طويل جداً"), 400
    try:
        ast.parse(source)
    except SyntaxError as exc:
        return jsonify(ok=False, error=f"خطأ بناء جملة: {exc}"), 400
    session = _session_name(data.get("session") or "default")
    timeout = max(1, min(int(data.get("timeout") or 30), MAX_TIMEOUT))
    workdir = _session_path(session)
    source_path = workdir / "__yd_code_run.py"
    source_path.write_text(source, encoding="utf-8")
    try:
        proc = subprocess.Popen(
            ["/usr/local/bin/python", "-I", str(source_path)], cwd=str(workdir),
            env={"PATH": os.environ.get("PATH", ""), "HOME": str(workdir), "LANG": "C.UTF-8"},
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            start_new_session=True, preexec_fn=lambda: _child_limits(timeout),
        )
        try:
            stdout, stderr = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            _kill_process_group(proc)
            stdout, stderr = proc.communicate()
            return jsonify(ok=False, error=f"انتهت المهلة بعد {timeout} ثانية",
                           stdout=(stdout or "")[-MAX_OUTPUT:], stderr=(stderr or "")[-MAX_OUTPUT:], timed_out=True), 200
        return jsonify(ok=proc.returncode == 0, exit_code=proc.returncode,
                       stdout=(stdout or "")[-MAX_OUTPUT:], stderr=(stderr or "")[-MAX_OUTPUT:], workdir=str(workdir))
    finally:
        source_path.unlink(missing_ok=True)


@app.post("/workspace/list")
def workspace_list():
    data = request.get_json(silent=True) or {}
    try:
        session = data.get("session") or "default"
        root = _session_path(session, str(data.get("path") or ""))
        if not root.exists() or not root.is_dir():
            return jsonify(ok=False, error="المجلد غير موجود"), 404
        base = _session_path(session)
        items = [{"name": p.name, "type": "directory" if p.is_dir() else "file", "size": p.stat().st_size if p.is_file() else 0}
                 for p in sorted(root.iterdir(), key=lambda x: x.name.lower())[:500]]
        return jsonify(ok=True, items=items, path=str(root.relative_to(base)))
    except Exception as e:
        return jsonify(ok=False, error=str(e)), 400


@app.post("/workspace/read")
def workspace_read():
    data = request.get_json(silent=True) or {}
    try:
        p = _session_path(data.get("session") or "default", str(data.get("path") or ""))
        if not p.is_file():
            return jsonify(ok=False, error="الملف غير موجود"), 404
        raw = p.read_text(encoding="utf-8", errors="replace")
        return jsonify(ok=True, path=str(data.get("path")), content=raw[:MAX_OUTPUT], truncated=len(raw) > MAX_OUTPUT)
    except Exception as e:
        return jsonify(ok=False, error=str(e)), 400


@app.post("/workspace/write")
def workspace_write():
    data = request.get_json(silent=True) or {}
    try:
        rel = str(data.get("path") or "").strip()
        content = str(data.get("content") or "")
        if not rel or len(content) > MAX_SOURCE:
            return jsonify(ok=False, error="path/content غير صالح"), 400
        p = _session_path(data.get("session") or "default", rel)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        return jsonify(ok=True, path=rel, size=len(content.encode("utf-8")))
    except Exception as e:
        return jsonify(ok=False, error=str(e)), 400


@app.post("/workspace/diff")
def workspace_diff():
    data = request.get_json(silent=True) or {}
    try:
        rel = str(data.get("path") or "")
        p = _session_path(data.get("session") or "default", rel)
        before = str(data.get("before") or "").splitlines(keepends=True)
        after = p.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True) if p.is_file() else []
        diff = "".join(difflib.unified_diff(before, after, fromfile="before", tofile=rel))
        return jsonify(ok=True, diff=diff[:MAX_OUTPUT])
    except Exception as e:
        return jsonify(ok=False, error=str(e)), 400


@app.post("/reset")
def reset():
    data = request.get_json(silent=True) or {}
    session = re.sub(r"[^a-zA-Z0-9_-]", "_", str(data.get("session") or "default"))[:64] or "default"
    workdir = WORKROOT / session
    if workdir.exists():
        shutil.rmtree(workdir, ignore_errors=True)
    workdir.mkdir(parents=True, exist_ok=True)
    return jsonify(ok=True, session=session)



# ───────────────── Browser bridge ─────────────────

def _browser_response(session: str, page):
    title = page.title()
    text = page.locator("body").inner_text(timeout=5000) if page.locator("body").count() else ""
    elements = page.evaluate("""() => {
      const visible = (el) => { const r = el.getBoundingClientRect(), s = getComputedStyle(el); return r.width > 0 && r.height > 0 && s.display !== 'none' && s.visibility !== 'hidden' && s.opacity !== '0'; };
      return Array.from(document.querySelectorAll('a[href],button,input,textarea,select,[role="button"],[role="link"]'))
        .filter(visible).slice(0, 80).map((el, i) => {
          const ref = String(i + 1); el.setAttribute('data-yd-agent-ref', ref);
          const type = (el.getAttribute('type') || '').toLowerCase();
          let safeHref = '';
          if (el.tagName === 'A' && el.href) { try { const u = new URL(el.href); safeHref = u.origin + u.pathname; } catch {} }
          return {ref: i + 1, tag: el.tagName.toLowerCase(), type,
            text: (el.innerText || (/^(submit|button|reset)$/.test(type) ? (el.value || '') : '')).trim().slice(0, 180), label: (el.getAttribute('aria-label') || el.getAttribute('title') || '').slice(0, 180),
            placeholder: (el.getAttribute('placeholder') || '').slice(0, 120), name: (el.getAttribute('name') || '').slice(0, 120),
            href: safeHref.slice(0, 300)};
        });
    }""")
    return {"ok": True, "session": session, "url": page.url, "title": title, "text": text[:MAX_BROWSER_TEXT], "truncated": len(text) > MAX_BROWSER_TEXT, "elements": elements}

@app.post("/browser/navigate")
def browser_navigate():
    data = request.get_json(silent=True) or {}
    try:
        url = _safe_url(data.get("url") or "")
        session, _, page = _browser_context(data.get("session") or "default")
        page.goto(url, wait_until="domcontentloaded", timeout=BROWSER_TIMEOUT_MS)
        return jsonify(_browser_response(session, page))
    except PlaywrightTimeoutError:
        return jsonify(ok=False, error="انتهت مهلة تحميل الصفحة"), 504
    except Exception as e:
        return jsonify(ok=False, error=str(e)), 400

@app.post("/browser/read")
def browser_read():
    data = request.get_json(silent=True) or {}
    try:
        session, _, page = _browser_context(data.get("session") or "default")
        return jsonify(_browser_response(session, page))
    except Exception as e:
        return jsonify(ok=False, error=str(e)), 400

@app.post("/browser/click")
def browser_click():
    data = request.get_json(silent=True) or {}
    try:
        session, _, page = _browser_context(data.get("session") or "default")
        selector = str(data.get("selector") or "").strip()
        ref = data.get("ref")
        if ref is not None:
            try:
                ref = int(ref)
            except (TypeError, ValueError):
                return jsonify(ok=False, error="مرجع العنصر غير صالح"), 400
            if ref < 1 or ref > 80:
                return jsonify(ok=False, error="مرجع العنصر خارج النطاق"), 400
            selector = f'[data-yd-agent-ref="{ref}"]'
        if not selector or len(selector) > 500:
            return jsonify(ok=False, error="أرسل ref من لقطة الصفحة أو selector صالحاً"), 400
        locator = page.locator(selector).first
        meta = locator.evaluate("""el => { const type = (el.getAttribute('type') || '').toLowerCase(); let safeHref = '';
          if (el.tagName === 'A' && el.href) { try { const u = new URL(el.href); safeHref = u.origin + u.pathname; } catch {} }
          return {tag: el.tagName.toLowerCase(), type,
            text: (el.innerText || (/^(submit|button|reset)$/.test(type) ? (el.value || '') : '')).trim().slice(0, 180),
            label: (el.getAttribute('aria-label') || el.getAttribute('title') || '').slice(0, 180),
            href: safeHref, form: el.form ? (el.form.innerText || '') : ''}; }""")
        action_label = " ".join(str(meta.get(k) or "") for k in ("text", "label", "href"))
        form_context = str(meta.get("form") or "")[:1000]
        high_impact = re.compile(r"(?:buy|purchase|pay|checkout|place\s+order|submit|send|post|publish|delete|remove|transfer|confirm|save\s+changes|unsubscribe|cancel\s+subscription|شراء|ادفع|دفع|إتمام\s+الطلب|تأكيد|إرسال|نشر|حذف|إزالة|تحويل|إلغاء\s+الاشتراك|حفظ\s+التغييرات)", re.I)
        search_action = re.compile(r"(?:search|find|lookup|filter|بحث|ابحث|تصفية|انتقال)", re.I)
        is_submit = meta.get("type") == "submit" or (meta.get("tag") == "button" and bool(meta.get("form")))
        requires_confirmation = bool(high_impact.search(action_label)) or (is_submit and not search_action.search(action_label + " " + form_context))
        page_origin = urlparse(page.url).scheme + "://" + (urlparse(page.url).netloc or "unknown-site")
        action_summary = ((action_label[:180] or "إرسال نموذج الصفحة") + " at " + page_origin)[:300]
        supplied_summary = re.sub(r"\s+", " ", str(data.get("action_summary") or "").strip()).casefold()
        exact_summary = supplied_summary == re.sub(r"\s+", " ", action_summary.strip()).casefold()
        if requires_confirmation and (not bool(data.get("confirmed")) or not exact_summary):
            return jsonify(ok=False, requires_confirmation=True, action_summary=action_summary, url=page.url,
                           error="يتطلب هذا الإجراء موافقة صريحة من المستخدم قبل تنفيذه."), 409
        locator.click(timeout=BROWSER_TIMEOUT_MS)
        page.wait_for_timeout(300)
        return jsonify(_browser_response(session, page))
    except PlaywrightTimeoutError:
        return jsonify(ok=False, error="تعذر النقر ضمن المهلة"), 504
    except Exception as e:
        return jsonify(ok=False, error=str(e)), 400

@app.post("/browser/type")
def browser_type():
    data = request.get_json(silent=True) or {}
    try:
        session, _, page = _browser_context(data.get("session") or "default")
        selector = str(data.get("selector") or "").strip()
        text = str(data.get("text") or "")
        ref = data.get("ref")
        if ref is not None:
            try:
                ref = int(ref)
            except (TypeError, ValueError):
                return jsonify(ok=False, error="مرجع العنصر غير صالح"), 400
            if ref < 1 or ref > 80:
                return jsonify(ok=False, error="مرجع العنصر خارج النطاق"), 400
            selector = f'[data-yd-agent-ref="{ref}"]'
        if not selector or len(selector) > 500 or len(text) > 5_000:
            return jsonify(ok=False, error="selector/text غير صالح"), 400
        locator = page.locator(selector).first
        meta = locator.evaluate("""el => ({type: (el.getAttribute('type') || '').toLowerCase(), name: (el.getAttribute('name') || '').toLowerCase(),
          id: (el.id || '').toLowerCase(), autocomplete: (el.getAttribute('autocomplete') || '').toLowerCase(),
          label: (el.getAttribute('aria-label') || el.getAttribute('placeholder') || '').toLowerCase()})""")
        sensitive = re.compile(r"(?:password|passwd|secret|token|otp|one-time-code|credit|card|cvv|cvc|iban|bank|ssn|pin|كلمة\s*المرور|رمز\s*(?:التحقق|سري)|بطاقة|حساب\s*بنكي)", re.I)
        if meta.get("type") == "password" or any(sensitive.search(str(meta.get(k) or "")) for k in ("name", "id", "autocomplete", "label")):
            return jsonify(ok=False, error="الكتابة في حقول كلمات المرور أو بيانات الدفع/الرموز الحساسة غير مسموحة."), 403
        locator.fill(text, timeout=BROWSER_TIMEOUT_MS)
        return jsonify(_browser_response(session, page))
    except PlaywrightTimeoutError:
        return jsonify(ok=False, error="تعذر الكتابة ضمن المهلة"), 504
    except Exception as e:
        return jsonify(ok=False, error=str(e)), 400

@app.post("/browser/screenshot")
def browser_screenshot():
    data = request.get_json(silent=True) or {}
    try:
        session, _, page = _browser_context(data.get("session") or "default")
        raw = page.screenshot(type="jpeg", quality=68, full_page=bool(data.get("full_page")))
        if len(raw) > MAX_SCREENSHOT:
            return jsonify(ok=False, error="الصورة أكبر من الحد المسموح"), 413
        return jsonify(ok=True, session=session, url=page.url, mime="image/jpeg", base64=base64.b64encode(raw).decode("ascii"))
    except Exception as e:
        return jsonify(ok=False, error=str(e)), 400

@app.post("/browser/close")
def browser_close():
    data = request.get_json(silent=True) or {}
    _close_browser(data.get("session") or "default")
    return jsonify(ok=True)

# ───────────────── Built-in Python tools ─────────────────

def _tool_echo(args: dict) -> Any:
    return {"echo": args}


def _tool_calc(args: dict) -> Any:
    expr = str(args.get("expr") or args.get("expression") or "").strip()
    if not expr:
        raise ValueError("expr مطلوب")
    # حساب آمن عبر AST
    node = ast.parse(expr, mode="eval")
    for n in ast.walk(node):
        if isinstance(n, (ast.Call, ast.Attribute, ast.Name)) and not isinstance(n, ast.Load):
            pass
        if isinstance(n, ast.Call):
            raise ValueError("الدوال غير مسموحة")
        if isinstance(n, ast.Attribute):
            raise ValueError("الخصائص غير مسموحة")
        if isinstance(n, ast.Name) and n.id not in {"True", "False", "None"}:
            # نسمح فقط بالأرقام والعمليات
            raise ValueError(f"اسم غير مسموح: {n.id}")
    return {"result": eval(compile(node, "<calc>", "eval"), {"__builtins__": {}}, {})}


def _tool_json_pretty(args: dict) -> Any:
    raw = args.get("data", args.get("json", ""))
    if isinstance(raw, (dict, list)):
        data = raw
    else:
        data = json.loads(str(raw))
    return {"pretty": json.dumps(data, ensure_ascii=False, indent=2)}


def _tool_text_stats(args: dict) -> Any:
    text = str(args.get("text") or "")
    lines = text.splitlines()
    words = re.findall(r"\S+", text)
    return {
        "chars": len(text),
        "chars_no_space": len(re.sub(r"\s+", "", text)),
        "words": len(words),
        "lines": len(lines),
    }


BUILTIN_TOOLS: dict[str, dict] = {
    "echo": {"fn": _tool_echo, "description": "إرجاع المدخلات كما هي (اختبار)"},
    "calc": {"fn": _tool_calc, "description": "حساب تعبير رياضي بسيط: {expr: '2+2*3'}"},
    "json_pretty": {"fn": _tool_json_pretty, "description": "تنسيق JSON: {data: ...}"},
    "text_stats": {"fn": _tool_text_stats, "description": "إحصاء نص: {text: '...'}"},
}


def list_registered() -> list[dict]:
    items = []
    for p in sorted(TOOLS_DIR.glob("*.py")):
        name = p.stem
        meta = TOOLS_DIR / f"{name}.meta.json"
        desc = ""
        if meta.exists():
            try:
                desc = json.loads(meta.read_text(encoding="utf-8")).get("description") or ""
            except Exception:
                pass
        items.append({"name": name, "description": desc, "type": "registered"})
    return items


def load_registered(name: str):
    path = TOOLS_DIR / f"{name}.py"
    if not path.is_file():
        return None
    src = path.read_text(encoding="utf-8")
    import math as _math
    import datetime as _datetime
    import collections as _collections
    import itertools as _itertools
    import functools as _functools
    import statistics as _statistics
    import hashlib as _hashlib
    import base64 as _base64
    import urllib.parse as _urlparse

    # مكتبات مسموحة فقط داخل الأدوات المسجّلة
    allowed_modules = {
        "re": re,
        "json": json,
        "math": _math,
        "datetime": _datetime,
        "collections": _collections,
        "itertools": _itertools,
        "functools": _functools,
        "statistics": _statistics,
        "hashlib": _hashlib,
        "base64": _base64,
        "urllib": __import__("urllib"),
        "urllib.parse": _urlparse,
    }

    def safe_import(mod_name, globals=None, locals=None, fromlist=(), level=0):
        root = mod_name.split(".")[0]
        if mod_name in allowed_modules:
            return allowed_modules[mod_name]
        if root in allowed_modules and not fromlist:
            return allowed_modules[root]
        if root in allowed_modules:
            return allowed_modules[root]
        raise ImportError(f"الاستيراد غير مسموح: {mod_name}")

    safe_builtins = {
        "abs": abs, "min": min, "max": max, "sum": sum, "len": len, "range": range,
        "enumerate": enumerate, "sorted": sorted, "list": list, "dict": dict, "set": set,
        "tuple": tuple, "str": str, "int": int, "float": float, "bool": bool,
        "print": print, "isinstance": isinstance, "type": type, "round": round,
        "zip": zip, "map": map, "filter": filter, "any": any, "all": all,
        "repr": repr, "sorted": sorted, "reversed": reversed, "pow": pow,
        "__import__": safe_import,
    }
    ns: dict[str, Any] = {
        "__builtins__": safe_builtins,
        # متاحة مباشرة بدون import
        "re": re,
        "json": json,
        "math": _math,
    }
    exec(compile(src, str(path), "exec"), ns, ns)
    fn = ns.get("run")
    if not callable(fn):
        raise RuntimeError("الأداة لا تحتوي دالة run(args)")
    return fn


# ───────────────── Python API (متوافق مع PythonBridgeTool.php) ─────────────────

@app.get("/tools")
def tools_list():
    items = []
    for name, info in BUILTIN_TOOLS.items():
        items.append({"name": name, "description": info["description"], "type": "builtin"})
    items.extend(list_registered())
    return jsonify(ok=True, tools=items, count=len(items))


@app.post("/run")
def tools_run():
    data = request.get_json(silent=True) or {}
    tool = re.sub(r"[^a-zA-Z0-9_\-]", "", str(data.get("tool") or ""))
    args = data.get("args") if isinstance(data.get("args"), dict) else {}
    if not tool:
        return jsonify(ok=False, error="tool مطلوب"), 400

    session = str(data.get("session") or "")
    started = time.time()
    try:
        if tool in BUILTIN_TOOLS:
            result = BUILTIN_TOOLS[tool]["fn"](args)
        else:
            fn = load_registered(tool)
            if fn is None:
                return jsonify(ok=False, error=f"أداة غير موجودة: {tool}"), 404
            result = fn(args)
        duration_ms = int((time.time() - started) * 1000)
        try:
            import metrics as _metrics
            _metrics.record(tool, True, duration_ms, session=session)
        except Exception:
            pass
        return jsonify(
            ok=True,
            tool=tool,
            result=result,
            duration_s=round(duration_ms / 1000, 2),
        )
    except Exception as e:
        duration_ms = int((time.time() - started) * 1000)
        trace = traceback.format_exc()
        try:
            import metrics as _metrics
            _metrics.record(tool, False, duration_ms, error=str(e), session=session)
        except Exception:
            pass
        # trace كامل حتى 4000 حرف بدل رسالة مجردة -- تصحيح أخطاء مباشر
        return jsonify(
            ok=False,
            tool=tool,
            error=str(e),
            trace=trace[-4000:],
            duration_s=round(duration_ms / 1000, 2),
        ), 200


@app.post("/register")
def tools_register():
    data = request.get_json(silent=True) or {}
    name = re.sub(r"[^a-zA-Z0-9_\-]", "", str(data.get("name") or "").lower())
    source = str(data.get("source") or "")
    description = str(data.get("description") or "")[:200]

    if not name or len(source) < 10:
        return jsonify(ok=False, error="name/source مطلوبان"), 400
    if len(source) > MAX_SOURCE:
        return jsonify(ok=False, error="الكود طويل جداً"), 400
    if "def run(" not in source:
        return jsonify(ok=False, error="يجب أن يحتوي الكود على def run(args):"), 400
    if BLOCKED_PY.search(source):
        return jsonify(ok=False, error="الكود يطلب وصول نظامي أو تنفيذاً ديناميكياً غير مسموح به"), 400

    # تحقق بناء الجملة
    try:
        ast.parse(source)
    except SyntaxError as e:
        return jsonify(ok=False, error=f"خطأ بناء جملة: {e}"), 400

    # اختبار في sandbox معزول قبل الاعتماد النهائي -- force:true يتجاوز الفشل
    try:
        import sandbox as _sandbox
        test = _sandbox.test_tool_source(source, data.get("sample_args") or {})
        if not test.get("sandbox_ok") and not data.get("force"):
            return jsonify(ok=False, error="فشل اختبار الأداة في sandbox قبل التسجيل", detail=test), 422
    except ImportError:
        pass  # وحدة sandbox غير مثبتة -- تخطَّ الاختبار بدل كسر التسجيل

    path = TOOLS_DIR / f"{name}.py"
    path.write_text(source, encoding="utf-8")
    (TOOLS_DIR / f"{name}.meta.json").write_text(
        json.dumps({"name": name, "description": description}, ensure_ascii=False),
        encoding="utf-8",
    )
    return jsonify(ok=True, name=name, description=description)


@app.delete("/tools/<name>")
def tools_delete(name: str):
    name = re.sub(r"[^a-zA-Z0-9_\-]", "", name.lower())
    if not name:
        return jsonify(ok=False, error="اسم غير صالح"), 400
    py = TOOLS_DIR / f"{name}.py"
    meta = TOOLS_DIR / f"{name}.meta.json"
    if not py.exists():
        return jsonify(ok=False, error="غير موجودة"), 404
    py.unlink(missing_ok=True)
    meta.unlink(missing_ok=True)
    return jsonify(ok=True, deleted=name)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
