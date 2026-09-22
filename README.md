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
- `docs/live-e2e-validation.md` — real child execution, result delivery, parent
  wake, and persisted-state evidence.
- `docs/multi-agent-e2e-validation.md` — Claude/Kimi adapters, fixture-driven
  tests, idle wake, active-turn insertion, and Gemini probe status.

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
sab delegate --agent claude --task-file /absolute/path/to/task.md --delivery idle
sab delegate --agent kimi --task-file /absolute/path/to/task.md --delivery immediate
sab show TASK_ID
sab run ATTEMPT_ID
sab dispatch DELIVERY_ID
```

`sab delegate` reads `CODEX_THREAD_ID` and `CODEX_SESSION_ID`, snapshots the
task into the project-local exchange, and durably queues an attempt. `sab run`
selects the Codex, Claude Code, or Kimi Code adapter, records native events,
validates and freezes the structured result, and creates a durable delivery.
`sab dispatch` uses `turn/start` for an idle parent. For an immediate delivery,
pass the exact active turn identity with `--expected-turn-id`; the dispatcher
then uses `turn/steer` and never silently changes delivery modes.

The live integration harness creates a dedicated parent task, runs a real
Codex child, dispatches the result through the shared Desktop app-server,
waits for the parent wake turn, and acknowledges the delivery:

```bash
PYTHONPATH=src python3 tools/e2e_codex_bridge.py --agent claude --delivery idle
PYTHONPATH=src python3 tools/e2e_codex_bridge.py --agent kimi --delivery immediate
```
