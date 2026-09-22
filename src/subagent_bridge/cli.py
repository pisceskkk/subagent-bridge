"""Command-line interface for the Bridge control plane."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys

from .service import app_server_instance_id, prepare_delegation, show_task
from .storage import Store


def default_state_dir() -> Path:
    root = os.environ.get("XDG_STATE_HOME")
    return Path(root) / "subagent-bridge" if root else Path.home() / ".local/state/subagent-bridge"


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="sab")
    root.add_argument("--state-dir", type=Path, default=default_state_dir())
    commands = root.add_subparsers(dest="command", required=True)
    commands.add_parser("init", help="initialize the control-plane database")
    delegate = commands.add_parser("delegate", help="durably queue a delegated task")
    delegate.add_argument("--agent", required=True)
    delegate.add_argument("--task-file", type=Path, required=True)
    delegate.add_argument("--context-file", type=Path)
    delegate.add_argument("--delivery", choices=("idle", "immediate"), default="idle")
    delegate.add_argument("--workspace", type=Path, default=Path.cwd())
    delegate.add_argument(
        "--app-server-socket",
        type=Path,
        default=Path.home() / ".codex/app-server-control/app-server-control.sock",
    )
    show = commands.add_parser("show", help="show one task")
    show.add_argument("task_id")
    return root


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    store = Store(args.state_dir / "bridge.sqlite3")
    store.migrate()
    if args.command == "init":
        print(json.dumps({"ok": True, "state_dir": str(args.state_dir)}))
        return 0
    if args.command == "show":
        result = show_task(store, args.task_id)
        if result is None:
            print(f"unknown task: {args.task_id}", file=sys.stderr)
            return 1
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    thread_id = os.environ.get("CODEX_THREAD_ID")
    session_id = os.environ.get("CODEX_SESSION_ID")
    if not thread_id or not session_id:
        print(
            "sab delegate must run inside a Codex thread with CODEX_THREAD_ID and CODEX_SESSION_ID",
            file=sys.stderr,
        )
        return 2
    host_id = os.environ.get("CODEX_REMOTE_HOST_ID") or os.environ.get("CODEX_HOST_ID")
    try:
        result = prepare_delegation(
            store,
            state_dir=args.state_dir,
            workspace=args.workspace,
            task_file=args.task_file,
            context_file=args.context_file,
            agent_kind=args.agent,
            delivery_mode=args.delivery,
            codex_thread_id=thread_id,
            codex_session_id=session_id,
            app_server_instance_id=app_server_instance_id(args.app_server_socket, host_id),
            app_server_socket=str(args.app_server_socket.expanduser().resolve()),
            app_server_host_id=host_id,
        )
    except (OSError, ValueError) as exc:
        print(f"delegate failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

