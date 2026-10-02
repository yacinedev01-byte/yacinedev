"""
YACINEDEV sandbox -- عزل على مستوى العملية (rlimits + venv مؤقتة) مع تثبيت مكتبات حرة.
ملاحظة صادقة: هذا عزل عملية (process isolation) ضمن نفس حاوية Railway، وليس حاويات
Docker منفصلة لكل تنفيذ -- Railway لا يدعم Docker-in-Docker بدون صلاحيات مميزة.
لعزل متعدد المستأجرين بمستوى Docker حقيقي استخدم خدمة متخصصة مثل E2B أو Modal Sandboxes
واستبدل _run() هنا باستدعاء واجهتها.
"""
from __future__ import annotations
import json
import os
import re
import resource
import shutil
import signal
import subprocess
import time
import uuid
from pathlib import Path
from flask import Blueprint, jsonify, request

bp = Blueprint("sandbox", __name__)

SANDBOX_ROOT = Path(os.environ.get("SANDBOX_ROOT", "/tmp/yd_sandboxes"))
SANDBOX_ROOT.mkdir(parents=True, exist_ok=True)

MAX_TIMEOUT = 180
MAX_OUTPUT = 200_000
ALLOWED_PACKAGE_RE = re.compile(r"^[a-zA-Z0-9_\-.]+(==[a-zA-Z0-9_\-.]+)?$")


def _child_limits(timeout: int, mem_mb: int = 512) -> None:
    try:
        resource.setrlimit(resource.RLIMIT_CPU, (timeout + 5, timeout + 5))
        resource.setrlimit(resource.RLIMIT_AS, (mem_mb * 1024 * 1024, mem_mb * 1024 * 1024))
        resource.setrlimit(resource.RLIMIT_FSIZE, (100 * 1024 * 1024, 100 * 1024 * 1024))
        resource.setrlimit(resource.RLIMIT_NOFILE, (256, 256))
        resource.setrlimit(resource.RLIMIT_NPROC, (128, 128))
    except (OSError, ValueError):
        pass


def _kill_group(proc: subprocess.Popen) -> None:
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (OSError, ProcessLookupError):
        try:
            proc.kill()
        except OSError:
            pass


def _run(cmd: list[str], cwd: Path, timeout: int, mem_mb: int = 512) -> dict:
    started = time.time()
    proc = subprocess.Popen(
        cmd, cwd=str(cwd), env=os.environ.copy(),
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        start_new_session=True, preexec_fn=lambda: _child_limits(timeout, mem_mb),
    )
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
        timed_out = False
    except subprocess.TimeoutExpired:
        _kill_group(proc)
        stdout, stderr = proc.communicate()
        timed_out = True
    return {
        "ok": proc.returncode == 0 and not timed_out,
        "exit_code": proc.returncode,
        "stdout": (stdout or "")[-MAX_OUTPUT:],
        "stderr": (stderr or "")[-MAX_OUTPUT:],
        "timed_out": timed_out,
        "duration_s": round(time.time() - started, 2),
    }


def _box_path(box_id: str) -> Path:
    box_id = re.sub(r"[^a-zA-Z0-9_\-]", "", box_id)[:64] or "default"
    return (SANDBOX_ROOT / box_id).resolve()


@bp.post("/sandbox/create")
def sandbox_create():
    data = request.get_json(silent=True) or {}
    runtime = str(data.get("runtime") or "python").lower()
    box_id = str(data.get("id") or f"box_{uuid.uuid4().hex[:10]}")
    box = _box_path(box_id)
    box.mkdir(parents=True, exist_ok=True)

    if runtime == "python":
        res = _run(["python3", "-m", "venv", str(box / "venv")], cwd=box, timeout=60)
    elif runtime == "node":
        (box / "package.json").write_text('{"name":"yd-sandbox","version":"1.0.0"}', encoding="utf-8")
        res = {"ok": True, "duration_s": 0, "stdout": "node workspace ready", "stderr": ""}
    else:
        return jsonify(ok=False, error="runtime يجب أن يكون python أو node"), 400
    return jsonify(ok=res["ok"], id=box_id, runtime=runtime, detail=res)


