"""Resolve one scenario run into immutable input snapshots without external calls."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import (
    ConfigurationError,
    parse_yaml,
    validate_connection,
    validate_dataset,
    validate_scenario,
)
from .dataset import seed_payload_from_dataset


def _read(path: Path) -> bytes:
    try:
        return path.read_bytes()
    except OSError as exc:
        raise ConfigurationError(f"설정 파일을 읽을 수 없습니다: {path}") from exc


@dataclass(frozen=True)
class ExecutionPlan:
    """JSON-backed snapshots expose detached copies, preserving the resolved input."""

    _plan_json: str
    _dataset_json: str
    _payload_json: str
    _sources: tuple[tuple[str, str, bytes], ...]

    def to_dict(self) -> dict[str, Any]:
        return json.loads(self._plan_json)

    @property
    def connection(self) -> dict[str, Any]:
        return self.to_dict()["connection"]

    @property
    def scenario(self) -> dict[str, Any]:
        return self.to_dict()["scenario"]

    @property
    def dataset(self) -> dict[str, Any]:
        return json.loads(self._dataset_json)

    @property
    def seed_payload(self) -> dict[str, Any]:
        return json.loads(self._payload_json)

    @property
    def test_case_count(self) -> int:
        return self.to_dict()["dataset"]["test_case_count"]

    @property
    def source_paths(self) -> dict[str, Path]:
        return {name: Path(path) for name, path, _ in self._sources}

    @property
    def source_bytes(self) -> dict[str, bytes]:
        return {name: source for name, _, source in self._sources}


def load_execution_plan(
    *,
    api_url: str,
    target_url: str,
    target_model: str,
    scenario_path: Path,
    dataset_path: Path,
    target_revision: str | None = None,
) -> ExecutionPlan:
    """Read, validate, hash, and resolve scenario, dataset, and connection inputs."""
    resolved_scenario_path = Path(scenario_path).resolve()
    resolved_dataset_path = Path(dataset_path).resolve()
    scenario_bytes = _read(resolved_scenario_path)
    dataset_bytes = _read(resolved_dataset_path)
    scenario = parse_yaml(scenario_bytes, resolved_scenario_path)
    dataset = parse_yaml(dataset_bytes, resolved_dataset_path)
    validate_scenario(scenario)
    validate_dataset(dataset)
    connection = validate_connection(
        api_url=api_url,
        target_url=target_url,
        target_model=target_model,
        target_revision=target_revision,
        target_type=scenario["target"]["type"],
    )
    payload, count = seed_payload_from_dataset(dataset, resolved_dataset_path)
    sources = (
        ("scenario", str(resolved_scenario_path), scenario_bytes),
        ("dataset", str(resolved_dataset_path), dataset_bytes),
    )
    plan = {
        "schema_version": 1,
        "connection": connection,
        "scenario": scenario,
        "dataset": {"id": dataset["id"], "test_case_count": count},
        "sources": {
            name: {"path": source_path, "sha256": hashlib.sha256(source).hexdigest()}
            for name, source_path, source in sources
        },
    }
    try:
        return ExecutionPlan(
            json.dumps(plan, allow_nan=False),
            json.dumps(dataset, allow_nan=False),
            json.dumps(payload, allow_nan=False),
            sources,
        )
    except (ValueError, TypeError) as exc:
        raise ConfigurationError("실행 입력은 유한한 JSON 값이어야 합니다.") from exc
