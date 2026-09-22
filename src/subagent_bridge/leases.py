"""SQLite-backed leases with monotonically increasing fencing generations."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import sqlite3


class LeaseHeld(RuntimeError):
    pass


class LeaseLost(RuntimeError):
    pass


@dataclass(frozen=True)
class Lease:
    scope: str
    key: str
    owner: str
    generation: int
    expires_at: str


def _parse(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _expiry(now: datetime, ttl_seconds: float) -> str:
    if ttl_seconds <= 0:
        raise ValueError("lease TTL must be positive")
    return (now + timedelta(seconds=ttl_seconds)).isoformat()


def acquire(
    connection: sqlite3.Connection,
    scope: str,
    key: str,
    owner: str,
    *,
    ttl_seconds: float = 30,
    now: datetime | None = None,
) -> Lease:
    moment = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    expires_at = _expiry(moment, ttl_seconds)
    row = connection.execute(
        "SELECT owner, generation, expires_at FROM leases WHERE scope=? AND key=?",
        (scope, key),
    ).fetchone()
    if row is None:
        generation = 1
        connection.execute(
            "INSERT INTO leases(scope,key,owner,generation,expires_at) VALUES(?,?,?,?,?)",
            (scope, key, owner, generation, expires_at),
        )
    elif row["owner"] == owner:
        if _parse(row["expires_at"]) <= moment:
            raise LeaseLost(f"lease {scope}/{key} expired before reacquire")
        generation = row["generation"]
        connection.execute(
            "UPDATE leases SET expires_at=? WHERE scope=? AND key=? AND owner=? AND generation=?",
            (expires_at, scope, key, owner, generation),
        )
    elif _parse(row["expires_at"]) <= moment:
        generation = row["generation"] + 1
        connection.execute(
            "UPDATE leases SET owner=?, generation=?, expires_at=? WHERE scope=? AND key=?",
            (owner, generation, expires_at, scope, key),
        )
    else:
        raise LeaseHeld(
            f"lease {scope}/{key} held by {row['owner']} until {row['expires_at']}"
        )
    return Lease(scope, key, owner, generation, expires_at)


def renew(
    connection: sqlite3.Connection,
    lease: Lease,
    *,
    ttl_seconds: float = 30,
    now: datetime | None = None,
) -> Lease:
    moment = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    row = connection.execute(
        "SELECT owner, generation, expires_at FROM leases WHERE scope=? AND key=?",
        (lease.scope, lease.key),
    ).fetchone()
    if row is None:
        raise LeaseLost(f"lease {lease.scope}/{lease.key} no longer exists")
    if row["owner"] != lease.owner or row["generation"] != lease.generation:
        raise LeaseLost(f"lease {lease.scope}/{lease.key} fencing token changed")
    if _parse(row["expires_at"]) <= moment:
        raise LeaseLost(f"lease {lease.scope}/{lease.key} expired")
    expires_at = _expiry(moment, ttl_seconds)
    updated = connection.execute(
        "UPDATE leases SET expires_at=? WHERE scope=? AND key=? AND owner=? AND generation=?",
        (expires_at, lease.scope, lease.key, lease.owner, lease.generation),
    ).rowcount
    if updated != 1:
        raise LeaseLost(f"lease {lease.scope}/{lease.key} lost during renew")
    return Lease(lease.scope, lease.key, lease.owner, lease.generation, expires_at)


def fence(
    connection: sqlite3.Connection,
    lease: Lease,
    *,
    now: datetime | None = None,
) -> None:
    moment = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    row = connection.execute(
        "SELECT owner, generation, expires_at FROM leases WHERE scope=? AND key=?",
        (lease.scope, lease.key),
    ).fetchone()
    if (
        row is None
        or row["owner"] != lease.owner
        or row["generation"] != lease.generation
        or _parse(row["expires_at"]) <= moment
    ):
        raise LeaseLost(f"lease {lease.scope}/{lease.key} is no longer valid")


def release(connection: sqlite3.Connection, lease: Lease) -> bool:
    deleted = connection.execute(
        "DELETE FROM leases WHERE scope=? AND key=? AND owner=? AND generation=?",
        (lease.scope, lease.key, lease.owner, lease.generation),
    ).rowcount
    return deleted == 1

