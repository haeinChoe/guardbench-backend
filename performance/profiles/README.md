# Performance Scenarios

성능 실험에서는 시나리오, Dataset, 연결 입력, 인프라 구성을 분리한다.

1. **Dataset**: 한 TestRun이 처리할 TestCase 집합과 고정된 건수다(`performance/datasets/baseline-v1.yaml` 78건, `performance/datasets/large-suite-491-v1.yaml` 491건).
2. **Test Type**: 실험 목적이다. `SMOKE`, `STRESS`, `SOAK` 세 유형만 허용하며 그 밖의 `test.type`은 설정 오류로 거부한다.
3. **Workload**: 부하 패턴이다. `concurrent_test_runs`, `ramp_up_seconds`/`duration_seconds` 또는 단계형 `stages`, `max_iterations_per_vu`, timeout/polling 값이 여기에 속한다.
4. **Connection Inputs & Infrastructure Ownership**: API/Target 연결 URL·model·revision은 Runner CLI 인자로 전달한다. 인프라 구성, 실행 기반, 실행 전 capacity·queue 확인, 인프라 지표 수집·판정, 결과 보존 및 `(W, C, T)` 결합은 IaC가 소유한다([ADR 0017](../../docs/decisions/0017-performance-runner-infrastructure-boundary.md)).

## 시나리오 파일 구조

```yaml
test:
  id: EXAMPLE-SMOKE
  name: example smoke check
  type: SMOKE
workload:
  concurrent_test_runs: 2
  ramp_up_seconds: 0
  duration_seconds: 1
  max_iterations_per_vu: 1
  completion_timeout_seconds: 10
  polling_interval_seconds: 0.1
target:
  type: HTTP_ENDPOINT
acceptance:
  api:
    create_latency_ms: {p50: 1000, p95: 2000, p99: 3000}
    error_rate: 0
  completion:
    max_seconds: 10
    failure_rate: 0
```

- `max_iterations_per_vu`가 양수이면 상한에 도달한 VU는 남은 stage 시간 동안 1초 간격으로 대기하며 요청을 보내지 않는다. 0이면 상한이 없다.
- `target`은 `type: HTTP_ENDPOINT`만 허용한다. URL, model, revision은 시나리오 YAML에 두지 않는다.
- `acceptance`는 앱 수준 기준(`api.create_latency_ms`, `api.error_rate`, `completion.max_seconds`, `completion.failure_rate`)만 허용하며 알 수 없는 키는 거부한다.
- 검증용 fixture 시나리오는 `performance/fixtures/profiles/`에 있다. 이 파일의 수치는 기능 검증용이며 실제 성능 목표로 해석하지 않는다.

## 단계형 workload

`ramp_up_seconds` + `duration_seconds` 대신 `stages` 배열을 사용할 수 있다. 두 형식은 혼용하지 않는다.

```yaml
workload:
  concurrent_test_runs: 2
  stages:
    - {duration_seconds: 1, concurrent_test_runs: 1}
    - {duration_seconds: 2, concurrent_test_runs: 2}
    - {duration_seconds: 1, concurrent_test_runs: 0}
  max_iterations_per_vu: 0
  completion_timeout_seconds: 10
  polling_interval_seconds: 0.1
```

- `stages`는 비어 있지 않은 배열이며, 각 `duration_seconds`는 유한한 양수, `concurrent_test_runs`는 0 이상의 정수다.
- 최소 하나의 양수 target이 있어야 하며, 최상위 `concurrent_test_runs`는 stage target의 최댓값과 같아야 한다.
- `C`는 최대 configured VU target이다. 한 VU는 TestRun 한 건을 생성하고 `FINISHED`까지 polling한다.
- 단계형 경로의 `gracefulRampDown`과 공통 `gracefulStop`은 `completion_timeout_seconds + max(1, ceil(polling_interval_seconds))`초로 설정한다.
- `completion.iterations` 판정은 `stages` 시나리오에 적용해 `started = completed > 0`을 요구하며 `interrupted`가 남으면 FAIL 처리한다.

검증 테스트 실행:

```bash
python3 -m unittest performance.runner.test_workload_stages -v
```

`k6` 바이너리가 설치되어 있으면 실제 `k6 inspect`와 로컬 모의 HTTP API 실행 테스트까지 함께 수행한다.
