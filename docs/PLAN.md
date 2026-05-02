# orchPlan.md

A local, file-based multi-agent coding orchestrator. This document is the
single source of truth for the project's design and scope. Other models and
contributors should read it end-to-end before proposing changes, and update
it in the same PR as any design-affecting change.

**Status:** design complete; substrate tasks T-0001…T-0029 implemented and
tested. T-0018 planner auto-invocation is now wired:
`submit_request` invokes the planner wrapper, consumes its `planned`
handoff, and ingests validated pending tasks without manual wrapper
execution. T-0019 agent-driver invocation is also wired: `run_once`
consumes worker, critic, and integrator inbox messages by invoking the
matching wrapper and only acknowledges those role messages after a
successful handoff. T-0020 continuous `orch run` is wired: the runtime
polls for actionable work, sleeps on idle using the configured interval,
and shuts down cleanly on SIGINT/SIGTERM. T-0021 budget enforcement is
now wired for plan task-count caps and continuous-run wall-clock caps. The
full local pipeline — request submission, plan ingestion, worker dispatch,
default Opus self-review, optional worker→Gemini critic handoff, and critic
verdict routing through `MergeDriver`, rework, escalation, or abandonment —
is in place and exercised by the unit suite. T-0023 first-drive documentation
is available at `docs/RUNBOOK.md`.

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
| Critic | Gemini | CLI subprocess (opt-in / big-refactor invocation) |
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
- `critic_override`: optional `gemini | both` task-level opt-in over
  `[critic] mode`
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
  `orch chat` is the recommended human-driven entry point: it lets Opus
  drive the explicit plan/decompose/dispatch/diff/rework/merge primitives
  from a plain terminal. `orch run` remains the unattended event-loop mode.
- **Gemini planner** produces a single `plans/P-XXXX.md` with embedded task
  YAML blocks. Never writes into `.orch/tasks/` directly.
- **Gemini critic** is now opt-in for big refactors or unusual risk. When
  invoked, it reviews diffs against ACs and policies; never runs code.
  Auto-rejects diffs >800 added lines or >12 files (recommend split). The
  default quality-check loop is external Opus inspection of exported diffs,
  followed by explicit `orch merge`, `orch rework`, or abandonment. Global
  critic routing is configured by `[critic] mode = "opus" | "gemini" |
  "both"` in `orchestrator.toml`; the default is `"opus"`. Individual task
  YAML may opt into Gemini with `critic_override: "gemini"` or request a
  non-final second opinion with `critic_override: "both"`.
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

**Designed only (this doc):** none for the local MVP substrate and first
test drive.

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

14. **T-0014 project-sandbox-image**
    - Objective: Provide a project-specific Docker image definition and build
      command for sandboxed task and integration checks.
    - Owned files: `docker/orchestra-sandbox.Dockerfile`, `.dockerignore`,
      `.orch/config/orchestrator.toml`, `orch/config.py`, `orch/images.py`,
      `orch/cli.py`, `tests/test_config.py`, `tests/test_images.py`,
      `tests/test_cli.py`, `tests/test_runner.py`, `README.md`,
      `docs/PLAN.md`.
    - Acceptance:
      - Sandbox config names the image tag, Dockerfile, and build context.
      - The project Dockerfile installs runtime and test dependencies.
      - `.dockerignore` excludes git, virtualenv, caches, and runtime
        orchestrator state from the build context.
      - `orch image build --print` emits the exact configured Docker build
        command without requiring Docker.
      - Image build command construction rejects configured paths outside the
        repo root.

15. **T-0015 planner-handoff-ingest**
    - Objective: Convert Gemini planner handoffs into validated pending task
      YAMLs that the existing dispatcher can run.
    - Owned files: `orch/plans.py`, `orch/runtime.py`,
      `tests/test_plans.py`, `tests/test_runtime.py`, `README.md`,
      `docs/PLAN.md`.
    - Acceptance:
      - Runtime handles orchestrator inbox messages with
        `action: "planned"` and a repo-relative `plan_path`.
      - Markdown plan artifacts are scanned for fenced YAML task blocks.
      - Extracted tasks are schema-validated before any pending YAML is
        written.
      - Duplicate task ids and plan paths outside the repo are rejected.
      - After successful ingest, runtime dispatches through the existing
        dispatcher when worker capacity is available.

