# orchPlan.md

A local, file-based multi-agent coding orchestrator. This document is the
single source of truth for the project's design and scope. Other models and
contributors should read it end-to-end before proposing changes, and update
it in the same PR as any design-affecting change.

**Status:** design complete; substrate tasks (T-0001…T-0005), inbox
runtime substrate (T-0006), dispatcher (T-0007), subprocess runner
(T-0008), merge driver (T-0009), runtime CLI (T-0010), Docker sandbox
runner, external model wrapper scripts, and per-worktree ownership hooks
implemented and tested.
Project-specific Docker images remain out of scope for this phase.

---

## 1. Goal & Scope

Given a git repo and a natural-language request, produce reviewed, tested,
merged commits on `main` with no human in the loop for the happy path, using
multiple specialized models coordinated locally.

**In scope (MVP):**
- Single-host execution, single repo, single `main` branch.
- One user request → one plan → N tasks → merges.
- Local filesystem + git + append-only logs as the only persistence.
- Serial execution by default; opt-in parallelism per plan.

**Out of scope (MVP):**
- Remote/distributed workers, multi-repo coordination.
- Web UI, dashboards, databases, message brokers.
- Auth between agents, multi-tenant isolation.
- Automatic dependency upgrades or migrations.

---

## 2. Stack (fixed)

| Role | Model | Invocation |
|---|---|---|
| Orchestrator | Claude Opus | Claude Code subagent; never edits source |
| Planner | Gemini | CLI subprocess |
| Critic | Gemini | CLI subprocess (separate invocation) |
| Worker | Codex GPT-5.5 | CLI subprocess inside a worktree |
| Integration reviewer | Codex GPT-5.5 | CLI subprocess against temp worktree |

Isolation: one `git worktree` per active worker task. No shared mutable state
besides the filesystem under `.orch/` and the git repo itself.

Command sandbox: task and integration commands run through the configured
Docker sandbox by default. The repo is mounted read-only at `/workspace`; the
active command working directory is mounted read-write over its matching path.
The default network mode is `none`.

---

## 3. Directory Layout

```
.orch/                          # all orchestrator state (gitignored except config/ + schemas/)
├── config/
│   ├── orchestrator.toml       # model bindings, CLI paths, concurrency, budgets
│   └── policies.toml           # forbidden globs, command allowlist defaults
├── schemas/
│   └── task.schema.json        # task contract (committed)
├── requests/                   # raw user requests, append-only md
├── plans/                      # Gemini planner outputs: P-XXXX-*.md
├── tasks/
│   ├── pending/                # not yet picked up
│   ├── active/                 # currently assigned
│   └── done/                   # merged or abandoned
├── worktrees/                  # one git worktree per active task: T-XXXX-*/
├── logs/
│   ├── orchestrator/           # decision log, jsonl per day
│   ├── planner/                # planner stdout/stderr per invocation
│   ├── critic/                 # critic reviews, md per task per pass
│   ├── workers/                # worker transcripts, dir per task
│   └── integrator/             # integration reviewer transcripts
├── patches/                    # exported diffs per task: T-XXXX.patch
├── summaries/                  # post-merge summaries: T-XXXX.md
├── locks/                      # advisory flock files for task pickup
└── inbox/                      # cross-agent messages: <to>/<from>-<ts>.json
```

---

## 4. Task Schema

JSON Schema (draft 2020-12) at `.orch/schemas/task.schema.json`. Required
fields and constraints:

- `id`: `^T-[0-9]{4}(-[a-z0-9-]+)?$`
- `objective`: ≤280 chars
- `owned_files`: ≥1 glob; the worker may only edit files matching these
- `forbidden_files`: globs the worker must never touch (overrides owned)
- `allowed_commands`: exact command strings the worker may run
- `acceptance_criteria[]`: each `{id, check, kind: command|file_exists|manual}`
- `dependencies[]`: other task ids that must be `merged` first
- `branch`: `^task/T-[0-9]{4}`
- `worktree_path`: under `.orch/worktrees/`
- `status`: one of `pending | planned | in_progress | self_review |
  critic_review | integration_review | ready_to_merge | merged | blocked |
  abandoned`
- `review_notes[]`: append-only `{author, timestamp, verdict, body}` where
  author ∈ {`gemini-critic`, `codex-integrator`, `claude-orchestrator`}

