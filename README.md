# Cursor Phone Bridge

Mirror your local Cursor IDE Agent Chat to a Telegram bot. Approve tool calls and send follow-up prompts from your phone. No port forwarding, no public IP, no cloud account beyond Telegram.

```
Phone (Telegram)
   |
   |  long-poll over WAN
   v
Telegram servers
   |
   |  long-poll
   v
Bridge daemon (127.0.0.1:8765)  ----->  Cursor IDE
   ^                                       |
   |                                       |  invokes hook scripts via stdio
   |                                       v
   +---- HTTP from hook scripts -----  Hook runner (Python)
```

## What you get

- Every prompt you type into Cursor is mirrored to your phone.
- Every agent response (and optionally thinking) is mirrored to your phone.
- Tool approvals (shell commands, MCP, sensitive file reads) are sent to your phone with **Approve / Deny / Always allow exact** buttons.
- Anything you type to the bot from your phone is queued; on the agent's next turn end (`stop` hook), it gets injected as the next user prompt — Cursor auto-runs it.
- An allowlist (regex over command strings) so trusted commands never bother you.
- **Plan-mode tracking**: when Cursor's agent is working a plan, the bridge watches `~/.cursor/plans/*.plan.md` and pings Telegram every time a step transitions (e.g. `START: build api`, `DONE: add tests`). On phone use `/plan` for the live state of all tracked plans, `/plan <id-prefix>` for a full step-list of one.
- **Auto-wake when Cursor is idle** (Windows, **opt-in**, off by default). Cursor's hooks API only consumes the followup queue when the agent's `stop` hook fires — and `stop` only fires when there's an active turn. If you send a Telegram message while the agent is idle (no turn running), the bridge can focus the Cursor window and paste the message in directly via UI automation (`bridge/wake_cursor.ps1`). Enable with `auto_wake_cursor = true` in `config.toml`. **Caveat:** the script presses `Ctrl+L` to focus the chat input then pastes + presses Enter. If you have a draft already in the chat box, your Telegram message will be appended to it. The script no longer sends `Ctrl+A`+`Del` (an earlier version did, which could overwrite the active editor pane if `Ctrl+L` failed to move focus). Test with `pwsh -File bridge/wake_cursor.ps1 -DryRun -Message ignored` before enabling — it'll just focus Cursor and report which window class ended up focused.

## What it can't do

- Open a brand new Cursor chat from cold from your phone. You need an existing/idle Agent Chat session for follow-ups to attach to. (V2 plan: spawn `cursor-agent` CLI for cold-start.)
- Per-hunk diff approval. The hook payload doesn't include diff details.

## Requirements

- Windows 10/11 (the installer is PowerShell; the Python code is cross-platform)
- Python 3.11+ on PATH
- Cursor 1.7+ (for hooks support)
- A Telegram account

## Setup

### 1. Make the bot

