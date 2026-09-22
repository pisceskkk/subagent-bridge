# Subagent Bridge

Subagent Bridge is a WSL-first daemon for delegating bounded work from a
Codex parent thread to independent headless agents and delivering structured
results back through the same Codex app-server used by ChatGPT Desktop.

The current repository contains the architecture, Desktop/app-server
feasibility findings, and a small protocol demo used to validate shared
Unix-WebSocket access.

## Documents

- `docs/design.md` — agent-neutral bridge design.
- `docs/final-wsl-desktop-architecture.md` — target WSL/Desktop deployment,
  storage, delivery, recovery, and security model.
- `docs/codex-desktop-feasibility.md` — experiments and verified app-server
  behavior.

## Demo verification

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
python3 tools/codex_app_server_demo.py --help
```

The commands that create a thread or start a turn require an explicit
`--execute` flag.

## Control-plane CLI

Install the local package, initialize its private state database, and queue a
task from inside a Desktop-managed Codex thread:

```bash
python3 -m pip install -e .
sab init
sab delegate --agent codex --task-file /absolute/path/to/task.md --delivery idle
sab show TASK_ID
sab run ATTEMPT_ID
sab dispatch DELIVERY_ID
```

`sab delegate` reads `CODEX_THREAD_ID` and `CODEX_SESSION_ID`, snapshots the
task into the project-local exchange, and durably queues an attempt. `sab run`
executes an isolated, ephemeral `codex exec --json` child, records its native
events, validates and freezes its structured result, and creates a durable
delivery. `sab dispatch` starts a new turn only when the bound parent is idle.

The live integration harness creates a dedicated parent task, runs a real
Codex child, dispatches the result through the shared Desktop app-server,
waits for the parent wake turn, and acknowledges the delivery:

```bash
PYTHONPATH=src python3 tools/e2e_codex_bridge.py --timeout 300
```
