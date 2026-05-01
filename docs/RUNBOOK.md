# First-Drive Runbook

This runbook walks through the first end-to-end Orchestra test drive on a
throwaway repository. Use a disposable repo for this procedure: the goal is
to verify the local planner, worker, critic, sandbox, and merge loop before
pointing Orchestra at anything valuable.

## 1. Prepare Orchestra

From the Orchestra checkout in Linux, macOS, or WSL:

```bash
python -m venv .venv
. .venv/bin/activate
pip install -e ".[dev]"
```

On Windows, run Orchestra from WSL for the first drive because the inbox
uses `fcntl` file locks:

```bash
cd /mnt/c/Users/<you>/path/to/Orchestra
```

If you are only editing docs or checking non-runtime commands from
PowerShell, use the Windows activation path instead:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
```

Confirm the CLI is installed:

```bash
orch --help
```

## 2. Authenticate Model CLIs

Install and authenticate the model CLIs in the same shell where you will run
`orch run`.

- Gemini CLI: follow the official Gemini CLI authentication guide:
  <https://google-gemini.github.io/gemini-cli/docs/get-started/authentication.html>
- Codex CLI: follow the official Codex CLI authentication guidance:
  <https://help.openai.com/en/articles/11381614>

At minimum, these commands must succeed:

```bash
gemini --version
codex --version
```

For unattended runs, prefer non-interactive authentication methods supported
by each CLI, such as environment variables or previously cached credentials.
Orchestra only invokes the CLIs; it does not manage provider credentials.

## 3. Create And Clone A Throwaway Target Repo

Create a small seed repository, then clone it into the directory Orchestra
will modify:

```bash
mkdir -p /tmp/orchestra-first-drive-seed
cd /tmp/orchestra-first-drive-seed
git init -b main
cat > calc.py <<'PY'
def subtract(a, b):
    return a - b
PY
cat > test_calc.py <<'PY'
from calc import subtract


def test_subtract():
    assert subtract(3, 1) == 2
PY
cat > pyproject.toml <<'TOML'
[project]
name = "orchestra-first-drive"
version = "0.1.0"
requires-python = ">=3.11"

[tool.pytest.ini_options]
pythonpath = ["."]
TOML
git add .
git commit -m "initial calc project"

cd /tmp
git clone /tmp/orchestra-first-drive-seed orchestra-first-drive
cd /tmp/orchestra-first-drive
```

Run the baseline test once:

```bash
python -m pip install pytest
pytest -q
```

## 4. Copy Orchestra Control Files Into The Target

Orchestra keeps runtime state in the repo it is operating on. From the target
repo, copy the committed Orchestra control files from your Orchestra checkout
and create the runtime directory skeleton:

```bash
mkdir -p .orch
cp -R /path/to/Orchestra/.orch/config .orch/config
cp -R /path/to/Orchestra/.orch/schemas .orch/schemas
cp -R /path/to/Orchestra/examples examples
cp -R /path/to/Orchestra/docker docker

mkdir -p \
  .orch/requests \
  .orch/plans \
  .orch/tasks/pending \
  .orch/tasks/active \
  .orch/tasks/done \
  .orch/worktrees \
  .orch/logs/orchestrator \
  .orch/logs/planner \
  .orch/logs/critic \
  .orch/logs/workers \
  .orch/logs/integrator \
  .orch/patches \
  .orch/summaries \
  .orch/locks \
  .orch/inbox/orchestrator \
  .orch/inbox/worker \
  .orch/inbox/critic \
  .orch/inbox/integrator
