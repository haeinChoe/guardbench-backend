# Runner Input Validation Fixtures

These files validate the Runner CLI input resolution and scenario/dataset
contracts ([ADR 0016](../../docs/decisions/0016-performance-experiment-plan.md),
[ADR 0017](../../docs/decisions/0017-performance-runner-infrastructure-boundary.md)).
Names, counts, workload values, and acceptance numbers are synthetic test inputs.

Run `--dry-run` from the repository root with Python dependencies from
`performance/requirements.txt` installed:

```bash
python3 -m performance.runner.cli \
  --api-url http://127.0.0.1:9 \
  --target-url http://127.0.0.1:9/v1/chat/completions \
  --target-model m \
  --scenario performance/fixtures/profiles/smoke.yaml \
  --dataset performance/fixtures/datasets/two-cases.yaml \
  --result-dir /tmp/guardbench-fixture-plan \
  --dry-run
```

Use a fresh `--result-dir` for each invocation. `--dry-run` performs no network
or subprocess calls, prints the resolved plan JSON, and writes
`scenario.yaml`, `dataset.yaml`, and `execution-plan.json`.

- `profiles/smoke.yaml`: `SMOKE` single-target workload fixture (`target.type: HTTP_ENDPOINT`, app-level `acceptance` only).
- `profiles/staged-stress.yaml`: `STRESS` staged workload fixture (`stages` curve + app-level `acceptance`).
- `datasets/two-cases.yaml`: deterministic two-case `synthetic-testset` fixture.

Python consumers call `performance.runner.plan.load_execution_plan(...)`.
The frozen `ExecutionPlan` exposes `to_dict()`, `connection`, `scenario`,
`dataset`, `seed_payload`, `test_case_count`, `source_paths`, and `source_bytes`.
Each source YAML file is read once before environment expansion so SHA-256 hashes
reflect the raw input bytes and later file or environment edits cannot mutate
the resolved snapshot.

Run the Runner test suite:

```bash
python3 -m unittest discover -s performance -p 'test_*.py' -t .
```
