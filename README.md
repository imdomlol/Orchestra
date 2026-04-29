# Orchestra

Orchestra is a local, file-based multi-agent coding orchestrator. It is
designed to coordinate planning, implementation, review, testing, and merge
work across specialized model roles while keeping git and the filesystem as
the source of truth.

The MVP is intentionally small:

- one local machine
- one target repository
- one user request at a time
- git worktrees for worker isolation
- Docker for command sandboxing
- explicit task files
- append-only logs and durable artifacts under `.orch/`

See [docs/PLAN.md](docs/PLAN.md) for the architecture and task roadmap.

## Current Status

The project is in substrate implementation. The local contracts are now real
enough to invoke external agent CLIs through thin role wrappers:

1. repository skeleton
2. task schema and validator
3. config loader
4. task store
5. worktree manager
6. inbox
7. dispatcher
8. subprocess runner
9. merge driver
10. runtime CLI
11. Docker sandbox runner
12. external model wrappers

Per-worktree ownership hooks and project-specific Docker images remain next.

## Repository Layout

```text
.orch/                 committed config/schemas plus ignored runtime state
docs/                  design documents
examples/              sample task files and fixtures
orch/                  Python orchestration package
scripts/               command-line helper scripts
tests/                 pytest suite
```

## Development

Create a virtual environment and install the package in editable mode:

```bash
python -m venv .venv
. .venv/bin/activate
pip install -e ".[dev]"
```

Run the checks:

```bash
pytest
python scripts/validate_task.py examples/task.example.yaml
```

Docker must be available for real task and integration command execution. The
default sandbox image is configured in `.orch/config/orchestrator.toml`.

The model wrapper entry points compose checked-in role prompts, pass artifact
paths to the configured CLI on stdin, capture logs, and post JSON handoffs to
`.orch/inbox/orchestrator/`:

```bash
orch-gemini-planner --request-path .orch/requests/R-0001.md
orch-gemini-critic --task-yaml-path .orch/tasks/active/T-0001.yaml --diff-path .orch/patches/T-0001.patch
orch-codex-worker --task-id T-0001 --task-yaml-path .orch/tasks/active/T-0001.yaml --worktree-path .orch/worktrees/T-0001
orch-codex-integrator --task-id T-0001 --task-yaml-path .orch/tasks/active/T-0001.yaml --patch-path .orch/patches/T-0001.patch
```