```

Use the installed `orch` command from the Orchestra virtualenv. Do not replace
the target repo's own source files or `pyproject.toml`; they belong to the
project under test.

If your shell cannot find `orch`, either activate the Orchestra virtualenv or
run it by module from the Orchestra checkout.

## 5. Run Preflight

From the throwaway target repo:

```bash
orch doctor
```

Fix any failing checks before continuing. The doctor checks the configured
Gemini and Codex commands, Docker reachability, git identity, schema
validation, and required `.orch/` directories.

## 6. Build The Sandbox Image

Inspect the Docker build command first:

```bash
orch image build --print
```

Then build the configured sandbox image:

```bash
orch image build
```

The default image tag and Dockerfile come from
`.orch/config/orchestrator.toml`:

```toml
[sandbox]
image = "orchestra-sandbox:py3.12"
dockerfile = "docker/orchestra-sandbox.Dockerfile"
build_context = "."
```

## 7. Submit A Canned Request

Use a tiny request with an obvious testable outcome:

```bash
orch submit "Add a function add(a, b) to calc.py with a pytest unit test in test_calc.py."
```

This writes a request under `.orch/requests/` and posts a
`submit_request` message to `.orch/inbox/orchestrator/`.

## 8. Start The Runtime

For the first drive, tick the event loop manually until you see each stage:

```bash
orch run --once
orch run --once
orch run --once
orch run --once
orch run --once
orch run --once
```

Expected progression:

```text
planning / plan_ingested
dispatched
agent_ran for worker
critic_dispatched
agent_ran for critic
merged
```

After the one-shot path is understood, run continuously:

```bash
orch run
```

Stop the continuous loop with `Ctrl-C` or SIGTERM. The runtime logs a clean
shutdown and removes `.orch/locks/orchestrator.pid`.

## 8a. Opus-driven mode (`orch chat`)

For human-supervised work, prefer the terminal chat driver. It uses the
Claude Agent SDK, so authenticate Claude Code first or set an API key in the
shell where you run it:

```bash
claude login
# or: export ANTHROPIC_API_KEY=...
orch chat "Add a function add(a, b) to calc.py with a pytest unit test."
```

Opus will decide when to call `orch plan`, write task YAML through
`orch decompose`, run workers with `orch dispatch`, inspect patches with
`orch diff`, and then choose `orch rework` or `orch merge`. Use follow-up
messages at the `you> ` prompt, `/model <id>` to switch models, `/save` to
print the transcript path, and `/quit` to leave. For scripts and smoke tests:

```bash
orch chat --once "say hi"
orch chat --once --dry-run "say hi"
```

The dry-run form is the only mode that does not require Claude Code OAuth or
`ANTHROPIC_API_KEY`. Transcripts live under `.orch/logs/chat/`.

## 9. Inspect Successful Artifacts

After a successful run, inspect:

- `.orch/plans/` for the planner artifact.
- `.orch/tasks/done/` for the merged task YAML.
- `.orch/patches/` for the critic diff and merge patch.
- `.orch/logs/` for planner, worker, critic, integrator, and orchestrator
  transcripts.
- `git log --oneline --decorate --max-count=5` for the resulting merge commit
  on `main`.
- `calc.py` and `test_calc.py` for the implemented function and test.

Run the final target test suite:

```bash
pytest -q
```

## 10. Kill Switch And Cleanup

Use `Ctrl-C` to stop `orch run`. If a process is wedged, terminate it with
SIGTERM before deleting state:

```bash
pkill -TERM -f "orch run"
```

State that is safe to delete between throwaway runs:

```text
.orch/requests/
.orch/plans/
.orch/tasks/
.orch/worktrees/
.orch/patches/
.orch/logs/
.orch/inbox/
.orch/locks/
.orch/summaries/
```

Keep:

```text
.orch/config/
.orch/schemas/
```

Also remove leftover task branches and integration worktrees if a run stopped
mid-merge:

```bash
git worktree list
git worktree remove .orch/worktrees/_integration
git branch --list "task/T-*"
git branch --list "integrate/T-*"
```

Delete only the worktrees and branches that belong to the throwaway run.

## 11. Resume Behavior

`orch run --once` and `orch run` call startup reconciliation before processing
work. Reconciliation clears stale pid files, reads active task YAMLs, replays
the orchestrator inbox oldest-first, and treats files plus inbox messages as
authoritative. If the run was interrupted after dispatching a worker or critic
message, start the loop again from the same target repo:

```bash
orch run --once
```

If the budget cap stopped the run, raise the relevant value in
`.orch/config/orchestrator.toml` and run again:

```toml
[budgets]
max_tasks_per_request = 5
max_wall_clock_minutes = 60
```
