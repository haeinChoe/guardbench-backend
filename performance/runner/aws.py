"""AWS preflight and metric collection kept outside the k6 workload."""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import ConfigurationError, load_yaml


def _boto3():
    try:
        import boto3
    except ImportError as exc:  # pragma: no cover - exercised in a missing-tool installation
        raise ConfigurationError("AWS metric 수집에는 performance/requirements.txt 설치가 필요합니다.") from exc
    return boto3


def queue_urls_from_environment() -> tuple[list[str], list[str]]:
    source = [item.strip() for item in os.environ.get("PERF_SOURCE_QUEUE_URLS", "").split(",") if item.strip()]
    dlq = [item.strip() for item in os.environ.get("PERF_DLQ_URLS", "").split(",") if item.strip()]
    if not source or not dlq:
        raise ConfigurationError("PERF_SOURCE_QUEUE_URLS와 PERF_DLQ_URLS를 모두 설정해야 합니다.")
    return source, dlq


class QueueInspector:
    def __init__(self, region: str | None = None) -> None:
        boto3 = _boto3()
        self.client = boto3.client(
            "sqs",
            region_name=region or os.environ.get("AWS_REGION", "ap-northeast-2"),
            endpoint_url=os.environ.get("PERF_SQS_ENDPOINT_URL") or None,
        )

    def snapshot(self, queue_urls: list[str]) -> list[dict[str, Any]]:
        result = []
        for queue_url in queue_urls:
            try:
                response = self.client.get_queue_attributes(
                    QueueUrl=queue_url,
                    AttributeNames=[
                        "ApproximateNumberOfMessages",
                        "ApproximateNumberOfMessagesNotVisible",
                        "ApproximateNumberOfMessagesDelayed",
                    ],
                )
            except Exception as exc:
                raise ConfigurationError("SQS queue 상태 조회에 실패했습니다.") from exc
            attributes = response.get("Attributes", {})
            result.append({
                "queueUrl": queue_url,
                "visible": int(attributes.get("ApproximateNumberOfMessages", 0)),
                "notVisible": int(attributes.get("ApproximateNumberOfMessagesNotVisible", 0)),
                "delayed": int(attributes.get("ApproximateNumberOfMessagesDelayed", 0)),
                "oldestAgeSeconds": None,
            })
        return result

    @staticmethod
    def is_empty(snapshot: list[dict[str, Any]], *, include_in_flight: bool = True) -> bool:
        for queue in snapshot:
            if queue["visible"] or queue["delayed"]:
                return False
            if include_in_flight and queue["notVisible"]:
                return False
        return True


def _positive_integer(value: Any, description: str) -> int:
    if isinstance(value, bool):
        raise ConfigurationError(f"{description}는 양의 정수여야 합니다.")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ConfigurationError(f"{description}는 양의 정수여야 합니다.") from exc
    if parsed < 1 or str(value).strip() != str(parsed):
        raise ConfigurationError(f"{description}는 양의 정수여야 합니다.")
    return parsed


def _container_work_items_concurrency(task_definition: dict[str, Any]) -> int:
    containers = task_definition.get("containerDefinitions")
    if not isinstance(containers, list):
        raise ConfigurationError("ECS task definition에 containerDefinitions가 없습니다.")

    matches: list[dict[str, Any]] = []
    for container in containers:
        if not isinstance(container, dict):
            continue
        environment = container.get("environment", [])
        if not isinstance(environment, list):
            continue
        matches.extend(
            item for item in environment
            if isinstance(item, dict) and item.get("name") == "GUARDBENCH_WORKER_WORK_ITEMS_CONCURRENCY"
        )
    if len(matches) != 1:
        raise ConfigurationError(
            "ECS task definition에 GUARDBENCH_WORKER_WORK_ITEMS_CONCURRENCY 환경변수가 하나 필요합니다."
        )
    return _positive_integer(
        matches[0].get("value"),
        "GUARDBENCH_WORKER_WORK_ITEMS_CONCURRENCY",
    )


class InfrastructureCapacityCollector:
    """Collect the configured AWS capacity immediately before a workload starts."""

    def __init__(self, region: str | None = None, *, ecs_client=None, rds_client=None,
                 sagemaker_client=None) -> None:
        boto3 = _boto3() if any(client is None for client in (ecs_client, rds_client, sagemaker_client)) else None
        region_name = region or os.environ.get("AWS_REGION", "ap-northeast-2")
        self.ecs_client = ecs_client or boto3.client("ecs", region_name=region_name)
        self.rds_client = rds_client or boto3.client("rds", region_name=region_name)
        self.sagemaker_client = sagemaker_client or boto3.client("sagemaker", region_name=region_name)

    def collect(self, *, expected: dict[str, Any]) -> dict[str, Any]:
        """Observe the resources named by the experiment manifest's expected capacity."""
        from .capacity import collect_manifest_capacity
        return collect_manifest_capacity(self, expected)


