"""Command-line interface for Orchestra."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
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
        if args.command == "run":
            OrchestraRuntime, result_to_dict = _runtime_api()
            runtime = OrchestraRuntime.from_config(root=args.root)
            if args.once:
                result = runtime.run_once()
                print(json.dumps(result_to_dict(result), sort_keys=True))
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


def _print_run_result(result: object) -> None:
    _, result_to_dict = _runtime_api()
    print(json.dumps(result_to_dict(result), sort_keys=True), flush=True)


if __name__ == "__main__":
    raise SystemExit(main())