17. **T-0017 critic-review-ingest**
    - Objective: Route critic verdicts into the next lifecycle step: merge,
      worker rework, escalation, or abandonment.
    - Owned files: `orch/runtime.py`, `tests/test_runtime.py`, `docs/PLAN.md`.
    - Acceptance:
      - Runtime handles orchestrator inbox messages with
        `action: "critic_reviewed"`, `task_id`, and `verdict`.
      - On `approve`: transitions to `integration_review`, calls
        `MergeDriver.merge_task()`, and returns `kind="merged"` on success.
      - On `approve` with integration failure: routes back to worker
        (`kind="merge_failed_reworking"`) or escalates to `blocked` after
        `max_retries` integration failures (`kind="escalated"`).
      - On `request_changes` within retry budget: appends review note,
        transitions task to `in_progress`, posts worker re-dispatch message
        (`kind="critic_rework_dispatched"`).
      - On `request_changes` at or beyond `max_retries` prior rounds:
        transitions task to `blocked` (`kind="escalated"`).
      - On `reject`: transitions task to `abandoned` (`kind="abandoned"`).
      - Malformed messages (missing task_id or invalid verdict) raise before
        ack, leaving the message for retry.

16. **T-0016 worker-critic-handoff**
    - Objective: Route completed worker branches into critic review using
      durable diff artifacts and inbox handoff messages when Gemini critic
      routing is enabled.
    - Owned files: `orch/review.py`, `orch/runtime.py`,
      `tests/test_review.py`, `tests/test_runtime.py`, `README.md`,
      `docs/PLAN.md`.
    - Acceptance:
      - Runtime handles orchestrator inbox messages with
        `action: "worker_completed"` and `task_id`.
      - Review dispatch exports `main..task/<id>` to
        `.orch/patches/<id>.diff`.
      - Task status transitions to `critic_review` for Gemini-final review.
        The default T-0028 route keeps completed work in `self_review`.
      - Critic inbox messages include only artifact paths and role metadata.
      - Malformed worker completion messages remain unacknowledged by failing
        before ack.

24. **T-0024 plan-only-cli**
    - Objective: Add `orch plan "<request>"` as a preview path that invokes
      the Gemini planner through `ModelWrapper`, writes the request artifact,
      and prints the produced Markdown plan path without entering the
      autonomous task-ingest flow.
    - Owned files: `orch/cli.py`, `orch/runtime.py`, `tests/test_cli.py`,
      `tests/test_runtime.py`, `docs/PLAN.md`.
    - Acceptance:
      - `OrchestraRuntime.plan_only(request: str) -> Path` writes the
        request under `.orch/requests/`, invokes `gemini-planner`, and
        returns the resolved `.orch/plans/` Markdown path.
      - `orch plan "add foo"` exits 0 and prints only the plan path.
      - The plan-only path does not leave a `planned` inbox message and
      does not extract or write task YAMLs.
      - Planner failures raise `PlanOnlyError`; stderr remains under
        `.orch/logs/planner/`.

25. **T-0025 decompose-cli**
    - Objective: Add an explicit `orch decompose` ingestion path for task
      YAML authored outside the Gemini Markdown plan flow.
    - Owned files: `orch/cli.py`, `orch/runtime.py`, `tests/test_cli.py`,
      `tests/test_runtime.py`, `docs/PLAN.md`.
    - Acceptance:
      - `orch decompose` reads one task YAML document from stdin,
        schema-validates it, writes it through `TaskStore` to
        `.orch/tasks/pending/<id>.yaml`, and prints the path.
      - `ingest_task_yaml(yaml_text: str) -> Path` exposes the same behavior
        for programmatic callers.
      - Malformed YAML, schema-invalid YAML, and duplicate task ids fail
        without writing or overwriting pending task files.
      - The existing autonomous `planned` message to `PlanIngestor` path
        remains intact.