class CloudWatchMetricCollector:
    def __init__(self, config_path: Path, region: str | None = None, *,
                 resources: dict[str, Any], cloudwatch_client=None) -> None:
        self.config = load_yaml(config_path)
        worker = resources["ecs"]["worker"]
        cluster_name = worker["cluster_identifier"].rsplit("/", 1)[-1]
        service_name = worker["service_identifier"].rsplit("/", 1)[-1]
        # ECS/RDS/SageMaker dimensions come only from the experiment manifest; SQS names stay environment inputs.
        bindings = {
            "AWS/ECS": {"ClusterName": cluster_name, "ServiceName": service_name},
            "ECS/ContainerInsights": {"ClusterName": cluster_name, "ServiceName": service_name},
            "AWS/RDS": {"DBInstanceIdentifier": resources["rds"]["db_instance_identifier"]},
            "AWS/SageMaker": {"EndpointName": resources["sagemaker"]["endpoint_name"],
                              "VariantName": resources["sagemaker"]["production_variant_name"]},
        }
        for definition in self.config.get("metrics", []):
            for key, value in bindings.get(definition.get("namespace"), {}).items():
                if key in definition.get("dimensions", {}):
                    definition["dimensions"][key] = value
        boto3 = _boto3() if cloudwatch_client is None else None
        self.client = cloudwatch_client if cloudwatch_client is not None else boto3.client(
            "cloudwatch",
            region_name=region or os.environ.get("AWS_REGION", "ap-northeast-2"),
            endpoint_url=os.environ.get("PERF_CLOUDWATCH_ENDPOINT_URL") or None,
        )

    def validate_configuration(self) -> None:
        missing = []
        for definition in self.config.get("metrics", []):
            dimensions = definition.get("dimensions", {})
            if any(not str(value).strip() or "${" in str(value) for value in dimensions.values()):
                missing.append(str(definition.get("id", "unknown")))
        if missing:
            raise ConfigurationError(
                "CloudWatch metric 필수 resource dimension이 설정되지 않았습니다: " + ", ".join(missing)
            )

    def collect(self, started_at: datetime, finished_at: datetime) -> dict[str, Any]:
        definitions = self.config.get("metrics", [])
        queries = []
        included = []
        skipped = []
        for definition in definitions:
            dimensions = definition.get("dimensions", {})
            if any(not str(value).strip() or "${" in str(value) for value in dimensions.values()):
                skipped.append({"id": definition.get("id"), "reason": "필수 resource dimension 미설정"})
                continue
            query_id = "q" + "".join(char if char.isalnum() else "_" for char in definition["id"]).lower()
            queries.append({
                "Id": query_id,
                "MetricStat": {
                    "Metric": {
                        "Namespace": definition["namespace"],
                        "MetricName": definition["metric_name"],
                        "Dimensions": [{"Name": key, "Value": str(value)} for key, value in dimensions.items()],
                    },
                    "Period": int(definition.get("period_seconds", 60)),
                    "Stat": definition.get("statistic", "Average"),
                },
                "ReturnData": True,
            })
            included.append(definition["id"])

        if not queries:
            return {"status": "NOT_CONFIGURED", "metrics": [], "skipped": skipped}
        try:
            response = self.client.get_metric_data(
                MetricDataQueries=queries,
                StartTime=started_at.astimezone(timezone.utc),
                EndTime=finished_at.astimezone(timezone.utc),
                ScanBy="TimestampDescending",
            )
        except Exception as exc:
            raise ConfigurationError("CloudWatch metric 조회에 실패했습니다.") from exc
        by_id = {item["Id"]: item for item in response.get("MetricDataResults", [])}
        metrics = []
        for definition_id in included:
            query_id = "q" + "".join(char if char.isalnum() else "_" for char in definition_id).lower()
            item = by_id.get(query_id, {})
            metrics.append({
                "id": definition_id,
                "label": item.get("Label"),
                "timestamps": [value.isoformat() for value in item.get("Timestamps", [])],
                "values": item.get("Values", []),
                "status": item.get("StatusCode"),
            })
        return {"status": "COLLECTED", "metrics": metrics, "skipped": skipped}
