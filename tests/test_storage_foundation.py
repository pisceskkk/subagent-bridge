from __future__ import annotations

from pathlib import Path
import stat
import tempfile
import unittest

from subagent_bridge.files import (
    attempt_exchange_dir,
    atomic_write_json,
    snapshot_inputs,
)
from subagent_bridge.storage import SCHEMA_VERSION, Store


class StorageFoundationTest(unittest.TestCase):
    def test_store_migrates_and_uses_private_modes(self):
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "state" / "bridge.sqlite3"
            store = Store(database)
            store.migrate()
            self.assertEqual(
                store.fetchone("SELECT value FROM meta WHERE key='schema_version'")["value"],
                str(SCHEMA_VERSION),
            )
            self.assertEqual(stat.S_IMODE(database.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(database.parent.stat().st_mode), 0o700)

    def test_atomic_json_replaces_symlink_instead_of_following_it(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "target.json"
            target.write_text("untouched")
            destination = root / "value.json"
            destination.symlink_to(target)
            atomic_write_json(destination, {"ok": True})
            self.assertEqual(target.read_text(), "untouched")
            self.assertFalse(destination.is_symlink())

    def test_exchange_snapshot_is_git_ignored_and_immutable(self):
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary) / "project"
            project.mkdir()
            (project / ".git").mkdir()
            task = project / "request.md"
            task.write_text("first task")
            attempt = attempt_exchange_dir(project, "task_1", "att_1")
            first = snapshot_inputs(attempt, task)
            second = snapshot_inputs(attempt, task)
            self.assertEqual(first["context_version"], second["context_version"])
            self.assertEqual(
                (project / ".subagent-bridge" / ".gitignore").read_text().splitlines()[-1],
                "*",
            )
            task.write_text("changed task")
            with self.assertRaises(FileExistsError):
                snapshot_inputs(attempt, task)

    def test_symlinked_project_state_is_refused(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project = root / "project"
            project.mkdir()
            (project / ".git").mkdir()
            outside = root / "outside"
            outside.mkdir()
            (project / ".subagent-bridge").symlink_to(outside, target_is_directory=True)
            with self.assertRaises(ValueError):
                attempt_exchange_dir(project, "task_1", "att_1")


if __name__ == "__main__":
    unittest.main()
