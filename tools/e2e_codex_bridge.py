#!/usr/bin/env python3
"""Exercise a real Codex child, result delivery, parent wake, and acknowledgement."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import queue
import time

from subagent_bridge.app_server import CodexAppServerClient
from subagent_bridge.claude_runner import run_claude_attempt
from subagent_bridge.codex_runner import run_codex_attempt
from subagent_bridge.delivery import acknowledge, dispatch_one
from subagent_bridge.gemini_runner import run_gemini_attempt
from subagent_bridge.kimi_runner import run_kimi_attempt
from subagent_bridge.service import app_server_instance_id, prepare_delegation
from subagent_bridge.storage import Store


def wait_for_turn(client: CodexAppServerClient, thread_id: str, turn_id: str, timeout: float) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            event = client.notifications.get(timeout=max(0.01, deadline - time.monotonic()))
        except queue.Empty as exc:
            raise TimeoutError(f"timed out waiting for turn {turn_id}") from exc
        params = event.get("params", {})
        if (
            event.get("method") == "turn/completed"
            and params.get("threadId") == thread_id
            and params.get("turn", {}).get("id") == turn_id
        ):
            return event
    raise TimeoutError(f"timed out waiting for turn {turn_id}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--socket",
        type=Path,
        default=Path.home() / ".codex/app-server-control/app-server-control.sock",
    )
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    parser.add_argument("--timeout", type=float, default=300)
    parser.add_argument(
        "--agent", choices=("codex", "claude", "kimi", "gemini"), default="codex"
    )
    parser.add_argument("--delivery", choices=("idle", "immediate"), default="idle")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    workspace = args.workspace.resolve()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    state = workspace / ".subagent-bridge" / "e2e" / stamp
    state.mkdir(parents=True, exist_ok=False)
    task_file = state / "child-task.md"
    task_file.write_text(
        "Compute 37 * 43. Return the number and a one-sentence arithmetic check. "
        "Do not modify repository files.",
        encoding="utf-8",
    )
    store = Store(state / "bridge.sqlite3")
    store.migrate()
    socket_path = str(args.socket.expanduser().resolve())
    client = CodexAppServerClient.connect(socket_path, timeout=args.timeout)
    report: dict = {"state_dir": str(state)}
    try:
        client.initialize(timeout=args.timeout)
        parent = client.request(
            "thread/start",
            {
                "cwd": str(workspace),
                "approvalPolicy": "never",
                "sandbox": "workspace-write",
                "serviceName": "subagent_bridge_e2e",
            },
            args.timeout,
        )["thread"]
        parent_id = parent["id"]
        report["parent_thread_id"] = parent_id
        active_turn_id = None
        if args.delivery == "idle":
            ready = client.request(
                "turn/start",
                {
                    "threadId": parent_id,
                    "input": [{"type": "text", "text": "Bridge E2E parent setup. Reply exactly PARENT_READY."}],
                },
                args.timeout,
            )["turn"]
            wait_for_turn(client, parent_id, ready["id"], args.timeout)
            report["parent_ready_turn_id"] = ready["id"]
        else:
            active = client.request(
                "turn/start",
                {
                    "threadId": parent_id,
                    "input": [{
                        "type": "text",
                        "text": "Run `sleep 30` in the terminal, then report any bridge handoff received during this turn.",
                    }],
                },
                args.timeout,
            )["turn"]
            active_turn_id = active["id"]
            report["parent_active_turn_id"] = active_turn_id

        prepared = prepare_delegation(
            store,
            state_dir=state,
            workspace=workspace,
            task_file=task_file,
            context_file=None,
            agent_kind=args.agent,
            delivery_mode=args.delivery,
            codex_thread_id=parent_id,
            codex_session_id="e2e:" + parent_id,
            app_server_instance_id=app_server_instance_id(socket_path, None),
            app_server_socket=socket_path,
        )
        report["task_id"] = prepared["task_id"]
        report["attempt_id"] = prepared["attempt_id"]
        runners = {
            "codex": run_codex_attempt,
            "claude": run_claude_attempt,
            "kimi": run_kimi_attempt,
            "gemini": run_gemini_attempt,
        }
        runner = runners[args.agent]
        child = runner(store, prepared["attempt_id"], timeout_seconds=args.timeout)
        report["child"] = child
        if child["status"] != "done":
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return 1
        delivery = child["delivery"]
        dispatched = dispatch_one(
            store,
            client,
            delivery["delivery_id"],
            timeout=args.timeout,
            expected_turn_id=active_turn_id,
        )
        report["dispatch"] = dispatched
        if dispatched["status"] != "submitted":
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return 1
        row = store.fetchone(
            "SELECT native_turn_id FROM deliveries WHERE delivery_id=?",
            (delivery["delivery_id"],),
        )
        wait_for_turn(client, parent_id, row["native_turn_id"], args.timeout)
        report["parent_wake_turn_id"] = row["native_turn_id"]
        report["acknowledgement"] = acknowledge(
            store,
            delivery_id=delivery["delivery_id"],
            receipt=delivery["receipt"],
            parent_id=prepared["parent_id"],
        )
        counts = {}
        with store.connect() as connection:
            for table in ("tasks", "attempts", "observations", "deliveries"):
                counts[table] = connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
        report["record_counts"] = counts
        report["parent_snapshot"] = client.request(
            "thread/read", {"threadId": parent_id, "includeTurns": True}, args.timeout
        )["thread"]
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    finally:
        client.close()


if __name__ == "__main__":
    raise SystemExit(main())
