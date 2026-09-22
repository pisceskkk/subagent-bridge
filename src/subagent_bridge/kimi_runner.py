"""Run one queued attempt with Kimi Code and persist its result."""

from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import shutil
import subprocess
from typing import Any, Mapping

from .codex_runner import (
    SENSITIVE_CHILD_ENV,
    _load_attempt,
    _record_observation,
    _set_attempt,
    utcnow,
)
from .delivery import create_delivery
from .files import atomic_write_bytes, ensure_private_dir
from .results import FrozenResultConflict, ResultValidationError, collect_result
from .storage import Store


def _kimi_prompt(row: Mapping[str, Any]) -> str:
    exchange = Path(row["exchange_path"])
    task = (exchange / "inputs/task.md").read_text(encoding="utf-8")
    context_path = exchange / "inputs/context.md"
    context = context_path.read_text(encoding="utf-8") if context_path.exists() else ""
    template = {
        "schema_version": 1,
        "task_id": row["task_id"],
        "attempt_id": row["attempt_id"],
        "context_version": row["context_version"],
        "status": "completed|failed|blocked|unavailable",
        "summary": "string",
        "artifacts": [],
        "verification": {"performed": [], "limitations": []},
        "needs_parent": [],
    }
    return (
        "Complete this delegated task without modifying repository files. Return exactly one JSON "
        "object, with no Markdown fence or surrounding prose, matching this template (replace the "
        f"status enum with one value):\n{json.dumps(template, ensure_ascii=False)}\n\n"
        f"TASK\n{task}\n\nCONTEXT\n{context or '(none)'}\n"
    )


def _parse_kimi_events(
    store: Store, attempt_id: str, stdout: str
) -> tuple[str | None, dict[str, Any] | None, list[str]]:
    native_session_id = None
    payload = None
    errors: list[str] = []
    for number, line in enumerate(stdout.splitlines(), 1):
        try:
            event = json.loads(line)
            if not isinstance(event, dict) or not isinstance(event.get("role"), str):
                raise ValueError("event must be an object with a string role")
        except (json.JSONDecodeError, ValueError) as exc:
            event = {"type": "bridge.invalid_jsonl", "line": number, "error": str(exc), "raw": line[:4096]}
            errors.append(f"invalid JSONL event at line {number}")
        _record_observation(store, attempt_id, event)
        if event.get("type") == "session.resume_hint" and isinstance(event.get("session_id"), str):
            native_session_id = event["session_id"]
        if event.get("role") == "assistant" and isinstance(event.get("content"), str):
            try:
                candidate = json.loads(event["content"])
                if isinstance(candidate, dict):
                    payload = candidate
            except json.JSONDecodeError:
                pass
    if payload is None and not errors:
        errors.append("Kimi assistant event has no JSON result object")
    return native_session_id, payload, errors


def run_kimi_attempt(
    store: Store,
    attempt_id: str,
    *,
    executable: str | None = None,
    timeout_seconds: float = 300,
    environ: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    row = _load_attempt(store, attempt_id)
    executable = executable or shutil.which("kimi") or str(Path.home() / ".kimi-code/bin/kimi")
    control = ensure_private_dir(row["control_path"])
    output = ensure_private_dir(Path(row["exchange_path"]) / "outputs")
    result_path = output / "result.json"
    command = [executable, "--prompt", _kimi_prompt(row), "--output-format", "stream-json"]
    child_env = dict(os.environ if environ is None else environ)
    for key in SENSITIVE_CHILD_ENV:
        child_env.pop(key, None)
    _set_attempt(store, attempt_id, "starting", submission_status="submitting")
    try:
        process = subprocess.Popen(
            command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            cwd=row["project_root"], env=child_env, start_new_session=True,
        )
    except OSError as exc:
        _set_attempt(store, attempt_id, "failed", submission_status="rejected", error=str(exc))
        raise
    _set_attempt(
        store, attempt_id, "working", submission_status="submitted",
        process_handle_json=json.dumps({"pid": process.pid, "started_at": utcnow()}),
    )
    try:
        stdout, stderr = process.communicate(timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGTERM)
        try:
            stdout, stderr = process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            stdout, stderr = process.communicate()
        _record_observation(store, attempt_id, {"type": "bridge.timeout", "timeout_seconds": timeout_seconds})
        _set_attempt(store, attempt_id, "failed", error=f"Kimi timed out after {timeout_seconds}s")
        return {"attempt_id": attempt_id, "status": "failed", "error": "timeout"}
    atomic_write_bytes(control / "kimi.stdout.jsonl", stdout.encode())
    atomic_write_bytes(control / "kimi.stderr.log", stderr[-262144:].encode())
    native_session_id, payload, errors = _parse_kimi_events(store, attempt_id, stdout)
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
            task_id=row["task_id"], attempt_id=attempt_id, context_version=row["context_version"],
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
