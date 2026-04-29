"""Console entry points for external model role wrappers."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from orch.model_wrapper import ModelWrapper


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    context = {
        "task_id": args.task_id,
        "task_yaml_path": args.task_yaml_path,
        "worktree_path": args.worktree_path,
        "request_path": args.request_path,
        "plan_path": args.plan_path,
        "patch_path": args.patch_path,
        "diff_path": args.diff_path,
        "integration_worktree_path": args.integration_worktree_path,
    }
    context = {key: value for key, value in context.items() if value is not None}

    try:
        wrapper = ModelWrapper(root=args.root)
        result = wrapper.run_role(
            args.role,
            timeout_seconds=args.timeout_seconds,
            log_name=args.log_name,
            inbox_role=args.inbox_role,
            **context,
        )
    except Exception as exc:
        print(f"orch-wrapper: {exc}", file=sys.stderr)
        return 1

    print(
        json.dumps(
            {
                "role": result.role,
                "returncode": result.process.returncode,
                "timed_out": result.process.timed_out,
                "stdout_path": str(result.process.stdout_path),
                "stderr_path": str(result.process.stderr_path),
                "handoff_path": str(result.handoff_path) if result.handoff_path else None,
            },
            sort_keys=True,
        )
    )
    return 0 if result.succeeded else 1


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="orch-wrapper")
    parser.add_argument("role")
    parser.add_argument("--root", type=Path, default=Path("."))
    parser.add_argument("--timeout-seconds", type=int)
    parser.add_argument("--log-name")
    parser.add_argument("--inbox-role", default="orchestrator")
    parser.add_argument("--task-id")
    parser.add_argument("--task-yaml-path")
    parser.add_argument("--worktree-path")
    parser.add_argument("--request-path")
    parser.add_argument("--plan-path")
    parser.add_argument("--patch-path")
    parser.add_argument("--diff-path")
    parser.add_argument("--integration-worktree-path")
    return parser


def planner_main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    return main(["gemini-planner", *args])


def critic_main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    return main(["gemini-critic", *args])


def worker_main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    return main(["codex-worker", *args])


def integrator_main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    return main(["codex-integrator", *args])


if __name__ == "__main__":
    raise SystemExit(main())
