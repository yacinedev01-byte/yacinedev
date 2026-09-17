"""
YACINEDEV Shell Agent — خدمة تنفيذ أوامر شل حقيقية داخل حاوية معزولة.
تُنشر على Railway (أو أي مزوّد حاويات) بشكل مستقل عن استضافة PHP.
الفكرة: agent_loop.php (PHP) يرسل الأمر إلى هذه الخدمة عبر HTTP، تنفّذه
هنا (حيث توجد صلاحيات كاملة + apt/pip/npm)، وترجّع stdout/stderr/exit_code.

⚠️ هذه الحاوية تُنفّذ أوامر عشوائية بأمر من نموذج LLM. يجب أن تبقى:
   - معزولة تماماً عن أي بيانات/خدمات حساسة (لا تربطها بقاعدة بياناتك الرئيسية)
   - محمية بمفتاح API قوي (X-Api-Key) — لا تنشرها بدون مفتاح
   - قابلة لإعادة التصفير (لا تخزّن فيها أسرار دائمة)
"""
import os
import re
import subprocess
import time
from pathlib import Path

from flask import Flask, request, jsonify

app = Flask(__name__)

API_KEY = os.environ.get("SHELL_API_KEY", "")
WORKROOT = Path(os.environ.get("SHELL_WORKROOT", "/tmp/yd_sandbox"))
WORKROOT.mkdir(parents=True, exist_ok=True)

MAX_TIMEOUT = 120
MAX_OUTPUT = 200_000  # حرف

# حماية إضافية على مستوى الخدمة نفسها (فوق ما يفلتره PHP)
BLOCKED_PATTERNS = [
    r"\brm\s+-rf\s+/(\s|$)",
    r"\bmkfs\b",
    r"\bdd\s+if=.*of=/dev",
    r":\(\)\s*\{\s*:\|:&\s*\}\s*;\s*:",  # fork bomb
    r"\bshutdown\b|\breboot\b",
]


def is_blocked(cmd: str):
    for pat in BLOCKED_PATTERNS:
        if re.search(pat, cmd, re.IGNORECASE):
            return pat
    return None


@app.before_request
def check_auth():
    if request.path == "/health":
        return None
    if not API_KEY:
        return jsonify(ok=False, error="SHELL_API_KEY غير مضبوط على السيرفر"), 500
    if request.headers.get("X-Api-Key") != API_KEY:
        return jsonify(ok=False, error="مفتاح API غير صحيح"), 401


@app.get("/health")
def health():
    return jsonify(ok=True, service="yd-shell-agent")


@app.post("/shell")
def shell():
    data = request.get_json(silent=True) or {}
    cmd = str(data.get("cmd") or "").strip()
    if not cmd:
        return jsonify(ok=False, error="cmd مطلوب"), 400

    blocked = is_blocked(cmd)
    if blocked:
        return jsonify(ok=False, error=f"أمر مرفوض لأسباب أمان: {blocked}"), 400

    timeout = int(data.get("timeout") or 30)
    timeout = max(1, min(timeout, MAX_TIMEOUT))

    session = str(data.get("session") or "default")
    session = re.sub(r"[^a-zA-Z0-9_-]", "_", session)[:64] or "default"
    workdir = WORKROOT / session
    workdir.mkdir(parents=True, exist_ok=True)

    started = time.time()
    try:
        proc = subprocess.run(
            ["/bin/bash", "-lc", cmd],
            cwd=str(workdir),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        stdout = proc.stdout[-MAX_OUTPUT:]
        stderr = proc.stderr[-MAX_OUTPUT:]
        return jsonify(
            ok=proc.returncode == 0,
            exit_code=proc.returncode,
            stdout=stdout,
            stderr=stderr,
            duration_s=round(time.time() - started, 2),
            workdir=str(workdir),
        )
    except subprocess.TimeoutExpired as e:
        return jsonify(
            ok=False,
            error=f"انتهت المهلة بعد {timeout} ثانية",
            stdout=(e.stdout or "")[-MAX_OUTPUT:] if e.stdout else "",
            stderr=(e.stderr or "")[-MAX_OUTPUT:] if e.stderr else "",
        ), 200
    except Exception as e:
        return jsonify(ok=False, error=str(e)), 500


@app.post("/reset")
def reset():
    data = request.get_json(silent=True) or {}
    session = re.sub(r"[^a-zA-Z0-9_-]", "_", str(data.get("session") or "default"))[:64] or "default"
    workdir = WORKROOT / session
    if workdir.exists():
        import shutil
        shutil.rmtree(workdir, ignore_errors=True)
    workdir.mkdir(parents=True, exist_ok=True)
    return jsonify(ok=True, session=session)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
