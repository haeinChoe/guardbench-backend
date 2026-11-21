"""Profile and Dataset loading with strict, environment-driven configuration."""

from __future__ import annotations

import math
import os
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import yaml


class ConfigurationError(ValueError):
    """Raised when a profile or dataset cannot be safely executed."""


_ENV_PATTERN = re.compile(r"\$\{([A-Z0-9_]+)(?::-([^}]*))?}")


def expand_environment(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: expand_environment(item) for key, item in value.items()}
    if isinstance(value, list):
        return [expand_environment(item) for item in value]
    if not isinstance(value, str):
        return value

    def replace(match: re.Match[str]) -> str:
        return os.environ.get(match.group(1), match.group(2) or "")

    return _ENV_PATTERN.sub(replace, value)


def parse_yaml(source: bytes, path: Path) -> dict[str, Any]:
    try:
        value = yaml.safe_load(source.decode("utf-8"))
    except (UnicodeDecodeError, yaml.YAMLError) as exc:
        raise ConfigurationError(f"설정 파일을 읽을 수 없습니다: {path}") from exc
    if not isinstance(value, dict):
        raise ConfigurationError(f"설정 파일 최상위는 object여야 합니다: {path}")
    return expand_environment(value)


def load_yaml(path: Path) -> dict[str, Any]:
    try:
        source = path.read_bytes()
    except OSError as exc:
        raise ConfigurationError(f"설정 파일을 읽을 수 없습니다: {path}") from exc
    return parse_yaml(source, path)


def _object(value: Any, path: str, *, required=(), optional=()) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ConfigurationError(f"{path}는 object여야 합니다.")
    missing = set(required) - value.keys()
    unknown = value.keys() - set(required) - set(optional)
    if missing or unknown:
        raise ConfigurationError(f"{path}: missing={sorted(missing)}, unknown={sorted(unknown, key=str)}")
    return value


def _string(value: Any, path: str) -> str:
    if not isinstance(value, str) or not value.strip() or "${" in value:
        raise ConfigurationError(f"{path}는 해석된 비어 있지 않은 문자열이어야 합니다.")
    return value


def _number(value: Any, path: str, *, minimum: float = 0) -> float:
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or (isinstance(value, float) and not math.isfinite(value)) or value < minimum):
        raise ConfigurationError(f"{path}는 {minimum} 이상인 숫자여야 합니다.")
    return value


