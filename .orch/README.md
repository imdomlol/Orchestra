# `.orch`

This directory contains Orchestra state.

Committed, stable inputs:

- `config/`
- `config/prompts/`
- `schemas/`

Ignored runtime state:

- `requests/`
- `plans/`
- `tasks/`
- `worktrees/`
- `logs/`
- `patches/`
- `summaries/`
- `locks/`
- `inbox/`

Runtime files are durable local state, but they are not committed by default.