26. **T-0026 synchronous-dispatch-cli**
    - Objective: Add `orch dispatch <task_id>` so an external Opus agent can
      synchronously dispatch one named pending task, create its worker
      worktree, and run the `codex-worker` wrapper without waiting for the
      inbox-driven runtime loop.
    - Owned files: `orch/cli.py`, `orch/runtime.py`, `tests/test_cli.py`,
      `tests/test_runtime.py`, `docs/PLAN.md`.
    - Acceptance:
      - `orch dispatch T-0001` looks up `.orch/tasks/pending/T-0001.yaml`,
        creates the worktree with ownership hooks, moves the YAML to
        `active/`, invokes `codex-worker` through `ModelWrapper`, and blocks
        until the wrapper returns.
      - On worker success, the task remains in `active/` with
        `status: self_review`, leaving the branch diff exportable by later
        patch/diff primitives.
      - Owned-file collisions with active tasks exit non-zero with a clear
        message, leave the pending YAML in place, and do not create a
        worktree.
      - Wrapper failure appends a `claude-orchestrator` review note, leaves
        the task in `active/`, and exits non-zero.
      - The existing autonomous `dispatch_next()` and worker inbox driver
        behavior remains unchanged.

27. **T-0027 manual-review-primitives**
    - Objective: Add explicit `orch diff`, `orch rework`, and `orch merge`
      commands so an external Opus agent can inspect patches and choose the
      next step without Gemini critic routing in the default loop.
    - Owned files: `orch/cli.py`, `orch/runtime.py`, `orch/review.py`,
      `tests/test_cli.py`, `tests/test_runtime.py`, `tests/test_review.py`,
      `docs/PLAN.md`.
    - Acceptance:
      - `orch diff <task_id>` exports `main..task/<id>` to
        `.orch/patches/<id>.diff` if missing, then prints the diff contents.
      - `orch rework <task_id> --notes "<text>"` appends a
        `claude-orchestrator` `request_changes` review note, transitions the
        task to `in_progress`, and synchronously reruns the codex worker using
        the T-0026 dispatch path.
      - `orch merge <task_id>` transitions to `integration_review` and calls
        `MergeDriver.merge_task`; integration failure leaves the task in
        `integration_review` with a `claude-orchestrator` failure note and
        exits non-zero.
      - These manual primitives do not enforce `max_retries`; Opus decides
        whether to rework, merge, or abandon.
      - The inbox-driven `critic_reviewed` routing remains intact for opt-in
        Gemini critic runs.

28. **T-0028 critic-mode-gating**
    - Objective: Gate Gemini critic invocation behind a global config mode
      and per-task opt-in so Opus is the default final reviewer after worker
      completion.
    - Owned files: `.orch/config/orchestrator.toml`,
      `.orch/schemas/task.schema.json`, `orch/config.py`, `orch/runtime.py`,
      `orch/review.py`, `tests/test_config.py`, `tests/test_runtime.py`,
      `tests/test_review.py`, `docs/PLAN.md`.
    - Acceptance:
      - `[critic] mode = "opus" | "gemini" | "both"` loads from
        `orchestrator.toml`, defaulting to `"opus"`.
      - Task YAML can set optional `critic_override: "gemini" | "both"`.
      - In `"opus"` mode, `worker_completed` leaves the task in
        `self_review` with no Gemini critic inbox message.
      - In `"gemini"` mode, `worker_completed` follows the existing Gemini
        critic flow through `critic_review`.
      - In `"both"` mode, the Gemini critic is invoked as a second opinion
        while the task remains in `self_review`; Opus's manual verdict is
        final.
      - Unit tests cover all three modes with mocked wrappers, including the
        default zero-Gemini path and per-task opt-in over the global default.

