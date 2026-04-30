#!/usr/bin/env python3
"""Validate Orchestra task YAML files against the committed JSON schema."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any

import jsonschema
import yaml


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SCHEMA = REPO_ROOT / ".orch" / "schemas" / "task.schema.json"


def load_yaml(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle)
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a YAML mapping")
    return data


def load_schema(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        schema = json.load(handle)
    if not isinstance(schema, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return schema


def validate_task(task_path: Path, schema_path: Path = DEFAULT_SCHEMA) -> None:
    task = load_yaml(task_path)
    schema = load_schema(schema_path)

    validator_cls = jsonschema.validators.validator_for(schema)
    validator_cls.check_schema(schema)
    validator = validator_cls(schema, format_checker=jsonschema.FormatChecker())

    errors = sorted(validator.iter_errors(task), key=lambda error: error.path)
    if errors:
        lines = [f"{task_path} failed validation:"]
        for error in errors:
            location = ".".join(str(part) for part in error.absolute_path) or "<root>"
            lines.append(f"- {location}: {error.message}")
        raise ValueError("\n".join(lines))


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("task", type=Path, help="Path to a task YAML file")
    parser.add_argument(
        "--schema",
        type=Path,
        default=DEFAULT_SCHEMA,
        help=f"Path to task schema. Defaults to {DEFAULT_SCHEMA}",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    try:
        validate_task(args.task, args.schema)
    except Exception as exc:  # noqa: BLE001 - CLI should print validation errors.
        print(exc, file=sys.stderr)
        return 1
    print(f"{args.task} is valid")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
