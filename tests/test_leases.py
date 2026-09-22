from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest

from subagent_bridge.leases import LeaseHeld, LeaseLost, acquire, fence, release, renew
from subagent_bridge.storage import Store


class LeaseTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.store = Store(Path(self.temporary.name) / "bridge.sqlite3")
        self.store.migrate()
        self.now = datetime(2026, 9, 22, tzinfo=timezone.utc)

    def test_other_owner_cannot_take_live_lease(self):
        with self.store.transaction(immediate=True) as connection:
            acquire(connection, "parent", "p1", "worker-a", now=self.now)
        with self.store.transaction(immediate=True) as connection:
            with self.assertRaises(LeaseHeld):
                acquire(connection, "parent", "p1", "worker-b", now=self.now)

    def test_expired_takeover_increments_fencing_generation(self):
        with self.store.transaction(immediate=True) as connection:
            first = acquire(
                connection, "parent", "p1", "worker-a", ttl_seconds=1, now=self.now
            )
        later = self.now + timedelta(seconds=2)
        with self.store.transaction(immediate=True) as connection:
            second = acquire(connection, "parent", "p1", "worker-b", now=later)
            with self.assertRaises(LeaseLost):
                fence(connection, first, now=later)
            fence(connection, second, now=later)
        self.assertEqual(second.generation, first.generation + 1)

    def test_renew_preserves_generation_and_release_is_fenced(self):
        with self.store.transaction(immediate=True) as connection:
            lease = acquire(connection, "session", "s1", "worker-a", now=self.now)
            renewed = renew(
                connection,
                lease,
                ttl_seconds=60,
                now=self.now + timedelta(seconds=1),
            )
            self.assertEqual(renewed.generation, lease.generation)
            self.assertTrue(release(connection, renewed))
            self.assertFalse(release(connection, renewed))

    def test_expired_owner_cannot_silently_reacquire(self):
        with self.store.transaction(immediate=True) as connection:
            acquire(connection, "task", "t1", "worker-a", ttl_seconds=1, now=self.now)
        with self.store.transaction(immediate=True) as connection:
            with self.assertRaises(LeaseLost):
                acquire(
                    connection,
                    "task",
                    "t1",
                    "worker-a",
                    now=self.now + timedelta(seconds=2),
                )


if __name__ == "__main__":
    unittest.main()
