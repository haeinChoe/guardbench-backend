"""Resolve one experiment into immutable input snapshots without external calls."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import (
    ConfigurationError, _integer, _object, _string, parse_yaml,
    validate_dataset, validate_profile,
)
from .dataset import seed_payload_from_manifest


def _read(path: Path) -> bytes:
    try:
        return path.read_bytes()
    except OSError as exc:
        raise ConfigurationError(f"설정 파일을 읽을 수 없습니다: {path}") from exc


def _capacity_units(value: Any, path: str) -> str:
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return str(value)
    if isinstance(value, str) and re.fullmatch(r"[0-9]+", value) and int(value) > 0:
        return str(int(value))
    raise ConfigurationError(f"{path}는 양의 정수 또는 정수 문자열이어야 합니다.")


def validate_experiment(manifest: dict[str, Any]) -> None:
    _object(manifest, "experiment", required=("schema_version", "id", "mode", "infrastructure", "profile", "dataset"))
    if type(manifest["schema_version"]) is not int or manifest["schema_version"] != 1:
        raise ConfigurationError("experiment.schema_version은 1이어야 합니다.")
    for field in ("id", "mode", "profile", "dataset"):
        _string(manifest[field], f"experiment.{field}")
    if manifest["mode"] not in {"synthetic", "aws"}:
        raise ConfigurationError("experiment.mode는 synthetic 또는 aws여야 합니다.")
    infrastructure = _object(manifest["infrastructure"], "infrastructure", required=("id", "revision", "expected"))
    for field in ("id", "revision"):
        _string(infrastructure[field], f"infrastructure.{field}")
    expected = _object(infrastructure["expected"], "infrastructure.expected", required=("ecs", "rds", "sagemaker"))
    ecs = _object(expected["ecs"], "infrastructure.expected.ecs", required=("worker",), optional=("api",))
    for role, service in ecs.items():
        fields = ("cluster_identifier", "service_identifier", "task_cpu", "task_memory", "desired_count")
        if role == "worker":
            fields += ("work_items_concurrency",)
        path = f"infrastructure.expected.ecs.{role}"
        _object(service, path, required=fields)
        for field in ("cluster_identifier", "service_identifier"):
            _string(service[field], f"{path}.{field}")
        for field in ("task_cpu", "task_memory"):
            service[field] = _capacity_units(service[field], f"{path}.{field}")
        _integer(service["desired_count"], f"{path}.desired_count", minimum=1)
        if role == "worker":
            _integer(service["work_items_concurrency"], f"{path}.work_items_concurrency", minimum=1)
    rds = _object(expected["rds"], "infrastructure.expected.rds", required=("db_instance_identifier", "db_instance_class"))
    for field, value in rds.items():
        _string(value, f"infrastructure.expected.rds.{field}")
    sagemaker = _object(expected["sagemaker"], "infrastructure.expected.sagemaker", required=(
        "endpoint_name", "production_variant_name", "instance_type", "desired_instance_count"))
    for field in ("endpoint_name", "production_variant_name", "instance_type"):
        _string(sagemaker[field], f"infrastructure.expected.sagemaker.{field}")
    _integer(sagemaker["desired_instance_count"], "infrastructure.expected.sagemaker.desired_instance_count", minimum=1)


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
    def profile(self) -> dict[str, Any]:
        return self.to_dict()["profile"]

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


def load_execution_plan(path: Path) -> ExecutionPlan:
    """Read, validate, hash and resolve a manifest and its source-relative inputs."""
    path = Path(path).resolve()
    experiment_bytes = _read(path)
    manifest = parse_yaml(experiment_bytes, path)
    validate_experiment(manifest)
    profile_path = (path.parent / manifest["profile"]).resolve()
    dataset_path = (path.parent / manifest["dataset"]).resolve()
    profile_bytes = _read(profile_path)
    dataset_bytes = _read(dataset_path)
    profile = parse_yaml(profile_bytes, profile_path)
    dataset = parse_yaml(dataset_bytes, dataset_path)
    validate_profile(profile)
    validate_dataset(dataset)
    payload, count = seed_payload_from_manifest(dataset, dataset_path)
    sources = (("experiment", str(path), experiment_bytes),
               ("profile", str(profile_path), profile_bytes),
               ("dataset", str(dataset_path), dataset_bytes))
    plan = {
        "schema_version": 1,
        "experiment": {"id": manifest["id"], "mode": manifest["mode"]},
        "infrastructure": manifest["infrastructure"],
        "profile": profile,
        "dataset": {"id": dataset["id"], "test_case_count": count},
        "sources": {name: {"path": source_path, "sha256": hashlib.sha256(source).hexdigest()}
                    for name, source_path, source in sources},
    }
    try:
        return ExecutionPlan(json.dumps(plan, allow_nan=False), json.dumps(dataset, allow_nan=False),
                             json.dumps(payload, allow_nan=False), sources)
    except (ValueError, TypeError) as exc:
        raise ConfigurationError("실행 입력은 유한한 JSON 값이어야 합니다.") from exc
