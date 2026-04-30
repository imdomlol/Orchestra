"""Single-shot Gemini SDK wrapper: reads prompt from stdin, writes response to stdout.

Replaces the Gemini CLI subprocess to avoid the agentic multi-turn loop that
burns free-tier quota (each tool call = one API request). This script makes
exactly one generate_content call per invocation.

Usage:
    echo "your prompt" | python -m orch.gemini_sdk_runner [--model MODEL]

Environment:
    GEMINI_API_KEY  required
    GEMINI_MODEL    optional model override (default: gemini-2.5-flash)
"""

from __future__ import annotations

import argparse
import os
import sys


_DEFAULT_MODEL = "gemini-2.5-flash"


_VERSION = "1.0.0"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="gemini_sdk_runner", add_help=False)
    parser.add_argument("--model", default=None)
    parser.add_argument("--version", action="store_true")
    args, _ = parser.parse_known_args(argv)

    if args.version:
        print(f"gemini_sdk_runner {_VERSION}")
        return 0

    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        print("gemini_sdk_runner: GEMINI_API_KEY is not set", file=sys.stderr)
        return 1

    try:
        from google import genai
    except ImportError:
        print(
            "gemini_sdk_runner: google-genai is not installed; run: uv add google-genai",
            file=sys.stderr,
        )
        return 1

    prompt = sys.stdin.read()
    if not prompt.strip():
        print("gemini_sdk_runner: empty prompt on stdin", file=sys.stderr)
        return 1

    model = args.model or os.environ.get("GEMINI_MODEL", _DEFAULT_MODEL)

    client = genai.Client(api_key=api_key)
    response = client.models.generate_content(model=model, contents=prompt)
    print(response.text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