1. Open Telegram, message `@BotFather`, send `/newbot`, follow prompts. Save the **bot token**.
2. Message `@userinfobot` to learn your numeric Telegram **user ID**.
3. Open a chat with your new bot and send it any message (otherwise it can't DM you first).

### 2. Install

From this repo root, in PowerShell:

```powershell
.\install.ps1
```

First run will:
- `pip install -r requirements.txt`
- Copy `bridge/config.toml.example` -> `bridge/config.toml`
- Print a generated random secret

Edit `bridge/config.toml`:

```toml
[telegram]
bot_token = "123456:ABC..."          # from BotFather
allowed_user_ids = [123456789]       # your Telegram numeric ID

[bridge]
secret = "the-generated-secret-from-installer"
```

Then re-run the installer:

```powershell
.\install.ps1
```

This second run will:
- Write `%USERPROFILE%\.cursor\hooks.json` (backing up any existing one)
- Write `%USERPROFILE%\.cursor\cursor-phone-bridge.json` (so hook scripts can find the bridge)
- Optionally register a Scheduled Task that starts the bridge at logon

### 3. Restart Cursor

Cursor watches `hooks.json` but a restart is the safest way to make sure all hooks load.

### 4. Verify

In any Cursor project, open Agent Chat and send a message. You should:
- See `[project] session started` in Telegram.
- See `[project] you typed: ...` mirrored.
- See the agent's response mirrored.
- Get an `Approve` / `Deny` button when the agent tries to run a shell command (unless the command matches an allowlist pattern).

If something's wrong, check the Hooks tab in Cursor Settings (it shows hook errors).

## Telegram commands

- `/help` - list commands
- `/status` - show conversations and queue depth
- `/now` - what is the agent doing right now: idle/busy plus the last ~12 events (your prompts, agent thoughts, tool calls, responses, turn ends) for the active conversation. The buffer is in-memory and resets when the bridge restarts.
- `/use <prefix>` - route follow-ups to a specific conversation (no arg = most recent)
- `/allow <regex>` - auto-allow matching shell commands (full-match)
- `/deny <regex>` - auto-deny matching shell commands
- `/patterns` - list custom patterns
- `/unpattern <regex>` - remove a pattern
- `/stop` - deny all currently-pending approvals

Plain text messages are queued as the next user prompt for the active conversation.

## Default safe commands

The bridge ships with a small allowlist in `config.toml`: `ls`, `pwd`, `cat ...`, `git status`, `git diff ...`, `git log ...`. Add more with `/allow` from your phone or by editing `config.toml`.

## Files

| Path | Purpose |
|---|---|
| `bridge/bridge.py` | Daemon: HTTP server (loopback) + Telegram bot (long-poll) |
| `bridge/state.py` | SQLite wrapper |
| `bridge/config.toml` | Your config (gitignored) |
| `hooks/runner.py` | Single Python dispatcher invoked by Cursor for every hook event |
| `hooks/hooks.json.template` | Template; installer renders it into `~/.cursor/hooks.json` with absolute paths |
| `install.ps1` | Idempotent Windows installer |

## Security notes

- The HTTP server binds `127.0.0.1` only and requires a `Bearer <secret>` header on every request. Other local processes can't impersonate hooks (unless they can read your `~/.cursor/cursor-phone-bridge.json`, which is yours-only by NTFS perms by default).
- Telegram messages are dropped unless `from.id` is in `telegram.allowed_user_ids`.
- `beforeShellExecution` and `beforeMCPExecution` hooks are configured `failClosed: true` — if the bridge daemon is down, Cursor blocks the tool call instead of fail-opening.
- `beforeReadFile` only escalates to a phone prompt for sensitive paths (`.env`, ssh keys, `.pem`, `.npmrc`, etc.); see `SENSITIVE_FILE_PATTERNS` in `hooks/runner.py`.

## Troubleshooting

**"Phone never gets messages."**
Check that the bridge is running: visit `http://127.0.0.1:8765/health` in a browser, expect `{"ok":true}`. Check that you've messaged the bot first (Telegram bots can't DM users who haven't initiated).

**"Approval prompts don't appear."**
Open Cursor Settings -> Hooks tab. Confirm hooks are loaded. Check the Hooks output channel for errors. Confirm `~/.cursor/cursor-phone-bridge.json` exists with `url` and `secret`.

**"The agent gets blocked even when I approve."**
Check that the hook timeout in `~/.cursor/hooks.json` (`timeout: 600`) is greater than `bridge.approval_timeout` in `config.toml`. The runner uses 600s; the bridge defaults to 300s. Don't make them equal.

**"I tap Approve on Telegram but Cursor still asks me to approve on PC."**
This is a Cursor 2.6.x bug, not the bridge. Cursor currently ignores `permission: "allow"` returned from `beforeShellExecution` / `beforeMCPExecution` hooks ([forum thread](https://forum.cursor.com/t/beforeshellexecution-hook-permissions-allow-ask-ignored-allow-list-takes-precedence/144244), [related](https://forum.cursor.com/t/hooks-return-allow-but-mcp-tool-still-requires-manual-approval-gets-skipped/155434)). Only `deny` is honored. So:
- **Phone Allow tap = no-op** (Cursor still asks you on the PC).
- **Phone Deny tap = real remote abort** (always works).
- **Workaround for true remote work:** Enable Cursor's *Auto-Run in Agent Mode* (Settings → Features → Chat → "Allow auto-run"). Then Cursor never asks on PC; the phone gets a notification per command and you can hit **Deny** within the hook timeout (600s by default) to abort. Treat the phone as a remote kill switch + audit log instead of a remote approval gate.

**"Every shell command in Cursor is denied with 'returned no output'."**
Your `~/.cursor/hooks.json` is pointing at `pythonw.exe`. That binary detaches stdout, so Cursor reads nothing back and the `failClosed: true` rule blocks the tool. Edit `hooks.json` and replace every `pythonw.exe` with `python.exe`. The installer (newer versions) does this automatically.

**"Hook runner sees `Expecting value: line 1 column 1 (char 0)` parse errors in `~/.cursor/cursor-phone-bridge.log`."**
Cursor (at least v2.6.x on Windows) pipes the JSON payload to the hook's stdin with a UTF-8 BOM (`EF BB BF`). The runner already handles this via `sys.stdin.buffer.read()` + `decode("utf-8-sig")`. If you're seeing the error on an older runner, pull the latest `hooks/runner.py`.

**"How do I see what hooks Cursor is actually invoking?"**
Tail `~/.cursor/cursor-phone-bridge.log`. Each invocation writes `runner invoked argv=[...]` plus the parsed payload keys. If nothing appears when you send a chat message, hooks aren't wired up — re-check `~/.cursor/hooks.json` and restart Cursor.

**"How do I uninstall?"**
- `Unregister-ScheduledTask -TaskName CursorPhoneBridge -Confirm:$false`
- Delete `%USERPROFILE%\.cursor\hooks.json` (or restore the `.bak` the installer saved)
- Delete `%USERPROFILE%\.cursor\cursor-phone-bridge.json`

## Roadmap

- `cursor-agent` CLI fallback for cold-start chats from phone (no Cursor window required)
- One Telegram topic per project for cleaner threading
- Diff snippet preview in approval messages
- Streaming response text via message edits instead of one big chunk per turn
