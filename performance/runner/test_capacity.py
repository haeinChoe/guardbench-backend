"""Capacity preflight using AWS client doubles; runner seed boundaries live in test_execution."""
import os
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from performance.runner.aws import InfrastructureCapacityCollector, CloudWatchMetricCollector
from performance.runner.capacity import assert_manifest_capacity
from performance.runner.config import ConfigurationError
from performance.runner.plan import load_execution_plan

ROOT = Path(__file__).resolve().parents[2]
MANIFEST = ROOT / 'performance/fixtures/experiments/smoke.yaml'


def clients(expected):
    ecs, rds, sm = Mock(), Mock(), Mock()
    services, definitions = {}, {}
    for role, config in expected['ecs'].items():
        arn = f'arn:aws:ecs:region:account:task-definition/{role}:1'
        services[config['service_identifier']] = {
            'clusterArn': 'arn:aws:ecs:region:account:cluster/' + config['cluster_identifier'],
            'serviceName': config['service_identifier'], 'taskDefinition': arn,
            'status': 'ACTIVE', 'desiredCount': config['desired_count'],
            'runningCount': config['desired_count'], 'pendingCount': 0,
            'deployments': [{'status': 'PRIMARY', 'rolloutState': 'COMPLETED', 'taskDefinition': arn,
                             'desiredCount': config['desired_count'], 'runningCount': config['desired_count'], 'pendingCount': 0}],
        }
        definitions[arn] = {'taskDefinitionArn': arn, 'cpu': config['task_cpu'], 'memory': config['task_memory'],
                            'containerDefinitions': [{'environment': [{'name': 'GUARDBENCH_WORKER_WORK_ITEMS_CONCURRENCY',
                              'value': str(config['work_items_concurrency'])}]}] if role == 'worker' else []}
    ecs.describe_services.side_effect = lambda **kw: {'services': [services[kw['services'][0]]]}
    ecs.describe_task_definition.side_effect = lambda **kw: {'taskDefinition': definitions[kw['taskDefinition']]}
    rds.describe_db_instances.return_value = {'DBInstances': [{
        'DBInstanceIdentifier': expected['rds']['db_instance_identifier'],
        'DBInstanceClass': expected['rds']['db_instance_class'], 'DBInstanceStatus': 'available'}]}
    sm.describe_endpoint.return_value = {'EndpointName': expected['sagemaker']['endpoint_name'],
        'EndpointStatus': 'InService', 'EndpointConfigName': 'config', 'ProductionVariants': [{
            'VariantName': expected['sagemaker']['production_variant_name'],
            'DesiredInstanceCount': expected['sagemaker']['desired_instance_count'],
            'CurrentInstanceCount': expected['sagemaker']['desired_instance_count']}]}
    sm.describe_endpoint_config.return_value = {'ProductionVariants': [{
        'VariantName': expected['sagemaker']['production_variant_name'],
        'InstanceType': expected['sagemaker']['instance_type']}]}
    return ecs, rds, sm, services, definitions


class ManifestCapacityTest(unittest.TestCase):
    def setUp(self):
        self.expected = load_execution_plan(MANIFEST).to_dict()['infrastructure']['expected']
        self.expected['ecs']['api'] = {**self.expected['ecs']['worker'], 'service_identifier': 'fixture-api'}
        del self.expected['ecs']['api']['work_items_concurrency']
        self.ecs, self.rds, self.sm, self.services, self.definitions = clients(self.expected)
        self.collector = InfrastructureCapacityCollector(ecs_client=self.ecs, rds_client=self.rds, sagemaker_client=self.sm)

    def test_role_specific_observations_and_no_api_worker_environment(self):
        actual = self.collector.collect(expected=self.expected)
        self.assertTrue(assert_manifest_capacity(self.expected, actual)['passed'])
        self.assertNotIn('work_items_concurrency', actual['ecs']['api'])
        self.assertNotEqual(actual['ecs']['worker']['service_identifier'], actual['ecs']['api']['service_identifier'])
        for config in self.expected['ecs'].values():
            self.ecs.describe_services.assert_any_call(cluster=config['cluster_identifier'], services=[config['service_identifier']])
        self.rds.describe_db_instances.assert_called_once_with(DBInstanceIdentifier=self.expected['rds']['db_instance_identifier'])
        self.sm.describe_endpoint.assert_called_once_with(EndpointName=self.expected['sagemaker']['endpoint_name'])

    def test_optional_api_not_queried(self):
        del self.expected['ecs']['api']
        self.assertTrue(assert_manifest_capacity(self.expected, self.collector.collect(expected=self.expected))['passed'])
        self.assertEqual(1, self.ecs.describe_services.call_count)

    def test_incomplete_response_and_lookup_failures_cannot_be_configuration(self):
        for client, method, incomplete in ((self.ecs, 'describe_services', {'services': []}),
                (self.rds, 'describe_db_instances', {'DBInstances': []}),
                (self.sm, 'describe_endpoint', {}), (self.sm, 'describe_endpoint_config', {})):
            for failure in (False, True):
                with self.subTest(method=method, failure=failure):
                    original = getattr(client, method)
                    setattr(client, method, Mock(side_effect=RuntimeError('unavailable')) if failure else Mock(return_value=incomplete))
                    with self.assertRaises(ConfigurationError):
                        self.collector.collect(expected=self.expected)
                    setattr(client, method, original)

    def test_worker_environment_missing_blocks_but_api_environment_is_unneeded(self):
        definition = next(value for key, value in self.definitions.items() if '/worker:' in key)
        definition['containerDefinitions'] = []
        with self.assertRaisesRegex(ConfigurationError, 'CONCURRENCY'):
            self.collector.collect(expected=self.expected)

    def test_cloudwatch_resource_dimensions_come_from_manifest_not_environment(self):
        env = {key: 'wrong-resource' for key in ('PERF_ECS_CLUSTER', 'PERF_ECS_SERVICE', 'PERF_RDS_INSTANCE_ID',
                                               'PERF_SAGEMAKER_ENDPOINT_NAME', 'PERF_SAGEMAKER_VARIANT_NAME')}
        env.update({f'PERF_{prefix}_{name}_NAME': f'{prefix}-{name}'
                    for prefix in ('SOURCE_QUEUE', 'DLQ') for name in ('RESOLVE', 'WORK_ITEMS', 'FINALIZE')})
        with patch.dict(os.environ, env, clear=True):
            collector = CloudWatchMetricCollector(ROOT / 'performance/metrics/aws.yaml', resources=self.expected, cloudwatch_client=Mock())
            collector.validate_configuration()
            self.assertEqual('wrong-resource', os.environ['PERF_ECS_SERVICE'])
            for metric in collector.config['metrics']:
                dimensions = metric['dimensions']
                self.assertNotIn('wrong-resource', dimensions.values())
                if metric['namespace'] == 'AWS/SQS':
                    self.assertTrue(dimensions['QueueName'].startswith(('SOURCE_QUEUE-', 'DLQ-')))
                elif 'ServiceName' in dimensions:
                    self.assertEqual(self.expected['ecs']['worker']['service_identifier'], dimensions['ServiceName'])


if __name__ == '__main__':
    unittest.main()
