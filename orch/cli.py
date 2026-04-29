"""Command-line interface for Orchestra."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from orch.runtime import OrchestraRuntime, result_to_dict


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "submit":
            runtime = OrchestraRuntime.from_config(root=args.root)
            result = runtime.submit(args.prompt)
            print(result.request_path)
            return 0
        if args.command == "run":
            runtime = OrchestraRuntime.from_config(root=args.root)
            if args.once:
                result = runtime.run_once()
                print(json.dumps(result_to_dict(result), sort_keys=True))
                return 0
            parser.error("only --once is implemented")
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
    return parser


if __name__ == "__main__":
    raise SystemExit(main())