def _integer(value: Any, path: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ConfigurationError(f"{path}는 {minimum} 이상인 정수여야 합니다.")
    return value


def validate_profile(profile: dict[str, Any]) -> None:
    _object(profile, "profile", required=("test", "workload", "target", "acceptance"))
    test = profile.get("test")
    workload = profile.get("workload")
    target = profile.get("target")
    acceptance = profile.get("acceptance")
    _object(test, "test", required=("id", "name", "type"))
    _string(test["id"], "test.id")
    _string(test["name"], "test.name")
    _string(test["type"], "test.type")
    if test.get("type") not in {"SMOKE", "LOAD", "PEAK", "STRESS", "SOAK"}:
        raise ConfigurationError("test.type은 SMOKE, LOAD, PEAK, STRESS, SOAK 중 하나여야 합니다.")
    _object(workload, "workload", required=("concurrent_test_runs", "completion_timeout_seconds",
            "polling_interval_seconds"), optional=("max_iterations_per_vu", "stages",
            "ramp_up_seconds", "duration_seconds"))
    _integer(workload["concurrent_test_runs"], "workload.concurrent_test_runs", minimum=1)
    if "stages" in workload:
        if "ramp_up_seconds" in workload or "duration_seconds" in workload:
            raise ConfigurationError("workload.stages는 ramp_up_seconds/duration_seconds와 혼용할 수 없습니다.")
        stages = workload["stages"]
        if not isinstance(stages, list) or not stages:
            raise ConfigurationError("workload.stages는 비어 있지 않은 배열이어야 합니다.")
        targets = []
        for index, stage in enumerate(stages):
            path = f"workload.stages[{index}]"
            _object(stage, path, required=("duration_seconds", "concurrent_test_runs"))
            duration = _number(stage["duration_seconds"], f"{path}.duration_seconds")
            if duration <= 0:
                raise ConfigurationError(f"{path}.duration_seconds는 양수여야 합니다.")
            targets.append(_integer(stage["concurrent_test_runs"], f"{path}.concurrent_test_runs"))
        if max(targets) != workload["concurrent_test_runs"]:
            raise ConfigurationError("workload.concurrent_test_runs는 stages의 최대 양수 target과 같아야 합니다.")
    else:
        for field in ("ramp_up_seconds", "duration_seconds"):
            if field not in workload:
                raise ConfigurationError(f"workload.{field}는 필수입니다.")
        _number(workload["ramp_up_seconds"], "workload.ramp_up_seconds")
        _number(workload["duration_seconds"], "workload.duration_seconds", minimum=1)
    _number(workload["completion_timeout_seconds"], "workload.completion_timeout_seconds", minimum=1)
    _number(workload["polling_interval_seconds"], "workload.polling_interval_seconds", minimum=0.1)
    if "max_iterations_per_vu" in workload:
        _integer(workload["max_iterations_per_vu"], "workload.max_iterations_per_vu")

    _object(target, "target", required=("type", "identifier", "model"), optional=("revision",))
    if target.get("type") != "HTTP_ENDPOINT":
        raise ConfigurationError("target.type은 HTTP_ENDPOINT여야 합니다.")
    for field in ("identifier", "model"):
        _string(target.get(field), f"target.{field}")
    if "revision" in target and not isinstance(target["revision"], str):
        raise ConfigurationError("target.revision은 문자열이어야 합니다.")
    try:
        parsed_target = urlparse(target["identifier"])
    except ValueError as exc:
        raise ConfigurationError("target.identifier URL이 올바르지 않습니다.") from exc
    if parsed_target.scheme not in {"http", "https"} or not parsed_target.netloc \
            or parsed_target.username or parsed_target.password or parsed_target.fragment:
        raise ConfigurationError("target.identifier는 userinfo와 fragment가 없는 HTTP/HTTPS URL이어야 합니다.")

    _object(acceptance, "acceptance", required=("api", "completion"))
    api = _object(acceptance["api"], "acceptance.api", required=("create_latency_ms", "error_rate"))
    _object(api["create_latency_ms"], "acceptance.api.create_latency_ms", required=("p50", "p95", "p99"))
    completion = _object(acceptance["completion"], "acceptance.completion",
                         required=("max_seconds", "failure_rate", "queue_drain_seconds", "dlq_messages"))
    for field in ("p50", "p95", "p99"):
        _number(api.get("create_latency_ms", {}).get(field), f"acceptance.api.create_latency_ms.{field}", minimum=0.001)
    _number(api.get("error_rate"), "acceptance.api.error_rate")
    if api["error_rate"] > 1:
        raise ConfigurationError("acceptance.api.error_rate는 0과 1 사이여야 합니다.")
    _number(completion.get("max_seconds"), "acceptance.completion.max_seconds", minimum=0.001)
    _number(completion.get("failure_rate"), "acceptance.completion.failure_rate")
    if completion["failure_rate"] > 1:
        raise ConfigurationError("acceptance.completion.failure_rate는 0과 1 사이여야 합니다.")
    _number(completion.get("queue_drain_seconds"), "acceptance.completion.queue_drain_seconds", minimum=0.001)
    _number(completion.get("dlq_messages"), "acceptance.completion.dlq_messages")


def validate_dataset(dataset: dict[str, Any]) -> None:
    source = dataset.get("source")
    _string(dataset.get("id"), "dataset.id")
    if not dataset.get("id") or dataset.get("immutable") is not True:
        raise ConfigurationError("Dataset은 id와 immutable: true를 가져야 합니다.")
    if not isinstance(source, dict):
        raise ConfigurationError("Dataset source는 object여야 합니다.")

    source_type = _string(source.get("type"), "dataset.source.type")
    expected = source.get("expected_test_case_count")
    if isinstance(expected, bool) or not isinstance(expected, int) or expected < 1:
        raise ConfigurationError("Dataset의 expected_test_case_count가 올바르지 않습니다.")

    if source_type == "http-testset":
        if not source.get("path"):
            raise ConfigurationError("http-testset Dataset source는 path를 가져야 합니다.")
        sha256 = source.get("sha256")
        if not isinstance(sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", sha256):
            raise ConfigurationError("immutable http-testset Dataset source에는 sha256 checksum이 필요합니다.")
        _string(source["path"], "dataset.source.path")
        return

    if source_type == "synthetic-testset":
        return

    raise ConfigurationError("Dataset source.type은 http-testset 또는 synthetic-testset이어야 합니다.")