29. **T-0029 opus-chat-cli**
    - Objective: Add `orch chat` as a terminal-native interactive
      orchestrator where Claude Opus drives the explicit orchestration
      primitives through Claude Agent SDK tool use. The implementation uses
      Claude Code OAuth from `claude login` as the primary auth path and
      falls back to `ANTHROPIC_API_KEY`.
    - Owned files: `orch/chat.py`, `orch/cli.py`, `orch/config.py`,
      `.orch/config/orchestrator.toml`, `pyproject.toml`,
      `tests/test_chat.py`, `tests/test_cli.py`, `README.md`,
      `docs/RUNBOOK.md`, `docs/PLAN.md`.
    - Acceptance:
      - `orch chat "<request>"` and bare `orch chat` start a shell session
        with `/quit`, `/save`, and `/model <id>` slash commands.
      - `--once` runs one non-interactive turn, and `--dry-run` is the only
        no-credential path; otherwise `claude login` or `ANTHROPIC_API_KEY`
        is required.
      - Opus receives tools for plan, decompose, dispatch, diff, rework,
        merge, task listing, read-only file reads, and allowlisted read-only
        shell commands through an in-process SDK MCP server.
      - Tool calls shell out through existing CLI primitives where those
        primitives exist; non-zero exits are returned to the model.
      - Session transcripts are written under `.orch/logs/chat/`.

**Required external pieces (out of scope of T-0001…T-0029):**
- None for the local MVP substrate; real model CLI credentials and provider
  availability are environment-specific runtime concerns.

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
- Next iterations are captured in §14 (First Test Drive) — these block
  the first real end-to-end run. Beyond that, candidates include wiring
  the optional `codex-integrator` pre-merge review step, parallel
  dispatch hardening, and a human-escalation notification channel.
- When in doubt: prefer fewer features, stricter contracts, more logs.

---

## 14. First Test Drive — Required Implementation

T-0001…T-0023 give us a working substrate plus the first-drive runbook.
This section enumerates the discrete tasks required before a user can
perform a first real end-to-end run.

The shape of the remaining gap is closed for the first test drive:
`run_once` now spawns the planner, worker, critic, and integrator wrappers,
`orch run` continuously polls that substrate with budget caps, preflight
checks are in place, and `docs/RUNBOOK.md` documents the first-drive
procedure and kill switch.

**T-0018 planner-auto-invocation — implemented**
  - Objective: When the runtime processes a `submit_request` message,
    invoke the configured planner CLI through `ModelWrapper`, capture its
    handoff, and post a `planned` message to the orchestrator inbox in
    the same `run_once` call.
  - Owned files: `orch/runtime.py`, `orch/model_wrapper.py`,
    `tests/test_runtime.py`, `tests/test_model_wrapper.py`,
    `docs/PLAN.md`.
  - Acceptance:
    - Submitting a request and calling `orch run --once` produces a plan
      artifact and at least one pending task without manual intervention.
    - Planner failures (non-zero exit, missing handoff) ack the request
      message, emit a `planning_failed` `RunOnceResult`, and write
      stderr to `.orch/logs/planner/`. The loop does not spin on the
      same failing request.
    - Tests cover the success path, missing-handoff path, and non-zero
      exit path with a mocked wrapper subprocess.

**T-0019 agent-driver loop — implemented**
  - Objective: Add a runtime-side driver that consumes the `worker`,
    `critic`, and `integrator` inboxes by invoking each role's wrapper
    through `ModelWrapper`, capturing the JSON handoff, and acking the
    role inbox message only after the wrapper succeeds.
  - Owned files: `orch/runtime.py`, `orch/model_wrapper.py`,
    `tests/test_runtime.py`, `docs/PLAN.md`.
  - Acceptance:
    - `run_once` drives at most one wrapper subprocess per call and
      remains deterministic given the same inbox state.
    - A failed wrapper leaves the role inbox message in place for retry,
      with the failure recorded under `.orch/logs/<role>/`.
    - The worker→critic→merge happy path is exercised end-to-end with
      mocked wrappers; no manual CLI invocation is required.

