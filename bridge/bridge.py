"""Cursor Phone Bridge.

Runs a local HTTP server on 127.0.0.1:8765 that Cursor hooks talk to,
plus a Telegram bot (long polling) that you talk to from your phone.

Flow:
- Cursor agent wants to run a shell command -> beforeShellExecution hook
  POSTs /approve -> bridge sends Telegram message with Approve/Deny buttons
  -> phone taps Approve -> bridge returns JSON to hook -> Cursor proceeds.
- Cursor agent finishes a turn -> stop hook GETs /next-followup ->
  bridge returns next phone-queued message (if any) -> Cursor auto-runs it.
- All prompts + responses mirrored to Telegram via /event.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import re
import sys
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Deque, Optional

from aiohttp import web

try:
    import tomllib  # py3.11+
except ModuleNotFoundError:  # pragma: no cover
    import tomli as tomllib  # type: ignore

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from state import State


log = logging.getLogger("bridge")


# --------------------------- config ---------------------------


@dataclass
class TelegramCfg:
    bot_token: str
    allowed_user_ids: list[int]
    notify_chat_id: Optional[int] = None


@dataclass
class BridgeCfg:
    host: str = "127.0.0.1"
    port: int = 8765
    secret: str = ""
    approval_timeout: int = 300


@dataclass
class BehaviorCfg:
    allow_shell_patterns: list[str] = field(default_factory=list)
    deny_shell_patterns: list[str] = field(default_factory=list)
    stream_thoughts: bool = False
    max_message_chars: int = 3800
    auto_wake_cursor: bool = True
    wake_idle_threshold_secs: int = 15
    # Cursor command-palette string used to focus the chat input. Cursor
    # fuzzy-matches; the top hit gets executed when we press Enter. "Focus
    # Chat" matches Cursor's "Workbench: Focus on Chat View" / similar.
    # Override here if your Cursor language pack uses different verbs.
    wake_focus_command: str = "Focus Chat"


@dataclass
class Config:
    telegram: TelegramCfg
    bridge: BridgeCfg
    behavior: BehaviorCfg


def load_config(path: Path) -> Config:
    raw = tomllib.loads(path.read_text(encoding="utf-8"))
    tg = raw.get("telegram", {})
    br = raw.get("bridge", {})
    bh = raw.get("behavior", {})
    return Config(
        telegram=TelegramCfg(
            bot_token=tg["bot_token"],
            allowed_user_ids=list(tg.get("allowed_user_ids", [])),
            notify_chat_id=tg.get("notify_chat_id"),
        ),
        bridge=BridgeCfg(**{k: v for k, v in br.items() if k in BridgeCfg.__annotations__}),
        behavior=BehaviorCfg(**{k: v for k, v in bh.items() if k in BehaviorCfg.__annotations__}),
    )


# --------------------------- shared state ---------------------------


@dataclass
class PendingApproval:
    future: asyncio.Future
    chat_id: int
    message_id: int
    tool: str
    summary: str
    db_id: int


@dataclass
class ActivityEntry:
    ts: float          # unix seconds
    kind: str          # prompt | thought | response | tool | turn_end | subagent_start | subagent_stop | wake_inject
    text: str          # short summary, already truncated


# How many events to keep in the per-conversation rolling buffer for /now.
_ACTIVITY_BUFFER_MAX = 30
# Per-entry char cap when formatting /now output (keep total under Telegram 4096).
_ACTIVITY_ENTRY_MAXCHARS = 240


class BridgeState:
    def __init__(self, config: Config, db: State):
        self.config = config
        self.db = db
        self.tg: Optional[Application] = None
        self.pending: dict[str, PendingApproval] = {}
        # which conversation phone messages get routed to (overrides "most recent")
        self.active_conversation_id: Optional[str] = None
        # In-memory rolling buffer of recent activity per conversation, used by
        # the /now Telegram command. Lost on bridge restart by design — we only
        # care about "what is happening RIGHT NOW", not historical reconstruction.
        self.recent_activity: dict[str, Deque[ActivityEntry]] = {}

    def record_activity(self, conv_id: Optional[str], kind: str, text: str) -> None:
        if not conv_id:
            return
        buf = self.recent_activity.get(conv_id)
        if buf is None:
            buf = deque(maxlen=_ACTIVITY_BUFFER_MAX)
            self.recent_activity[conv_id] = buf
        snippet = text.strip().replace("\r", "")
        if len(snippet) > _ACTIVITY_ENTRY_MAXCHARS:
            snippet = snippet[:_ACTIVITY_ENTRY_MAXCHARS] + "…"
        buf.append(ActivityEntry(ts=time.time(), kind=kind, text=snippet))

    def notify_chat_id(self) -> int:
        if self.config.telegram.notify_chat_id is not None:
            return self.config.telegram.notify_chat_id
        return self.config.telegram.allowed_user_ids[0]

    async def send(self, text: str, **kwargs: Any) -> Any:
        assert self.tg is not None
        return await self.tg.bot.send_message(
            chat_id=self.notify_chat_id(),
            text=_truncate(text, self.config.behavior.max_message_chars),
            **kwargs,
        )

    def shell_decision(self, command: str) -> Optional[str]:
        """Return 'allow' / 'deny' / None (= ask phone) based on patterns + db allowlist."""
        # denylist wins
        for pat in self.config.behavior.deny_shell_patterns:
            if re.fullmatch(pat, command):
                return "deny"
        for pat, kind in self.db.list_patterns():
            if kind == "deny" and re.fullmatch(pat, command):
                return "deny"
        # then allowlist
        for pat in self.config.behavior.allow_shell_patterns:
            if re.fullmatch(pat, command):
                return "allow"
        for pat, kind in self.db.list_patterns():
            if kind == "allow" and re.fullmatch(pat, command):
                return "allow"
        return None

    def routing_conversation_id(self) -> Optional[str]:
        if self.active_conversation_id:
            return self.active_conversation_id
        rec = self.db.most_recent_conversation()
        return rec[0] if rec else None


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 20] + "\n\n... [truncated]"


def _md_escape(text: str) -> str:
    """Escape MarkdownV2 special chars."""
    return re.sub(r"([_*\[\]()~`>#+\-=|{}.!\\])", r"\\\1", text)


# --------------------------- HTTP handlers ---------------------------


def _check_auth(request: web.Request) -> Optional[web.Response]:
    state: BridgeState = request.app["state"]
    expected = state.config.bridge.secret
    got = request.headers.get("Authorization", "")
    if not expected or got != f"Bearer {expected}":
        return web.json_response({"error": "unauthorized"}, status=401)
    return None


# --------------------------- cursor plan-file watcher ---------------------------

# Cursor 2.6.x writes plan-mode todos to ~/.cursor/plans/<slug>.plan.md
# every time the agent updates step status. The file is YAML frontmatter +
# markdown. We poll that directory (cheaper and more compatible than
# OS-level FS watchers on Windows / WSL) and diff against the last snapshot
# we persisted to drive Telegram notifications.

PLANS_DIR = Path.home() / ".cursor" / "plans"
PLAN_POLL_INTERVAL_S = 2.0


def _parse_plan_file(path: Path) -> tuple[str, list[dict]]:
    """Return (plan_name, [{id, content, status}, ...]).

    Lightweight parser tailored to Cursor's plan format; does not depend on
    PyYAML. Cursor's frontmatter has top-level keys `name`, `overview`,
    `todos` (list of mappings), `isProject`. Each todo entry is exactly:
        - id: <slug>
          content: <string, possibly quoted>
          status: pending|in_progress|completed|cancelled
    """
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return "", []
    if not text.startswith("---"):
        return path.stem, []
    end = text.find("\n---", 3)
    if end < 0:
        return path.stem, []
    fm = text[3:end]

    name_m = re.search(r"^name:\s*(.+)$", fm, re.MULTILINE)
    name = name_m.group(1).strip() if name_m else path.stem

    todos: list[dict] = []
    todos_block_m = re.search(
        r"^todos:\s*\n((?:[ \t]+.*\n?)+)",
        fm,
        re.MULTILINE,
    )
    if not todos_block_m:
        return name, todos
    block = todos_block_m.group(1)

    items = re.split(r"^  - ", block, flags=re.MULTILINE)
    for item in items[1:]:
        tid_m = re.search(r"^id:\s*(.+)$", item, re.MULTILINE)
        # content can be: bare, 'single-quoted', "double-quoted", or
        # in rare cases a multi-line YAML scalar. We grab a single line
        # and strip surrounding quotes.
        content_m = re.search(
            r"^\s*content:\s*(.+?)\s*$",
            item,
            re.MULTILINE,
        )
        status_m = re.search(r"^\s*status:\s*(\w+)\s*$", item, re.MULTILINE)
        content = content_m.group(1).strip() if content_m else ""
        if len(content) >= 2 and content[0] == content[-1] and content[0] in ("'", '"'):
            content = content[1:-1]
        todos.append({
            "id": tid_m.group(1).strip() if tid_m else "",
            "content": content,
            "status": status_m.group(1).strip() if status_m else "pending",
        })
    return name, todos


async def plan_watcher(state: "BridgeState") -> None:
    """Poll PLANS_DIR every PLAN_POLL_INTERVAL_S seconds, diff each changed
    plan-file vs our DB snapshot, and notify Telegram per state transition.

    Boot-strap policy: on the very first scan after bridge startup we silently
    absorb all existing plan files (no Telegram spam for old plans). After that,
    a *new* plan file (mtime > bridge_started_ts) triggers a "PLAN STARTED"
    intro, and any subsequent change to a tracked plan triggers a step diff."""
    if not PLANS_DIR.is_dir():
        log.info("plan watcher: %s does not exist; will retry as needed", PLANS_DIR)
    bridge_started_ts = time.time()
    log.info("plan watcher started, poll=%.1fs (boot ts=%.0f)",
             PLAN_POLL_INTERVAL_S, bridge_started_ts)
    while True:
        try:
            await _scan_plans_once(state, bridge_started_ts)
        except Exception as e:
            log.exception("plan watcher tick failed: %s", e)
        await asyncio.sleep(PLAN_POLL_INTERVAL_S)


async def _scan_plans_once(state: "BridgeState", bridge_started_ts: float) -> None:
    if not PLANS_DIR.is_dir():
        return
    for path in PLANS_DIR.glob("*.plan.md"):
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        plan_id = path.stem.removesuffix(".plan") if path.stem.endswith(".plan") else path.stem
        prev = state.db.get_plan(plan_id)
        if prev is not None and prev[2] >= mtime:
            continue

        name, new_todos = _parse_plan_file(path)
        if not new_todos:
            state.db.set_plan(plan_id, name, str(path),
                              json.dumps([], sort_keys=True), mtime)
            continue
        new_json = json.dumps(new_todos, sort_keys=True)

        prev_todos: list[dict] = []
        first_sighting = prev is None
        if prev is not None:
            try:
                parsed = json.loads(prev[0])
                if isinstance(parsed, list):
                    prev_todos = parsed
            except Exception:
                prev_todos = []
            if prev[0] == new_json:
                state.db.set_plan(plan_id, name, str(path), new_json, mtime)
                continue

        state.db.set_plan(plan_id, name, str(path), new_json, mtime)

        if first_sighting:
            # Only intro plans CREATED after the bridge started running, so
            # restarting the bridge against a folder full of historical plans
            # doesn't spam Telegram. mtime > bridge_started_ts means the file
            # was written after this process began.
            if mtime >= bridge_started_ts:
                await _send_plan_intro(state, name, plan_id, new_todos)
        else:
            await _send_plan_diff(state, name, plan_id, prev_todos, new_todos)


async def _send_plan_intro(state: "BridgeState", name: str, plan_id: str,
                           todos: list[dict]) -> None:
    lines = [f"PLAN STARTED: {name}"]
    lines.append(f"id: {plan_id}")
    lines.append("")
    lines.extend(_plan_summary_lines(todos))
    try:
        await state.send("\n".join(lines), parse_mode=None)
    except Exception as e:
        log.warning("failed to send plan intro: %s", e)


async def _send_plan_diff(state: "BridgeState", name: str, plan_id: str,
                          prev: list[dict], new: list[dict]) -> None:
    transitions = _diff_todos(prev, new)
    if not transitions:
        return
    done = sum(1 for t in new if t.get("status") == "completed")
    total = len(new)
    header = f"PLAN [{name}] {done}/{total}"
    lines = [header, ""]
    lines.extend(transitions)
    # If the plan is now fully done, append a celebration line and a short summary.
    if done == total and total > 0:
        lines.append("")
        lines.append("ALL STEPS COMPLETED.")
    try:
        await state.send("\n".join(lines), parse_mode=None)
    except Exception as e:
        log.warning("failed to send plan diff: %s", e)


# --------------------------- todo / plan helpers ---------------------------


_STATUS_ICON = {
    "completed": "[x]",
    "in_progress": "[~]",
    "cancelled": "[/]",
    "pending": "[ ]",
}


def _todos_by_id(todos: list[dict]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for i, t in enumerate(todos or []):
        if not isinstance(t, dict):
            continue
        # fall back to index if the agent didn't supply ids
        tid = str(t.get("id") or f"_idx_{i}")
        out[tid] = t
    return out


def _plan_summary_lines(todos: list[dict], max_items: int = 12) -> list[str]:
    if not todos:
        return ["(empty plan)"]
    done = sum(1 for t in todos if t.get("status") == "completed")
    in_prog = sum(1 for t in todos if t.get("status") == "in_progress")
    total = len(todos)
    lines = [f"plan: {done}/{total} done, {in_prog} in progress"]
    shown = 0
    for t in todos:
        if shown >= max_items:
            lines.append(f"... +{total - shown} more")
            break
        icon = _STATUS_ICON.get(t.get("status", "pending"), "[ ]")
        content = (t.get("content") or "").strip()
        if len(content) > 80:
            content = content[:77] + "..."
        lines.append(f"{icon} {content}")
        shown += 1
    return lines


def _diff_todos(old: list[dict], new: list[dict]) -> list[str]:
    """Produce human-readable lines describing transitions from old -> new."""
    old_map = _todos_by_id(old)
    new_map = _todos_by_id(new)

    # also build a content->status map as a fallback when ids change between calls
    def by_content(items: dict[str, dict]) -> dict[str, str]:
        return {(t.get("content") or "").strip(): (t.get("status") or "pending")
                for t in items.values() if t.get("content")}

    old_by_content = by_content(old_map)
    transitions: list[str] = []

    for tid, new_t in new_map.items():
        old_t = old_map.get(tid)
        new_status = new_t.get("status", "pending")
        content = (new_t.get("content") or "").strip()
        old_status = (old_t or {}).get("status") if old_t else old_by_content.get(content)
        if old_t is None and old_status is None:
            transitions.append(f"+ added [{new_status}] {content[:80]}")
        elif old_status != new_status:
            arrow = f"{old_status or 'new'} -> {new_status}"
            if new_status == "completed":
                transitions.append(f"DONE: {content[:80]}  ({arrow})")
            elif new_status == "in_progress":
                transitions.append(f"START: {content[:80]}  ({arrow})")
            elif new_status == "cancelled":
                transitions.append(f"CANCEL: {content[:80]}  ({arrow})")
            else:
                transitions.append(f"{arrow}: {content[:80]}")

    # removed items
    for tid, old_t in old_map.items():
        if tid not in new_map:
            content = (old_t.get("content") or "").strip()
            # only report removal if content also disappeared (avoid id-churn noise)
            if content and content not in {(t.get("content") or "").strip() for t in new_map.values()}:
                transitions.append(f"- removed: {content[:80]}")

    return transitions


async def handle_event(request: web.Request) -> web.Response:
    """Fire-and-forget event from a hook. Body: {kind, conversation_id, project_root?, ...}."""
    if (resp := _check_auth(request)) is not None:
        return resp
    state: BridgeState = request.app["state"]
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "bad json"}, status=400)

    kind = body.get("kind")
    conv_id = body.get("conversation_id")
    project_root = body.get("project_root")

    if conv_id:
        state.db.touch_conversation(conv_id, project_root)

    label = _project_label(project_root)

    try:
        if kind == "session_start":
            await state.send(f"[{label}] session started\n`{conv_id}`", parse_mode=None)
        elif kind == "prompt_submit":
            text = body.get("prompt", "")
            state.record_activity(conv_id, "prompt", text)
            await state.send(f"[{label}] you typed:\n> {text}", parse_mode=None)
        elif kind == "agent_response":
            text = body.get("text", "")
            state.record_activity(conv_id, "response", text)
            await state.send(f"[{label}] agent:\n{text}", parse_mode=None)
        elif kind == "agent_thought":
            text = body.get("text", "")
            state.record_activity(conv_id, "thought", text)
            if state.config.behavior.stream_thoughts:
                await state.send(f"[{label}] (thinking) {text}", parse_mode=None)
        elif kind == "stop":
            status = body.get("status", "?")
            state.record_activity(conv_id, "turn_end", f"status={status}")
            await state.send(f"[{label}] turn ended ({status})", parse_mode=None)
        elif kind == "tool_used":
            tool = body.get("tool", "?")
            summary = body.get("summary", "")
            state.record_activity(conv_id, "tool", f"{tool}: {summary}" if summary else tool)
            await state.send(f"[{label}] used {tool}: {summary}", parse_mode=None)
        elif kind == "subagent_start":
            stype = body.get("subagent_type", "?")
            desc = body.get("description", "")
            state.record_activity(conv_id, "subagent_start", f"{stype}: {desc}")
            await state.send(f"[{label}] subagent START ({stype})\n{desc}", parse_mode=None)
        elif kind == "subagent_stop":
            stype = body.get("subagent_type", "?")
            status = body.get("status", "?")
            state.record_activity(conv_id, "subagent_stop", f"{stype} ({status})")
            await state.send(f"[{label}] subagent STOP ({stype}, {status})", parse_mode=None)
        else:
            log.warning("unknown event kind: %s", kind)
    except Exception as e:
        log.exception("failed to forward event to telegram: %s", e)

    return web.json_response({"ok": True})


async def handle_next_followup(request: web.Request) -> web.Response:
    """stop hook polls this. Returns next queued phone message or {} if none."""
    if (resp := _check_auth(request)) is not None:
        return resp
    state: BridgeState = request.app["state"]
    conv_id = request.query.get("conversation_id")
    if not conv_id:
        return web.json_response({})
    msg = state.db.pop_followup(conv_id)
    if msg is None:
        return web.json_response({})
    log.info("delivered followup to %s: %s", conv_id, msg[:80])
    # Tell the phone the queued message just got injected into Cursor.
    try:
        snippet = msg if len(msg) <= 200 else msg[:200] + "..."
        await state.send(f"-> injected into Cursor [{conv_id[:8]}]:\n> {snippet}", parse_mode=None)
    except Exception as e:
        log.warning("failed to notify phone of injection: %s", e)
    return web.json_response({"followup_message": msg})


async def handle_approve(request: web.Request) -> web.Response:
    """Synchronous approval. Blocks until phone responds, allowlist matches, or timeout."""
    if (resp := _check_auth(request)) is not None:
        return resp
    state: BridgeState = request.app["state"]
    body = await request.json()

    tool = body.get("tool", "unknown")
    conv_id = body.get("conversation_id")
    project_root = body.get("project_root")
    label = _project_label(project_root)
    command = body.get("command", "")
    cwd = body.get("cwd", "")
    summary = body.get("summary") or command or json.dumps(body.get("tool_input", {}))[:200]

    # auto decision via patterns (only for shell-like tools)
    if tool in ("Shell", "beforeShellExecution") and command:
        auto = state.shell_decision(command)
        if auto == "allow":
            state.db.log_approval_request(conv_id, tool, json.dumps(body))
            return web.json_response({"permission": "allow"})
        if auto == "deny":
            db_id = state.db.log_approval_request(conv_id, tool, json.dumps(body))
            state.db.log_approval_decision(db_id, "deny", "denylist")
            return web.json_response({
                "permission": "deny",
                "user_message": "Blocked by denylist pattern.",
                "agent_message": "Command blocked by user denylist.",
            })

    db_id = state.db.log_approval_request(conv_id, tool, json.dumps(body))
    approval_id = uuid.uuid4().hex[:12]

    # send Telegram approval message
    text = f"[{label}] {tool} approval"
    if command:
        text += f"\n\n`{command}`"
    if cwd:
        text += f"\n\nin `{cwd}`"
    if not command and summary:
        text += f"\n\n{summary}"
    # Cursor >=2.6 ignores hook 'allow' for shell/MCP (forum-confirmed bug).
    # Only 'deny' is honored. Be honest about it on the phone.
    if tool in ("Shell", "beforeShellExecution") or tool.startswith("MCP:"):
        text += "\n\n_Cursor bug: Allow is no-op (still click on PC). Deny = remote abort._"

    keyboard = [
        [
            InlineKeyboardButton("Allow (no-op)", callback_data=f"a:{approval_id}:allow"),
            InlineKeyboardButton("Deny", callback_data=f"a:{approval_id}:deny"),
        ]
    ]
    if tool in ("Shell", "beforeShellExecution") and command:
        # offer "always allow exact" - escape the command into a safe regex
        safe = re.escape(command)
        keyboard.append([
            InlineKeyboardButton("Always allow exact", callback_data=f"a:{approval_id}:allow_exact"),
        ])

    assert state.tg is not None
    msg = await state.tg.bot.send_message(
        chat_id=state.notify_chat_id(),
        text=_truncate(text, state.config.behavior.max_message_chars),
        reply_markup=InlineKeyboardMarkup(keyboard),
    )

    loop = asyncio.get_running_loop()
    fut: asyncio.Future = loop.create_future()
    state.pending[approval_id] = PendingApproval(
        future=fut,
        chat_id=msg.chat_id,
        message_id=msg.message_id,
        tool=tool,
        summary=summary,
        db_id=db_id,
    )

    timeout = state.config.bridge.approval_timeout
    try:
        decision: str = await asyncio.wait_for(fut, timeout=timeout)
    except asyncio.TimeoutError:
        decision = "deny"
        state.db.log_approval_decision(db_id, "deny", "timeout")
        try:
            await state.tg.bot.edit_message_text(
                chat_id=msg.chat_id,
                message_id=msg.message_id,
                text=_truncate(text + "\n\n[timeout - denied]", state.config.behavior.max_message_chars),
            )
        except Exception:
            pass
        state.pending.pop(approval_id, None)
        return web.json_response({
            "permission": "deny",
            "user_message": "Approval timed out (no phone response).",
            "agent_message": "Tool call denied: phone did not respond in time.",
        })

    state.pending.pop(approval_id, None)

    # handle "allow_exact" by adding pattern then allowing
    if decision == "allow_exact" and command:
        state.db.add_pattern(re.escape(command), "allow")
        decision = "allow"

    state.db.log_approval_decision(db_id, decision, "phone")
    return web.json_response({"permission": decision})


def _project_label(project_root: Optional[str]) -> str:
    if not project_root:
        return "?"
    return Path(project_root).name or project_root


# --------------------------- Telegram handlers ---------------------------


def _is_allowed(update: Update, state: BridgeState) -> bool:
    user = update.effective_user
    if user is None:
        return False
    return user.id in state.config.telegram.allowed_user_ids


async def tg_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    state: BridgeState = ctx.bot_data["state"]
    if not _is_allowed(update, state):
        return
    query = update.callback_query
    if not query or not query.data:
        return
    parts = query.data.split(":", 2)
    if len(parts) != 3 or parts[0] != "a":
        await query.answer("unknown")
        return
    _, approval_id, decision = parts
    pending = state.pending.get(approval_id)
    if pending is None:
        await query.answer("expired or already answered")
        return
    if not pending.future.done():
        pending.future.set_result(decision)
    await query.answer({"allow": "approved", "deny": "denied", "allow_exact": "approved + saved"}.get(decision, "ok"))
    new_text = (query.message.text or "") + f"\n\n[{decision}]"
    try:
        await query.edit_message_text(text=_truncate(new_text, state.config.behavior.max_message_chars))
    except Exception:
        pass


_WAKE_SCRIPT = Path(__file__).parent / "wake_cursor.ps1"


async def _wake_cursor_with_text(text: str, workspace_hint: str,
                                  focus_command: str = "Focus Chat") -> tuple[bool, str]:
    """Try to inject `text` into Cursor's chat input directly via Windows UI
    automation. Returns (ok, detail). Non-blocking on the asyncio loop —
    runs the PowerShell helper as a subprocess.

    workspace_hint is a substring to match against Cursor window titles
    (typically the project folder name) so we hit the right window when the
    user has multiple Cursors open.

    focus_command is the command-palette string the script will invoke to
    focus the chat input (e.g. "Focus Chat"). The script opens Ctrl+Shift+P,
    pastes this string, and presses Enter — Cursor's fuzzy matcher picks
    the top hit. We avoid Ctrl+L because it's a TOGGLE (closes the chat
    panel if it's already open)."""
    if not _WAKE_SCRIPT.is_file():
        return False, f"wake script missing at {_WAKE_SCRIPT}"
    try:
        proc = await asyncio.create_subprocess_exec(
            "powershell.exe",
            "-NoProfile",
            "-ExecutionPolicy", "Bypass",
            "-File", str(_WAKE_SCRIPT),
            "-Message", text,
            "-WorkspaceMatch", workspace_hint or "",
            "-FocusCommand", focus_command or "Focus Chat",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=8.0)
        except asyncio.TimeoutError:
            proc.kill()
            return False, "wake script timeout"
        if proc.returncode == 0:
            return True, (stdout.decode("utf-8", "replace").strip() or "ok")
        return False, (stderr.decode("utf-8", "replace").strip()
                       or f"exit={proc.returncode}")
    except Exception as e:
        return False, f"wake failed: {e}"


