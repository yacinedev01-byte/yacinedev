"""
YACINEDEV Agent — Shell + Python tools on Railway
- POST /shell          تنفيذ أوامر شل
- GET  /tools          قائمة أدوات بايثون
- POST /run            تشغيل أداة بايثون
- POST /register       تسجيل أداة بايثون جديدة
- POST /reset          تصفير مجلد الجلسة
- GET  /health         فحص الخدمة
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
from pathlib import Path
from typing import Any

from flask import Flask, request, jsonify

app = Flask(__name__)

API_KEY = os.environ.get("SHELL_API_KEY") or os.environ.get("PYTHON_API_KEY") or ""
WORKROOT = Path(os.environ.get("SHELL_WORKROOT", "/tmp/yd_sandbox"))
TOOLS_DIR = Path(os.environ.get("PYTHON_TOOLS_DIR", "/tmp/yd_python_tools"))
WORKROOT.mkdir(parents=True, exist_ok=True)
TOOLS_DIR.mkdir(parents=True, exist_ok=True)

MAX_TIMEOUT = 120
MAX_OUTPUT = 200_000
MAX_SOURCE = 250_000

BLOCKED_SHELL = [
    r"\brm\s+-rf\s+/(\s|$)",
    r"\bmkfs\b",
    r"\bdd\s+if=.*of=/dev",
    r":\(\)\s*\{\s*:\|:&\s*\}\s*;\s*:",
    r"\bshutdown\b|\breboot\b",
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
        features=["shell", "python_tools", "python_run", "python_register"],
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
        proc = subprocess.run(
            ["/bin/bash", "-lc", cmd],
            cwd=str(workdir),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return jsonify(
            ok=proc.returncode == 0,
            exit_code=proc.returncode,
            stdout=(proc.stdout or "")[-MAX_OUTPUT:],
            stderr=(proc.stderr or "")[-MAX_OUTPUT:],
            duration_s=round(time.time() - started, 2),
            workdir=str(workdir),
        )
    except subprocess.TimeoutExpired as e:
        return jsonify(
            ok=False,
            error=f"انتهت المهلة بعد {timeout} ثانية",
            stdout=((e.stdout or "") if isinstance(e.stdout, str) else "")[-MAX_OUTPUT:],
            stderr=((e.stderr or "") if isinstance(e.stderr, str) else "")[-MAX_OUTPUT:],
        ), 200
    except Exception as e:
        return jsonify(ok=False, error=str(e)), 500


@app.post("/reset")
def reset():
    data = request.get_json(silent=True) or {}
    session = re.sub(r"[^a-zA-Z0-9_-]", "_", str(data.get("session") or "default"))[:64] or "default"
    workdir = WORKROOT / session
    if workdir.exists():
        shutil.rmtree(workdir, ignore_errors=True)
    workdir.mkdir(parents=True, exist_ok=True)
    return jsonify(ok=True, session=session)


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
    # تنفيذ معزول نسبياً: لا builtins خطرة
    safe_builtins = {
        "abs": abs, "min": min, "max": max, "sum": sum, "len": len, "range": range,
        "enumerate": enumerate, "sorted": sorted, "list": list, "dict": dict, "set": set,
        "tuple": tuple, "str": str, "int": int, "float": float, "bool": bool,
        "print": print, "isinstance": isinstance, "type": type, "round": round,
        "zip": zip, "map": map, "filter": filter, "any": any, "all": all,
        "json": json, "re": re, "math": __import__("math"),
    }
    ns: dict[str, Any] = {"__builtins__": safe_builtins}
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

    started = time.time()
    try:
        if tool in BUILTIN_TOOLS:
            result = BUILTIN_TOOLS[tool]["fn"](args)
        else:
            fn = load_registered(tool)
            if fn is None:
                return jsonify(ok=False, error=f"أداة غير موجودة: {tool}"), 404
            result = fn(args)
        return jsonify(
            ok=True,
            tool=tool,
            result=result,
            duration_s=round(time.time() - started, 2),
        )
    except Exception as e:
        return jsonify(
            ok=False,
            tool=tool,
            error=str(e),
            trace=traceback.format_exc()[-2000:],
            duration_s=round(time.time() - started, 2),
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
