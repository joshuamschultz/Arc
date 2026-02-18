"""Live messaging demo — two agents communicating through shared storage.

Boots both agents' messaging modules against team/shared/, registers
entities, and exercises the messaging tools to prove end-to-end communication.

Usage:
    cd /Users/joshschultz/AI/Arc
    source .venv/bin/activate
    python team/serve_demo.py
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock

# Ensure packages are importable
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "packages" / "arcagent"))

from arcagent.modules.messaging import MessagingModule


def _make_ctx(workspace: Path) -> MagicMock:
    """Create a mock ModuleContext (no full agent needed)."""
    ctx = MagicMock()
    ctx.bus = MagicMock()
    ctx.bus.subscribe = MagicMock()
    ctx.tool_registry = MagicMock()
    ctx.tool_registry.register = MagicMock()
    ctx.workspace = workspace
    ctx.config = MagicMock()
    ctx.config.agent.name = workspace.name
    return ctx


def _find_tool(ctx: MagicMock, name: str):
    """Find a registered tool by name."""
    for call in ctx.tool_registry.register.call_args_list:
        tool = call.args[0]
        if tool.name == name:
            return tool
    raise ValueError(f"Tool '{name}' not found")


def _print(label: str, data: str) -> None:
    """Pretty-print JSON tool output."""
    parsed = json.loads(data)
    print(f"\n  [{label}] {json.dumps(parsed, indent=2)}")


async def main() -> None:
    team_root = Path(__file__).resolve().parent / "shared"
    team_root.mkdir(parents=True, exist_ok=True)

    print("=" * 60)
    print("  ArcTeam Messaging — Live Demo")
    print("=" * 60)

    # --- Boot Josh's agent ---
    josh_ws = Path(__file__).resolve().parent / "my_agent"
    josh_module = MessagingModule(
        config={
            "enabled": True,
            "entity_id": "agent://my_agent",
            "entity_name": "Josh",
            "roles": ["leader", "architect"],
            "capabilities": ["planning", "code-review", "delegation"],
            "poll_interval_seconds": 1.0,
            "auto_ack": True,
            "audit_hmac_key": "arcteam-local-dev",
            "max_messages_per_poll": 20,
        },
        team_config=MagicMock(root=str(team_root)),
        telemetry=MagicMock(),
        workspace=josh_ws,
    )
    josh_ctx = _make_ctx(josh_ws)
    await josh_module.startup(josh_ctx)
    print("\n[+] Josh (agent://my_agent) — ONLINE")

    # --- Boot Brad's agent ---
    brad_ws = Path(__file__).resolve().parent / "brad_agent"
    brad_module = MessagingModule(
        config={
            "enabled": True,
            "entity_id": "agent://brad_agent",
            "entity_name": "Brad",
            "roles": ["executor", "ops"],
            "capabilities": ["task-execution", "file-management"],
            "poll_interval_seconds": 1.0,
            "auto_ack": True,
            "audit_hmac_key": "arcteam-local-dev",
            "max_messages_per_poll": 20,
        },
        team_config=MagicMock(root=str(team_root)),
        telemetry=MagicMock(),
        workspace=brad_ws,
    )
    brad_ctx = _make_ctx(brad_ws)
    await brad_module.startup(brad_ctx)
    print("[+] Brad (agent://brad_agent) — ONLINE")

    # --- Step 1: Discover teammates ---
    print("\n" + "-" * 60)
    print("  Step 1: Entity Discovery")
    print("-" * 60)

    josh_list = _find_tool(josh_ctx, "messaging_list_entities")
    result = await josh_list.execute()
    entities = json.loads(result)
    for e in entities:
        print(f"  - {e['id']} ({e['name']}) roles={e['roles']} caps={e['capabilities']}")

    # --- Step 2: Josh sends Brad a task ---
    print("\n" + "-" * 60)
    print("  Step 2: Josh sends Brad a task")
    print("-" * 60)

    josh_send = _find_tool(josh_ctx, "messaging_send")
    result = await josh_send.execute(
        to="agent://brad_agent",
        body="Hey Brad, please review the PR for the messaging module and run the tests.",
        msg_type="task",
        priority="high",
        subject="Review messaging module PR",
        action_required=True,
    )
    sent = json.loads(result)
    print(f"  Josh sent message {sent['id']} (thread: {sent['thread_id']})")

    # Brief pause for file writes to flush
    await asyncio.sleep(0.5)

    # --- Step 3: Brad checks inbox ---
    print("\n" + "-" * 60)
    print("  Step 3: Brad checks inbox")
    print("-" * 60)

    brad_inbox = _find_tool(brad_ctx, "messaging_check_inbox")
    result = await brad_inbox.execute()
    inbox = json.loads(result)
    print(f"  Brad has {inbox['unread']} unread message(s)")
    for stream, msgs in inbox.get("streams", {}).items():
        for m in msgs:
            print(f"  - [{m['priority']}] From {m['sender']}: {m['subject']}")
            print(f"    Body: {m['body']}")
            print(f"    Action required: {m['action_required']}")

    # --- Step 4: Brad replies ---
    print("\n" + "-" * 60)
    print("  Step 4: Brad replies to Josh")
    print("-" * 60)

    brad_send = _find_tool(brad_ctx, "messaging_send")
    result = await brad_send.execute(
        to="agent://my_agent",
        body="On it. Running tests now. Will report back with results.",
        msg_type="ack",
        priority="normal",
        subject="Re: Review messaging module PR",
        reply_to=sent["id"],
    )
    reply = json.loads(result)
    print(f"  Brad replied with message {reply['id']} (thread: {reply['thread_id']})")

    await asyncio.sleep(0.5)

    # --- Step 5: Josh checks inbox ---
    print("\n" + "-" * 60)
    print("  Step 5: Josh checks inbox")
    print("-" * 60)

    josh_inbox = _find_tool(josh_ctx, "messaging_check_inbox")
    result = await josh_inbox.execute()
    inbox = json.loads(result)
    print(f"  Josh has {inbox['unread']} unread message(s)")
    for stream, msgs in inbox.get("streams", {}).items():
        for m in msgs:
            print(f"  - [{m['priority']}] From {m['sender']}: {m['subject']}")
            print(f"    Body: {m['body']}")

    # --- Step 6: Create a channel and broadcast ---
    print("\n" + "-" * 60)
    print("  Step 6: Create #ops channel and broadcast")
    print("-" * 60)

    from arcteam.types import Channel

    await josh_module._svc.create_channel(Channel(
        name="ops",
        description="Operations channel for team coordination",
        members=["agent://my_agent", "agent://brad_agent"],
    ))
    print("  Created channel #ops with both agents")

    josh_channels = _find_tool(josh_ctx, "messaging_list_channels")
    result = await josh_channels.execute()
    channels = json.loads(result)
    for ch in channels:
        print(f"  - #{ch['name']}: {ch['description']} (members: {ch['members']})")

    # Josh sends to channel
    result = await josh_send.execute(
        to="channel://ops",
        body="Team standup: messaging module is live. All tests passing. Ready for integration.",
        msg_type="info",
        priority="normal",
        subject="Daily standup",
    )
    channel_msg = json.loads(result)
    print(f"  Josh posted to #ops: message {channel_msg['id']}")

    await asyncio.sleep(0.5)

    # Brad reads channel
    result = await brad_inbox.execute()
    inbox = json.loads(result)
    print(f"  Brad polls inbox: {inbox['unread']} new message(s)")
    for stream, msgs in inbox.get("streams", {}).items():
        for m in msgs:
            print(f"  - [#{stream}] {m['sender']}: {m['body'][:80]}")

    # --- Step 7: Brad sends results ---
    print("\n" + "-" * 60)
    print("  Step 7: Brad sends test results back")
    print("-" * 60)

    result = await brad_send.execute(
        to="agent://my_agent",
        body="All 158 arcteam tests passing. 1073 arcagent unit tests passing. "
             "21 messaging module tests passing. LGTM for merge.",
        msg_type="result",
        priority="high",
        subject="Test Results: messaging module",
        action_required=False,
    )
    results_msg = json.loads(result)
    print(f"  Brad sent results: message {results_msg['id']}")

    await asyncio.sleep(0.5)

    # Josh reads results
    result = await josh_inbox.execute()
    inbox = json.loads(result)
    print(f"  Josh polls inbox: {inbox['unread']} new message(s)")
    for stream, msgs in inbox.get("streams", {}).items():
        for m in msgs:
            print(f"  - [{m['msg_type']}] {m['sender']}: {m['body'][:120]}")

    # --- Step 8: Read full thread ---
    print("\n" + "-" * 60)
    print("  Step 8: Read full conversation thread")
    print("-" * 60)

    josh_thread = _find_tool(josh_ctx, "messaging_read_thread")
    # The thread is on Brad's DM stream
    stream_name = "arc.agent.brad_agent"
    result = await josh_thread.execute(
        stream=stream_name,
        thread_id=sent["thread_id"],
    )
    thread = json.loads(result)
    if isinstance(thread, list):
        print(f"  Thread {sent['thread_id']} ({len(thread)} messages):")
        for i, m in enumerate(thread, 1):
            print(f"  {i}. [{m['msg_type']}] {m['sender']}: {m['body'][:100]}")
    else:
        print(f"  Thread result: {json.dumps(thread, indent=2)}")

    # --- Cleanup ---
    print("\n" + "-" * 60)
    print("  Shutting down...")
    print("-" * 60)

    await josh_module.shutdown()
    print("  [x] Josh — offline")
    await brad_module.shutdown()
    print("  [x] Brad — offline")

    # Show what's on disk
    print("\n" + "-" * 60)
    print("  Shared storage (team/shared/):")
    print("-" * 60)
    for p in sorted(team_root.rglob("*")):
        if p.is_file():
            rel = p.relative_to(team_root)
            size = p.stat().st_size
            print(f"  {rel} ({size} bytes)")

    print("\n" + "=" * 60)
    print("  Demo complete. Agents communicated successfully!")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
