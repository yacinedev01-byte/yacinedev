#!/usr/bin/env python3
"""Read-only Railway health/auth smoke test."""
import os, sys, urllib.request, urllib.error
base = (sys.argv[1] if len(sys.argv) > 1 else os.environ.get("AGENT_URL", "http://localhost:8080")).rstrip("/")
key = os.environ.get("SHELL_API_KEY", "")
for path in ("/health", "/cmd/commands"):
    req = urllib.request.Request(base + path, headers={"X-Api-Key": key})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            print(path, r.status, r.read(500).decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        print(path, e.code, e.read(500).decode("utf-8", "replace")); raise SystemExit(1)
