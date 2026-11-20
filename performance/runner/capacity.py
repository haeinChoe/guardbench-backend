"""Independent AWS observations and strict manifest capacity comparison."""
from datetime import datetime, timezone
from typing import Any

from .config import ConfigurationError


def _field(value, key, resource):
    found = value.get(key)
    if found is None or (isinstance(found, str) and not found.strip()):
        raise ConfigurationError(f"{resource}.{key}: expected present / actual missing")
    return found


def _one(items, resource):
    if not isinstance(items, list) or len(items) != 1 or not isinstance(items[0], dict):
        raise ConfigurationError(f"{resource}: expected one resource / actual {items!r}")
    return items[0]


def _variant(items, name, resource):
    return _one([item for item in items if isinstance(item, dict) and item.get('VariantName') == name], resource)


def collect_manifest_capacity(collector, expected: dict[str, Any]) -> dict[str, Any]:
    # Imports stay local to avoid coupling legacy collection to this schema.
    from .aws import _container_work_items_concurrency
    actual = {"captured_at": datetime.now(timezone.utc).isoformat(), "ecs": {}}
    resource = "AWS"
    try:
        for role, config in expected['ecs'].items():
            resource = f"ecs.{role}"
            response = collector.ecs_client.describe_services(
                cluster=config['cluster_identifier'], services=[config['service_identifier']])
            if response.get('failures'):
                raise ConfigurationError(f"{resource}: expected available / actual failures {response['failures']!r}")
            service = _one(response.get('services'), resource)
            arn = _field(service, 'taskDefinition', resource)
            task = collector.ecs_client.describe_task_definition(taskDefinition=arn).get('taskDefinition', {})
            cluster_arn = _field(service, 'clusterArn', resource)
            cluster_id = cluster_arn if config['cluster_identifier'].startswith('arn:') else cluster_arn.rsplit('/', 1)[-1]
            service_id = _field(service, 'serviceArn' if config['service_identifier'].startswith('arn:') else 'serviceName', resource)
            observed = {
                'cluster_identifier': cluster_id, 'service_identifier': service_id,
                'task_definition_arn': _field(task, 'taskDefinitionArn', resource),
                'service_task_definition_arn': arn,
                'task_cpu': str(_field(task, 'cpu', resource)),
                'task_memory': str(_field(task, 'memory', resource)),
                'desired_count': _field(service, 'desiredCount', resource),
                'running_task_count': _field(service, 'runningCount', resource),
                'pending_task_count': _field(service, 'pendingCount', resource),
                'status': _field(service, 'status', resource),
                'deployments': _field(service, 'deployments', resource),
            }
            if role == 'worker':
                observed['work_items_concurrency'] = _container_work_items_concurrency(task)
            actual['ecs'][role] = observed
        resource = 'rds'
        db = _one(collector.rds_client.describe_db_instances(
            DBInstanceIdentifier=expected['rds']['db_instance_identifier']).get('DBInstances'), resource)
        actual['rds'] = {key: _field(db, aws_key, resource) for key, aws_key in (
            ('db_instance_identifier', 'DBInstanceIdentifier'), ('db_instance_class', 'DBInstanceClass'),
            ('status', 'DBInstanceStatus'))}
        resource = 'sagemaker'
        config = expected['sagemaker']
        endpoint = collector.sagemaker_client.describe_endpoint(EndpointName=config['endpoint_name'])
        variant = _variant(endpoint.get('ProductionVariants', []), config['production_variant_name'], resource)
        endpoint_config = collector.sagemaker_client.describe_endpoint_config(
            EndpointConfigName=_field(endpoint, 'EndpointConfigName', resource))
        variant_config = _variant(endpoint_config.get('ProductionVariants', []), config['production_variant_name'], resource)
        actual['sagemaker'] = {
            'endpoint_name': _field(endpoint, 'EndpointName', resource),
            'production_variant_name': _field(variant, 'VariantName', resource),
            'instance_type': _field(variant_config, 'InstanceType', resource),
            'desired_instance_count': _field(variant, 'DesiredInstanceCount', resource),
            'current_instance_count': _field(variant, 'CurrentInstanceCount', resource),
            'status': _field(endpoint, 'EndpointStatus', resource),
        }
        return actual
    except ConfigurationError as exc:
        raise ConfigurationError(f"{resource}: expected complete observable capacity / actual {exc}") from exc
    except Exception as exc:
        raise ConfigurationError(f"{resource}: expected observable capacity / actual lookup failed ({type(exc).__name__})") from exc


def assert_manifest_capacity(expected: dict[str, Any], actual: dict[str, Any]) -> dict[str, Any]:
    differences = []

    def compare(path, wanted, found):
        # bool must not pass an integer count comparison.
        if wanted != found or (isinstance(wanted, int) and type(found) is not int):
            differences.append(f"{path}: expected {wanted!r} / actual {found!r}")

    for role, config in expected['ecs'].items():
        path = f'ecs.{role}'
        service = actual.get('ecs', {}).get(role, {})
        for key, value in config.items():
            compare(f'{path}.{key}', value, service.get(key))
        compare(f'{path}.running_task_count', config['desired_count'], service.get('running_task_count'))
        compare(f'{path}.pending_task_count', 0, service.get('pending_task_count'))
        compare(f'{path}.status', 'ACTIVE', service.get('status'))
        arn = service.get('service_task_definition_arn')
        compare(f'{path}.task_definition_arn', arn, service.get('task_definition_arn'))
        deployments = service.get('deployments')
        if not isinstance(deployments, list) or len(deployments) != 1 or not isinstance(deployments[0], dict):
            differences.append(f'{path}.deployments: expected one completed deployment / actual {deployments!r}')
        else:
            deployment = deployments[0]
            for key, value in {'status': 'PRIMARY', 'rolloutState': 'COMPLETED', 'taskDefinition': arn,
                               'desiredCount': config['desired_count'], 'runningCount': config['desired_count'],
                               'pendingCount': 0}.items():
                compare(f'{path}.deployments.{key}', value, deployment.get(key))
    for resource, ready in (('rds', 'available'), ('sagemaker', 'InService')):
        observed = actual.get(resource, {})
        for key, value in expected[resource].items():
            compare(f'{resource}.{key}', value, observed.get(key))
        compare(f'{resource}.status', ready, observed.get('status'))
    compare('sagemaker.current_instance_count', expected['sagemaker']['desired_instance_count'],
            actual.get('sagemaker', {}).get('current_instance_count'))
    if differences:
        raise ConfigurationError('Performance preflight FAIL:\n' + '\n'.join(differences))
    return {'passed': True, 'expected': expected, 'actual': actual}
