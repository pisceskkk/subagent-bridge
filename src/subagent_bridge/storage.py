"""SQLite control-plane store and schema migrations."""

from __future__ import annotations

from contextlib import contextmanager
import os
from pathlib import Path
import sqlite3

from .files import PRIVATE_FILE_MODE, ensure_private_dir

SCHEMA_VERSION = 1

SCHEMA_V1 = """
CREATE TABLE meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE app_server_instances (
    instance_id TEXT PRIMARY KEY,
    socket_path TEXT NOT NULL,
    host_id TEXT,
    generation INTEGER NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE parent_sessions (
    parent_id TEXT PRIMARY KEY,
    codex_thread_id TEXT NOT NULL,
    codex_session_id TEXT NOT NULL,
    app_server_instance_id TEXT NOT NULL REFERENCES app_server_instances(instance_id),
    binding_generation INTEGER NOT NULL,
    cwd TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(codex_thread_id, app_server_instance_id, binding_generation)
);
CREATE TABLE agent_sessions (
    session_id TEXT PRIMARY KEY,
    agent_kind TEXT NOT NULL,
    native_ref_json TEXT,
    workspace_root TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE tasks (
    task_id TEXT PRIMARY KEY,
    parent_id TEXT NOT NULL REFERENCES parent_sessions(parent_id),
    previous_task_id TEXT REFERENCES tasks(task_id),
    session_id TEXT REFERENCES agent_sessions(session_id),
    agent_kind TEXT NOT NULL,
    session_mode TEXT NOT NULL CHECK(session_mode IN ('new', 'resume')),
    delivery_mode TEXT NOT NULL CHECK(delivery_mode IN ('idle', 'immediate')),
    context_version TEXT,
    context_hash TEXT,
    aggregate_status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE attempts (
    attempt_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(task_id),
    session_id TEXT REFERENCES agent_sessions(session_id),
    status TEXT NOT NULL,
    submission_status TEXT NOT NULL,
    control_path TEXT NOT NULL,
    exchange_path TEXT NOT NULL,
    project_root TEXT NOT NULL,
    process_handle_json TEXT,
    launch_json TEXT NOT NULL,
    result_path TEXT,
    result_hash TEXT,
    result_status TEXT,
    deadline TEXT,
    error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE deliveries (
    delivery_id TEXT PRIMARY KEY,
    parent_id TEXT NOT NULL REFERENCES parent_sessions(parent_id),
    parent_generation INTEGER NOT NULL,
    mode TEXT NOT NULL CHECK(mode IN ('idle', 'immediate')),
    status TEXT NOT NULL,
    message TEXT NOT NULL,
    message_hash TEXT NOT NULL,
    receipt_hash TEXT NOT NULL,
    native_turn_id TEXT,
    error TEXT,
    created_at TEXT NOT NULL,
    submitted_at TEXT,
    acknowledged_at TEXT,
    updated_at TEXT NOT NULL
);
CREATE TABLE delivery_items (
    delivery_id TEXT NOT NULL REFERENCES deliveries(delivery_id),
    task_id TEXT NOT NULL REFERENCES tasks(task_id),
    attempt_id TEXT NOT NULL REFERENCES attempts(attempt_id),
    handoff_type TEXT NOT NULL,
    result_version TEXT NOT NULL,
    PRIMARY KEY(delivery_id, task_id, attempt_id, handoff_type, result_version),
    UNIQUE(task_id, attempt_id, handoff_type, result_version)
);
CREATE TABLE leases (
    scope TEXT NOT NULL,
    key TEXT NOT NULL,
    owner TEXT NOT NULL,
    generation INTEGER NOT NULL,
    expires_at TEXT NOT NULL,
    PRIMARY KEY(scope, key)
);
CREATE TABLE project_workspaces (
    project_root TEXT PRIMARY KEY,
    exchange_root TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE observations (
    observation_id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    subject_kind TEXT NOT NULL,
    subject_id TEXT NOT NULL,
    sampled_at TEXT NOT NULL,
    payload_json TEXT NOT NULL
);
CREATE INDEX idx_tasks_status ON tasks(aggregate_status);
CREATE INDEX idx_attempts_task ON attempts(task_id);
CREATE INDEX idx_attempts_status ON attempts(status, submission_status);
CREATE INDEX idx_deliveries_status ON deliveries(status);
CREATE INDEX idx_deliveries_parent ON deliveries(parent_id);
CREATE INDEX idx_observations_subject ON observations(subject_kind, subject_id, sampled_at);
"""


def _statements(script: str) -> list[str]:
    statements: list[str] = []
    current: list[str] = []
    for line in script.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("--"):
            continue
        current.append(line)
        if stripped.endswith(";"):
            statements.append("\n".join(current))
            current = []
    if current:
        statements.append("\n".join(current))
    return statements


class Store:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        ensure_private_dir(self.path.parent)
        if not self.path.exists():
            descriptor = os.open(
                self.path,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                PRIVATE_FILE_MODE,
            )
            os.close(descriptor)
        if self.path.is_symlink() or not self.path.is_file():
            raise ValueError(f"database path must be a regular file: {self.path}")
        os.chmod(self.path, PRIVATE_FILE_MODE, follow_symlinks=False)

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=5000")
        return connection

    def migrate(self) -> None:
        with self.transaction(immediate=True) as connection:
            exists = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='meta'"
            ).fetchone()
            current = 0
            if exists:
                row = connection.execute(
                    "SELECT value FROM meta WHERE key='schema_version'"
                ).fetchone()
                current = int(row["value"]) if row else 0
            if current > SCHEMA_VERSION:
                raise RuntimeError(
                    f"database schema {current} is newer than supported {SCHEMA_VERSION}"
                )
            if current == 0:
                # executescript() performs an implicit commit. Execute the
                # statements individually so schema and version remain one
                # BEGIN IMMEDIATE transaction.
                for statement in _statements(SCHEMA_V1):
                    connection.execute(statement)
                connection.execute(
                    "INSERT INTO meta(key, value) VALUES('schema_version', ?)",
                    (str(SCHEMA_VERSION),),
                )

    @contextmanager
    def transaction(self, *, immediate: bool = False):
        connection = self.connect()
        try:
            connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def fetchone(self, sql: str, params: tuple[object, ...] = ()) -> sqlite3.Row | None:
        connection = self.connect()
        try:
            return connection.execute(sql, params).fetchone()
        finally:
            connection.close()