The schema is enforced on every write via `scripts/validate_task.py`.
Filled YAML example lives at `examples/task.example.yaml`.

---

## 5. Roles (summary; full prompts live in `.orch/config/prompts/`)

Each role has: purpose / allowed / forbidden / inputs / outputs / handoff /
escalation. Hard rules:

- **Claude orchestrator** never edits source. Writes only under `.orch/`.
  Owns scheduling, merges, escalation. Delegates by passing only
  `{task_id, task_yaml_path, worktree_path, role}` — no inline guidance.
- **Gemini planner** produces a single `plans/P-XXXX.md` with embedded task
  YAML blocks. Never writes into `.orch/tasks/` directly.
- **Gemini critic** reviews diffs against ACs and policies; never runs code.
  Auto-rejects diffs >800 added lines or >12 files (recommend split).
- **Codex worker** edits only inside its worktree, only paths matching
  `owned_files`, runs only `allowed_commands`. Never `--no-verify`, never
  installs deps unless authorized. Rejects flawed plans by setting
  `status=blocked` with a `risks[]` entry prefixed `PLAN_DEFECT:`.
- **Codex integration reviewer** merges branch into a temp worktree off
  fresh `main`, runs full suite. Never edits the worker's branch; never
  merges into `main` itself.

---

## 6. Lifecycle (state machine)

```
Intake → RepoInspection → Planning → PlanCritique → Decompose → Dispatch
       → WorkerExec → SelfReview → CriticReview → IntegrationReview
       → Merge → Done
```

Retry/failure transitions:
- `PlanCritique → Planning` on `request_changes`; `→ HumanEscalation` on reject.
- `CriticReview → WorkerExec` on `request_changes` (≤2 rounds; 3rd escalates).
- `IntegrationReview → WorkerExec` for rebase/fix; 2nd failure escalates.
- `WorkerExec → Dispatch` on transient crash (≤2 retries).
- `WorkerExec → Blocked` on impossible AC or `PLAN_DEFECT`.

---

## 7. Process Model

- Async event loop in the orchestrator watches `.orch/inbox/orchestrator/`.
  Subprocess invocations of Gemini/Codex are blocking inside async tasks.
- Decomposition: planner proposes, orchestrator commits.
- Scheduling: orchestrator only.
- Review: critic (vs plan) + integrator (vs `main`).
- Merge: orchestrator only.

Worker plan-rejection protocol (concrete):
1. Worker sets `status=blocked`.
2. Appends `risks[]` entry starting with `PLAN_DEFECT:`.
3. Posts `.orch/inbox/orchestrator/{task_id, action:"reject_plan",
   reason, suggested_owned_files[]}`.
4. Orchestrator must re-enter `Planning` before any further work on that task.

---

## 8. Parallel vs Serial

| Condition | Mode |
|---|---|
| Tasks share any `owned_files` glob | Serial |
| Tasks have a `dependencies[]` edge | Serial |
| Disjoint owned files, pure code, no codegen | Parallel |
| Touches `migrations/**` or generated files | Serial |
| >5 ready tasks | Parallel up to `max_workers` (default 2) |
| Sibling task got `request_changes` | Serial with that sibling |
| Integration review failed in last hour | Serial |

**MVP default: serial, one worker.** Parallelism opt-in via
`orchestrator.toml: max_workers`.

---

## 9. Merge Strategy

**Default:** export branch as patch, apply onto fresh integration branch off
`main`, run full suite, fast-forward `main` only on green.

```bash
git -C .orch/worktrees/$T format-patch main..$B --stdout > .orch/patches/$T.patch
git fetch origin main
git worktree add .orch/worktrees/_integration origin/main
cd .orch/worktrees/_integration && git checkout -b integrate/$T
git am --3way ../../patches/$T.patch
uv run pytest -q && uv run ruff check . && uv run mypy .
cd ../../.. && git checkout main
git merge --no-ff -m "merge($T): <objective>" integrate/$T
git worktree remove .orch/worktrees/_integration
git worktree remove .orch/worktrees/$T
git branch -D $B integrate/$T
mv .orch/tasks/active/$T.yaml .orch/tasks/done/
```

