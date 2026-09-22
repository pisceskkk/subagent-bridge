# Live Codex bridge validation

## Verified environment

- Date: 2026-09-22
- Codex CLI/app-server: 0.153.4
- Workspace: WSL repository connected to ChatGPT Desktop through SSH
- Transport: Desktop-managed Unix WebSocket app-server control socket
- Child backend: ephemeral `codex exec --json` with structured output

## End-to-end result

The integration harness completed the full workflow against real Codex
processes:

1. Created a dedicated parent task through the Desktop-managed app-server.
2. Completed a parent readiness turn and observed the task return to `idle`.
3. Queued an attempt with immutable task/context snapshots.
4. Started an independent Codex child thread.
5. Recorded the child's native event sequence: `thread.started`,
   `turn.started`, `item.completed`, `turn.completed`.
6. Validated the structured result identity and context version, then froze it
   in the control plane.
7. Created one deduplicated delivery and submitted it while the parent was
   idle.
8. Observed the parent wake turn read the frozen result and report
   `37 × 43 = 1591`.
9. Acknowledged the delivery after the parent turn completed.

The successful run produced these terminal database states:

| Record | Terminal state |
| --- | --- |
| Task | `aggregate_status=completed` |
| Attempt | `status=done`, `submission_status=submitted`, `result_status=completed` |
| Delivery | `status=acknowledged`, with a native parent turn ID |
| Observations | Four ordered native child events |

The parent task ID was `01a0c86b-cecb-7560-b530-c0b85338889e`; the independent
child thread ID was `01a0c86b-ebbf-7302-839c-ddb4065d44d1`. These identities
demonstrate that execution and parent delivery used separate Codex sessions.

## Data and failure checks

Automated tests cover successful collection plus malformed JSONL, a missing
`turn.completed` event, result identity mismatch, unsafe artifact paths,
frozen-result replacement, delivery deduplication, ambiguous submission, and
lease fencing. The live run additionally verified the strict structured-output
schema accepted by Codex, including explicit JSON types for constant identity
fields.

Parent thread/session variables are removed from the child process environment.
The database, frozen result, and child audit logs use owner-only file modes.
The project-local exchange is contained under a private directory and excluded
from Git.

## Current operational boundary

The runner persists `working` before waiting for the child and records all
native events after process completion. This is sufficient for durable terminal
state and recovery evidence; live per-event progress streaming remains a daemon
supervisor enhancement. The harness performs receipt acknowledgment only after
observing the parent turn complete. Production acknowledgment should be exposed
through the authenticated parent connector so completion evidence and result
consumption remain distinct.
