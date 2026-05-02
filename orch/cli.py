"""Command-line interface for Orchestra."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import shlex
import signal
import sys
import threading

from orch.doctor import Doctor
from orch.images import SandboxImageBuilder

OrchestraRuntime = None


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "submit":
            OrchestraRuntime, result_to_dict = _runtime_api()
            runtime = OrchestraRuntime.from_config(root=args.root)
            result = runtime.submit(args.prompt)
            print(result.request_path)
            return 0
        if args.command == "plan":
            OrchestraRuntime, result_to_dict = _runtime_api()
            runtime = OrchestraRuntime.from_config(root=args.root)
            print(runtime.plan_only(args.request))
            return 0
        if args.command == "decompose":
            from orch.runtime import ingest_task_yaml

            print(ingest_task_yaml(sys.stdin.read(), root=args.root))
            return 0
        if args.command == "dispatch":
            OrchestraRuntime, result_to_dict = _runtime_api()
            runtime = OrchestraRuntime.from_config(
                root=args.root,
                on_progress=_print_progress,
                model_stderr_sink=_model_stderr_sink,
            )
            result = runtime.dispatch_task(args.task_id)
            print(result.task_path)
            return 0
        if args.command == "diff":
            OrchestraRuntime, result_to_dict = _runtime_api()
            runtime = OrchestraRuntime.from_config(root=args.root)
            result = runtime.export_diff(args.task_id)
            print(result.contents, end="")
            return 0
        if args.command == "rework":
            OrchestraRuntime, result_to_dict = _runtime_api()
            runtime = OrchestraRuntime.from_config(
                root=args.root,
                on_progress=_print_progress,
                model_stderr_sink=_model_stderr_sink,
            )
            result = runtime.rework_task(args.task_id, args.notes)
            print(result.task_path)
            return 0
        if args.command == "merge":
            OrchestraRuntime, result_to_dict = _runtime_api()
            runtime = OrchestraRuntime.from_config(
                root=args.root,
                on_progress=_print_progress,
                model_stderr_sink=_model_stderr_sink,
            )
            result = runtime.merge_task(args.task_id)
            if result.merged:
                print(result.merge_result.message)
                return 0
            print(result.merge_result.message or result.merge_result.status, file=sys.stderr)
            return 1
        if args.command == "gemini-review":
            OrchestraRuntime, result_to_dict = _runtime_api()
            runtime = OrchestraRuntime.from_config(
                root=args.root,
                on_progress=_print_progress,
                model_stderr_sink=_model_stderr_sink,
            )
            handoff = runtime.review_with_gemini(args.task_id)
            print(json.dumps(handoff, indent=2, sort_keys=True))
            return 0
        if args.command == "list-tasks":
            from orch.task_store import TaskStore

            store = TaskStore(args.root)
            states = ["pending", "active", "done"] if args.status == "all" else [args.status]
            payload = {
                state: [str(path.relative_to(args.root)) for path in store.list_tasks(state)]
                for state in states
            }
            print(json.dumps(payload, indent=2))
            return 0
        if args.command == "chat":
            from orch.chat import run_chat

            request = " ".join(args.request) if args.request else None
            return run_chat(
                root=args.root,
                request=request,
                model=args.model,
                no_cache=args.no_cache,
                once=args.once,
                dry_run=args.dry_run,
            )
        if args.command == "run":
            OrchestraRuntime, result_to_dict = _runtime_api()
            runtime = OrchestraRuntime.from_config(
                root=args.root,
                on_progress=_print_progress,
                on_confirm=_ask_confirm,
                model_stderr_sink=_model_stderr_sink,
            )
            if args.once:
                result = runtime.run_once()
                _print_run_result(result)
                return 0
            stop_event = threading.Event()
            previous_handlers: dict[int, signal.Handlers] = {}

            def request_stop(signum: int, frame: object) -> None:
                stop_event.set()

            for signum in (signal.SIGINT, signal.SIGTERM):
                previous_handlers[signum] = signal.getsignal(signum)
                signal.signal(signum, request_stop)
            try:
                result = runtime.run(
                    stop_requested=stop_event.is_set,
                    on_result=_print_run_result,
                )
                return 0 if result.kind in {"idle", "stopped"} else 1
            finally:
                for signum, handler in previous_handlers.items():
                    signal.signal(signum, handler)
        if args.command == "image":
            builder = SandboxImageBuilder.from_config(root=args.root)
            if args.image_command == "build":
                if args.print:
                    print(shlex.join(builder.build_argv(no_cache=args.no_cache, pull=args.pull)))
                    return 0
                result = builder.build(no_cache=args.no_cache, pull=args.pull)
                if result.stdout:
                    print(result.stdout, end="")
                if result.stderr:
                    print(result.stderr, end="", file=sys.stderr)
                return 0 if result.succeeded else result.returncode
            parser.error("image requires a subcommand")
        if args.command == "doctor":
            report = Doctor.from_config(root=args.root).run()
            for line in report.lines():
                print(line)
            return 0 if report.passed else 1
        parser.print_help()
        return 2
    except Exception as exc:
        print(f"orch: {exc}", file=sys.stderr)
        return 1


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="orch")
    parser.add_argument(
        "--root",
        type=Path,
        default=Path("."),
        help="repository root containing .orch/",
    )
    subparsers = parser.add_subparsers(dest="command")

    submit = subparsers.add_parser("submit", help="record a request")
    submit.add_argument("prompt")

    plan = subparsers.add_parser(
        "plan",
        help="produce a plan without ingesting tasks",
    )
    plan.add_argument("request")

    subparsers.add_parser(
        "decompose",
        help="validate task YAML from stdin and write it to pending tasks",
    )

    dispatch = subparsers.add_parser(
        "dispatch",
        help="dispatch one pending task and run its worker synchronously",
    )
    dispatch.add_argument("task_id")

    diff = subparsers.add_parser(
        "diff",
        help="export and print the main..task/<id> diff for a task",
    )
    diff.add_argument("task_id")

    rework = subparsers.add_parser(
        "rework",
        help="record manual review notes and rerun the worker synchronously",
    )
    rework.add_argument("task_id")
    rework.add_argument("--notes", required=True, help="review notes for the worker")

    merge = subparsers.add_parser(
        "merge",
        help="merge one reviewed task through integration review",
    )
    merge.add_argument("task_id")

    gemini_review = subparsers.add_parser(
        "gemini-review",
        help="synchronously run the Gemini critic against a task and print the handoff JSON",
    )
    gemini_review.add_argument("task_id")

    list_tasks = subparsers.add_parser("list-tasks", help="list task YAML paths by status")
    list_tasks.add_argument(
        "--status",
        choices=("pending", "active", "done", "all"),
        default="all",
    )

    chat = subparsers.add_parser("chat", help="run an interactive Opus-driven orchestrator")
    chat.add_argument("request", nargs="*", help="initial request; stdin is used with --once if omitted")
    chat.add_argument("--model", help="Claude model id")
    chat.add_argument("--no-cache", action="store_true", help="disable prompt caching hints")
    chat.add_argument("--once", action="store_true", help="run a single non-interactive turn")
    chat.add_argument("--dry-run", action="store_true", help="validate chat setup without an API call")

    run = subparsers.add_parser("run", help="run the orchestrator loop")
    run.add_argument("--once", action="store_true", help="process one event")

    image = subparsers.add_parser("image", help="manage sandbox images")
    image_subparsers = image.add_subparsers(dest="image_command")
    build = image_subparsers.add_parser("build", help="build the sandbox image")
    build.add_argument("--print", action="store_true", help="print the docker build command")
    build.add_argument("--no-cache", action="store_true", help="pass --no-cache to docker build")
    build.add_argument("--pull", action="store_true", help="pass --pull to docker build")

    subparsers.add_parser("doctor", help="check local prerequisites")
    return parser


def _runtime_api():
    from orch.runtime import OrchestraRuntime as RuntimeClass, result_to_dict

    runtime_class = OrchestraRuntime or RuntimeClass
    return runtime_class, result_to_dict


_RESULT_LABELS: dict[str, str] = {
    "idle": "Idle — nothing to do",
    "stopped": "Stopped",
    "dispatched": "Task dispatched",
    "plan_ingested": "Plan ingested",
    "planning_failed": "Planning failed",
    "plan_rejected": "Plan rejected",
    "critic_dispatched": "Sent to critic",
    "critic_rework_dispatched": "Critic requested changes — reworking",
    "agent_ran": "Agent completed",
    "agent_failed": "Agent failed",
    "merged": "Merged",
    "merge_failed_reworking": "Merge failed — reworking",
    "escalated": "Escalated (blocked)",
    "abandoned": "Abandoned",
    "budget_exceeded": "Budget exceeded",
    "ignored_message": "Ignored message",
}


_STDERR_SUPPRESS = re.compile(
    r"^\s*(at |file:///|\[object Object\]|Full report available at:|bundle/chunk-"
    r"|Ripgrep is not available|Falling back to GrepTool"
    r"|cause:|retryDelayMs:|reason:)\s*",
)

_QUOTA_RE = re.compile(r"Please retry in ([\d.]+)s")


def _model_stderr_sink(line: str) -> None:
    """Filter model stderr — suppress stack traces, show clean error summaries."""
    stripped = line.strip()
    if not stripped:
        return
    if _STDERR_SUPPRESS.match(stripped):
        return

    if "TerminalQuotaError" in stripped or "exhausted your daily quota" in stripped:
        match = _QUOTA_RE.search(stripped)
        if match:
            secs = int(float(match.group(1)))
            print(f"  ✗ Quota exceeded — retry in {secs}s", flush=True)
        else:
            print("  ✗ Quota exceeded for this model", flush=True)
        return

    if "API key not valid" in stripped or "API_KEY_INVALID" in stripped:
        print("  ✗ Invalid API key — set a valid key and retry", flush=True)
        return

    if "code: 429" in stripped or '"code":429' in stripped:
        return

    print(f"  · {stripped}", flush=True)


def _print_progress(message: str) -> None:
    print(f"  → {message}", flush=True)


def _ask_confirm(message: str) -> bool:
    try:
        answer = input(f"\n{message} [y/N] ").strip().lower()
        return answer in ("y", "yes")
    except (EOFError, KeyboardInterrupt):
        return False


def _print_run_result(result: object) -> None:
    kind = getattr(result, "kind", "unknown")
    message = getattr(result, "message", "")
    label = _RESULT_LABELS.get(kind, kind)
    print(f"[{label}] {message}", flush=True)


if __name__ == "__main__":
    raise SystemExit(main())