async def tg_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    state: BridgeState = ctx.bot_data["state"]
    if not _is_allowed(update, state):
        return
    msg = update.effective_message
    if msg is None or not msg.text:
        return
    text = msg.text.strip()
    if not text:
        return
    conv_id = state.routing_conversation_id()
    if conv_id is None:
        await msg.reply_text("no active Cursor conversation. Open a chat in Cursor first (any message will register the session).")
        return
    rec = next((c for c in state.db.list_conversations(limit=20) if c[0] == conv_id), None)
    project_root = rec[1] if rec else None
    label = _project_label(project_root)
    last_seen_ts = rec[2] if rec else 0.0
    idle_secs = int(time.time() - last_seen_ts) if last_seen_ts else 9999
    pinned = " (pinned via /use)" if state.active_conversation_id == conv_id else ""

    # If the agent appears idle and auto-wake is enabled, try the UI-automation
    # path FIRST. We bypass the queue entirely on success: SendKeys submits the
    # message to Cursor as a fresh user prompt, which triggers a normal turn
    # and downstream `stop` hook handling.
    cfg = state.config.behavior
    wake_threshold = max(0, cfg.wake_idle_threshold_secs)
    if cfg.auto_wake_cursor and idle_secs >= wake_threshold:
        # Hint the wake script to pick the Cursor window for this project.
        # project_root looks like "/C:/Users/.../screen share" — last segment.
        hint = ""
        if project_root:
            hint = project_root.replace("\\", "/").rstrip("/").split("/")[-1]
        ok, detail = await _wake_cursor_with_text(text, hint, cfg.wake_focus_command)
        if ok:
            state.record_activity(conv_id, "wake_inject", text)
            await msg.reply_text(
                f"WAKE-INJECTED into [{label}] {conv_id[:8]}{pinned} "
                f"(idle {idle_secs}s).\n{detail}"
            )
            return
        # fall through to queue + warning if wake failed
        log.info("auto-wake failed, falling back to queue: %s", detail)
        wake_failure_note = f"\n(auto-wake tried and failed: {detail})"
    else:
        wake_failure_note = ""

    inserted_id = state.db.enqueue_followup(conv_id, text)
    depth = state.db.queue_depth(conv_id)
    dup_note = "\n(duplicate of last queued msg, not added again)" if inserted_id == 0 else ""
    if idle_secs >= wake_threshold:
        idle_warning = (
            f"\nWARNING: chat IDLE for {idle_secs}s and auto-wake "
            f"{'is OFF' if not cfg.auto_wake_cursor else 'failed'}. "
            f"Type ANY character (e.g. '.') in that Cursor chat to drain the queue."
        )
    else:
        idle_warning = "\n(agent appears active; will inject when its current turn ends)"
    await msg.reply_text(
        f"queued for [{label}] {conv_id[:8]}{pinned} (depth: {depth}).{dup_note}"
        f"{wake_failure_note}{idle_warning}\n"
        f"Wrong chat? /status to see all, /use <id> to pin one."
    )


