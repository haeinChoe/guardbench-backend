# 0016. 성능 실험 테스트 유형, 단계형 workload와 실패 진단 계획

> Status: APPROVED
> Owner: Backend
> Last reviewed: 2026-11-22
> Canonical source: GitHub
> Origin: GitHub Issue #20

- ADR Status: ACCEPTED
- Decision date: 2026-11-21
- Related Issue: #20, #37, #39; 구현·검증 #21–#26

인프라 구성 입력, 실행 전 capacity·queue 확인, 인프라 지표 수집·판정, 결과의 인프라 식별과
원격 보존 등 인프라 관련 범위는 [ADR 0017](0017-performance-runner-infrastructure-boundary.md)을 따른다.

## Context

성능 실험에서는 부하 시나리오와 Dataset, 연결 입력을 실행 전에 검증하고 재현 가능한
입력 해시와 앱 수준 결과 및 오류 진단 자료를 남겨야 한다. 외부 사용자 API, DB schema,
migration, production dependency 변경은 이 범위에 없다.

## Decision

### 단일 실행과 입력 스냅샷

- 한 번의 Runner 실행은 하나의 시나리오, 하나의 Dataset, 하나의 API·Target 연결 입력만 다룬다.
- matrix·병렬 실험, DB reset/TRUNCATE는 포함하지 않는다.
- 실행 계획(`execution-plan.json`)은 검증된 시나리오, Dataset ID와 TestCase 수, 연결 입력과
  원본 파일(`scenario.yaml`, `dataset.yaml`) byte의 SHA-256을 남긴다.
- Runner는 실행 전에 YAML 입력과 Dataset payload를 한 번씩 읽어 스냅샷으로 고정한다.
  실행 중 원본 파일이나 환경변수가 바뀌어도 고정된 스냅샷으로 seed와 결과를 작성한다.
- 유한하지 않은 수치(`NaN`, `Infinity`), 잘못된 중첩 타입, 알 수 없는 키, userinfo·fragment를
  포함한 URL은 `ConfigurationError`로 거절한다.
- `--dry-run`은 외부 API나 k6를 호출하지 않으며 `--result-dir`에 원본 입력(`scenario.yaml`,
  `dataset.yaml`)과 `execution-plan.json`만 기록한다.
- Runner CLI 연결 입력과 인프라 관련 경계는 [ADR 0017](0017-performance-runner-infrastructure-boundary.md)을 따른다.

### 테스트 유형

성능 테스트에는 `SMOKE`, `STRESS`, `SOAK` 세 유형을 사용한다.

| 유형 | 목적 |
| --- | --- |
| `SMOKE` | 적은 부하로 TestRun 접수부터 비동기 처리·완료·결과 수집까지 실행 경로를 확인한다. |
| `STRESS` | 부하를 높이면서 처리 한계, 지연과 실패 양상을 확인한다. |
| `SOAK` | 일정한 부하를 장시간 유지하며 지연 변화와 오류 누적을 확인한다. |

Runner 입력 검증은 위 세 유형만 허용하며 그 밖의 `test.type`은 설정 오류로 거부한다.
허용 유형의 존재와 개별 시나리오 구현 여부는 구분한다.

### 단계형 Workload와 튜플

시나리오 workload는 단일 목표(`ramp_up_seconds`/`duration_seconds`)와 설정 기반
`stages` 형식을 지원한다. `stages`는 `ramp_up_seconds`/`duration_seconds`와 혼용하지 않는다.

```yaml
concurrent_test_runs: 2  # configured stage target의 최댓값
stages:
  - {duration_seconds: 1, concurrent_test_runs: 1}
  - {duration_seconds: 2, concurrent_test_runs: 2}
  - {duration_seconds: 1, concurrent_test_runs: 0}
max_iterations_per_vu: 0
completion_timeout_seconds: 10
polling_interval_seconds: 0.1
```

- 각 stage의 `duration_seconds`는 유한한 양수, `concurrent_test_runs`는 0 이상의 정수다.
- `stages`는 비어 있지 않아야 하고 최소 하나의 양수 target이 필요하며, 최상위
  `workload.concurrent_test_runs`는 stage target의 최댓값과 같아야 한다.
- 튜플의 `C`는 관측된 동시 사용자 수가 아니라 **최대 configured VU target**이다.
  한 VU는 하나의 비동기 TestRun 완료를 기다린다.
