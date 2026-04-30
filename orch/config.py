"""Configuration loading for Orchestra."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import tomllib
from typing import Any


DEFAULT_CONFIG_DIR = Path(".orch/config")


@dataclass(frozen=True)
class ModelConfig:
    orchestrator: str
    planner: str
    critic: str
    worker: str
    integrator: str


@dataclass(frozen=True)
class CliConfig:
    gemini: str
    codex: str
    claude: str = "claude"


@dataclass(frozen=True)
class RuntimeConfig:
    max_workers: int
    default_timeout_seconds: int
    max_retries: int
    poll_interval_seconds: int = 2


@dataclass(frozen=True)
class SandboxConfig:
    mode: str
    docker: str
    image: str
    dockerfile: str
    build_context: str
    network: str
    workdir: str


@dataclass(frozen=True)
class BudgetConfig:
    max_tasks_per_request: int
    max_wall_clock_minutes: int


@dataclass(frozen=True)
class PolicyConfig:
    forbidden_globs: tuple[str, ...]
    default_allowed_commands: tuple[str, ...]
    max_added_lines: int
    max_changed_files: int


@dataclass(frozen=True)
class OrchestraConfig:
    models: ModelConfig
    cli: CliConfig
    runtime: RuntimeConfig
    sandbox: SandboxConfig
    budgets: BudgetConfig
    policies: PolicyConfig


def load_config(config_dir: Path = DEFAULT_CONFIG_DIR) -> OrchestraConfig:
    orchestrator = _load_toml(config_dir / "orchestrator.toml")
    policies = _load_toml(config_dir / "policies.toml")

    config = OrchestraConfig(
        models=ModelConfig(
            orchestrator=_required_str(orchestrator, "models", "orchestrator"),
            planner=_required_str(orchestrator, "models", "planner"),
            critic=_required_str(orchestrator, "models", "critic"),
            worker=_required_str(orchestrator, "models", "worker"),
            integrator=_required_str(orchestrator, "models", "integrator"),
        ),
        cli=CliConfig(
            gemini=_required_str(orchestrator, "cli", "gemini"),
            codex=_required_str(orchestrator, "cli", "codex"),
            claude=_section(orchestrator, "cli").get("claude", "claude") or "claude",
        ),
        runtime=RuntimeConfig(
            max_workers=_required_int(orchestrator, "runtime", "max_workers", minimum=1),
            default_timeout_seconds=_required_int(
                orchestrator, "runtime", "default_timeout_seconds", minimum=1
            ),
            max_retries=_required_int(orchestrator, "runtime", "max_retries", minimum=0),
            poll_interval_seconds=_optional_int(
                orchestrator, "runtime", "poll_interval_seconds", minimum=1, default=2
            ),
        ),
        sandbox=SandboxConfig(
            mode=_required_choice(orchestrator, "sandbox", "mode", {"docker"}),
            docker=_required_str(orchestrator, "sandbox", "docker"),
            image=_required_str(orchestrator, "sandbox", "image"),
            dockerfile=_required_str(orchestrator, "sandbox", "dockerfile"),
            build_context=_required_str(orchestrator, "sandbox", "build_context"),
            network=_required_choice(orchestrator, "sandbox", "network", {"none", "host"}),
            workdir=_required_str(orchestrator, "sandbox", "workdir"),
        ),
        budgets=BudgetConfig(
            max_tasks_per_request=_required_int(
                orchestrator, "budgets", "max_tasks_per_request", minimum=1
            ),
            max_wall_clock_minutes=_required_int(
                orchestrator, "budgets", "max_wall_clock_minutes", minimum=1
            ),
        ),
        policies=PolicyConfig(
            forbidden_globs=_required_str_tuple(policies, "forbidden_globs"),
            default_allowed_commands=_required_str_tuple(policies, "default_allowed_commands"),
            max_added_lines=_required_int(policies, None, "max_added_lines", minimum=1),
            max_changed_files=_required_int(policies, None, "max_changed_files", minimum=1),
        ),
    )
    return config


def _load_toml(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"missing config file: {path}")
    with path.open("rb") as handle:
        data = tomllib.load(handle)
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a TOML table")
    return data


def _section(data: dict[str, Any], section: str | None) -> dict[str, Any]:
    if section is None:
        return data
    value = data.get(section)
    if not isinstance(value, dict):
        raise ValueError(f"missing [{section}] section")
    return value


def _required_str(data: dict[str, Any], section: str | None, key: str) -> str:
    value = _section(data, section).get(key)
    if not isinstance(value, str) or not value.strip():
        prefix = f"{section}." if section else ""
        raise ValueError(f"{prefix}{key} must be a non-empty string")
    return value


def _required_choice(
    data: dict[str, Any], section: str | None, key: str, choices: set[str]
) -> str:
    value = _required_str(data, section, key)
    if value not in choices:
        prefix = f"{section}." if section else ""
        allowed = ", ".join(sorted(choices))
        raise ValueError(f"{prefix}{key} must be one of: {allowed}")
    return value


def _required_int(
    data: dict[str, Any], section: str | None, key: str, *, minimum: int
) -> int:
    value = _section(data, section).get(key)
    if not isinstance(value, int) or value < minimum:
        prefix = f"{section}." if section else ""
        raise ValueError(f"{prefix}{key} must be an integer >= {minimum}")
    return value


def _optional_int(
    data: dict[str, Any],
    section: str | None,
    key: str,
    *,
    minimum: int,
    default: int,
) -> int:
    value = _section(data, section).get(key, default)
    if not isinstance(value, int) or value < minimum:
        prefix = f"{section}." if section else ""
        raise ValueError(f"{prefix}{key} must be an integer >= {minimum}")
    return value


def _required_str_tuple(data: dict[str, Any], key: str) -> tuple[str, ...]:
    value = data.get(key)
    if not isinstance(value, list) or not value:
        raise ValueError(f"{key} must be a non-empty list")
    if not all(isinstance(item, str) and item.strip() for item in value):
        raise ValueError(f"{key} must contain only non-empty strings")
    return tuple(value)