On conflict: `git am --abort`, route back to `WorkerExec` with conflict
files in a `request_changes` review note. Worker rebases inside its own
worktree (`git rebase origin/main`). Two consecutive integration failures
on the same task escalate to human. The orchestrator never resolves
conflicts itself.

---

## 10. Failure Handling

| Failure | Detection | Retry | Rollback | Escalation |
|---|---|---|---|---|
| Worker crash | non-zero exit, no self-review note | ≤2 same YAML | `git reset --hard` | `status=blocked` after 2 |
| Invalid plan | schema fail / forbidden globs | 1 replan | discard YAML blocks | human after 1 |
| Merge conflict | `git am --3way` fails | 1 worker rebase | `git am --abort` | human after 1 |
| Flaky test | passes in worktree, fails in `_integration` | 1 re-run | none | mark in `risks[]`, human |
| Hallucinated path | pre-commit hook (path must match `owned_files` and exist or be created) | none | `git restore` | critic auto-rejects; 2nd → human |
| Dep install fail | exit code + `ResolutionImpossible` in stderr | none | revert lockfile | always human |
| Context exhaustion | truncation sentinel | 1 split attempt | none | human after 1 |
| Orchestrator interrupted | stale PID file on startup | resume, don't retry | leave worktrees | only on inconsistency |

Resume order on startup: (1) `.orch/config/*.toml`; (2)
`.orch/locks/orchestrator.pid` (clear stale flocks); (3) every YAML in
`.orch/tasks/active/`; (4) `.orch/inbox/orchestrator/` oldest-first;
(5) `git worktree list --porcelain` to reconcile. YAMLs + inbox are
authoritative; in-memory state is not.

---

## 11. What's Built vs What's Designed

**Designed only (this doc):** external model wrapper scripts, full autonomous
planner/critic/worker subprocess handoffs, and complete failure escalation
policy.

**Implemented runtime tasks:**
1. **T-0001 repo-skeleton** — `.orch/` tree + `.gitignore` + `README.md`.
2. **T-0002 task-schema** — schema, example, `scripts/validate_task.py`.
3. **T-0003 config-loader** — `orch.config` reads `orchestrator.toml` +
   `policies.toml` with defaults and validation.
4. **T-0004 task-store** — CRUD over `.orch/tasks/{pending,active,done}/`
   with flock pickup and schema validation on write.
5. **T-0005 worktree-manager** — create/destroy worktrees at
   `.orch/worktrees/<task_id>/` on branch `task/<task_id>`; refuse
   removal with unmerged commits.
6. **T-0006 inbox** — atomic write/read of `.orch/inbox/<role>/*.json`
   with ordering, explicit acknowledgement, and at-least-once delivery.
7. **T-0007 dispatcher** — pick the next ready pending task, respecting
   merged dependencies, active owned-file collisions, and `max_workers`;
   create its worktree, move its YAML to active, and post the worker handoff
   message.

**Implemented and tested:**
8. **T-0008 subprocess-runner**
   - Objective: Provide one local subprocess boundary for model CLIs and
     task acceptance commands.
   - Owned files: `orch/runner.py`, `tests/test_runner.py`, `docs/PLAN.md`.
   - Acceptance:
     - Exact-string command allowlists are enforced before spawning task
       commands.
     - `stdout` and `stderr` are captured under `.orch/logs/<role>/`.
     - Timeouts return a structured result and write a timeout note to
       stderr logs.
     - Runner refuses to execute with a working directory outside the repo
       root.

9. **T-0009 merge-driver**
   - Objective: Implement §9 happy-path patch export, integration worktree
     application, check execution, and final task transition as one structured
     merge API.
   - Owned files: `orch/merge.py`, `tests/test_merge.py`, `docs/PLAN.md`.
   - Acceptance:
     - Exports `main..task/<id>` to `.orch/patches/<id>.patch`.
     - Creates a fresh `_integration` worktree and `integrate/<id>` branch.
     - Applies patches with `git am --3way`, aborting and returning a conflict
       result on failure.
     - Runs configured full-suite commands through `orch.runner`.
     - On green, merges into `main`, moves the task YAML to `done/merged`, and
       removes integration and worker worktrees.

