"""Validate child-written results and freeze immutable control-plane evidence."""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
from pathlib import Path
import uuid
from typing import Any

from .files import PRIVATE_FILE_MODE, ensure_private_dir, fsync_dir

MAX_RESULT_BYTES = 1_048_576
RESULT_STATUSES = {"completed", "failed", "blocked", "unavailable"}
FROZEN_RESULT_NAME = "result.collected.json"


class ResultValidationError(ValueError):
    pass


class FrozenResultConflict(RuntimeError):
    pass


def _bounded_result(path: Path, output_root: Path) -> bytes:
    if path.is_symlink() or not path.is_file():
        raise ResultValidationError(f"result must be a regular non-symlink file: {path}")
    try:
        if path.resolve().parent != output_root.resolve():
            raise ResultValidationError("result.json must live directly in the output directory")
    except OSError as exc:
        raise ResultValidationError(f"cannot resolve result path: {exc}") from exc
    with path.open("rb") as handle:
        raw = handle.read(MAX_RESULT_BYTES + 1)
    if len(raw) > MAX_RESULT_BYTES:
        raise ResultValidationError(f"result exceeds {MAX_RESULT_BYTES} bytes")
    return raw


def _string_list(payload: dict[str, Any], key: str) -> list[str]:
    value = payload.setdefault(key, [])
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ResultValidationError(f"{key} must be a list of strings")
    return value


def _validate_artifacts(payload: dict[str, Any], output_root: Path) -> None:
    artifacts = payload.setdefault("artifacts", [])
    if not isinstance(artifacts, list):
        raise ResultValidationError("artifacts must be a list")
    normalized: list[str] = []
    resolved_root = output_root.resolve()
    for item in artifacts:
        if not isinstance(item, str) or not item:
            raise ResultValidationError("artifact paths must be non-empty strings")
        relative = Path(item)
        if relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts):
            raise ResultValidationError(f"unsafe artifact path: {item!r}")
        candidate = output_root / relative
        current = output_root
        for part in relative.parts:
            current = current / part
            if current.is_symlink():
                raise ResultValidationError(f"artifact path contains a symlink: {item!r}")
        if not candidate.is_file():
            raise ResultValidationError(f"artifact is not a regular file: {item!r}")
        try:
            candidate.resolve().relative_to(resolved_root)
        except (OSError, ValueError) as exc:
            raise ResultValidationError(f"artifact escapes output directory: {item!r}") from exc
        normalized.append(relative.as_posix())
    payload["artifacts"] = normalized


def validate_result(
    raw: bytes,
    *,
    task_id: str,
    attempt_id: str,
    context_version: str,
    output_root: Path,
) -> tuple[dict[str, Any], bytes]:
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ResultValidationError(f"result is not valid UTF-8 JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise ResultValidationError("result must be a JSON object")
    expected = {
        "schema_version": 1,
        "task_id": task_id,
        "attempt_id": attempt_id,
        "context_version": context_version,
    }
    for key, value in expected.items():
        if payload.get(key) != value:
            raise ResultValidationError(f"result {key} does not match expected value")
    if payload.get("status") not in RESULT_STATUSES:
        raise ResultValidationError("unsupported result status")
    if not isinstance(payload.get("summary"), str):
        raise ResultValidationError("summary must be a string")
    needs_parent = payload.setdefault("needs_parent", [])
    if not isinstance(needs_parent, list):
        raise ResultValidationError("needs_parent must be a list")
    verification = payload.setdefault(
        "verification", {"performed": [], "limitations": []}
    )
    if not isinstance(verification, dict):
        raise ResultValidationError("verification must be an object")
    _string_list(verification, "performed")
    _string_list(verification, "limitations")
    _validate_artifacts(payload, output_root)
    canonical = (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode()
    if len(canonical) > MAX_RESULT_BYTES:
        raise ResultValidationError("canonical result exceeds byte limit")
    return payload, canonical


def freeze_result(control_attempt: Path | str, canonical: bytes) -> tuple[Path, str]:
    control = ensure_private_dir(control_attempt)
    frozen = control / FROZEN_RESULT_NAME
    stage = control / f".{FROZEN_RESULT_NAME}.{uuid.uuid4().hex}.stage"
    try:
        descriptor = os.open(
            stage,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            PRIVATE_FILE_MODE,
        )
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(canonical)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(stage, frozen, follow_symlinks=False)
            os.chmod(frozen, PRIVATE_FILE_MODE, follow_symlinks=False)
            fsync_dir(control)
        except FileExistsError:
            if frozen.is_symlink() or not frozen.is_file():
                raise FrozenResultConflict("existing frozen result is not a regular file")
            with frozen.open("rb") as handle:
                existing = handle.read(MAX_RESULT_BYTES + 1)
            if existing != canonical:
                raise FrozenResultConflict("a different result is already frozen")
        return frozen, hashlib.sha256(canonical).hexdigest()
    finally:
        with contextlib.suppress(FileNotFoundError):
            stage.unlink()


def collect_result(
    *,
    result_path: Path | str,
    output_root: Path | str,
    control_attempt: Path | str,
    task_id: str,
    attempt_id: str,
    context_version: str,
) -> dict[str, Any]:
    output = Path(output_root)
    raw = _bounded_result(Path(result_path), output)
    payload, canonical = validate_result(
        raw,
        task_id=task_id,
        attempt_id=attempt_id,
        context_version=context_version,
        output_root=output,
    )
    frozen, digest = freeze_result(control_attempt, canonical)
    return {"payload": payload, "path": str(frozen), "sha256": digest}

