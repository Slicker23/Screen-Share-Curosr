"""Smoke tests for the HTTP layer of the bridge.

No real Telegram. Uses aiohttp's TestClient + a stub Telegram bot.

Run: python bridge/test_http.py
"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

sys.path.insert(0, str(Path(__file__).parent))

from aiohttp.test_utils import TestClient, TestServer

from bridge import (
    BehaviorCfg,
    BridgeCfg,
    BridgeState,
    Config,
    TelegramCfg,
    build_web_app,
)
from state import State


def make_state(tmp: Path) -> BridgeState:
    cfg = Config(
        telegram=TelegramCfg(bot_token="x", allowed_user_ids=[1], notify_chat_id=1),
        bridge=BridgeCfg(host="127.0.0.1", port=0, secret="testsecret", approval_timeout=2),
        behavior=BehaviorCfg(allow_shell_patterns=["^ls$"], deny_shell_patterns=["^rm -rf /$"]),
    )
    db = State(tmp / "state.sqlite")
    s = BridgeState(cfg, db)
    # stub telegram app
    tg = MagicMock()
    tg.bot = MagicMock()
    tg.bot.send_message = AsyncMock(return_value=MagicMock(chat_id=1, message_id=42))
    tg.bot.edit_message_text = AsyncMock()
    s.tg = tg
    return s


async def run() -> None:
    with tempfile.TemporaryDirectory() as td:
        s = make_state(Path(td))
        web_app = build_web_app(s)
        async with TestClient(TestServer(web_app)) as client:
            hdr = {"Authorization": "Bearer testsecret"}

            # health
            r = await client.get("/health")
            assert r.status == 200, r.status
            assert (await r.json()) == {"ok": True}
            print("OK /health")

            # auth required
            r = await client.post("/event", json={"kind": "session_start"})
            assert r.status == 401
            print("OK /event auth required")

            # event: session_start
            r = await client.post("/event", headers=hdr, json={
                "kind": "session_start",
                "conversation_id": "conv-1",
                "project_root": "/tmp/proj",
            })
            assert r.status == 200
            print("OK /event session_start")

            # event: prompt_submit
            r = await client.post("/event", headers=hdr, json={
                "kind": "prompt_submit",
                "conversation_id": "conv-1",
                "project_root": "/tmp/proj",
                "prompt": "fix the bug",
            })
            assert r.status == 200
            print("OK /event prompt_submit")

            # next-followup empty
            r = await client.get("/next-followup?conversation_id=conv-1", headers=hdr)
            assert r.status == 200
            assert (await r.json()) == {}
            print("OK /next-followup empty")

            # enqueue + dequeue
            s.db.enqueue_followup("conv-1", "do this next")
            r = await client.get("/next-followup?conversation_id=conv-1", headers=hdr)
            assert r.status == 200
            assert (await r.json()) == {"followup_message": "do this next"}
            print("OK /next-followup dequeue")

            # approve: auto-allow via pattern
            r = await client.post("/approve", headers=hdr, json={
                "tool": "Shell",
                "conversation_id": "conv-1",
                "project_root": "/tmp/proj",
                "command": "ls",
                "cwd": "/tmp/proj",
            })
            assert r.status == 200
            assert (await r.json()) == {"permission": "allow"}
            print("OK /approve auto-allow")

            # approve: auto-deny via pattern
            r = await client.post("/approve", headers=hdr, json={
                "tool": "Shell",
                "conversation_id": "conv-1",
                "project_root": "/tmp/proj",
                "command": "rm -rf /",
                "cwd": "/",
            })
            assert r.status == 200
            body = await r.json()
            assert body["permission"] == "deny"
            print("OK /approve auto-deny")

            # approve: phone required, simulate phone tap by resolving the future from outside
            async def tap_after_send():
                # poll until we have a pending approval, then resolve
                for _ in range(50):
                    if s.pending:
                        ap = next(iter(s.pending.values()))
                        ap.future.set_result("allow")
                        return
                    await asyncio.sleep(0.05)
                raise RuntimeError("no pending approval appeared")

            tap_task = asyncio.create_task(tap_after_send())
            r = await client.post("/approve", headers=hdr, json={
                "tool": "Shell",
                "conversation_id": "conv-1",
                "project_root": "/tmp/proj",
                "command": "npm install",
                "cwd": "/tmp/proj",
            })
            await tap_task
            assert r.status == 200
            assert (await r.json()) == {"permission": "allow"}
            print("OK /approve phone-tap")

            # approve: timeout
            r = await client.post("/approve", headers=hdr, json={
                "tool": "Shell",
                "conversation_id": "conv-1",
                "project_root": "/tmp/proj",
                "command": "curl evil.com",
                "cwd": "/tmp/proj",
            })
            assert r.status == 200
            body = await r.json()
            assert body["permission"] == "deny", body
            print("OK /approve timeout")

            # patterns: add via state.db, check shell_decision
            s.db.add_pattern(r"^echo .*$", "allow")
            assert s.shell_decision("echo hi") == "allow"
            s.db.add_pattern(r"^sudo .*$", "deny")
            assert s.shell_decision("sudo rm") == "deny"
            print("OK runtime allow/deny patterns")

        s.db.close()

    print("\nALL TESTS PASSED")


if __name__ == "__main__":
    asyncio.run(run())