**T-0020 continuous-run-loop — implemented**
  - Objective: Implement `orch run` (without `--once`) as a continuous
    loop that drains actionable work, sleeps for a configurable poll
    interval, and terminates cleanly on SIGINT/SIGTERM.
  - Owned files: `orch/cli.py`, `orch/runtime.py`, `tests/test_cli.py`,
    `tests/test_runtime.py`, `.orch/config/orchestrator.toml`,
    `docs/PLAN.md`.
  - Acceptance:
    - `orch run` polls until the request is exhausted or a budget cap
      from T-0021 is hit.
    - Poll interval is read from `[runtime] poll_interval_seconds` with
      a sane default (e.g. 2s).
    - Signal handling writes a clean shutdown line to
      `.orch/logs/orchestrator/`, removes the orchestrator pid file,
      and leaves active worktrees and inbox state intact for resume.

**T-0021 budget-enforcement — implemented**
  - Objective: Wire `[budgets]` from `orchestrator.toml` into the
    runtime so first-time users have hard caps before pointing Orchestra
    at a real repo.
  - Owned files: `orch/runtime.py`, `orch/plans.py`,
    `tests/test_runtime.py`, `tests/test_plans.py`, `docs/PLAN.md`.
  - Acceptance:
    - `max_tasks_per_request` rejects plan ingest when the count would
      exceed the cap, records a `budget_exceeded` event, and does not
      write partial pending YAMLs.
    - `max_wall_clock_minutes` is enforced per request: the continuous
      loop exits with a `budget_exceeded` `RunOnceResult` once the cap
      is hit, even if work remains.
    - Budget rejections leave inbox state recoverable so a follow-up run
      with raised budgets can resume the request.

**T-0022 doctor-preflight — implemented**
  - Objective: Add `orch doctor` to verify the local environment before
    a first run.
  - Owned files: `orch/cli.py`, `orch/doctor.py`,
    `tests/test_doctor.py`, `docs/PLAN.md`.
  - Acceptance:
    - Checks: configured `gemini` and `codex` CLIs are on `PATH` and
      respond to `--version`; `docker` is reachable and the configured
      sandbox image is present (or buildable); `git` is configured with
      a user name and email; `.orch/schemas/task.schema.json` validates
      `examples/task.example.yaml`; required `.orch/` subdirectories
      exist.
    - Prints one pass/fail line per check and exits non-zero on any
      failure.
    - Does not require live model authentication beyond `--version`.

