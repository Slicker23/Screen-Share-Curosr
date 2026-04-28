"""End-to-end test: spin up a mock bridge in a thread, drive runner.py via subprocess.

Validates: runner forms correct requests, parses correct responses, handles
fail-open vs fail-closed, sensitive file detection, etc.

Run: python hooks/test_runner.py
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

from aiohttp import web


# --- mock bridge ---------------------------------------------------------

PORT = 8767
SECRET = "testsecret"

received: list[tuple[str, dict]] = []
followup_queue: dict[str, list[str]] = {}
approve_response: dict = {"permission": "allow"}  # mutable: tests change this
approve_delay: float = 0.0


def auth_ok(req: web.Request) -> bool:
    return req.headers.get("Authorization") == f"Bearer {SECRET}"


async def m_event(req: web.Request) -> web.Response:
    if not auth_ok(req):
        return web.json_response({"error": "auth"}, status=401)
    body = await req.json()
    received.append(("event", body))
    return web.json_response({"ok": True})


async def m_approve(req: web.Request) -> web.Response:
    if not auth_ok(req):
        return web.json_response({"error": "auth"}, status=401)
    body = await req.json()
    received.append(("approve", body))
    if approve_delay:
        await asyncio.sleep(approve_delay)
    return web.json_response(approve_response)


async def m_next_followup(req: web.Request) -> web.Response:
    if not auth_ok(req):
        return web.json_response({"error": "auth"}, status=401)
    cid = req.query.get("conversation_id", "")
    received.append(("next-followup", {"conversation_id": cid}))
    queue = followup_queue.get(cid, [])
    if not queue:
        return web.json_response({})
    return web.json_response({"followup_message": queue.pop(0)})


async def m_health(_req: web.Request) -> web.Response:
    return web.json_response({"ok": True})


def build_mock_app() -> web.Application:
    app = web.Application()
    app.router.add_post("/event", m_event)
    app.router.add_post("/approve", m_approve)
    app.router.add_get("/next-followup", m_next_followup)
    app.router.add_get("/health", m_health)
    return app


def run_server_in_thread() -> tuple[threading.Thread, asyncio.AbstractEventLoop, web.AppRunner]:
    loop = asyncio.new_event_loop()
    runner_holder: dict = {}
    started = threading.Event()

    def thread_main() -> None:
        asyncio.set_event_loop(loop)
        app = build_mock_app()
        runner = web.AppRunner(app)
        loop.run_until_complete(runner.setup())
        site = web.TCPSite(runner, "127.0.0.1", PORT)
        loop.run_until_complete(site.start())
        runner_holder["runner"] = runner
        started.set()
        loop.run_forever()

    t = threading.Thread(target=thread_main, daemon=True)
    t.start()
    started.wait(timeout=10)
    return t, loop, runner_holder["runner"]


def stop_server(loop: asyncio.AbstractEventLoop, runner: web.AppRunner) -> None:
    asyncio.run_coroutine_threadsafe(runner.cleanup(), loop).result(timeout=5)
    loop.call_soon_threadsafe(loop.stop)


# --- driver ---------------------------------------------------------------


def call(event: str, payload: dict, env: dict, timeout: float = 30.0) -> dict:
    runner_py = str(ROOT / "hooks" / "runner.py")
    t0 = time.time()
    r = subprocess.run(
        [sys.executable, runner_py, event],
        input=json.dumps(payload).encode(),
        capture_output=True,
        env=env,
        timeout=timeout,
    )
    print(f"  ({event}: {time.time()-t0:.2f}s, rc={r.returncode})", file=sys.stderr)
    assert r.returncode == 0, r.stderr.decode()
    return json.loads(r.stdout.decode().strip() or "{}")


def main() -> None:
    global approve_response, approve_delay

    t, loop, runner = run_server_in_thread()
    print(f"mock bridge up on :{PORT}")
    try:
        env = {
            **os.environ,
            "CURSOR_PHONE_BRIDGE_URL": f"http://127.0.0.1:{PORT}",
            "CURSOR_PHONE_BRIDGE_SECRET": SECRET,
        }
        base = {
            "conversation_id": "c1",
            "generation_id": "g1",
            "model": "test",
            "hook_event_name": "x",
            "cursor_version": "1.7",
            "workspace_roots": [str(ROOT)],
        }

        out = call("sessionStart", {**base, "session_id": "c1"}, env)
        assert out == {}
        assert received[-1][0] == "event"
        assert received[-1][1]["kind"] == "session_start"
        print("OK sessionStart")

        out = call("beforeSubmitPrompt", {**base, "prompt": "hi", "attachments": []}, env)
        assert out == {"continue": True}
        assert received[-1][1]["kind"] == "prompt_submit"
        assert received[-1][1]["prompt"] == "hi"
        print("OK beforeSubmitPrompt")

        out = call("afterAgentResponse", {**base, "text": "hello back"}, env)
        assert out == {}
        assert received[-1][1]["kind"] == "agent_response"
        print("OK afterAgentResponse")

        out = call("afterAgentThought", {**base, "text": "thinking..."}, env)
        assert out == {}
        assert received[-1][1]["kind"] == "agent_thought"
        print("OK afterAgentThought")

        approve_response = {"permission": "allow"}
        out = call("beforeShellExecution", {**base, "command": "ls", "cwd": "/tmp", "sandbox": False}, env)
        assert out == {"permission": "allow"}, out
        assert received[-1][0] == "approve"
        assert received[-1][1]["tool"] == "Shell"
        assert received[-1][1]["command"] == "ls"
        print("OK beforeShellExecution allow")

        approve_response = {"permission": "deny", "user_message": "no", "agent_message": "denied"}
        out = call("beforeShellExecution", {**base, "command": "rm -rf /tmp/x", "cwd": "/tmp", "sandbox": False}, env)
        assert out["permission"] == "deny"
        assert out["user_message"] == "no"
        print("OK beforeShellExecution deny")

        approve_response = {"permission": "allow"}
        out = call("beforeMCPExecution", {**base, "tool_name": "myTool", "tool_input": '{"x":1}'}, env)
        assert out == {"permission": "allow"}, out
        assert received[-1][1]["tool"] == "MCP:myTool"
        print("OK beforeMCPExecution")

        out = call("beforeReadFile", {**base, "file_path": "/tmp/normal.txt", "content": "hi"}, env)
        assert out == {"permission": "allow"}
        # should NOT have hit the bridge for non-sensitive
        assert received[-1][0] != "approve" or "normal.txt" not in str(received[-1][1])
        print("OK beforeReadFile non-sensitive (local allow)")

        approve_response = {"permission": "deny"}
        out = call("beforeReadFile", {**base, "file_path": "/tmp/.env", "content": "SECRET=x"}, env)
        assert out["permission"] == "deny"
        assert received[-1][0] == "approve"
        assert ".env" in received[-1][1]["summary"]
        print("OK beforeReadFile sensitive (asks bridge)")

        out = call("stop", {**base, "status": "completed", "loop_count": 0}, env)
        assert out == {}
        print("OK stop empty queue")

        followup_queue["c1"] = ["next thing to do"]
        out = call("stop", {**base, "status": "completed", "loop_count": 0}, env)
        assert out == {"followup_message": "next thing to do"}
        print("OK stop with followup")

        # bridge offline -> approval fail-closed, mirror fail-open
        stop_server(loop, runner)
        time.sleep(0.3)

        out = call("beforeShellExecution", {**base, "command": "echo x", "cwd": "/tmp", "sandbox": False}, env)
        assert out.get("permission") == "deny", out
        assert "offline" in out.get("user_message", "").lower(), out
        print("OK bridge-offline -> shell deny (fail-closed)")

        out = call("afterAgentResponse", {**base, "text": "x"}, env)
        assert out == {}
        print("OK bridge-offline -> mirror noop (fail-open)")

        out = call("stop", {**base, "status": "completed", "loop_count": 0}, env)
        assert out == {}
        print("OK bridge-offline -> stop noop (fail-open)")

    finally:
        try:
            stop_server(loop, runner)
        except Exception:
            pass

    print("\nALL RUNNER TESTS PASSED")


if __name__ == "__main__":
    main()