@bp.post("/sandbox/install")
def sandbox_install():
    data = request.get_json(silent=True) or {}
    box_id = str(data.get("id") or "")
    runtime = str(data.get("runtime") or "python").lower()
    packages = data.get("packages") or []
    if not box_id or not isinstance(packages, list) or not packages:
        return jsonify(ok=False, error="id و packages (قائمة) مطلوبان"), 400
    for p in packages:
        if not ALLOWED_PACKAGE_RE.match(str(p)):
            return jsonify(ok=False, error=f"اسم مكتبة غير صالح: {p}"), 400

    box = _box_path(box_id)
    if not box.is_dir():
        return jsonify(ok=False, error="بيئة غير موجودة -- استدعِ /sandbox/create أولاً"), 404
    timeout = max(10, min(int(data.get("timeout") or 120), MAX_TIMEOUT))

    if runtime == "python":
        pip = box / "venv" / "bin" / "pip"
        if not pip.is_file():
            return jsonify(ok=False, error="venv غير موجود في هذه البيئة"), 400
        res = _run([str(pip), "install", "--no-input", "--disable-pip-version-check", *[str(p) for p in packages]],
                    cwd=box, timeout=timeout)
    elif runtime == "node":
        npm = shutil.which("npm")
        if not npm:
            return jsonify(ok=False, error="npm غير متوفر على الحاوية"), 500
        res = _run([npm, "install", "--no-audit", "--no-fund", *[str(p) for p in packages]], cwd=box, timeout=timeout)
    else:
        return jsonify(ok=False, error="runtime غير مدعوم"), 400
    return jsonify(ok=res["ok"], id=box_id, installed=packages, detail=res)


@bp.post("/sandbox/run")
def sandbox_run():
    data = request.get_json(silent=True) or {}
    box_id = str(data.get("id") or "")
    runtime = str(data.get("runtime") or "python").lower()
    source = str(data.get("source") or "")
    if not box_id or not source:
        return jsonify(ok=False, error="id و source مطلوبان"), 400
    if len(source) > 250_000:
        return jsonify(ok=False, error="الكود طويل جداً"), 400

    box = _box_path(box_id)
    if not box.is_dir():
        return jsonify(ok=False, error="بيئة غير موجودة"), 404
    timeout = max(1, min(int(data.get("timeout") or 30), MAX_TIMEOUT))

    if runtime == "python":
        py = box / "venv" / "bin" / "python"
        if not py.is_file():
            py = Path("/usr/local/bin/python3")
        src_path = box / "__run.py"
        src_path.write_text(source, encoding="utf-8")
        res = _run([str(py), str(src_path)], cwd=box, timeout=timeout)
        src_path.unlink(missing_ok=True)
    elif runtime == "node":
        node = shutil.which("node")
        if not node:
            return jsonify(ok=False, error="node غير متوفر على الحاوية"), 500
        src_path = box / "__run.js"
        src_path.write_text(source, encoding="utf-8")
        res = _run([node, str(src_path)], cwd=box, timeout=timeout)
        src_path.unlink(missing_ok=True)
    else:
        return jsonify(ok=False, error="runtime غير مدعوم"), 400

    res["traceback"] = res["stderr"] if ("Traceback" in res["stderr"] or "Error" in res["stderr"]) else ""
    return jsonify(**res, id=box_id)


@bp.post("/sandbox/teardown")
def sandbox_teardown():
    data = request.get_json(silent=True) or {}
    box_id = str(data.get("id") or "")
    if not box_id:
        return jsonify(ok=False, error="id مطلوب"), 400
    box = _box_path(box_id)
    if box.is_dir():
        shutil.rmtree(box, ignore_errors=True)
    return jsonify(ok=True, deleted=box_id)


def test_tool_source(source: str, sample_args: dict) -> dict:
    """اختبار أداة مسجّلة بعزل تام قبل اعتمادها نهائياً -- يُستدعى من /register في app.py."""
    if "def run(" not in source:
        return {"sandbox_ok": False, "error": "يجب أن يحتوي الكود على def run(args):"}

    box_id = f"test_{uuid.uuid4().hex[:10]}"
    box = _box_path(box_id)
    box.mkdir(parents=True, exist_ok=True)
    harness = (
        "import json, traceback\n"
        f"ARGS = {json.dumps(sample_args or {}, ensure_ascii=False)}\n"
        "ns = {}\n"
        f"exec(compile({source!r}, '<tool_under_test>', 'exec'), ns, ns)\n"
        "try:\n"
        "    result = ns['run'](ARGS)\n"
        "    print(json.dumps({'ok': True, 'result': result}, ensure_ascii=False, default=str))\n"
        "except Exception:\n"
        "    print(json.dumps({'ok': False, 'error': traceback.format_exc()}, ensure_ascii=False))\n"
    )
    harness_path = box / "__harness.py"
    harness_path.write_text(harness, encoding="utf-8")
    res = _run(["python3", "-I", str(harness_path)], cwd=box, timeout=15, mem_mb=256)
    shutil.rmtree(box, ignore_errors=True)

    try:
        parsed = json.loads((res.get("stdout") or "").strip().splitlines()[-1])
    except Exception:
        parsed = {"ok": False, "error": "تعذر تفسير خرج الاختبار", "raw_stderr": res.get("stderr", "")[-500:]}
    return {"sandbox_ok": res["ok"] and parsed.get("ok", False), "tool_result": parsed, "raw": res}


@bp.post("/sandbox/test_tool")
def sandbox_test_tool_route():
    data = request.get_json(silent=True) or {}
    source = str(data.get("source") or "")
    sample_args = data.get("sample_args") if isinstance(data.get("sample_args"), dict) else {}
    return jsonify(**test_tool_source(source, sample_args))
