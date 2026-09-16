#!/usr/bin/env python3
"""Claude Code hook entrypoint + installer for AgentBridge.

Usage (called by Claude Code, hook JSON on stdin):
    agentbridge-hook notify [--done]        file a phone notification, exit 0
    agentbridge-hook approve [--timeout S]  ask the phone, exit 0/2

Setup:
    python3 -m agentbridge.main install-hooks
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time

SETTINGS = os.path.expanduser("~/.claude/settings.json")
BIN_LINK = os.path.expanduser("~/bin/agentbridge-hook")
APPROVE_CMD = "~/bin/agentbridge-hook approve --timeout 120"
NOTIFY_CMD = "~/bin/agentbridge-hook notify"


def read_stdin_json() -> dict:
    try:
        raw = sys.stdin.read()
        return json.loads(raw) if raw.strip() else {}
    except (OSError, json.JSONDecodeError):
        return {}


def transcript_tail(path: str, limit: int = 3) -> list[str]:
    try:
        from .sessions import SessionManager
    except ImportError:
        return []
    try:
        with open(path, "rb") as f:
            f.seek(max(0, os.path.getsize(path) - 8192))
            raw = f.read().decode("utf-8", errors="replace")
    except OSError:
        return []
    out = []
    for line in raw.splitlines():
        text = SessionManager._human_line(line)
        if text:
            out.append(text)
    return out[-limit:]


def cmd_notify(done: bool = False) -> int:
    from . import hooks
    data = read_stdin_json()
    session = str(data.get("session_id", ""))[:8]
    message = str(data.get("message", "") or "")
    tail = transcript_tail(str(data.get("transcript_path", "") or ""))
    title = "Claude finished?" if done else "Claude needs you"
    detail = message or "\n".join(tail[-2:]) or "Idle prompt"
    if session:
        detail = f"[{session}] {detail}"
    hooks.submit("notify", title, detail[:800], session)
    return 0


def cmd_approve(timeout: float = 120) -> int:
    from . import hooks
    data = read_stdin_json()
    tool = str(data.get("tool_name", "tool"))
    tool_input = data.get("tool_input", {})
    session = str(data.get("session_id", ""))[:8]
    try:
        detail = json.dumps(tool_input, indent=1)[:1500]
    except (TypeError, ValueError):
        detail = str(tool_input)[:1500]
    cwd = str(data.get("cwd", "") or "")
    if cwd:
        detail = f"cwd: {cwd}\n{detail}"
    rid = hooks.submit("approval", f"Claude wants: {tool}", detail, session)
    verdict = hooks.wait_verdict(rid, timeout)
    if verdict == "approve":
        return 0
    sys.stderr.write(
        "Phone verdict: %s. Do NOT run this; explain and continue another way.\n"
        % ("denied" if verdict == "deny" else "no answer in time (default deny)"))
    return 2


def ensure_link() -> None:
    os.makedirs(os.path.dirname(BIN_LINK), exist_ok=True)
    src = os.path.abspath(__file__)
    try:
        if os.path.islink(BIN_LINK) or os.path.exists(BIN_LINK):
            os.remove(BIN_LINK)
        os.symlink(src, BIN_LINK)
        os.chmod(src, 0o755)
    except OSError as exc:
        print(f"cannot link {BIN_LINK}: {exc}", file=sys.stderr)


def load_settings() -> dict:
    try:
        with open(SETTINGS) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def install_hooks() -> int:
    ensure_link()
    if os.path.exists(SETTINGS):
        bak = SETTINGS + f".bak.{int(time.time())}"
        shutil.copy2(SETTINGS, bak)
        print(f"backed up settings to {bak}")
    cfg = load_settings()
    hooks_cfg = cfg.setdefault("hooks", {})

    def entry(cmd: str) -> dict:
        return {"matcher": "", "hooks": [{"type": "command", "command": cmd}]}

    def add(event: str, cmd: str, matcher: str = "") -> None:
        lst = hooks_cfg.setdefault(event, [])
        for e in lst:
            for h in e.get("hooks", []):
                if h.get("command", "").startswith("~/bin/agentbridge-hook"):
                    return  # already installed
        item = entry(cmd)
        item["matcher"] = matcher
        lst.append(item)

    add("Notification", NOTIFY_CMD)
    add("Stop", NOTIFY_CMD + " --done")
    add("PreToolUse", APPROVE_CMD, matcher="Bash")
    os.makedirs(os.path.dirname(SETTINGS), exist_ok=True)
    with open(SETTINGS, "w") as f:
        json.dump(cfg, f, indent=2)
    print("installed: Notification + Stop (phone pings), PreToolUse/Bash (phone approvals)")
    return 0


def uninstall_hooks() -> int:
    cfg = load_settings()
    hooks_cfg = cfg.get("hooks", {})
    for event in list(hooks_cfg):
        kept = [e for e in hooks_cfg[event]
                if not any(h.get("command", "").startswith("~/bin/agentbridge-hook")
                           for h in e.get("hooks", []))]
        if kept:
            hooks_cfg[event] = kept
        else:
            del hooks_cfg[event]
    with open(SETTINGS, "w") as f:
        json.dump(cfg, f, indent=2)
    print("removed AgentBridge hooks (backup file kept if one was made)")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="agentbridge-hook")
    sub = ap.add_subparsers(dest="cmd", required=True)
    n = sub.add_parser("notify")
    n.add_argument("--done", action="store_true")
    a = sub.add_parser("approve")
    a.add_argument("--timeout", type=float, default=120)
    args = ap.parse_args(argv)
    if args.cmd == "notify":
        return cmd_notify(done=args.done)
    return cmd_approve(timeout=args.timeout)


if __name__ == "__main__":
    sys.exit(main())
