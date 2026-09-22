from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from subagent_bridge.results import (
    FrozenResultConflict,
    ResultValidationError,
    collect_result,
)


def payload(**changes):
    value = {
        "schema_version": 1,
        "task_id": "task_1",
        "attempt_id": "att_1",
        "context_version": "v1-abc",
        "status": "completed",
        "summary": "done",
        "artifacts": ["report.md"],
        "verification": {"performed": ["unit test"], "limitations": []},
        "needs_parent": [],
    }
    value.update(changes)
    return value


class ResultCollectionTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.output = self.root / "exchange" / "outputs"
        self.output.mkdir(parents=True)
        self.control = self.root / "control"
        (self.output / "report.md").write_text("evidence")
        self.result = self.output / "result.json"

    def collect(self):
        return collect_result(
            result_path=self.result,
            output_root=self.output,
            control_attempt=self.control,
            task_id="task_1",
            attempt_id="att_1",
            context_version="v1-abc",
        )

    def test_valid_result_is_frozen_and_idempotent(self):
        self.result.write_text(json.dumps(payload()))
        first = self.collect()
        second = self.collect()
        self.assertEqual(first["sha256"], second["sha256"])
        self.assertEqual(Path(first["path"]).name, "result.collected.json")
        self.assertEqual(first["payload"]["artifacts"], ["report.md"])

    def test_changed_result_cannot_replace_frozen_evidence(self):
        self.result.write_text(json.dumps(payload()))
        self.collect()
        self.result.write_text(json.dumps(payload(summary="changed")))
        with self.assertRaises(FrozenResultConflict):
            self.collect()

    def test_identity_and_context_must_match(self):
        self.result.write_text(json.dumps(payload(attempt_id="other")))
        with self.assertRaises(ResultValidationError):
            self.collect()

    def test_artifact_traversal_and_symlink_are_rejected(self):
        self.result.write_text(json.dumps(payload(artifacts=["../outside"])))
        with self.assertRaises(ResultValidationError):
            self.collect()
        target = self.output / "real.md"
        target.write_text("real")
        (self.output / "linked.md").symlink_to(target)
        self.result.write_text(json.dumps(payload(artifacts=["linked.md"])))
        with self.assertRaises(ResultValidationError):
            self.collect()

    def test_symlinked_result_is_rejected(self):
        actual = self.output / "actual.json"
        actual.write_text(json.dumps(payload()))
        self.result.symlink_to(actual)
        with self.assertRaises(ResultValidationError):
            self.collect()


if __name__ == "__main__":
    unittest.main()
