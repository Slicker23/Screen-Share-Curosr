"""Cursor hook dispatcher.

Reads JSON from stdin, dispatches to the bridge HTTP server, prints JSON to stdout.
Cursor invokes us once per hook event: `python runner.py <event_name>`.

Hook docs: https://cursor.com/docs/hooks

Bridge connection: read from env vars, then ~/.cursor/cursor-phone-bridge.json
  CURSOR_PHONE_BRIDGE_URL    (default: http://127.0.0.1:8765)
  CURSOR_PHONE_BRIDGE_SECRET (required)

Fail-open: any exception -> emit {} and exit 0 so the agent isn't blocked when the
bridge is down. EXCEPT for hooks marked failClosed in hooks.json, where Cursor
itself enforces the deny on failure.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Optional


CONFIG_FILE = Path.home() / ".cursor" / "cursor-phone-bridge.json"
DEFAULT_URL = "http://127.0.0.1:8765"
LOG_FILE = Path.home() / ".cursor" / "cursor-phone-bridge.log"


def _trace(msg: str) -> None:
    try:
        with LOG_FILE.open("a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%H:%M:%S')} pid={os.getpid()} {msg}\n")
    except Exception:
        pass


_trace(f"runner invoked argv={sys.argv}")
TIMEOUT_EVENT = 5
TIMEOUT_APPROVAL = 600  # must be < hooks.json timeout

# sensitive file patterns that trigger a /approve on beforeReadFile.
# rest of the time we no-op (allow without bothering phone).
SENSITIVE_FILE_PATTERNS = [
    re.compile(r"\.env(\..+)?$"),
    re.compile(r"(^|[\\/])id_rsa(\..+)?$"),
    re.compile(r"(^|[\\/])id_ed25519(\..+)?$"),
    re.compile(r"(^|[\\/])\.npmrc$"),
    re.compile(r"(^|[\\/])credentials(\.json)?$"),
    re.compile(r"\.pem$"),
    re.compile(r"\.key$"),
]


def _load_bridge_config() -> tuple[str, Optional[str]]:
    url = os.environ.get("CURSOR_PHONE_BRIDGE_URL")
    secret = os.environ.get("CURSOR_PHONE_BRIDGE_SECRET")
    if url and secret:
        return url, secret
    if CONFIG_FILE.exists():
        try:
            data = json.loads(CONFIG_FILE.read_text(encoding="utf-8-sig"))
            return data.get("url", url or DEFAULT_URL), data.get("secret", secret)
        except Exception as e:
            sys.stderr.write(f"runner: failed to load {CONFIG_FILE}: {e}\n")
    return url or DEFAULT_URL, secret


def _http(method: str, url: str, secret: str, body: Optional[dict], timeout: int) -> dict:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={
            "Authorization": f"Bearer {secret}",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8") or "{}"
        return json.loads(raw)


def _emit(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj))
    sys.stdout.flush()


def _project_root(payload: dict) -> Optional[str]:
    roots = payload.get("workspace_roots") or []
    return roots[0] if roots else None


def _is_sensitive_file(path: str) -> bool:
    return any(p.search(path) for p in SENSITIVE_FILE_PATTERNS)


def _approve(url: str, secret: str, body: dict) -> dict:
    return _http("POST", f"{url}/approve", secret, body, TIMEOUT_APPROVAL)


def _event(url: str, secret: str, body: dict) -> None:
    try:
        _http("POST", f"{url}/event", secret, body, TIMEOUT_EVENT)
    except Exception:
        pass


def _next_followup(url: str, secret: str, conversation_id: str) -> Optional[str]:
    try:
        resp = _http("GET", f"{url}/next-followup?conversation_id={conversation_id}", secret, None, TIMEOUT_EVENT)
        return resp.get("followup_message")
    except Exception:
        return None


# --------------------------- per-event handlers ---------------------------


def h_session_start(p: dict, url: str, secret: str) -> dict:
    _event(url, secret, {
        "kind": "session_start",
        "conversation_id": p.get("conversation_id") or p.get("session_id"),
        "project_root": _project_root(p),
        "composer_mode": p.get("composer_mode"),
    })
    return {}


def h_before_submit_prompt(p: dict, url: str, secret: str) -> dict:
    _event(url, secret, {
        "kind": "prompt_submit",
        "conversation_id": p.get("conversation_id"),
        "project_root": _project_root(p),
        "prompt": p.get("prompt", ""),
    })
    return {"continue": True}


def h_after_agent_response(p: dict, url: str, secret: str) -> dict:
    _event(url, secret, {
        "kind": "agent_response",
        "conversation_id": p.get("conversation_id"),
        "project_root": _project_root(p),
        "text": p.get("text", ""),
    })
    return {}


def h_after_agent_thought(p: dict, url: str, secret: str) -> dict:
    _event(url, secret, {
        "kind": "agent_thought",
        "conversation_id": p.get("conversation_id"),
        "project_root": _project_root(p),
        "text": p.get("text", ""),
    })
    return {}


def h_before_shell_execution(p: dict, url: str, secret: str) -> dict:
    return _approve(url, secret, {
        "tool": "Shell",
        "conversation_id": p.get("conversation_id"),
        "project_root": _project_root(p),
        "command": p.get("command", ""),
        "cwd": p.get("cwd", ""),
        "sandbox": p.get("sandbox", False),
    })


def h_before_mcp_execution(p: dict, url: str, secret: str) -> dict:
    return _approve(url, secret, {
        "tool": f"MCP:{p.get('tool_name', '?')}",
        "conversation_id": p.get("conversation_id"),
        "project_root": _project_root(p),
        "command": "",
        "summary": f"{p.get('tool_name','?')} {p.get('tool_input','')[:300]}",
        "tool_input": p.get("tool_input"),
    })


def h_before_read_file(p: dict, url: str, secret: str) -> dict:
    file_path = p.get("file_path", "")
    if not _is_sensitive_file(file_path):
        return {"permission": "allow"}
    return _approve(url, secret, {
        "tool": "ReadFile",
        "conversation_id": p.get("conversation_id"),
        "project_root": _project_root(p),
        "command": "",
        "summary": f"read sensitive file: {file_path}",
    })


def h_subagent_start(p: dict, url: str, secret: str) -> dict:
    _event(url, secret, {
        "kind": "subagent_start",
        "conversation_id": p.get("conversation_id"),
        "project_root": _project_root(p),
        "subagent_type": p.get("subagent_type") or p.get("subagent") or "?",
        "description": p.get("description") or p.get("prompt", "")[:120],
    })
    return {"permission": "allow"}


def h_subagent_stop(p: dict, url: str, secret: str) -> dict:
    _event(url, secret, {
        "kind": "subagent_stop",
        "conversation_id": p.get("conversation_id"),
        "project_root": _project_root(p),
        "subagent_type": p.get("subagent_type") or p.get("subagent") or "?",
        "status": p.get("status", "completed"),
    })
    return {}


def h_stop(p: dict, url: str, secret: str) -> dict:
    conv_id = p.get("conversation_id")
    status = p.get("status", "completed")
    _event(url, secret, {
        "kind": "stop",
        "conversation_id": conv_id,
        "project_root": _project_root(p),
        "status": status,
        "loop_count": p.get("loop_count", 0),
    })
    if not conv_id:
        return {}
    # Cursor only consumes followup_message when status == "completed"
    # (per docs). Skip the queue otherwise so the message stays for next turn.
    if status != "completed":
        return {}
    msg = _next_followup(url, secret, conv_id)
    if msg:
        return {"followup_message": msg}
    return {}


HANDLERS = {
    "sessionStart": h_session_start,
    "beforeSubmitPrompt": h_before_submit_prompt,
    "afterAgentResponse": h_after_agent_response,
    "afterAgentThought": h_after_agent_thought,
    "beforeShellExecution": h_before_shell_execution,
    "beforeMCPExecution": h_before_mcp_execution,
    "beforeReadFile": h_before_read_file,
    "subagentStart": h_subagent_start,
    "subagentStop": h_subagent_stop,
    "stop": h_stop,
}


def main() -> int:
    if len(sys.argv) < 2:
        _emit({})
        return 0
    event = sys.argv[1]
    handler = HANDLERS.get(event)
    if handler is None:
        _emit({})
        return 0
    raw_bytes = b""
    try:
        # Cursor on Windows pipes JSON with a UTF-8 BOM; read raw bytes and
        # decode with utf-8-sig so the BOM is stripped before json.loads.
        raw_bytes = sys.stdin.buffer.read()
        text = raw_bytes.decode("utf-8-sig", errors="replace").strip()
        payload = json.loads(text) if text else {}
        _trace(f"event={event} payload_keys={list(payload.keys())}")
    except Exception as e:
        _trace(f"event={event} stdin parse error: {e} raw={raw_bytes[:200]!r}")
        _emit({})
        return 0

    url, secret = _load_bridge_config()
    _trace(f"event={event} url={url} secret={'set' if secret else 'MISSING'} payload_keys={list(payload.keys())}")
    if not secret:
        _emit({})
        return 0

    try:
        result = handler(payload, url, secret)
        _trace(f"event={event} result={result}")
    except urllib.error.URLError as e:
        # Bridge offline: fail-open and let the failClosed: true setting in
        # hooks.json (per-hook) be the single source of truth for blocking.
        # Returning {} here = no opinion, Cursor uses its own default behavior.
        _trace(f"event={event} bridge unreachable: {e}; emitting {{}}")
        _emit({})
        return 0
    except Exception as e:
        _trace(f"event={event} runner error: {e}")
        sys.stderr.write(f"hook runner error: {e}\n")
        _emit({})
        return 0

    _emit(result or {})
    return 0


if __name__ == "__main__":
    sys.exit(main())