async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    state: BridgeState = ctx.bot_data["state"]
    if not _is_allowed(update, state):
        return
    convs = state.db.list_conversations(limit=5)
    if not convs:
        await update.effective_message.reply_text("no conversations seen yet")
        return
    lines = ["conversations (most recent first):"]
    for cid, root, ts in convs:
        label = _project_label(root)
        depth = state.db.queue_depth(cid)
        marker = " *active*" if cid == state.active_conversation_id else ""
        lines.append(f"- [{label}] {cid[:8]}  q={depth}{marker}")
    if state.pending:
        lines.append(f"\npending approvals: {len(state.pending)}")
    await update.effective_message.reply_text("\n".join(lines))


async def cmd_use(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    state: BridgeState = ctx.bot_data["state"]
    if not _is_allowed(update, state):
        return
    if not ctx.args:
        state.active_conversation_id = None
        await update.effective_message.reply_text("routing reset to most recent conversation")
        return
    needle = ctx.args[0]
    for cid, _root, _ts in state.db.list_conversations(limit=50):
        if cid.startswith(needle):
            state.active_conversation_id = cid
            await update.effective_message.reply_text(f"routing -> {cid[:12]}")
            return
    await update.effective_message.reply_text("no match")


def _fmt_relative(seconds: float) -> str:
    s = int(seconds)
    if s < 60:
        return f"{s}s ago"
    if s < 3600:
        m, sec = divmod(s, 60)
        return f"{m}m{sec:02d}s ago"
    h, rem = divmod(s, 3600)
    m = rem // 60
    return f"{h}h{m:02d}m ago"


_KIND_TAGS = {
    "prompt":         "USER",
    "wake_inject":    "USER(phone)",
    "thought":        "thinking",
    "tool":           "tool",
    "response":       "agent",
    "turn_end":       "turn end",
    "subagent_start": "subagent>>",
    "subagent_stop":  "subagent<<",
}


async def cmd_now(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Dump the most recent activity for the active conversation so the user
    can see what the agent is doing right now without scrolling Telegram."""
    state: BridgeState = ctx.bot_data["state"]
    if not _is_allowed(update, state):
        return

    conv_id = state.routing_conversation_id()
    if conv_id is None:
        await update.effective_message.reply_text(
            "no active Cursor conversation. /status to list, /use <prefix> to pin one."
        )
        return

    rec = next((c for c in state.db.list_conversations(limit=20) if c[0] == conv_id), None)
    project_root = rec[1] if rec else None
    label = _project_label(project_root)
    last_seen_ts = rec[2] if rec else 0.0
    idle_secs = int(time.time() - last_seen_ts) if last_seen_ts else 9999

    buf = state.recent_activity.get(conv_id)
    if not buf:
        await update.effective_message.reply_text(
            f"[{label}] {conv_id[:8]}\nno activity recorded since bridge start. "
            f"(idle {idle_secs}s)"
        )
        return

    # Heuristic: if the most recent entry is "turn_end" the agent is idle.
    last = buf[-1]
    if last.kind == "turn_end":
        status_line = f"IDLE {idle_secs}s — last turn ended {_fmt_relative(time.time() - last.ts)}"
    else:
        status_line = f"BUSY ({_fmt_relative(time.time() - last.ts)} since last event)"

    n_show = min(len(buf), 12)  # last N entries; older are truncated
    pinned = " (pinned via /use)" if state.active_conversation_id == conv_id else ""
    lines = [
        f"[{label}] {conv_id[:8]}{pinned}",
        status_line,
        f"--- last {n_show} of {len(buf)} events ---",
    ]
    now_ts = time.time()
    for entry in list(buf)[-n_show:]:
        tag = _KIND_TAGS.get(entry.kind, entry.kind)
        rel = _fmt_relative(now_ts - entry.ts)
        # one-line preview: first 200 chars of the (already-truncated) text,
        # newlines collapsed to spaces so the layout stays compact.
        preview = " ".join(entry.text.split())
        if len(preview) > 200:
            preview = preview[:200] + "…"
        lines.append(f"[{rel:>10}] {tag}: {preview}")

    await update.effective_message.reply_text("\n".join(lines))


async def cmd_allow(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    state: BridgeState = ctx.bot_data["state"]
    if not _is_allowed(update, state):
        return
    if not ctx.args:
        await update.effective_message.reply_text("usage: /allow <regex>")
        return
    pat = " ".join(ctx.args)
    try:
        re.compile(pat)
    except re.error as e:
        await update.effective_message.reply_text(f"bad regex: {e}")
        return
    state.db.add_pattern(pat, "allow")
    await update.effective_message.reply_text(f"allowlisted: {pat}")


async def cmd_deny(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    state: BridgeState = ctx.bot_data["state"]
    if not _is_allowed(update, state):
        return
    if not ctx.args:
        await update.effective_message.reply_text("usage: /deny <regex>")
        return
    pat = " ".join(ctx.args)
    try:
        re.compile(pat)
    except re.error as e:
        await update.effective_message.reply_text(f"bad regex: {e}")
        return
    state.db.add_pattern(pat, "deny")
    await update.effective_message.reply_text(f"denylisted: {pat}")


async def cmd_patterns(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    state: BridgeState = ctx.bot_data["state"]
    if not _is_allowed(update, state):
        return
    rows = state.db.list_patterns()
    if not rows:
        await update.effective_message.reply_text("no user patterns. config defaults still apply.")
        return
    lines = [f"{kind}: {pat}" for pat, kind in rows]
    await update.effective_message.reply_text("\n".join(lines))


async def cmd_unpattern(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    state: BridgeState = ctx.bot_data["state"]
    if not _is_allowed(update, state):
        return
    if not ctx.args:
        await update.effective_message.reply_text("usage: /unpattern <regex>")
        return
    pat = " ".join(ctx.args)
    ok = state.db.remove_pattern(pat)
    await update.effective_message.reply_text("removed" if ok else "not found")


async def cmd_stop(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    state: BridgeState = ctx.bot_data["state"]
    if not _is_allowed(update, state):
        return
    # mark all pending as denied
    n = 0
    for ap in list(state.pending.values()):
        if not ap.future.done():
            ap.future.set_result("deny")
            n += 1
    await update.effective_message.reply_text(f"denied {n} pending approval(s). To interrupt the agent itself, do it in Cursor (no hook for that yet).")


async def cmd_queue(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    state: BridgeState = ctx.bot_data["state"]
    if not _is_allowed(update, state):
        return
    rows = state.db.list_queued(limit=20)
    if not rows:
        await update.effective_message.reply_text("queue is empty")
        return
    lines = ["queued messages (newest first):"]
    for qid, cid, message, _ts in rows:
        snippet = message if len(message) <= 60 else message[:60] + "..."
        lines.append(f"#{qid} [{cid[:8]}] {snippet}")
    await update.effective_message.reply_text("\n".join(lines))


async def cmd_clearqueue(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    state: BridgeState = ctx.bot_data["state"]
    if not _is_allowed(update, state):
        return
    if ctx.args:
        needle = ctx.args[0]
        # accept conversation id prefix
        target: Optional[str] = None
        for cid, _r, _t in state.db.list_conversations(limit=50):
            if cid.startswith(needle):
                target = cid
                break
        if target is None:
            await update.effective_message.reply_text("no match")
            return
        n = state.db.clear_queue(target)
        await update.effective_message.reply_text(f"cleared {n} from {target[:8]}")
    else:
        n = state.db.clear_queue(None)
        await update.effective_message.reply_text(f"cleared {n} message(s) from all queues")


async def cmd_plan(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    state: BridgeState = ctx.bot_data["state"]
    if not _is_allowed(update, state):
        return
    plans = state.db.list_plans()
    if not plans:
        await update.effective_message.reply_text(
            "no plans tracked yet.\n"
            f"watching {PLANS_DIR} every {PLAN_POLL_INTERVAL_S:g}s.\n"
            "Open a Cursor chat in Plan mode and the agent's first TodoWrite "
            "will land here."
        )
        return

    # /plan -> list most recent up to 5 with progress
    # /plan <id-prefix> -> dump full todos for that plan
    if not ctx.args:
        lines = ["tracked plans (newest first):"]
        for plan_id, plan_name, _path, updated_ts in plans[:8]:
            snap = state.db.get_plan(plan_id)
            todos: list[dict] = []
            if snap:
                try:
                    parsed = json.loads(snap[0])
                    if isinstance(parsed, list):
                        todos = parsed
                except Exception:
                    pass
            done = sum(1 for t in todos if t.get("status") == "completed")
            total = len(todos)
            in_prog = sum(1 for t in todos if t.get("status") == "in_progress")
            age = int(time.time() - updated_ts)
            short_id = plan_id[-12:] if len(plan_id) > 12 else plan_id
            lines.append(
                f"- {plan_name or '(unnamed)'}  {done}/{total} done"
                f" ({in_prog} active, {age}s ago)  id:{short_id}"
            )
        lines.append("")
        lines.append("/plan <id-prefix> for full step list")
        await update.effective_message.reply_text("\n".join(lines))
        return

    needle = ctx.args[0]
    match = next(
        (p for p in plans if p[0].endswith(needle) or p[0].startswith(needle) or needle in p[0]),
        None,
    )
    if match is None:
        await update.effective_message.reply_text(f"no plan matching '{needle}'")
        return
    plan_id, plan_name, _path, _ts = match
    snap = state.db.get_plan(plan_id)
    todos = []
    if snap:
        try:
            parsed = json.loads(snap[0])
            if isinstance(parsed, list):
                todos = parsed
        except Exception:
            pass
    lines = [f"plan: {plan_name}", f"id: {plan_id}", ""]
    lines.extend(_plan_summary_lines(todos, max_items=80))
    await update.effective_message.reply_text("\n".join(lines))


async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    state: BridgeState = ctx.bot_data["state"]
    if not _is_allowed(update, state):
        return
    await update.effective_message.reply_text(
        "commands:\n"
        "/status - list conversations + queue\n"
        "/now - what is the agent doing right now (last ~12 events + idle/busy)\n"
        "/plan [id-prefix] - show current TodoWrite plan for conversation\n"
        "/queue - list pending phone->Cursor messages\n"
        "/clearqueue [id-prefix] - drop queued messages (all if no arg)\n"
        "/use <prefix> - route my messages to a specific conversation (no arg = most recent)\n"
        "/allow <regex> - auto-approve matching shell commands\n"
        "/deny <regex> - auto-deny matching shell commands\n"
        "/patterns - list user patterns\n"
        "/unpattern <regex> - remove pattern\n"
        "/stop - deny all pending approvals\n"
        "any other text - injected via wake script if Cursor idle, else queued for next 'stop'"
    )


# --------------------------- main ---------------------------


def build_telegram_app(state: BridgeState) -> Application:
    app = ApplicationBuilder().token(state.config.telegram.bot_token).build()
    app.bot_data["state"] = state
    app.add_handler(CommandHandler("start", cmd_help))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("now", cmd_now))
    app.add_handler(CommandHandler("use", cmd_use))
    app.add_handler(CommandHandler("allow", cmd_allow))
    app.add_handler(CommandHandler("deny", cmd_deny))
    app.add_handler(CommandHandler("patterns", cmd_patterns))
    app.add_handler(CommandHandler("unpattern", cmd_unpattern))
    app.add_handler(CommandHandler("stop", cmd_stop))
    app.add_handler(CommandHandler("queue", cmd_queue))
    app.add_handler(CommandHandler("clearqueue", cmd_clearqueue))
    app.add_handler(CommandHandler("plan", cmd_plan))
    app.add_handler(CallbackQueryHandler(tg_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, tg_text))
    return app


def build_web_app(state: BridgeState) -> web.Application:
    app = web.Application()
    app["state"] = state
    app.router.add_post("/event", handle_event)
    app.router.add_post("/approve", handle_approve)
    app.router.add_get("/next-followup", handle_next_followup)
    app.router.add_get("/health", lambda r: web.json_response({"ok": True}))
    return app


async def run(config: Config) -> None:
    db_path = Path(__file__).parent / "state.sqlite"
    db = State(db_path)
    state = BridgeState(config, db)
    tg_app = build_telegram_app(state)
    state.tg = tg_app
    web_app = build_web_app(state)

    # start telegram polling
    await tg_app.initialize()
    await tg_app.start()
    assert tg_app.updater is not None
    await tg_app.updater.start_polling(drop_pending_updates=True)

    # start http server
    runner = web.AppRunner(web_app)
    await runner.setup()
    site = web.TCPSite(runner, config.bridge.host, config.bridge.port)
    await site.start()

    # plan-file watcher (Cursor's TodoWrite output lives in ~/.cursor/plans)
    watcher_task = asyncio.create_task(plan_watcher(state))

    log.info("bridge up: http://%s:%d  | telegram polling started", config.bridge.host, config.bridge.port)
    try:
        await state.send(
            f"bridge online (PID {sys.argv[0]} on {config.bridge.host}:{config.bridge.port}). /help for commands."
        )
    except Exception as e:
        log.warning("could not send startup message to telegram: %s", e)

    stop_event = asyncio.Event()
    try:
        await stop_event.wait()
    finally:
        watcher_task.cancel()
        try:
            await watcher_task
        except (asyncio.CancelledError, Exception):
            pass
        await tg_app.updater.stop()
        await tg_app.stop()
        await tg_app.shutdown()
        await runner.cleanup()
        db.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, default=Path(__file__).parent / "config.toml")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()

    logging.basicConfig(
        level=args.log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    if not args.config.exists():
        print(f"config not found: {args.config}\nCopy config.toml.example to config.toml and fill in.", file=sys.stderr)
        sys.exit(2)

    config = load_config(args.config)
    if not config.telegram.bot_token or "REPLACE" in config.telegram.bot_token:
        print("set telegram.bot_token in config.toml", file=sys.stderr)
        sys.exit(2)
    if not config.bridge.secret or "REPLACE" in config.bridge.secret:
        print("set bridge.secret in config.toml (use: python -c \"import secrets;print(secrets.token_urlsafe(32))\")", file=sys.stderr)
        sys.exit(2)
    if not config.telegram.allowed_user_ids:
        print("set telegram.allowed_user_ids in config.toml (your numeric Telegram user ID)", file=sys.stderr)
        sys.exit(2)

    asyncio.run(run(config))


if __name__ == "__main__":
    main()
