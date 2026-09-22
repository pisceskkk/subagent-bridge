"""Control-plane operations that are independent of a concrete agent runner."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import stat
import uuid
from typing import Any

from .files import (
    atomic_write_json,
    attempt_exchange_dir,
    ensure_private_dir,
    snapshot_inputs,
)
from .storage import Store


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_id(prefix: str) -> str:
    return prefix + uuid.uuid4().hex


def prepare_delegation(
    store: Store,
    *,
    state_dir: Path | str,
    workspace: Path | str,
    task_file: Path | str,
    context_file: Path | str | None,
    agent_kind: str,
    delivery_mode: str,
    codex_thread_id: str,
    codex_session_id: str,
    app_server_instance_id: str,
    app_server_socket: str,
    app_server_host_id: str | None = None,
) -> dict[str, Any]:
    if delivery_mode not in {"idle", "immediate"}:
        raise ValueError(f"unsupported delivery mode: {delivery_mode}")
    if not codex_thread_id or not codex_session_id:
        raise ValueError("Codex thread and session identities are required")
    workspace_root = Path(workspace).expanduser().resolve()
    state_root = ensure_private_dir(state_dir)
    task_id = new_id("task_")
    attempt_id = new_id("att_")
    session_id = new_id("ses_")
    control = ensure_private_dir(
        state_root / "tasks" / task_id / "attempts" / attempt_id
    )
    exchange = attempt_exchange_dir(workspace_root, task_id, attempt_id)
    snapshot = snapshot_inputs(exchange, task_file, context_file)
    created_at = utcnow()

    manifest = {
        "schema_version": 1,
        "task_id": task_id,
        "attempt_id": attempt_id,
        "session_id": session_id,
        "agent_kind": agent_kind,
        "session_mode": "new",
        "context_version": snapshot["context_version"],
        "created_at": created_at,
        "control_path": str(control),
        "exchange_path": str(exchange),
        "files": {
            "task": {"location": "control", "path": "task.json"},
            "launch": {"location": "control", "path": "launch.json"},
            "result_frozen": {
                "location": "control",
                "path": "result.collected.json",
            },
            "task_input": {"location": "exchange", "path": "inputs/task.md"},
            "context_input": {
                "location": "exchange",
                "path": "inputs/context.md",
            },
            "result": {"location": "exchange", "path": "outputs/result.json"},
        },
    }
    task_record = {
        "schema_version": 1,
        "task_id": task_id,
        "attempt_id": attempt_id,
        "parent": {
            "codex_thread_id": codex_thread_id,
            "codex_session_id": codex_session_id,
            "app_server_instance_id": app_server_instance_id,
        },
        "agent_kind": agent_kind,
        "session_mode": "new",
        "delivery_mode": delivery_mode,
        "context_version": snapshot["context_version"],
        "created_at": created_at,
    }
    launch_record = {
        "schema_version": 1,
        "intent": "queued",
        "task_id": task_id,
        "attempt_id": attempt_id,
        "agent_kind": agent_kind,
        "recorded_at": created_at,
    }
    atomic_write_json(control / "manifest.json", manifest)
    atomic_write_json(exchange / "manifest.json", manifest)
    atomic_write_json(control / "task.json", task_record)
    atomic_write_json(control / "launch.json", launch_record)

    with store.transaction(immediate=True) as connection:
        connection.execute(
            "INSERT INTO app_server_instances(instance_id,socket_path,host_id,generation,status,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?) ON CONFLICT(instance_id) DO UPDATE SET "
            "status=excluded.status, updated_at=excluded.updated_at",
            (
                app_server_instance_id,
                app_server_socket,
                app_server_host_id,
                1,
                "connected",
                created_at,
                created_at,
            ),
        )
        parent = connection.execute(
            "SELECT parent_id,binding_generation,codex_session_id FROM parent_sessions "
            "WHERE codex_thread_id=? AND app_server_instance_id=? "
            "ORDER BY binding_generation DESC LIMIT 1",
            (codex_thread_id, app_server_instance_id),
        ).fetchone()
        if parent and parent["codex_session_id"] == codex_session_id:
            parent_id = parent["parent_id"]
            parent_generation = parent["binding_generation"]
            connection.execute(
                "UPDATE parent_sessions SET status='active', updated_at=? WHERE parent_id=?",
                (created_at, parent_id),
            )
        else:
            parent_id = new_id("par_")
            parent_generation = parent["binding_generation"] + 1 if parent else 1
            connection.execute(
                "INSERT INTO parent_sessions(parent_id,codex_thread_id,codex_session_id,"
                "app_server_instance_id,binding_generation,cwd,status,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    parent_id,
                    codex_thread_id,
                    codex_session_id,
                    app_server_instance_id,
                    parent_generation,
                    str(workspace_root),
                    "active",
                    created_at,
                    created_at,
                ),
            )
        connection.execute(
            "INSERT INTO agent_sessions(session_id,agent_kind,workspace_root,created_at,updated_at) "
            "VALUES(?,?,?,?,?)",
            (session_id, agent_kind, str(workspace_root), created_at, created_at),
        )
        connection.execute(
            "INSERT INTO tasks(task_id,parent_id,session_id,agent_kind,session_mode,delivery_mode,"
            "context_version,context_hash,aggregate_status,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (
                task_id,
                parent_id,
                session_id,
                agent_kind,
                "new",
                delivery_mode,
                snapshot["context_version"],
                snapshot["task_sha256"],
                "created",
                created_at,
                created_at,
            ),
        )
        connection.execute(
            "INSERT INTO attempts(attempt_id,task_id,session_id,status,submission_status,control_path,"
            "exchange_path,project_root,launch_json,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (
                attempt_id,
                task_id,
                session_id,
                "queued",
                "pending",
                str(control),
                str(exchange),
                str(workspace_root),
                json.dumps(launch_record, separators=(",", ":")),
                created_at,
                created_at,
            ),
        )
        exchange_root = exchange.parents[2]
        connection.execute(
            "INSERT INTO project_workspaces(project_root,exchange_root,created_at,updated_at) "
            "VALUES(?,?,?,?) ON CONFLICT(project_root) DO UPDATE SET "
            "exchange_root=excluded.exchange_root,updated_at=excluded.updated_at",
            (str(workspace_root), str(exchange_root), created_at, created_at),
        )

    return {
        "task_id": task_id,
        "attempt_id": attempt_id,
        "session_id": session_id,
        "parent_id": parent_id,
        "parent_generation": parent_generation,
        "status": "queued",
        "control_path": str(control),
        "exchange_path": str(exchange),
        "context_version": snapshot["context_version"],
    }


def show_task(store: Store, task_id: str) -> dict[str, Any] | None:
    row = store.fetchone(
        "SELECT t.*,p.codex_thread_id,p.codex_session_id,p.app_server_instance_id,"
        "a.attempt_id,a.status AS attempt_status,a.submission_status,a.control_path,a.exchange_path,"
        "a.result_path,a.result_hash,a.result_status,a.error "
        "FROM tasks t JOIN parent_sessions p ON p.parent_id=t.parent_id "
        "LEFT JOIN attempts a ON a.attempt_id=(SELECT attempt_id FROM attempts "
        "WHERE task_id=t.task_id ORDER BY created_at DESC LIMIT 1) WHERE t.task_id=?",
        (task_id,),
    )
    return dict(row) if row else None


def app_server_instance_id(socket_path: Path | str, host_id: str | None) -> str:
    supplied = Path(socket_path).expanduser()
    if supplied.is_symlink() or not supplied.exists():
        raise ValueError(f"app-server socket is unavailable: {supplied}")
    path = supplied.resolve()
    metadata = path.stat(follow_symlinks=False)
    if not stat.S_ISSOCK(metadata.st_mode):
        raise ValueError(f"app-server socket is unavailable: {path}")
    material = f"{host_id or ''}\0{path}\0{metadata.st_dev}\0{metadata.st_ino}\0{metadata.st_ctime_ns}"
    return "app_" + hashlib.sha256(material.encode()).hexdigest()[:24]
