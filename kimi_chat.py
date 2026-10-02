#!/usr/bin/env python3
"""kimi_chat.py - محادثة Kimi بتوكنات مدمجة وتجديد تلقائي."""
import argparse
import base64
import json
import logging
import stat
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ACCESS = "eyJhbGciOiJIUzUxMiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJ1c2VyLWNlbnRlciIsImV4cCI6MTc5MzU1NTg1NywiaWF0IjoxNzkwOTYzODU3LCJqdGkiOiJkYXZ2MTRmYTBoZnZyc2psaXVxMCIsInR5cCI6ImFjY2VzcyIsImFwcF9pZCI6ImtpbWkiLCJzdWIiOiJkODJiazQ4aDhuanZxczhzYXZqMCIsInNwYWNlX2lkIjoiZDgyYms0MGg4bmp2cXM4c2FuOWciLCJhYnN0cmFjdF91c2VyX2lkIjoiZDgyYms0MGg4bmp2cXM4c2FuOTAiLCJzc2lkIjoiMTczMTcyNjQxNTcwNzkwNDk1NSIsInJlZ2lvbiI6Im92ZXJzZWFzIn0.ahqWpLmvHF49oEjKTT0WoqaNWswKtyme20aQ9XZTzH7SMXyeAp49E3sfB-kVeuXxaKLkE2pxnJ8hD2JMwEdAhA"
REFRESH = "eyJhbGciOiJIUzUxMiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJ1c2VyLWNlbnRlciIsImV4cCI6MTc5ODczOTg1NywiaWF0IjoxNzkwOTYzODU3LCJqdGkiOiJkYXZ2MTRmYTBoZnZyc2psaXVxZyIsInR5cCI6InJlZnJlc2giLCJhcHBfaWQiOiJraW1pIiwic3ViIjoiZDgyYms0OGg4bmp2cXM4c2F2ajAiLCJzcGFjZV9pZCI6ImQ4MmJrNDBoOG5qdnFzOHNhbjlnIiwiYWJzdHJhY3RfdXNlcl9pZCI6ImQ4MmJrNDBoOG5qdnFzOHNhbjkwIiwic3NpZCI6IjE3MzE3MjY0MTU3MDc5MDQ5NTUiLCJyZWdpb24iOiJvdmVyc2VhcyJ9.wAOc0yv9MKy6sLSh99BOGskwen4w6UTlm3naifl4DG2s0ZXK9aAYp6oZ5glweWbAPoDxWxd0PhjGZgOGmJthMw"

BASE = "https://kimi.moonshot.cn"
STATE = Path(__file__).resolve().with_name("kimi_state.json")
log = logging.getLogger("kimi")


class KimiError(RuntimeError):
    pass


class AuthError(KimiError):
    pass


def jwt_exp(tok: str) -> int:
    try:
        p = tok.split(".")[1]
        p += "=" * (-len(p) % 4)
        return int(json.loads(base64.urlsafe_b64decode(p))["exp"])
    except Exception:
        return 0


def fmt_exp(ts: int) -> str:
    if not ts:
        return "غير معروف"
    left = ts - int(time.time())
    when = time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))
    return f"{when} (منتهي)" if left <= 0 else f"{when} (باقي {left // 86400} يوم)"


def load_state() -> dict:
    if STATE.exists():
        try:
            d = json.loads(STATE.read_text("utf-8"))
            if d.get("access") and d.get("refresh"):
                return d
        except (OSError, json.JSONDecodeError) as e:
            log.warning("تعذرت قراءة ملف الحالة: %s", e)
    return {"access": ACCESS, "refresh": REFRESH}


def save_state(st: dict) -> None:
    try:
        STATE.write_text(json.dumps(st), "utf-8")
        STATE.chmod(stat.S_IRUSR | stat.S_IWUSR)
    except OSError as e:
        log.warning("تعذر حفظ الحالة: %s", e)


def request(method, path, token, body=None, timeout=60, stream=False):
    headers = {
        "Authorization": "Bearer " + token,
        "Content-Type": "application/json",
        "User-Agent": "Mozilla/5.0",
        "Origin": BASE,
        "Referer": BASE + "/",
        "Accept-Language": "ar,en;q=0.8",
    }
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, headers=headers, method=method)
    log.debug("%s %s", method, path)
    try:
        resp = urllib.request.urlopen(req, timeout=timeout)
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:300]
        if e.code == 401:
            raise AuthError(f"401 على {path}: {detail}") from e
        raise KimiError(f"HTTP {e.code} على {path}: {detail}") from e
    except urllib.error.URLError as e:
        raise KimiError(f"تعذر الاتصال: {e.reason}") from e
    except TimeoutError as e:
        raise KimiError("انتهت مهلة الاتصال") from e
    if stream:
        return resp
    with resp:
        raw = resp.read().decode("utf-8", "replace")
    if not raw.strip():
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        raise KimiError(f"الرد ليس JSON: {raw[:200]!r}") from e


