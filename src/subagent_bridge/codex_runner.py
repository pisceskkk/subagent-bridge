"""Run one queued attempt with ``codex exec`` and persist its evidence."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import signal
import subprocess
import uuid
from typing import Any, Mapping, Sequence

from .delivery import create_delivery
from .files import atomic_write_json, ensure_private_dir
from .results import ResultValidationError, collect_result
from .storage import Store


SENSITIVE_CHILD_ENV = {
    "CODEX_THREAD_ID",
    "CODEX_SESSION_ID",
    "CODEX_REMOTE_PAYLOAD",
    "CODEX_APP_SERVER_SOCKET",
    "HERDR_ENV",
}


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _result_schema(row: Mapping[str, Any]) -> dict[str, Any]:
    string_array = {"type": "array", "items": {"type": "string"}}
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "additionalProperties": False,
        "required": [
            "schema_version", "task_id", "attempt_id", "context_version",
            "status", "summary", "artifacts", "verification", "needs_parent",
        ],
        "properties": {
            "schema_version": {"type": "integer", "const": 1},
            "task_id": {"type": "string", "const": row["task_id"]},
            "attempt_id": {"type": "string", "const": row["attempt_id"]},
            "context_version": {"type": "string", "const": row["context_version"]},
            "status": {"enum": ["completed", "failed", "blocked", "unavailable"]},
            "summary": {"type": "string"},
            "artifacts": string_array,
            "verification": {
                "type": "object",
                "additionalProperties": False,
                "required": ["performed", "limitations"],
                "properties": {"performed": string_array, "limitations": string_array},
            },
            "needs_parent": {"type": "array", "items": {"type": "string"}},
        },
    }


def _prompt(row: Mapping[str, Any]) -> str:
    exchange = Path(row["exchange_path"])
    task = (exchange / "inputs/task.md").read_text(encoding="utf-8")
    context_path = exchange / "inputs/context.md"
    context = context_path.read_text(encoding="utf-8") if context_path.exists() else ""
    return (
        "You are a delegated Codex worker. Complete the task using the repository as read-only. "
        "Return only the JSON object required by the supplied output schema. Artifact paths, if "
        "any, are relative to the output directory; this run normally produces no artifacts.\n\n"
        f"TASK\n{task}\n\nCONTEXT\n{context or '(none)'}\n"
    )


def _record_observation(store: Store, attempt_id: str, payload: dict[str, Any]) -> None:
    with store.transaction(immediate=True) as connection:
        connection.execute(
            "INSERT INTO observations(observation_id,source,subject_kind,subject_id,sampled_at,payload_json) "
            "VALUES(?,?,?,?,?,?)",
            (
                "obs_" + uuid.uuid4().hex,
                "codex-exec-jsonl",
                "attempt",
                attempt_id,
                utcnow(),
                json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            ),
        )


def _set_attempt(store: Store, attempt_id: str, status: str, **values: Any) -> None:
    allowed = {"submission_status", "process_handle_json", "result_path", "result_hash", "result_status", "error"}
    if not set(values) <= allowed:
        raise ValueError("unsupported attempt update")
    assignments = ["status=?", "updated_at=?"] + [f"{key}=?" for key in values]
    params = [status, utcnow(), *values.values(), attempt_id]
    with store.transaction(immediate=True) as connection:
        connection.execute(
            f"UPDATE attempts SET {','.join(assignments)} WHERE attempt_id=?",
            tuple(params),
        )


def _load_attempt(store: Store, attempt_id: str) -> dict[str, Any]:
    row = store.fetchone(
        "SELECT a.*,t.context_version,t.aggregate_status FROM attempts a "
        "JOIN tasks t ON t.task_id=a.task_id WHERE a.attempt_id=?",
        (attempt_id,),
    )
    if row is None:
        raise ValueError(f"unknown attempt: {attempt_id}")
    if row["status"] != "queued" or row["submission_status"] != "pending":
        raise ValueError(f"attempt is not runnable: {row['status']}/{row['submission_status']}")
    return dict(row)


def _parse_events(store: Store, attempt_id: str, stdout: str) -> tuple[str | None, bool, list[str]]:
    native_thread_id: str | None = None
    completed = False
    errors: list[str] = []
    for number, line in enumerate(stdout.splitlines(), 1):
        try:
            event = json.loads(line)
            if not isinstance(event, dict) or not isinstance(event.get("type"), str):
                raise ValueError("event must be an object with a string type")
        except (json.JSONDecodeError, ValueError) as exc:
            event = {"type": "bridge.invalid_jsonl", "line": number, "error": str(exc), "raw": line[:4096]}
            errors.append(f"invalid JSONL event at line {number}")
        _record_observation(store, attempt_id, event)
        event_type = event["type"]
        if event_type == "thread.started" and isinstance(event.get("thread_id"), str):
            native_thread_id = event["thread_id"]
        elif event_type == "turn.completed":
            completed = True
        elif event_type in {"turn.failed", "error"}:
            errors.append(str(event.get("message") or event.get("error") or event_type))
    return native_thread_id, completed, errors


def run_codex_attempt(
    store: Store,
    attempt_id: str,
    *,
    executable: str = "codex",
    timeout_seconds: float = 300,
    extra_args: Sequence[str] = (),
    environ: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    row = _load_attempt(store, attempt_id)
    control = ensure_private_dir(row["control_path"])
    output = ensure_private_dir(Path(row["exchange_path"]) / "outputs")
    schema_path = control / "output-schema.json"
    result_path = output / "result.json"
    atomic_write_json(schema_path, _result_schema(row))
    command = [
        executable, "exec", "--json", "--ephemeral", "--sandbox", "read-only",
        "--output-schema", str(schema_path), "--output-last-message", str(result_path),
        "--cd", row["project_root"], *extra_args, "-",
    ]
    child_env = dict(os.environ if environ is None else environ)
    for key in SENSITIVE_CHILD_ENV:
        child_env.pop(key, None)
    started = utcnow()
    _set_attempt(store, attempt_id, "starting", submission_status="submitting")
    try:
        process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=row["project_root"],
            env=child_env,
            start_new_session=True,
        )
    except OSError as exc:
        _set_attempt(store, attempt_id, "failed", submission_status="rejected", error=str(exc))
        raise
    _set_attempt(
        store,
        attempt_id,
        "working",
        submission_status="submitted",
        process_handle_json=json.dumps({"pid": process.pid, "started_at": started}),
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
        _set_attempt(store, attempt_id, "failed", error=f"codex exec timed out after {timeout_seconds}s")
        return {"attempt_id": attempt_id, "status": "failed", "error": "timeout"}

    (control / "codex.stdout.jsonl").write_text(stdout, encoding="utf-8")
    (control / "codex.stderr.log").write_text(stderr[-262144:], encoding="utf-8")
    native_thread_id, completed, event_errors = _parse_events(store, attempt_id, stdout)
    if native_thread_id:
        with store.transaction(immediate=True) as connection:
            connection.execute(
                "UPDATE agent_sessions SET native_ref_json=?,updated_at=? WHERE session_id=?",
                (json.dumps({"thread_id": native_thread_id}), utcnow(), row["session_id"]),
            )
    if process.returncode != 0 or not completed or event_errors:
        detail = "; ".join(event_errors) or stderr.strip() or f"exit {process.returncode}"
        if not completed and process.returncode == 0:
            detail = "missing turn.completed event" + (f"; {detail}" if detail else "")
        _set_attempt(store, attempt_id, "failed", error=detail[:4096])
        return {"attempt_id": attempt_id, "status": "failed", "error": detail}
    try:
        collected = collect_result(
            result_path=result_path,
            output_root=output,
            control_attempt=control,
            task_id=row["task_id"],
            attempt_id=attempt_id,
            context_version=row["context_version"],
        )
    except (OSError, ResultValidationError) as exc:
        _set_attempt(store, attempt_id, "failed", error=str(exc))
        return {"attempt_id": attempt_id, "status": "failed", "error": str(exc)}
    result_status = collected["payload"]["status"]
    terminal = "done" if result_status == "completed" else "failed"
    _set_attempt(
        store,
        attempt_id,
        terminal,
        result_path=collected["path"],
        result_hash=collected["sha256"],
        result_status=result_status,
        error=None,
    )
    with store.transaction(immediate=True) as connection:
        connection.execute(
            "UPDATE tasks SET aggregate_status=?,updated_at=? WHERE task_id=?",
            (result_status, utcnow(), row["task_id"]),
        )
    delivery = create_delivery(store, task_id=row["task_id"], attempt_id=attempt_id)
    return {
        "task_id": row["task_id"],
        "attempt_id": attempt_id,
        "status": terminal,
        "result_status": result_status,
        "native_thread_id": native_thread_id,
        "delivery": delivery,
    }
