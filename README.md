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
- explicit task files
- append-only logs and durable artifacts under `.orch/`

See [docs/PLAN.md](docs/PLAN.md) for the architecture and task roadmap.

## Current Status

The project is in substrate implementation. The first goal is to make the
local contracts real before invoking any external agent CLIs:

1. repository skeleton
2. task schema and validator
3. config loader
4. task store
5. worktree manager
6. inbox

Dispatch, subprocess execution, integration, and model wrappers come after
those pieces are tested.

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
