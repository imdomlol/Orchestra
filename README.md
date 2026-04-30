# Orchestra

Orchestra is a local, file-based multi-agent coding orchestrator. You give
it a git repo and a natural-language request; it coordinates several
specialized model CLIs (Claude, Gemini, Codex) to plan, implement, review,
test, and merge the change — keeping git and the filesystem as the only
source of truth.

This README is the user guide. For architecture and the task roadmap, see
[docs/PLAN.md](docs/PLAN.md).

---

## Status at a glance

- Substrate complete: T-0001 through T-0019 (75 passing tests).
- The full local pipeline — request → plan ingest → worker dispatch →
  critic handoff → merge — is wired and tested with mocked subprocesses.
- `orch run --once` now invokes the planner, worker, critic, and
  integrator wrappers as role inbox messages become actionable.
- **Not yet wired:** continuous `orch run`, budget enforcement,
  `orch doctor`, and the first-drive runbook. Those are tracked as
  T-0020…T-0023 in [PLAN.md §14](docs/PLAN.md). Until those land,
  Orchestra is best treated as a substrate to develop against, not a
  hands-off autopilot.

If you want to do a real first end-to-end run, start with PLAN.md §14
and read this README in full first.

---

## Prerequisites

- Python 3.11+
- Git (configured with `user.name` and `user.email`)
- Docker (the daemon must be reachable; commands run inside a sandbox
  container by default)
- The model CLIs you intend to use, on `PATH`:
  - `gemini` — for the planner and critic roles
  - `codex` — for the worker and integrator roles
  - Authenticate each per its upstream docs; Orchestra does not own
    those credentials.
- **Windows users:** running the test suite requires WSL because the
  inbox uses `fcntl`. The orchestrator runtime itself is intended to
  run from a Unix-like shell.

---

## Install

```bash
git clone <this repo>
cd Orchestra
python -m venv .venv
. .venv/bin/activate
pip install -e ".[dev]"
```

This installs the `orch` CLI plus the role wrapper entry points
(`orch-gemini-planner`, `orch-gemini-critic`, `orch-codex-worker`,
`orch-codex-integrator`).

Verify the install:

```bash
orch --help
pytest
```

On Windows, run the test suite from WSL:

```bash
cd /mnt/c/Users/<you>/path/to/Orchestra
uv run --extra dev pytest -v
```

---

## First-time setup

1. **Build the sandbox image.** Tasks and integration checks run inside
   a Docker container by default. The image and Dockerfile are
   configured in `.orch/config/orchestrator.toml`.

   ```bash
   orch image build
   ```

   To inspect the build command without invoking Docker:

   ```bash
   orch image build --print
   ```

2. **Confirm the model CLIs are reachable.**

   ```bash
   gemini --version
   codex --version
   ```

   Both must succeed from the same shell where you'll run `orch`.

3. **Skim the config files** under `.orch/config/`:
   - `orchestrator.toml` — model bindings, CLI commands, sandbox
     settings, runtime concurrency, budgets.
   - `policies.toml` — forbidden globs and default allowed commands
     used when validating task YAMLs.
   - `prompts/` — the role prompts injected into each CLI invocation.

   The defaults are sensible for local use. Pay attention to
   `[runtime] max_workers` (default 1; serial-by-default), `max_retries`
   (critic and integration retry cap), and the `[budgets]` section
   (currently parsed but not yet enforced — see PLAN.md §14 T-0021).

---

## Daily workflow

Orchestra's runtime is a **deterministic event loop** — each call to
`orch run --once` processes exactly one inbox message or one dispatch
decision and returns a JSON result. You drive a request by repeatedly
ticking the loop.

### Submit a request

```bash
orch submit "Add a function add(a, b) to calc.py with a unit test."
```

This appends a request file under `.orch/requests/` and posts a
`submit_request` message to the orchestrator inbox. It prints the
request file path.

### Tick the loop

```bash
orch run --once
```

Each call returns a JSON line describing what happened, e.g.:

```json
{"kind": "dispatched", "message": "dispatched T-0001", "task_id": "T-0001", ...}
{"kind": "merged",     "message": "merged T-0001", ...}
{"kind": "idle",       "message": "no actionable work"}
```

Possible `kind` values include `dispatched`, `planning_failed`,
`plan_ingested`, `agent_ran`, `agent_failed`, `critic_dispatched`,
`merged`, `merge_failed_reworking`, `critic_rework_dispatched`,
`escalated`, `abandoned`, `idle`, `plan_rejected`, `ignored_message`.

### Model wrapper execution

`orch run --once` invokes the configured wrapper for the oldest
actionable role inbox message (`worker`, `critic`, or `integrator`) and
acknowledges that role message only after the wrapper exits successfully
and emits a JSON handoff. You can still run wrappers manually for
debugging:

```bash
# After a worker dispatch:
orch-codex-worker \
  --task-id T-0001 \
  --task-yaml-path .orch/tasks/active/T-0001.yaml \
  --worktree-path .orch/worktrees/T-0001

# After worker_completed → critic_dispatched:
orch-gemini-critic \
  --task-yaml-path .orch/tasks/active/T-0001.yaml \
  --diff-path .orch/patches/T-0001.diff

# Optional integration review (not yet routed by the runtime):
orch-codex-integrator \
  --task-id T-0001 \
  --task-yaml-path .orch/tasks/active/T-0001.yaml \
  --patch-path .orch/patches/T-0001.patch
```

