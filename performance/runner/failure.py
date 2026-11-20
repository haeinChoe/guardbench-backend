"""Failure diagnostics and partial artifact preservation for the performance runner."""

from __future__ import annotations

import json
import re
import shutil
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .storage import FAILURE_FILENAME, upload_failure_directory

FAILURE_SCHEMA_VERSION = 1
FAILURE_STATUS = "ERROR"
EXECUTION_PHASES = ("preflight", "seed", "k6", "drain", "metrics", "report", "upload")

_URL_USERINFO_PATTERN = re.compile(r"(https?://)([^/\s:@]+(?::[^/\s@]+)?@)")
_BEARER_PATTERN = re.compile(r"(?i)\b(Bearer\s+)([^\s,;'\"]+)")
_SECRET_KV_PATTERN = re.compile(
    r"(?i)\b(authorization|password|passwd|secret|token|api[_-]?key|access[_-]?key"
    r"|aws_secret_access_key|aws_session_token|x-amz-security-token|x-amz-signature)"
    r"(\s*[:=]\s*)([\"']?)(?!Bearer\s)([^\s,;'\"]+)\3"
)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


@dataclass
class RunContext:
    """Mutable execution metadata captured progressively for failure preservation."""

    run_id: str
    started_at: datetime
    result_dir: Path | None = None
    result_dir_owned: bool = False
    phase: str = "preflight"
    inputs: dict[str, Any] = field(default_factory=dict)
    revisions: dict[str, str] = field(default_factory=dict)
    dry_run: bool = False

    def set_phase(self, phase: str) -> None:
        if phase not in EXECUTION_PHASES:
            raise ValueError(f"Unsupported execution phase: {phase}")
        self.phase = phase

    def record_input(self, key: str, value: Any) -> None:
        if value is None:
            return
        if isinstance(value, str) and not value.strip():
            return
        self.inputs[key] = value

    def record_revision(self, key: str, value: str) -> None:
        cleaned = value.strip()
        if cleaned and cleaned.lower() != "unknown":
            self.revisions[key] = cleaned

    def record_experiment(
        self,
        *,
        experiment_path: Path | str | None = None,
        experiment_id: str | None = None,
        infrastructure_id: str | None = None,
        infrastructure_revision: str | None = None,
        mode: str | None = None,
    ) -> None:
        if experiment_path is not None:
            raw_path = str(experiment_path).strip()
            if raw_path:
                self.record_input("experiment_path", str(Path(raw_path).resolve()))
        self.record_input("experiment_id", experiment_id)
        self.record_input("infrastructure_id", infrastructure_id)
        self.record_input("infrastructure_revision", infrastructure_revision)
        self.record_input("experiment_mode", mode)

    def record_execution_plan(self, plan: Mapping[str, Any]) -> None:
        experiment = plan.get("experiment")
        experiment_dict = experiment if isinstance(experiment, Mapping) else {}
        infrastructure = plan.get("infrastructure")
        infrastructure_dict = infrastructure if isinstance(infrastructure, Mapping) else {}
        sources = plan.get("sources")
        sources_dict = sources if isinstance(sources, Mapping) else {}
        exp_source = sources_dict.get("experiment")
        exp_source_dict = exp_source if isinstance(exp_source, Mapping) else {}

        self.record_experiment(
            experiment_path=exp_source_dict.get("path"),
            experiment_id=experiment_dict.get("id"),
            infrastructure_id=infrastructure_dict.get("id"),
            infrastructure_revision=infrastructure_dict.get("revision"),
            mode=experiment_dict.get("mode"),
        )

        profile = plan.get("profile")
        if isinstance(profile, Mapping):
            test = profile.get("test")
            if isinstance(test, Mapping):
                self.record_input("profile_id", test.get("id"))
                self.record_input("profile_type", test.get("type"))
        dataset = plan.get("dataset")
        if isinstance(dataset, Mapping):
            self.record_input("dataset_id", dataset.get("id"))
            self.record_input("test_case_count", dataset.get("test_case_count"))


def sanitize_message(message: str) -> str:
    """Redact credentials, tokens, and URL userinfo while preserving diagnostic text."""
    scrubbed = _URL_USERINFO_PATTERN.sub(r"\1***@", message)
    scrubbed = _BEARER_PATTERN.sub(r"\1***", scrubbed)
    return _SECRET_KV_PATTERN.sub(r"\1\2\3***\3", scrubbed)


def error_summary(exc: BaseException, *, max_depth: int = 3) -> dict[str, Any]:
    """Summarize an exception and its causal chain without exposing secrets."""
    summary: dict[str, Any] = {
        "type": type(exc).__name__,
        "message": sanitize_message(str(exc)),
    }
    if exc.__cause__ is not None and max_depth > 0:
        summary["cause"] = error_summary(exc.__cause__, max_depth=max_depth - 1)
    return summary


def discover_available_artifacts(result_dir: Path, *, include_failure_file: bool = True) -> list[str]:
    """Return sorted filenames currently present in result_dir without fabricating missing files."""
    names = {entry.name for entry in result_dir.iterdir() if entry.is_file()}
    if include_failure_file:
        names.add(FAILURE_FILENAME)
    return sorted(names)