- Backend Runner 결과는 `(C, T)` (`concurrent_test_runs`, `test_case_count`)를 기록한다.
  인프라의 Worker 동시성 `W`를 결합해 `(W, C, T)`를 완성하는 책임은
  [ADR 0017](0017-performance-runner-infrastructure-boundary.md)에 따라 IaC가 소유한다.
- 각 테스트 유형은 이름별 분기 대신 같은 schema의 다른 설정으로 표현한다.
- 단계형 경로에서 `gracefulRampDown`은 VU 수를 줄일 때 진행 중 iteration을 기다리는 최대 시간이고,
  `gracefulStop`은 시나리오 전체 종료 시 진행 중 iteration을 기다리는 최대 시간이다.
  단계형 `gracefulRampDown`과 공통 `gracefulStop`은
  `completion_timeout_seconds + max(1, ceil(polling_interval_seconds))`초로 설정한다.
  추가 여유는 완료 timeout 직후 실패 판정과 지표 기록을 마칠 시간을 주기 위한 것이다.
  유예 시간 내 완료를 보장하지 않으며, 시간이 만료되면 k6가 진행 중 iteration을 강제 중단할 수 있다.
- `interrupted` iteration이 남으면 결과에 별도 기록하고 단계형 경로의
  `completion.iterations` 판정에서 유효한 완료 측정으로 인정하지 않는다.
  단일 target(`ramp_up_seconds`/`duration_seconds`) 경로의 `gracefulRampDown`은 `0s`다.
- 앱 수준 acceptance와 k6 exit `99`의 측정 FAIL 의미를 사용한다. 임의의 한계 지점이나 원인은
  자동 단정하지 않고, 목표값은 실행 전에 확정한다.

### 실패 진단 자료와 종료 코드

- 종료 코드: `0` 측정 PASS 또는 `--dry-run`, `1` 측정 FAIL(k6 threshold exit `99` 포함), `2` 실행 오류.
- 오류는 완료 결과로 위장하지 않고 별도 `failure.json`에 기록한다.
- `failure.json`은 `schema_version: 1`, `run_id`, `status: "ERROR"`, `phase`(`preflight`, `seed`, `k6`, `report`),
  `started_at`, `failed_at`, `error`(`type`, `message`, optional `cause`), 확보된 `inputs`,
  `available_artifacts`, `preservation_errors`를 남긴다.
- 미생성 파일은 임의로 채우지 않으며, 이미 존재하는 `--result-dir`는 덮어쓰지 않는다.
- 입력 검증 이전에 fallback `run_id`와 최소 컨텍스트를 마련해 조기 설정 오류도 기록한다.
- 예외 메시지와 입력 기록 시 URL userinfo, Bearer token, secret/key/password 패턴을 마스킹한다.
  보존 자체가 실패해도 원래 실패를 덮어쓰지 않고 `stderr`에 원래 실패와 보존 실패를 함께 알린다.
- 결과의 인프라 식별, 인프라 지표 및 원격 결과 보존은
  [ADR 0017](0017-performance-runner-infrastructure-boundary.md)을 따른다.

## Consequences

단일 시나리오·Dataset·연결 입력과 SHA-256 스냅샷으로 실행 조건을 재현하고,
측정 FAIL(`1`)과 도구/API 실행 오류(`2`)를 명확히 분리해 진단할 수 있다.
인프라 구성과 관측·보존 경계는 [ADR 0017](0017-performance-runner-infrastructure-boundary.md)에서 독립 관리한다.

## Validation

- 시나리오/Dataset 스키마, 유한 수치, URL 검증과 `--dry-run` 무호출 경계를 검사한다.
- `SMOKE`/`STRESS`/`SOAK` 유형, 선언된 k6 stages, `gracefulRampDown`/`gracefulStop`, `(C, T)` 의미를 검사한다.
- 완료 결과(`result.json`, `report.md`)의 식별·해시·미수집 metric 비생성(`null`)과 오류(`failure.json`) 분리를 검사한다.
- 실제 Runner와 실제 k6, 모의 HTTP API를 연결한 로컬 E2E로 정상 완료(`0`)·측정 FAIL(`1`)·실행 오류(`2`) 및 프로세스 정리를 검증한다.
