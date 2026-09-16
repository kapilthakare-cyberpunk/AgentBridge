#!/usr/bin/env python3
"""AgentBridge command center: serve, token, hooks, status."""
from __future__ import annotations

import argparse
import asyncio
import os
import sys

from . import hooks
from .server import DEFAULT_PORT, load_token, serve


def cmd_token(regenerate: bool = False) -> int:
    from .server import TOKEN_FILE
    if regenerate:
        try:
            os.remove(TOKEN_FILE)
        except OSError:
            pass
    print(load_token())
    return 0


def cmd_status() -> int:
    from .server import TOKEN_FILE
    print(f"token file: {'present' if os.path.exists(TOKEN_FILE) else 'missing'}")
    print(f"pending requests: {len(hooks.pending_requests())}")
    settings = os.path.expanduser("~/.claude/settings.json")
    try:
        with open(settings) as f:
            installed = "agentbridge-hook" in f.read()
    except OSError:
        installed = False
    print(f"claude hooks: {'installed' if installed else 'not installed'}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="agentbridge")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("serve", help="run the bridge server")
    s.add_argument("--port", type=int, default=DEFAULT_PORT)
    t = sub.add_parser("token", help="print (or regenerate) the pairing token")
    t.add_argument("--regenerate", action="store_true")
    sub.add_parser("install-hooks", help="wire Claude Code hooks + notify")
    sub.add_parser("uninstall-hooks", help="remove Claude Code hooks")
    sub.add_parser("status", help="token/hooks/pending overview")
    args = ap.parse_args(argv)

    if args.cmd == "serve":
        try:
            asyncio.run(serve(args.port))
        except KeyboardInterrupt:
            pass
        return 0
    if args.cmd == "token":
        return cmd_token(args.regenerate)
    if args.cmd == "install-hooks":
        from .claude_hook import install_hooks
        return install_hooks()
    if args.cmd == "uninstall-hooks":
        from .claude_hook import uninstall_hooks
        return uninstall_hooks()
    return cmd_status()


if __name__ == "__main__":
    sys.exit(main())
