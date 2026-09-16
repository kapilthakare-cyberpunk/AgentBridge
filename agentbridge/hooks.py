"""File-based inbox between Claude hooks and the bridge.

Hook scripts file requests under ~/.agentbridge/pending/ and block waiting
for a verdict file under ~/.agentbridge/verdicts/. The bridge forwards
approval requests to the phone and writes back the verdict. Notify requests
are forwarded as phone notifications. Timeouts always fail safe (deny).
"""
from __future__ import annotations

import json
import os
import time
import uuid

BASE = os.path.expanduser("~/.agentbridge")
PENDING = os.path.join(BASE, "pending")
VERDICTS = os.path.join(BASE, "verdicts")


def _ensure() -> None:
    os.makedirs(PENDING, exist_ok=True)
    os.makedirs(VERDICTS, exist_ok=True)


def submit(kind: str, title: str, detail: str = "", session: str = "") -> str:
    """File a request. kind is 'approval' or 'notify'. Returns the request id."""
    _ensure()
    rid = uuid.uuid4().hex[:12]
    with open(os.path.join(PENDING, rid + ".json"), "w") as f:
        json.dump({"rid": rid, "kind": kind, "title": title, "detail": detail,
                   "session": session, "created": time.time()}, f)
    return rid


def pending_requests(max_age: float = 3600) -> list[dict]:
    """Unresolved requests, oldest first. Prunes stale files."""
    _ensure()
    now = time.time()
    out: list[dict] = []
    try:
        files = sorted(os.listdir(PENDING))
    except OSError:
        return out
    for fn in files:
        if not fn.endswith(".json"):
            continue
        path = os.path.join(PENDING, fn)
        try:
            with open(path) as f:
                req = json.load(f)
        except (OSError, json.JSONDecodeError):
            continue
        if now - req.get("created", 0) > max_age:
            try:
                os.remove(path)
            except OSError:
                pass
            continue
        if req.get("kind") == "approval" and os.path.exists(
                os.path.join(VERDICTS, req.get("rid", "") + ".json")):
            continue  # already resolved
        out.append(req)
    out.sort(key=lambda r: r.get("created", 0))
    return out


def write_verdict(rid: str, verdict: str) -> None:
    _ensure()
    with open(os.path.join(VERDICTS, rid + ".json"), "w") as f:
        json.dump({"rid": rid, "verdict": verdict, "at": time.time()}, f)


def read_verdict(rid: str) -> str | None:
    try:
        with open(os.path.join(VERDICTS, rid + ".json")) as f:
            return json.load(f).get("verdict")
    except (OSError, json.JSONDecodeError):
        return None


def wait_verdict(rid: str, timeout: float) -> str | None:
    """Block up to timeout seconds for a verdict. Returns None on timeout."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        verdict = read_verdict(rid)
        if verdict:
            return verdict
        time.sleep(1)
    return None