10. **T-0010 orch-run-cli**
    - Objective: Add an `orch` CLI that ties request submission, inbox polling,
      dispatch, subprocess execution, critic/integrator handoff, and resume
      reconciliation into one serial-by-default runtime loop.
    - Owned files: `orch/cli.py`, `orch/runtime.py`, `tests/test_cli.py`,
      `tests/test_runtime.py`, `pyproject.toml`, `docs/PLAN.md`.
    - Acceptance:
      - `orch submit "<prompt>"` writes an append-only request file and posts
        an orchestrator inbox nudge.
      - `orch run --once` processes the oldest actionable inbox or dispatch
        event deterministically.
      - Startup reconciliation follows §10 resume order.
      - Runtime uses `T-0006` inbox, `T-0007` dispatcher, `T-0008` runner, and
        `T-0009` merge driver rather than duplicating those contracts.
11. **T-0011 docker-sandbox-runner**
    - Objective: Execute allowlisted commands inside Docker containers rather
      than directly on the host.
    - Owned files: `orch/runner.py`, `orch/config.py`,
      `.orch/config/orchestrator.toml`, `tests/test_runner.py`,
      `tests/test_config.py`, `docs/PLAN.md`.
    - Acceptance:
      - Sandbox config declares Docker binary, image, container workdir, and
        network mode.
      - Docker runner preserves the existing exact command allowlist contract.
      - Repo root is mounted read-only while the command cwd is mounted
        read-write.
      - Docker command construction is tested without requiring Docker during
        the unit test suite.

12. **T-0012 external-model-wrappers**
    - Objective: Add thin role wrapper scripts for Gemini and Codex subprocess
      handoffs.
    - Owned files: `orch/model_wrapper.py`, `orch/wrapper_cli.py`,
      `orch/runner.py`, `tests/test_model_wrapper.py`, `pyproject.toml`,
      `README.md`, `docs/PLAN.md`.
    - Acceptance:
      - Wrappers inject checked-in role prompts and artifact-path context.
      - Configured external CLI commands receive the composed prompt on stdin.
      - stdout and stderr are captured through the shared runner log contract.
      - JSON handoffs emitted by the model are posted to the durable inbox.
      - Console entry points exist for planner, critic, worker, and integrator
        roles.

13. **T-0013 worktree-ownership-hooks**
    - Objective: Install a local pre-commit hook in every worker worktree that
      enforces task `owned_files` and `forbidden_files` before commits.
    - Owned files: `orch/worktree.py`, `orch/dispatcher.py`,
      `orch/merge.py`, `tests/test_worktree.py`, `tests/test_dispatcher.py`,
      `README.md`, `docs/PLAN.md`.
    - Acceptance:
      - Dispatch passes task ownership globs into worktree creation.
      - Worker worktrees use worktree-local `core.hooksPath` configuration.
      - The hook rejects staged paths outside `owned_files`.
      - The hook rejects staged paths matching `forbidden_files`, even if also
        owned.
      - Merge cleanup removes the task hook directory with the worker worktree.

**Required external pieces (out of scope of T-0001…T-0013):**
- Project-specific Docker images that contain each repo's dependencies.

---

## 12. Tradeoffs Accepted by MVP

- Serial-by-default leaves throughput on the table for debuggability.
- File-based inbox + flock has no fairness guarantees; fine at 1–2 workers.
- No durable queue — small crash windows can leave inconsistent state;
  resume reconciliation handles it but the window exists.
- Single host, no auth between agents.
- Patch-based merge collapses worker-internal commits into one merge
  commit on `main` (intentional).
- Two-round critic cap will sometimes escalate hard tasks prematurely.
- Safety relies on the Docker daemon boundary; users with Docker access are
  effectively privileged on the host, so this is a practical isolation layer
  rather than a multi-tenant security boundary.
- No built-in cost/latency cap until `orchestrator.toml` budgets are added
  (max tasks per request, max wall-clock, max tokens) — do this before
  first real run.

---

## 13. How to Iterate on This Plan

- Treat §3 (layout), §4 (schema), §5 (role contracts), §9 (merge) as
  load-bearing. Changes here ripple through every task and require updating
  this doc in the same PR.
- §8 (parallelism) and §10 (failure policy) are tunable; change freely
  once T-0010 is running and there is real data.
- Project-specific Docker images are the next implementation deliverable
  before the happy path can run without hand-authored task YAML.
- When in doubt: prefer fewer features, stricter contracts, more logs.
