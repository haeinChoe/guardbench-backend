"""Dataset fixture parsing and seed payload construction."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from .config import ConfigurationError


def _synthetic_seed_payload(dataset: dict[str, Any]) -> tuple[dict[str, Any], int]:
    expected = dataset["source"]["expected_test_case_count"]
    test_cases = [
        {
            "name": f"perf-{index:03d}",
            "input": f"performance-case-{index:03d}",
            "expectedAction": "ALLOW",
            "severity": "LOW",
            "category": "performance",
        }
        for index in range(1, expected + 1)
    ]
    payload = {
        "name": dataset.get("name", dataset["id"]),
        "description": (
            f"Synthetic fixed-size workload for performance benchmarking ({expected} TestCases)."
        ),
        "testCases": test_cases,
    }
    return payload, expected


def seed_payload_from_dataset(dataset: dict[str, Any], dataset_path: Path) -> tuple[dict[str, Any], int]:
    """Resolve a validated dataset once; callers retain the resulting payload snapshot."""
    if dataset["source"]["type"] == "synthetic-testset":
        return _synthetic_seed_payload(dataset)

    source_path = (dataset_path.parent / dataset["source"]["path"]).resolve()
    try:
        source_bytes = source_path.read_bytes()
        source = source_bytes.decode("utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise ConfigurationError(f"Dataset source를 읽을 수 없습니다: {source_path}") from exc
    expected_sha256 = dataset["source"].get("sha256")
    actual_sha256 = hashlib.sha256(source_bytes).hexdigest()
    if expected_sha256 and actual_sha256 != expected_sha256:
        raise ConfigurationError(f"Dataset source checksum이 고정된 dataset 정의와 다릅니다: {source_path}")

    start = source.find("\n{")
    if start < 0:
        start = source.find("{")
    if start < 0:
        raise ConfigurationError(f"JSON seed payload를 찾을 수 없습니다: {source_path}")
    try:
        payload, _ = json.JSONDecoder().raw_decode(source[start + (1 if source[start] == "\n" else 0):])
    except json.JSONDecodeError as exc:
        raise ConfigurationError(f"Dataset source의 JSON payload가 올바르지 않습니다: {source_path}") from exc
    if not isinstance(payload, dict):
        raise ConfigurationError("Dataset seed payload는 object여야 합니다.")
    test_cases = payload.get("testCases")
    if not isinstance(test_cases, list):
        raise ConfigurationError("Dataset seed payload에 testCases 배열이 없습니다.")
    expected = dataset["source"]["expected_test_case_count"]
    if len(test_cases) != expected:
        raise ConfigurationError(f"Dataset {dataset['id']}의 TestCase 수가 {expected}가 아닙니다: {len(test_cases)}")
    return payload, len(test_cases)