**T-0023 first-drive-runbook — implemented**
  - Objective: Document the end-to-end first-run procedure so a user can
    reproduce a successful test drive on a throwaway repo.
  - Owned files: `docs/RUNBOOK.md`, `README.md`, `docs/PLAN.md`.
  - Acceptance:
    - Step-by-step instructions for: cloning a sample target repo,
      running `orch doctor`, building the sandbox image, submitting a
      small canned request (e.g. "add a function `add(a, b)` to
      `calc.py` with a unit test"), and starting `orch run`.
    - Documents how to authenticate the Gemini and Codex CLIs (links to
      the upstream docs; Orchestra does not own those credentials).
    - Lists the artifacts to inspect after a successful run:
      `.orch/plans/`, `.orch/tasks/done/`, `.orch/patches/`,
      `.orch/logs/`, and the resulting merge commit on `main`.
    - Documents the kill switch: how to stop the loop, what state is
      safe to delete between runs, and how `startup_reconcile` resumes
      partial work.

With T-0021…T-0023 landed, a user with valid Gemini and Codex credentials
and a working Docker daemon should be able to follow `docs/RUNBOOK.md`, run
`orch doctor`, `orch image build`, `orch submit "..."`, and `orch run`, and
watch a small change land on `main` of a throwaway target repo. That is the
bar for "first test drive."

---

## 15. Delegate-Always Pivot

The next product direction is an Opus-led chat workflow where Claude Opus is
the user's strategic interface, not the coding engine. Opus should spend
tokens on understanding intent, writing high-quality prompts, decomposing
plans, and making merge/rework decisions. Implementation work should be
delegated to Codex workers by default. Gemini should be used for planning and
architecture analysis through a single-call interface, not as an agentic CLI
that explores the repo on its own.

### Target Workflow

```
User
  -> Claude Opus chat: understand goal, clarify only when necessary
  -> Gemini planner: produce architecture, task plan, risks, test strategy
  -> Claude Opus chat: convert plan into bounded Codex tasks
  -> Codex workers: edit, test, and report compact structured results
  -> Codex reviewer or Opus: review only summaries/diffs needed for decision
  -> Claude Opus chat: merge/rework/abandon and summarize outcome to user
```

This becomes the preferred workflow for `orch chat`. The older unattended
`orch submit` -> `orch run` pipeline remains available as a substrate and
regression target, but it is no longer the primary UX.

### Role Policy

- **Claude Opus chat**
  - Allowed: talk to the user, decide whether clarification is required,
    write the Gemini planning prompt, ingest the Gemini plan, author task
    YAML, dispatch workers, inspect compact summaries, request focused diffs,
    choose merge/rework/abandon.
  - Forbidden by default: editing source, reading broad file trees, reading
    full worker transcripts, and performing implementation itself.
  - May inspect code only when worker summaries identify a blocker,
    ambiguity, failed acceptance check, or suspicious diff.
- **Gemini planner**
  - Runs as a one-shot planning model call with a bounded intent packet and
    optional repo summary.
  - Produces a Markdown plan containing architecture notes, task boundaries,
    dependencies, risks, and test strategy.
  - Does not edit files and does not autonomously inspect the repo.
- **Codex worker**
  - Owns implementation. Reads relevant files, edits only `owned_files`, runs
    allowed tests, and returns a compact structured handoff.
  - Reports `needs_attention` instead of dumping logs into Opus context.
- **Codex reviewer / integrator**
  - Default reviewer for routine code correctness and test integration.
  - Opus only performs final product judgment or high-risk review when the
    task changes architecture, security, data migration, or major user-facing
    behavior.

### Context Discipline

Worker and reviewer handoffs must fit in a compact structured envelope:

```yaml
task_id: T-0031
status: completed
files_changed:
  - orch/chat.py
  - tests/test_chat.py
commands_run:
  - uv run pytest tests/test_chat.py -q
result: passed
notes:
  - Added delegate-always mode prompt routing.
needs_attention: []
```

Claude should not read `.orch/logs/**`, full patches, or full file contents
unless `needs_attention` is non-empty or the user explicitly asks for that
detail. Tool results returned to Opus should be truncated and summarized by
default, with explicit commands for full artifacts.

### Task Sizing Policy

The planner and Opus decomposer should prefer fewer, larger tasks:

| Request shape | Default task count |
|---|---|
| Tiny or greenfield, <10 files | 1 Codex task |
| Normal feature or bug fix | 1-3 Codex tasks |
| Clear disjoint surfaces | 2-4 parallel Codex tasks |
| Cross-cutting refactor or migration | 3-6 serial/parallel tasks |

Task splitting is justified only by real file ownership boundaries,
parallelism, risk isolation, or review clarity. It is not justified merely
because the plan has many conceptual steps.

### Configuration Target

Add an explicit delegate-always mode:

```toml
[mode.delegate_always]
enabled = true
chat_model = "claude-opus-4-7"
planner_model = "gemini"
worker_model = "codex-gpt-5.5"
reviewer_model = "codex-gpt-5.5"

claude_reads_files = "on_attention"
claude_reads_worker_logs = "on_failure"
gemini_interface = "sdk_single_call"
max_tasks_small_repo = 1
max_tasks_default = 3
max_parallel_workers = 2
```

This section should be loaded into runtime config without breaking the
existing `[models]`, `[runtime]`, and `[critic]` sections.

### Implementation Tasks

**T-0030 delegate-mode-config — implemented**
  - Objective: Add config support for delegate-always mode and expose it to
    chat orchestration.
  - Owned files: `orch/config.py`, `.orch/config/orchestrator.toml`,
    `tests/test_config.py`, `docs/PLAN.md`.
  - Acceptance:
    - Config loads `[mode.delegate_always]` with defaults matching this
      section.
    - Existing config files without the section continue to load.
    - Invalid read policies or non-positive task caps fail clearly.
    - Tests cover default, configured, and invalid values.

**T-0031 gemini-single-call-planner — implemented**
  - Objective: Replace Gemini CLI planner usage in chat-first planning with a
    single-call SDK runner that cannot enter an agentic tool loop.
  - Owned files: `orch/gemini_sdk_runner.py`, `orch/model_wrapper.py`,
    `tests/test_model_wrapper.py`, `tests/test_runtime.py`,
    `.orch/config/orchestrator.toml`, `docs/PLAN.md`.
  - Acceptance:
    - `gemini-planner` can be configured to use the SDK runner by default.
    - One planner invocation maps to one model request from Orchestra's
      perspective.
    - The runner emits the same `ORCH_HANDOFF` shape already consumed by
      `ModelWrapper`.
    - Tests mock the SDK boundary and verify no shell Gemini CLI is required.

**T-0032 chat-intent-packet**
  - Objective: Teach `orch chat` to create a compact planning packet before
    calling Gemini.
  - Owned files: `orch/chat.py`, `.orch/config/prompts/claude-chat.md`,
    `tests/test_chat.py`, `docs/PLAN.md`.
  - Acceptance:
    - The chat system prompt instructs Opus to call `plan` with an intent
      packet instead of broad repo-reading commands.
    - The intent packet includes goal, constraints, known repo context, risk
      level, and desired output.
    - Opus asks clarifying questions only when required to avoid risky
      assumptions.
    - Tests verify the exposed tool descriptions and prompt text encode this
      policy.

**T-0033 bounded-decomposition**
  - Objective: Add task-count guardrails to the Opus decomposition workflow.
  - Owned files: `orch/chat.py`, `orch/plans.py`,
    `.orch/config/prompts/claude-chat.md`, `tests/test_chat.py`,
    `tests/test_plans.py`, `docs/PLAN.md`.
  - Acceptance:
    - Chat guidance requires 1 task for tiny/greenfield requests unless there
      is an explicit ownership or parallelism reason.
    - Decomposition rejects or warns on plans exceeding delegate mode caps.
    - Rejection message asks Opus to consolidate tasks rather than blindly
      ingesting over-split plans.
    - Tests cover small-repo one-task guidance and cap enforcement.

**T-0034 compact-worker-handoffs**
  - Objective: Ensure Codex worker results are summarized before Opus sees
    them.
  - Owned files: `orch/model_wrapper.py`, `orch/runtime.py`, `orch/chat.py`,
    `.orch/config/prompts/codex-worker.md`, `tests/test_model_wrapper.py`,
    `tests/test_runtime.py`, `tests/test_chat.py`, `docs/PLAN.md`.
  - Acceptance:
    - Worker prompts require the compact handoff envelope documented above.
    - Chat tool payloads show handoff summary, changed files, commands, test
      result, and `needs_attention`, not raw logs.
    - Full stdout/stderr paths remain available for explicit debugging.
    - Tests verify truncation and summary behavior.

**T-0035 codex-default-review**
  - Objective: Make Codex the default code reviewer/integrator for routine
    worker output while leaving Opus in charge of final decisions.
  - Owned files: `orch/runtime.py`, `orch/review.py`, `orch/chat.py`,
    `.orch/config/orchestrator.toml`, `tests/test_runtime.py`,
    `tests/test_review.py`, `tests/test_chat.py`, `docs/PLAN.md`.
  - Acceptance:
    - Routine completed tasks can be sent to `codex-integrator` before Opus
      reads a full diff.
    - Opus receives a compact reviewer verdict and only requests full diffs
      when needed.
    - High-risk tasks can still route to Gemini critic or Opus review.
    - Existing manual `orch diff`, `orch rework`, and `orch merge` commands
      keep working.

**T-0036 delegate-chat-runbook**
  - Objective: Rewrite the primary user documentation around delegate-always
    `orch chat`.
  - Owned files: `README.md`, `docs/RUNBOOK.md`, `docs/PLAN.md`.
  - Acceptance:
    - README presents `orch chat` as the preferred workflow.
    - Runbook explains Claude login, Gemini SDK credentials, Codex CLI
      credentials, task caps, and where summaries/logs live.
    - The older `orch submit`/`orch run` path is documented as unattended
      mode, not the main recommendation.

### Success Criteria

- A small repo replacement request (for example, "replace this repo with a
  Python pygame Pong game") uses one Gemini planning call and one Codex
  worker task by default.
- Claude Opus does not read full logs or implement code on the happy path.
- The user can remain in `orch chat` while Opus delegates all coding work.
- The final answer reports changed files, commands run, and whether tests
  passed without exposing full transcripts.
- Token spend scales with uncertainty and review risk, not with the number of
  implementation steps in a simple task.

---

## Known Issues

### BUG-001 — Gemini CLI is an agentic loop, not a single-call tool

**Symptom:** `orch run --once` always hits `TerminalQuotaError` (daily quota
exhausted) on the first Gemini planner call, every session, without ever
producing a plan. The free-tier limit is 20 requests per day for
`gemini-3-flash` (Gemini 2.5 Flash).

**Root cause — architectural mismatch:** `model_wrapper.py` treats the
`gemini` CLI as a simple, one-shot inference tool: send prompt via stdin,
get response, done. In reality the Gemini CLI is a **full agentic loop**
(analogous to Claude Code). When invoked with `-p "..."`, it starts a
multi-turn agent session that autonomously decides to use tools — file reads,
GrepTool, ripgrep, etc. — to explore the workspace before producing output.
Evidence from log output:

```
Ripgrep is not available. Falling back to GrepTool.
TerminalQuotaError: You have exhausted your daily quota on this model.
```

Every tool call and every agent turn is a **separate API request**. A single
planning invocation likely costs 5–20 API requests depending on how much the
agent explores the codebase. The free-tier daily cap of 20 total requests is
therefore exhausted within one or two real runs, and every subsequent
invocation in the same calendar day hits the error immediately.

**Why it has never worked:** The quota was burned by the very first real
planning session (or earlier exploratory testing). No single run has ever
stayed within the 20-request daily budget because the agentic tool loop
consumes far more than 1 request per invocation.

**Files affected:** `orch/model_wrapper.py` (`ROLE_SPECS`, invocation model
for `gemini-planner` and `gemini-critic`).

**Fix directions (pick one):**
1. **Use the Gemini API SDK directly** (e.g. `google-generativeai` Python
   library) instead of the Gemini CLI. A direct SDK call = exactly 1 API
   request per invocation, no agentic loop, no tool calls.
2. **Upgrade to a paid Gemini API key.** The per-minute and per-day limits
   are high enough that agentic usage becomes practical, but this does not
   fix the underlying "more calls than expected" behaviour.
3. **Constrain the Gemini CLI's tool use** via a policy file or
   `--allowed-tools` flag so it cannot make additional tool calls during
   headless operation, forcing single-turn behaviour.

Option 1 is the cleanest fix for the local MVP: a thin Python wrapper that
calls the Gemini SDK once with the composed prompt and writes the response to
stdout in the same format `extract_handoff` already expects.
