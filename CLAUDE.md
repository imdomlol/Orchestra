# Orchestra — Claude Code Context

## What this project is

A local, file-based multi-agent coding orchestrator. Given a git repo and a natural-language request, it coordinates multiple specialized AI models (Claude orchestrator, Gemini planner/critic, Codex worker/integrator) to produce reviewed, tested, merged commits with minimal human intervention.

The full design is in [docs/PLAN.md](docs/PLAN.md) — read it before any design-affecting change.

## Running tests

Tests require a Unix environment (`fcntl` dependency). On Windows, use WSL:

```bash
# First time only — install uv in WSL:
curl -LsSf https://astral.sh/uv/install.sh | sh
source $HOME/.local/bin/env

# Run all tests:
cd /mnt/c/Users/dominic/Documents/GitHub/Orchestra
uv run --extra dev pytest -v
```

All 69 tests should pass. The suite does not require Docker or real model credentials.

## Project layout

```
orch/           Python package — all runtime logic
  cli.py        Entry point: orch submit / orch run / orch image
  runtime.py    Orchestration loop (run_once, submit, startup_reconcile)
  dispatcher.py Picks next ready pending task, creates worktree, posts worker message
  inbox.py      Durable JSON message inbox under .orch/inbox/<role>/
  task_store.py File-backed YAML task CRUD with flock pickup and schema validation
  worktree.py   git worktree create/destroy + pre-commit ownership hooks
  runner.py     Subprocess executor with allowlist; DockerRunner wraps in sandbox
  merge.py      Patch export → integration worktree → git am → checks → git merge
  review.py     Worker → critic handoff: exports diff, transitions status, posts message
  plans.py      Extracts embedded YAML task blocks from planner markdown artifacts
  model_wrapper.py  Composes role prompts, invokes model CLIs, posts handoff JSON
  wrapper_cli.py    Console entry points for each model role
  config.py     Loads .orch/config/*.toml into typed dataclasses
  images.py     Docker sandbox image builder

.orch/          Orchestrator state (gitignored except config/ and schemas/)
  config/       orchestrator.toml, policies.toml, prompts/
  schemas/      task.schema.json (JSON Schema 2020-12)

tests/          One test file per module; run with pytest
docs/PLAN.md    Authoritative design doc and implementation status
examples/       task.example.yaml
scripts/        validate_task.py
docker/         orchestra-sandbox.Dockerfile
```

## Implemented tasks (T-0001 through T-0017)

| Task | Module(s) | What it does |
|------|-----------|--------------|
| T-0001 | `.orch/` skeleton | Directory layout, `.gitignore`, README |
| T-0002 | `scripts/validate_task.py` | JSON Schema validation for task YAMLs |
| T-0003 | `orch/config.py` | Config loader for `orchestrator.toml` + `policies.toml` |
| T-0004 | `orch/task_store.py` | CRUD over `.orch/tasks/{pending,active,done}/` |
| T-0005 | `orch/worktree.py` | `git worktree` create/destroy per task |
| T-0006 | `orch/inbox.py` | Atomic JSON inbox with at-least-once delivery |
| T-0007 | `orch/dispatcher.py` | Picks next ready task, respects deps + owned-file collisions |
| T-0008 | `orch/runner.py` | Subprocess executor with command allowlist and log capture |
| T-0009 | `orch/merge.py` | Patch-based integration worktree merge driver |
| T-0010 | `orch/cli.py`, `orch/runtime.py` | `orch submit` / `orch run --once` CLI |
| T-0011 | `orch/runner.py` | Docker sandbox runner (ro root, rw cwd) |
| T-0012 | `orch/model_wrapper.py`, `orch/wrapper_cli.py` | Role wrappers for model CLIs |
| T-0013 | `orch/worktree.py`, `orch/dispatcher.py` | Per-worktree pre-commit ownership hooks |
| T-0014 | `orch/images.py`, `docker/` | Project sandbox Docker image builder |
| T-0015 | `orch/plans.py`, `orch/runtime.py` | Planner handoff → pending task YAMLs |
| T-0016 | `orch/review.py`, `orch/runtime.py` | Worker completion → critic diff export + dispatch |
| T-0017 | `orch/runtime.py` | Critic verdict → merge / rework / escalate / abandon |

## Key data contracts

**Orchestrator inbox actions handled by `run_once()`:**

| action | payload | runtime result |
|--------|---------|----------------|
| `submit_request` | `request_path` | dispatches next ready task |
| `planned` | `plan_path` | ingests YAML blocks, dispatches |
| `reject_plan` | `task_id`, `reason` | acks, returns `plan_rejected` |
| `worker_completed` | `task_id` | exports diff, posts to critic inbox |
| `critic_reviewed` | `task_id`, `verdict`, `body` | routes by verdict (see below) |

**`critic_reviewed` verdict routing:**

| verdict | condition | result |
|---------|-----------|--------|
| `approve` | — | `integration_review` → `MergeDriver` → `merged` or `merge_failed_reworking` |
| `request_changes` | prior rounds < `max_retries` | append note → `in_progress` → worker re-dispatch |
| `request_changes` | prior rounds ≥ `max_retries` | `blocked` (escalate) |
| `reject` | — | `abandoned` |

**Task status flow:**
```
pending → in_progress → self_review → critic_review → integration_review
       → merged (done/)  or  blocked / abandoned (done/)
```

**Retry counters** are derived from `review_notes` in the task YAML:
- Critic rounds: count `gemini-critic` notes with `verdict: request_changes`
- Integration failures: count `codex-integrator` notes with `verdict: request_changes`
- Cap controlled by `max_retries` in `orchestrator.toml` (default 2)

## Design constraints (load-bearing)

- The orchestrator (Claude) **never edits source**; writes only under `.orch/`.
- All task YAMLs are validated against `.orch/schemas/task.schema.json` on every write.
- Inbox messages are **not acked until after the operation succeeds** — failures leave messages for retry.
- `MergeDriver` is the only code that writes to `main`; it's always called from the orchestrator runtime.
- Task status transitions are enforced by `STATUS_BY_DIR` in `task_store.py`.
- Worktrees are created per task; the pre-commit hook enforces `owned_files`/`forbidden_files`.

## What's still designed-only (not yet implemented)

- Continuous `orch run` loop (currently only `--once`; call it in a loop externally).
- Budget enforcement (`max_tasks_per_request`, `max_wall_clock_minutes` in config but not wired).
- Optional `codex-integrator` model review step between critic approval and `MergeDriver`.
- Full parallel dispatch (config exists; `max_workers` works, but not battle-tested).
- Human escalation notification mechanism beyond task status transitions.
