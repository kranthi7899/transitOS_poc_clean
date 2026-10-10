"""Integration (program lead): wires the five mock products, plumbing, tools, workflow and panel into
one in-process system (local profile: SQLite files + SQLite bus, TestClient transport, sim clock).

- `system.System`: the running system with a deterministic `tick()` / `run_until_idle()` / `restart()`.
- `world.make_world()`: Track F's `World` over a fresh `System` (scenario verification).
- `demo`: narrated happy path, `python -m transitos.integration.demo`.
"""