class Kimi:
    def __init__(self):
        self.st = load_state()
        self.chat_id = None

    def refresh(self) -> None:
        d = request("GET", "/api/auth/token/refresh", self.st["refresh"], timeout=20)
        if not d.get("access_token"):
            raise KimiError(f"رد التجديد بلا access_token: {str(d)[:200]}")
        self.st = {
            "access": d["access_token"],
            "refresh": d.get("refresh_token") or self.st["refresh"],
        }
        save_state(self.st)
        log.info("تم التجديد، ينتهي: %s", fmt_exp(jwt_exp(self.st["access"])))

    def token(self) -> str:
        if jwt_exp(self.st["access"]) - time.time() < 300:
            log.info("الـ access قارب الانتهاء، جاري التجديد")
            self.refresh()
        return self.st["access"]

    def call(self, method, path, body=None, stream=False, timeout=60):
        try:
            return request(method, path, self.token(), body, timeout, stream)
        except AuthError:
            log.info("401، تجديد ثم إعادة المحاولة")
            self.refresh()
            return request(method, path, self.st["access"], body, timeout, stream)

    def check(self) -> None:
        self.call("GET", "/api/user", timeout=20)

    def new_chat(self) -> None:
        r = self.call("POST", "/api/chat", {
            "name": "terminal", "is_example": False, "kimiplus_id": "kimi",
            "born_from": "home", "source": "web", "tags": [],
        })
        self.chat_id = r.get("id")
        if not self.chat_id:
            raise KimiError(f"لم يُنشأ chat_id: {str(r)[:200]}")

    def ask(self, text: str, print_stream: bool = True) -> str:
        if not self.chat_id:
            self.new_chat()
        resp = self.call("POST", f"/api/chat/{self.chat_id}/completion/stream", {
            "kimiplus_id": "kimi", "model": "kimi", "use_search": False,
            "messages": [{"role": "user", "content": text}],
            "refs": [], "history": [], "scene_labels": [],
            "use_semantic_memory": False, "use_deep_research": False,
            "extend": {"sidebar": True},
        }, stream=True, timeout=120)
        parts = []
        with resp:
            for raw in resp:
                line = raw.decode("utf-8", "replace").strip()
                if not line.startswith("data:"):
                    continue
                try:
                    ev = json.loads(line[5:].strip())
                except json.JSONDecodeError:
                    continue
                kind = ev.get("event")
                if kind == "cmpl":
                    t = ev.get("text", "")
                    parts.append(t)
                    if print_stream:
                        sys.stdout.write(t)
                        sys.stdout.flush()
                elif kind == "error":
                    raise KimiError(f"خطأ من الخادم: {str(ev)[:200]}")
                elif kind == "all_done":
                    break
        if print_stream:
            print()
        reply = "".join(parts)
        if not reply:
            raise KimiError("الرد فارغ")
        return reply

    def cleanup(self) -> None:
        if self.chat_id:
            try:
                self.call("DELETE", f"/api/chat/{self.chat_id}", timeout=15)
            except KimiError as e:
                log.warning("تعذر حذف المحادثة: %s", e)


def main() -> int:
    ap = argparse.ArgumentParser(description="محادثة Kimi بالتوكن المدمج")
    ap.add_argument("message", nargs="*", help="الرسالة (بدونها يبدأ وضع المحادثة)")
    ap.add_argument("--check", action="store_true", help="فحص التوكن فقط")
    ap.add_argument("--refresh", action="store_true", help="تجديد التوكن الآن")
    ap.add_argument("--keep", action="store_true", help="لا تحذف المحادثة بعد الانتهاء")
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args()
    logging.basicConfig(
        level=logging.DEBUG if a.verbose else logging.WARNING,
        format="%(levelname)s: %(message)s",
    )

    k = Kimi()
    try:
        if a.refresh:
            k.refresh()
            print(f"✅ تم التجديد، ينتهي: {fmt_exp(jwt_exp(k.st['access']))}")
            return 0
        if a.check:
            k.check()
            print(f"✅ التوكن يعمل، ينتهي: {fmt_exp(jwt_exp(k.st['access']))}")
            return 0
        if a.message:
            k.ask(" ".join(a.message))
            return 0
        print("وضع المحادثة (اكتب exit للخروج)")
        while True:
            try:
                q = input("\nأنت: ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                break
            if q.lower() in ("exit", "quit", "خروج"):
                break
            if q:
                print("Kimi: ", end="")
                k.ask(q)
        return 0
    except KimiError as e:
        print(f"❌ فشل: {e}")
        return 1
    finally:
        if not a.keep:
            k.cleanup()


if __name__ == "__main__":
    sys.exit(main())
