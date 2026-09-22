"""Run one queued attempt with Claude Code and persist its result."""

from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import subprocess
from typing import Any, Mapping

from .codex_runner import (
    SENSITIVE_CHILD_ENV,
    _load_attempt,
    _prompt,
    _record_observation,
    _result_schema,
    _set_attempt,
    utcnow,
)
from .delivery import create_delivery
from .files import atomic_write_bytes, ensure_private_dir
from .results import FrozenResultConflict, ResultValidationError, collect_result
from .storage import Store


def _parse_claude_events(
    store: Store, attempt_id: str, stdout: str
) -> tuple[str | None, dict[str, Any] | None, list[str]]:
    native_session_id = None
    result_payload = None
    errors: list[str] = []
    for number, line in enumerate(stdout.splitlines(), 1):
        try:
            event = json.loads(line)
            if not isinstance(event, dict) or not isinstance(event.get("type"), str):
                raise ValueError("event must be an object with a string type")
        except (json.JSONDecodeError, ValueError) as exc:
            event = {"type": "bridge.invalid_jsonl", "line": number, "error": str(exc), "raw": line[:4096]}
            errors.append(f"invalid JSONL event at line {number}")
        event_type = event["type"]
        if event_type == "system" and event.get("subtype") == "thinking_tokens":
            continue
        _record_observation(store, attempt_id, event)
        if isinstance(event.get("session_id"), str):
            native_session_id = event["session_id"]
        if event_type == "result":
            if event.get("subtype") != "success" or event.get("is_error"):
                errors.append(str(event.get("result") or event.get("subtype") or "Claude failed"))
            structured = event.get("structured_output")
            if isinstance(structured, dict):
                result_payload = structured
            elif isinstance(event.get("result"), str):
                try:
                    candidate = json.loads(event["result"])
                    if isinstance(candidate, dict):
                        result_payload = candidate
                except json.JSONDecodeError:
                    pass
    if result_payload is None and not errors:
        errors.append("Claude result event has no structured output")
    return native_session_id, result_payload, errors


def run_claude_attempt(
    store: Store,
    attempt_id: str,
    *,
    executable: str = "claude",
    timeout_seconds: float = 300,
    environ: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    row = _load_attempt(store, attempt_id)
    control = ensure_private_dir(row["control_path"])
    output = ensure_private_dir(Path(row["exchange_path"]) / "outputs")
    result_path = output / "result.json"
    schema = _result_schema(row)
    schema.pop("$schema", None)
    command = [
        executable, "-p", "--output-format", "stream-json", "--verbose",
        "--permission-mode", "plan", "--tools", "", "--safe-mode",
        "--no-session-persistence", "--json-schema",
        json.dumps(schema, separators=(",", ":")),
    ]
    child_env = dict(os.environ if environ is None else environ)
    for key in SENSITIVE_CHILD_ENV:
        child_env.pop(key, None)
    _set_attempt(store, attempt_id, "starting", submission_status="submitting")
    try:
        process = subprocess.Popen(
            command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, cwd=row["project_root"], env=child_env, start_new_session=True,
        )
    except OSError as exc:
        _set_attempt(store, attempt_id, "failed", submission_status="rejected", error=str(exc))
        raise
    _set_attempt(
        store, attempt_id, "working", submission_status="submitted",
        process_handle_json=json.dumps({"pid": process.pid, "started_at": utcnow()}),
    )
    try:
        stdout, stderr = process.communicate(_prompt(row), timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGTERM)
        try:
            stdout, stderr = process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            stdout, stderr = process.communicate()
        _record_observation(store, attempt_id, {"type": "bridge.timeout", "timeout_seconds": timeout_seconds})
        _set_attempt(store, attempt_id, "failed", error=f"Claude timed out after {timeout_seconds}s")
        return {"attempt_id": attempt_id, "status": "failed", "error": "timeout"}
    atomic_write_bytes(control / "claude.stdout.jsonl", stdout.encode())
    atomic_write_bytes(control / "claude.stderr.log", stderr[-262144:].encode())
    native_session_id, payload, errors = _parse_claude_events(store, attempt_id, stdout)
    if native_session_id:
        with store.transaction(immediate=True) as connection:
            connection.execute(
                "UPDATE agent_sessions SET native_ref_json=?,updated_at=? WHERE session_id=?",
                (json.dumps({"session_id": native_session_id}), utcnow(), row["session_id"]),
            )
    if process.returncode != 0 or errors or payload is None:
        details = errors.copy()
        if process.returncode != 0 and stderr.strip():
            details.append(stderr.strip())
        detail = "; ".join(details) or f"exit {process.returncode}"
        _set_attempt(store, attempt_id, "failed", error=detail[:4096])
        return {"attempt_id": attempt_id, "status": "failed", "error": detail}
    atomic_write_bytes(result_path, (json.dumps(payload, ensure_ascii=False) + "\n").encode())
    try:
        collected = collect_result(
            result_path=result_path, output_root=output, control_attempt=control,
            task_id=row["task_id"], attempt_id=attempt_id,
            context_version=row["context_version"],
        )
    except (OSError, ResultValidationError, FrozenResultConflict) as exc:
        _set_attempt(store, attempt_id, "failed", error=str(exc))
        return {"attempt_id": attempt_id, "status": "failed", "error": str(exc)}
    result_status = collected["payload"]["status"]
    terminal = "done" if result_status == "completed" else "failed"
    _set_attempt(
        store, attempt_id, terminal, result_path=collected["path"],
        result_hash=collected["sha256"], result_status=result_status, error=None,
    )
    with store.transaction(immediate=True) as connection:
        connection.execute(
            "UPDATE tasks SET aggregate_status=?,updated_at=? WHERE task_id=?",
            (result_status, utcnow(), row["task_id"]),
        )
    delivery = create_delivery(store, task_id=row["task_id"], attempt_id=attempt_id)
    return {
        "task_id": row["task_id"], "attempt_id": attempt_id, "status": terminal,
        "result_status": result_status, "native_session_id": native_session_id,
        "delivery": delivery,
    }
