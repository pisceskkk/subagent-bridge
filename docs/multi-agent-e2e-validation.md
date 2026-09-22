# Multi-agent end-to-end validation

## Adapter boundary

Each child CLI owns its command construction, native event parsing, session
identity extraction, terminal-state interpretation, and structured-result
extraction. The shared control plane owns attempt transitions, result
validation and freezing, delivery deduplication, parent dispatch, and
acknowledgment.

Sanitized native output samples are stored under
`tests/fixtures/agent_events/`. Unit tests consume these fixtures without
requiring network access, vendor credentials, or usage quota.

## Live results

Validation was performed on 2026-09-22 through the Desktop-managed WSL Codex
app-server.

| Child adapter | Native version/session evidence | Delivery path | Result |
| --- | --- | --- | --- |
| Codex | CLI/app-server 0.153.4, independent thread ID | idle `turn/start` | completed and acknowledged |
| Claude Code | 2.1.216, independent session ID | idle `turn/start` | completed and acknowledged |
| Kimi Code | 2.0.2, `session.resume_hint` ID | idle `turn/start` | completed and acknowledged |
| Kimi Code | 2.0.2, independent session ID | active `turn/steer` | completed and acknowledged in the existing turn |
| Gemini CLI | 0.59.0 | adapter live probe | authentication rejected with `UNSUPPORTED_CLIENT` |

Claude used its native `--json-schema` structured output. Its accepted schema
dialect omits the draft metadata field while retaining strict types, required
fields, constants, and `additionalProperties: false`. Kimi has no native JSON
schema option in prompt mode, so its adapter requests one JSON object and then
applies the same Bridge-side identity, status, artifact, and context checks.

## Immediate delivery evidence

The immediate run used parent thread
`01a0c89d-e0d1-7191-8dd3-a71b74bbc475` and active turn
`01a0c89d-e105-7e83-a18a-df877ab91349`. While that turn was executing a
30-second command, the Kimi child completed and the dispatcher called
`turn/steer` with the exact active turn ID.

The app-server accepted that same turn ID. Persisted history contains one turn:
the initial parent message, its command execution, the injected bridge handoff,
the frozen-result read, and the parent's final response. This distinguishes
active-turn insertion from idle wake, which creates a new turn.

## Reliability coverage

The automated suite includes sanitized Claude, Kimi, and Gemini fixtures plus
adapter execution and dispatcher tests. It checks structured output parsing,
native session capture, invalid JSONL, missing completion, identity mismatch,
artifact traversal, immutable result collection, delivery deduplication,
ambiguous submission, lease fencing, idle deferral, and exact-turn steering.

The Gemini adapter implements the documented headless stream events (`init`,
`message`, `tool_use`, `tool_result`, `error`, and `result`) and has sanitized
success and authentication-failure fixtures. Live execution requires an
account/client configuration accepted by its service; the current probe was
rejected before a model turn, without recording account data. The full adapter
harness recorded the attempt as `failed`, left `result_status` unset, and
created zero deliveries, confirming that authentication failure cannot wake a
parent task with a fabricated result.