def build_failure_payload(
    context: RunContext,
    exc: BaseException,
    *,
    failed_at: datetime,
    available_artifacts: list[str],
    preservation_errors: list[dict[str, str]],
) -> dict[str, Any]:
    """Build the versioned failure.json diagnostic payload."""
    return {
        "schema_version": FAILURE_SCHEMA_VERSION,
        "run_id": context.run_id,
        "status": FAILURE_STATUS,
        "phase": context.phase,
        "started_at": _iso(context.started_at),
        "failed_at": _iso(failed_at),
        "error": error_summary(exc),
        "inputs": dict(context.inputs),
        "revisions": dict(context.revisions),
        "available_artifacts": list(available_artifacts),
        "preservation_errors": list(preservation_errors),
    }


def _copy_raw_inputs(context: RunContext, record_error: Callable[[str, BaseException], None]) -> None:
    if context.result_dir is None or not context.result_dir_owned:
        return
    for input_key, target_name in (
        ("experiment_path", "experiment.yaml"),
        ("profile_path", "profile.yaml"),
        ("dataset_path", "dataset.yaml"),
    ):
        raw_path = context.inputs.get(input_key)
        if not isinstance(raw_path, str):
            continue
        source_path = Path(raw_path)
        dest_path = context.result_dir / target_name
        try:
            if source_path.is_file() and not dest_path.exists():
                shutil.copyfile(source_path, dest_path)
        except OSError as exc:
            record_error("copy_input_artifact", exc)


def _backfill_experiment_metadata_from_plan(context: RunContext) -> None:
    if context.result_dir is None or not context.result_dir_owned:
        return
    plan_path = context.result_dir / "execution-plan.json"
    if not plan_path.is_file():
        return
    try:
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    if not isinstance(plan, dict):
        return

    experiment = plan.get("experiment") if isinstance(plan.get("experiment"), dict) else {}
    infrastructure = plan.get("infrastructure") if isinstance(plan.get("infrastructure"), dict) else {}
    sources = plan.get("sources") if isinstance(plan.get("sources"), dict) else {}
    exp_source = sources.get("experiment") if isinstance(sources.get("experiment"), dict) else {}

    candidates = {
        "experiment_path": exp_source.get("path"),
        "experiment_id": experiment.get("id"),
        "experiment_mode": experiment.get("mode"),
        "infrastructure_id": infrastructure.get("id"),
        "infrastructure_revision": infrastructure.get("revision"),
    }
    for key, value in candidates.items():
        if key not in context.inputs:
            context.record_input(key, value)


def preserve_failure(
    context: RunContext,
    exc: BaseException,
    *,
    bucket: str | None = None,
    s3_client=None,
    upload_failure_fn=upload_failure_directory,
) -> tuple[Path | None, list[dict[str, str]]]:
    """Persist failure.json and partial artifacts while never masking the original cause."""
    if context.dry_run:
        return None, []

    preservation_errors: list[dict[str, str]] = []
    failed_at = datetime.now(timezone.utc)
    failure_path: Path | None = None

    def record_preservation_error(step: str, err: BaseException) -> None:
        preservation_errors.append({
            "step": step,
            "type": type(err).__name__,
            "message": sanitize_message(str(err)),
        })

    try:
        if context.result_dir is not None and not context.result_dir_owned:
            try:
                context.result_dir.mkdir(parents=True, exist_ok=False)
            except Exception as dir_exc:
                record_preservation_error("create_result_dir", dir_exc)
            else:
                context.result_dir_owned = True
                _copy_raw_inputs(context, record_preservation_error)

        if context.result_dir is not None and context.result_dir_owned:
            _backfill_experiment_metadata_from_plan(context)
            candidate_path = context.result_dir / FAILURE_FILENAME
            try:
                available = discover_available_artifacts(context.result_dir, include_failure_file=True)
                payload = build_failure_payload(
                    context,
                    exc,
                    failed_at=failed_at,
                    available_artifacts=available,
                    preservation_errors=preservation_errors,
                )
                candidate_path.write_text(
                    json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
                    encoding="utf-8",
                )
                failure_path = candidate_path
            except Exception as write_exc:
                failure_path = None
                record_preservation_error("write_failure_artifact", write_exc)

        if bucket and failure_path is not None and context.phase != "upload":
            try:
                upload_failure_fn(context.result_dir, bucket, context.run_id, s3_client=s3_client)
            except Exception as upload_exc:
                record_preservation_error("upload_failure_artifacts", upload_exc)
                try:
                    available = discover_available_artifacts(context.result_dir, include_failure_file=True)
                    payload = build_failure_payload(
                        context,
                        exc,
                        failed_at=failed_at,
                        available_artifacts=available,
                        preservation_errors=preservation_errors,
                    )
                    failure_path.write_text(
                        json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
                        encoding="utf-8",
                    )
                except Exception as rewrite_exc:
                    record_preservation_error("rewrite_failure_artifact", rewrite_exc)
    except Exception as unexpected_exc:  # pragma: no cover - defensive guard against masking original error
        record_preservation_error("preserve_failure", unexpected_exc)

    if preservation_errors:
        original_type = type(exc).__name__
        original_message = sanitize_message(str(exc))
        formatted = "; ".join(
            f"{item['step']}({item['type']}: {item['message']})"
            for item in preservation_errors
        )
        print(
            f"ERROR (original, phase={context.phase}, run_id={context.run_id}): "
            f"{original_type}: {original_message}\n"
            f"WARNING (preservation failed): {formatted}",
            file=sys.stderr,
        )

    return failure_path, preservation_errors
