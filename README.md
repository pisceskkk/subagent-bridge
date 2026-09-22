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
