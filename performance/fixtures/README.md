# Experiment input validation fixtures

These files validate the Runner's experiment manifest v1 interface
([ADR 0016](../../docs/decisions/0016-performance-experiment-plan.md), APPROVED/ACCEPTED).
Resource names, CPU/memory, counts, workload and acceptance values are mock inputs.
They do not define an AWS small preset or operating goals.

Run from the repository root with Python dependencies from
`performance/requirements.txt` installed:

```bash
python3 -m performance.runner.cli \
  --experiment performance/fixtures/experiments/mock-small-operating.yaml \
  --dry-run \
  --result-dir /tmp/guardbench-fixture-plan
```

Use a new result directory for each invocation. No AWS credentials, running API,
DB or k6 are needed. The command validates and prints the plan, writes
`execution-plan.json` and `dry-run.json`, and preserves original `experiment.yaml`,
`profile.yaml` and `dataset.yaml` files. It makes no external calls.
At this stage `--experiment` requires `--dry-run`; actual execution follows in #22.

A manifest selects one infrastructure ID/revision, expected capacity, Profile and
Dataset. Worker ECS, RDS and SageMaker expectations are required; API ECS is
optional and requires a complete API service expectation when present. Counts
must be positive integers. CPU and memory accept positive integers or decimal
integer strings and normalize to decimal strings. Unknown fields and malformed
nested values are rejected. IaC owns actual capacity; Profile owns workload,
target and acceptance and cannot contain capacity fields.

Profile/Dataset references resolve relative to the experiment file, independently
of the shell's working directory. Existing Dataset source paths still resolve
relative to the Dataset manifest and HTTP fixtures retain their pinned checksum
verification. Environment expansion happens before validation. Missing target
URL/model values, unresolved required input, nonfinite numbers and credential-bearing
target URLs are rejected.

`execution-plan.json` contains `schema_version: 1`, `experiment: {id}`,
`infrastructure: {id, revision, expected}`, the full resolved Profile,
`dataset: {id, test_case_count}` and `sources`. Each of the experiment/Profile/Dataset
sources includes its absolute path and SHA-256 of the original file bytes, before
environment expansion or CPU/memory normalization.

Python consumers call `performance.runner.plan.load_execution_plan(Path(...))`.
The frozen `ExecutionPlan` exposes `to_dict()`, `profile`, `dataset`, `seed_payload`,
`test_case_count`, `source_paths` and `source_bytes`. Mapping/payload properties
return detached copies. The loader reads each YAML input once and retains the
parsed seed payload so later file or environment changes cannot alter the plan.

The existing default smoke CLI and explicit `--profile`/`--dataset` flows remain
supported because the runtime verification script, Docker entrypoint and existing
Runner consumers use them. Explicitly combining either option with `--experiment`
is an error. Legacy dry runs retain their `dry-run.json` shape. Product APIs,
DB schema, deployment configuration and production dependencies are unchanged.

```bash
python3 -m unittest discover -s performance/runner -p 'test_*.py'
```