Each wrapper composes the checked-in role prompt, pipes it to the
configured CLI on stdin, captures stdout/stderr to `.orch/logs/<role>/`,
and posts the model's JSON handoff to the orchestrator inbox. The next
`orch run --once` will pick that up.

### A typical sequence

```text
orch submit "..."
orch run --once          # → invokes planner and ingests pending tasks
orch run --once          # → dispatches T-0001 to worker inbox
orch run --once          # → invokes worker wrapper; posts {"action": "worker_completed", ...}
orch run --once          # → exports diff, dispatches to critic
orch run --once          # → invokes critic wrapper; posts {"action": "critic_reviewed", "verdict": "approve"}
orch run --once          # → MergeDriver runs full suite, fast-forwards main
```

---

## What gets written where

```text
.orch/
  requests/      one .md per submitted request (append-only)
  plans/         planner artifacts P-XXXX.md
  tasks/
    pending/     ready to dispatch
    active/      currently in flight
    done/        merged / abandoned
  worktrees/     one git worktree per active task
  patches/       T-XXXX.patch (merge driver) and T-XXXX.diff (critic input)
  logs/          per-role stdout/stderr
  inbox/         per-role JSON messages (orchestrator/, worker/, critic/, ...)
  locks/         orchestrator.pid + advisory flocks
  summaries/     post-merge summaries
```

Every task YAML is validated against `.orch/schemas/task.schema.json` on
write. Inbox messages are at-least-once: they are not acked until the
operation succeeds, so a crash leaves them for retry.

To validate an existing task file:

```bash
python scripts/validate_task.py examples/task.example.yaml
```

---

## Inspecting a run

- **Where is task T-0001 in the lifecycle?**
  Look in `.orch/tasks/{pending,active,done}/T-0001.yaml`.
- **What did the worker output?**
  `.orch/logs/workers/T-0001/`
- **What did the critic say?**
  `.orch/logs/critic/T-0001/` plus the appended `review_notes` block
  inside the task YAML.
- **What was the proposed change?**
  `.orch/patches/T-0001.diff` (critic input) and
  `.orch/patches/T-0001.patch` (merge driver input).
- **Was it merged?**
  `git log` on `main` for a `merge(T-0001): <objective>` commit, plus
  the YAML moving to `.orch/tasks/done/`.

---

## Stopping, resuming, cleaning up

- **Stop:** the `--once` loop is one-shot, so just don't tick it again.
  A continuous `orch run` (T-0020) will support clean SIGINT shutdown.
- **Resume:** `orch run --once` calls `startup_reconcile` first. It
  reads every YAML in `.orch/tasks/active/`, replays the orchestrator
  inbox oldest-first, and clears stale `orchestrator.pid`. Files and
  inbox are authoritative; in-memory state is not.
- **Reset between experiments:** the safe-to-delete state lives under
  `.orch/{requests,plans,tasks,worktrees,patches,logs,inbox,locks,summaries}/`.
  Keep `.orch/config/` and `.orch/schemas/`. You may also want to remove
  any `task/T-*` git branches and the `.orch/worktrees/_integration`
  worktree if a previous run aborted mid-merge.

---

## Configuration reference (short)

```toml
# .orch/config/orchestrator.toml
[models]
orchestrator = "claude-opus"
planner      = "gemini"
critic       = "gemini"
worker       = "codex-gpt-5.5"
integrator   = "codex-gpt-5.5"

[cli]
gemini = "gemini"     # exact command(s) used to invoke the CLI
codex  = "codex"

[runtime]
max_workers              = 1     # serial-by-default
default_timeout_seconds  = 1800
max_retries              = 2     # critic and integration retry cap

[sandbox]
mode            = "docker"
docker          = "docker"
image           = "orchestra-sandbox:py3.12"
dockerfile      = "docker/orchestra-sandbox.Dockerfile"
build_context   = "."
network         = "none"
workdir         = "/workspace"

[budgets]
max_tasks_per_request   = 5      # parsed; not yet enforced (T-0021)
max_wall_clock_minutes  = 60     # parsed; not yet enforced (T-0021)
```

---

## Repository layout

```text
.orch/         committed config + schemas; runtime state (gitignored)
docs/          design documents (PLAN.md is authoritative)
docker/        sandbox Dockerfile
examples/      sample task YAML
orch/          Python package — runtime, dispatcher, runner, merge, ...
scripts/       helper scripts (validate_task.py)
tests/         pytest suite (one file per module)
```

---

## Safety notes

- The sandbox boundary is the Docker daemon. Anyone with Docker access
  on the host is effectively privileged; treat this as a practical
  isolation layer, not a multi-tenant security boundary.
- Budgets are not yet enforced. Until T-0021 lands, do not point
  Orchestra at a repo you care about — a runaway plan could spawn many
  tasks. Use a throwaway target repo.
- The orchestrator (Claude) never edits source. Workers may only edit
  files matching their task's `owned_files`, enforced by a per-worktree
  pre-commit hook.
- The merge driver is the only path that writes to `main`. It applies
  the task patch onto a fresh integration worktree off origin/main and
  fast-forwards only on a green test run.

---

## Further reading

- [docs/PLAN.md](docs/PLAN.md) — full architecture, task roadmap, and
  failure-handling policy. §14 lists the work needed for the first
  unattended end-to-end run.
- `.orch/config/prompts/` — the role prompts used by each wrapper.
- `examples/task.example.yaml` — a fully filled task YAML.
