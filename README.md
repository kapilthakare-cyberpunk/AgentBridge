# AgentBridge

Phone-to-Mac AI agent bridge. Your Galaxy talks to your MacBook over your
own network: chat with terminal agents, get pinged when work finishes,
approve or deny Claude's Bash calls from your pocket.

No cloud, no account, stdlib-only Python on the Mac side.

## Layout

- `agentbridge/server.py` — asyncio WebSocket server, token auth, router
- `agentbridge/sessions.py` — managed pty sessions + read-only watchers
  (agent processes, Claude/Codex log tails, custom file watches)
- `agentbridge/hooks.py` — file inbox between Claude hooks and the bridge
- `agentbridge/claude_hook.py` — Claude Code hook entrypoint + installer
- `agentbridge/main.py` — `agentbridge` command center

The Android app lives in `~/Projects/AgentPhone`.

## Quickstart (Mac)

```bash
cd ~/Projects/AgentBridge
python3 -m agentbridge.main serve        # prints ws://LAN-IP:9876 + token
python3 -m agentbridge.main install-hooks  # Claude notify + Bash approvals
```

On the phone enter the LAN IP (or Tailscale IP anywhere) and the token.
Keep the app's persistent connection on; it reconnects by itself.

## What v1 covers

- **Managed sessions**: the phone spawns any terminal command
  (`claude`, `codex`, …) in a pty — full chat, kill, exit + idle detection.
- **Claude Code hooks**: `Stop`/`Notification` → phone pings ("done?"),
  `PreToolUse` on Bash → phone Approve/Deny, default-deny on 120s timeout.
- **Watchers (read-only, honest label in UI)**: running agent CLIs, Claude
  and Codex transcript tails, any log file you point it at. IDE-internal
  agents can't be remote-driven; watch them here instead.

## Protocol (JSON text frames, v1)

Phone → bridge: `hello{token}`, `list`, `spawn{command,cwd}`, `input{id,text}`,
`kill{id}`, `watch{path}`, `unwatch{id}`, `approval-verdict{rid,verdict}`, `ping`.
Bridge → phone: `hello-ok`, `session-list`, `spawned`, `output{id,chunk}`,
`session-ended{id,code}`, `session-idle{id,idleSec}`, `watcher-output`,
`approval-request{rid,…}`, `approval-resolved`, `notify{kind,title,body}`,
`error`, `pong`.

## Security notes

- Token auth on every connection; token file is 0600. Wrong token: dropped.
- Binds LAN (`0.0.0.0:9876`) — reachable by anyone on your Wi-Fi with the
  token. For anywhere-access use Tailscale and bind accordingly.
- Hook approvals default-deny on timeout or when the phone is offline.
- `uninstall-hooks`: `python3 -m agentbridge.main uninstall-hooks`.
