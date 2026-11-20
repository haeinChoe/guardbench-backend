"""S3 persistence for completed and failed performance-run artifacts."""

from __future__ import annotations

import hashlib
import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import boto3
from botocore.exceptions import BotoCoreError, ClientError


RESULTS_PREFIX = "performance/results"
FAILURES_PREFIX = "performance/failures"
FAILURE_FILENAME = "failure.json"
UPLOAD_COMPLETE_FILENAME = "upload-complete.json"
UPLOAD_COMPLETE_SCHEMA_VERSION = 1

RESULT_FILENAMES = (
    "profile.yaml",
    "dataset.yaml",
    "k6-summary.json",
    "aws-metrics.json",
    "result.json",
    "report.md",
)
# Provisional optional artifacts for pending #20 (DRAFT ADR 0016) / #21 / #24 integration; RESULT_FILENAMES remains strict.
OPTIONAL_RESULT_FILENAMES = (
    "experiment.yaml",
    "execution-plan.json",
)
PARTIAL_ARTIFACT_FILENAMES = (FAILURE_FILENAME, *RESULT_FILENAMES, *OPTIONAL_RESULT_FILENAMES)


class ResultUploadError(RuntimeError):
    """Raised when performance-run artifacts cannot be persisted to S3."""


def result_key(run_id: str, filename: str) -> str:
    """Return the canonical S3 object key for a completed run artifact."""
    return f"{RESULTS_PREFIX}/{run_id}/{filename}"


def failure_key(run_id: str, filename: str) -> str:
    """Return the separate S3 object key for failure diagnostics and partial artifacts."""
    return f"{FAILURES_PREFIX}/{run_id}/{filename}"


def _read_result_schema_version(result_dir: Path) -> int | None:
    result_path = result_dir / "result.json"
    try:
        payload = json.loads(result_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    version = payload.get("schema_version")
    if isinstance(version, int) and not isinstance(version, bool):
        return version
    return None


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def upload_result_directory(result_dir: Path, bucket: str, run_id: str, *, s3_client=None) -> None:
    if not bucket.strip():
        raise ResultUploadError("PERFORMANCE_RESULTS_BUCKET이 비어 있습니다.")
    if (result_dir / FAILURE_FILENAME).is_file():
        raise ResultUploadError(
            "실패 진단 파일(failure.json)이 포함된 디렉터리는 완료 결과 경로로 업로드할 수 없습니다."
        )
    missing = [name for name in RESULT_FILENAMES if not (result_dir / name).is_file()]
    if missing:
        raise ResultUploadError(f"업로드할 완료 결과 파일이 없습니다: {', '.join(missing)}")
    files_to_upload = [
        *RESULT_FILENAMES,
        *(name for name in OPTIONAL_RESULT_FILENAMES if (result_dir / name).is_file()),
    ]
    file_hashes: dict[str, str] = {}
    try:
        client = s3_client or boto3.client("s3")
        for filename in files_to_upload:
            local_path = result_dir / filename
            client.upload_file(str(local_path), bucket, result_key(run_id, filename))
            file_hashes[filename] = _sha256_file(local_path)

        marker: dict[str, object] = {
            "schema_version": UPLOAD_COMPLETE_SCHEMA_VERSION,
            "run_id": run_id,
            "status": "COMPLETE",
            "completed_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "files": list(files_to_upload),
            "file_sha256": file_hashes,
        }
        result_schema_version = _read_result_schema_version(result_dir)
        if result_schema_version is not None:
            marker["result_schema_version"] = result_schema_version

        with tempfile.TemporaryDirectory() as marker_dir:
            marker_path = Path(marker_dir) / UPLOAD_COMPLETE_FILENAME
            marker_path.write_text(json.dumps(marker, indent=2) + "\n", encoding="utf-8")
            client.upload_file(str(marker_path), bucket, result_key(run_id, UPLOAD_COMPLETE_FILENAME))
    except (BotoCoreError, ClientError, OSError) as exc:
        raise ResultUploadError("S3 결과 업로드에 실패했습니다.") from exc


def upload_failure_directory(result_dir: Path, bucket: str, run_id: str, *, s3_client=None) -> list[str]:
    """Upload failure.json and any existing partial artifacts to the failure prefix."""
    if not bucket.strip():
        raise ResultUploadError("PERFORMANCE_RESULTS_BUCKET이 비어 있습니다.")
    if not (result_dir / FAILURE_FILENAME).is_file():
        raise ResultUploadError(f"업로드할 실패 진단 파일이 없습니다: {FAILURE_FILENAME}")
    files_to_upload = [name for name in PARTIAL_ARTIFACT_FILENAMES if (result_dir / name).is_file()]
    uploaded_keys: list[str] = []
    try:
        client = s3_client or boto3.client("s3")
        for filename in files_to_upload:
            key = failure_key(run_id, filename)
            client.upload_file(str(result_dir / filename), bucket, key)
            uploaded_keys.append(key)
    except (BotoCoreError, ClientError, OSError) as exc:
        raise ResultUploadError("S3 실패 진단 결과 업로드에 실패했습니다.") from exc
    return uploaded_keys
