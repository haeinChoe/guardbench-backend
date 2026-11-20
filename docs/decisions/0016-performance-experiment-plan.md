# 0016. 인프라 구성과 시나리오를 분리한 성능 실험 실행 계획

> Status: APPROVED
> Owner: Backend
> Last reviewed: 2026-11-21
> Canonical source: GitHub
> Origin: GitHub Issue #20

- ADR Status: ACCEPTED
- Decision date: 2026-11-21
- Related Issue: #20; 구현·검증 #21–#26

## Context

성능 실험은 하나의 인프라 구성을 고정하고 선택한 부하 시나리오를 실행한다.
small 등의 구성 이름은 측정 대상 서비스의 자원 규모를 가리킨다. 인프라 구성과
부하 시나리오를 분리해야 동일한 용량에서 운영·스트레스 패턴을 비교할 수 있다.

현재 실행 조건은 Profile, Dataset과 여러 환경변수에 분산되어 있다. 이 조합을
실행 전에 검증하고 결과까지 보존할 단일 입력이 필요하다. 인프라 capacity는
IaC/배포 설정이, workload와 acceptance는 Profile이 소유하는 기존 경계를 유지한다.

현재 Profile은 workload/acceptance/target을 소유한다. `--profile`, `--dataset` 호출은
`bin/run-performance`, `bin/verify-runtime`, Docker ENTRYPOINT와 기존 실행 테스트에서 사용한다.
결과 소비처는 `performance-analysis-bundle.yml`, Runner report/storage와 관련 테스트다.
외부 사용자 API·DB schema·migration·production dependency 변경은 이 범위에 없다.

## Decision

### 용어와 소유권

- **인프라 구성**: 예상 사용량을 운영할 ECS·classifier 자원/설정 묶음. 실제 값과 적용은 IaC가 소유한다.
- **시나리오**: 고정 인프라에 가할 workload와 실행 전에 정한 acceptance. 기존 Profile이 소유한다.
- **실행 manifest**: 인프라 구성 식별/예상값과 Profile/Dataset 파일을 연결하는 단일 실험 입력.
- **실행 계획**: manifest와 참조 파일을 검증·해석하여 보존하는 설정 snapshot. 실행 ID와 구분한다.

한 manifest는 한 실험만 가리킨다. matrix/병렬 실험, DB reset/TRUNCATE, queue purge,
AWS resource 생성/삭제, CodeBuild/Fargate 선택은 포함하지 않는다.

### 테스트 유형과 프로필 구현 범위

성능 테스트에는 `SMOKE`, `STRESS`, `SOAK` 세 유형을 사용한다. 인프라 구성은
실행 동안 고정하고 테스트 유형은 해당 인프라에서 확인할 실험 목적을 나타낸다.

| 유형 | 목적 |
| --- | --- |
| `SMOKE` | 적은 부하로 TestRun 접수부터 비동기 처리·완료·결과 수집까지 실행 경로를 확인한다. |
| `STRESS` | 부하를 높이면서 처리 한계, 지연, SQS backlog와 실패 양상을 확인한다. |
| `SOAK` | 일정한 부하를 장시간 유지하며 메모리·연결·backlog·오류 누적을 확인한다. |

현재 프로필 파일은 `performance/profiles/smoke.yaml`(`SMOKE`)과
`performance/profiles/concurrent-10.yaml`(`LOAD`)이다. 입력 검증은 `SMOKE`, `LOAD`,
`PEAK`, `STRESS`, `SOAK`를 허용하지만 `PEAK`, `STRESS`, `SOAK`의 프로필 파일은 없다.
허용 타입의 존재와 해당 프로필의 구현 여부를 구분한다.

이 결정은 사용할 유형과 목적을 정의한다. 각 프로필의 부하 수치·시간·판정 기준,
단계형 부하의 구체적인 구현은 별도 Issue #23에서 다룬다. 기존 `LOAD` 프로필과
허용 타입의 전환·제거는 해당 구현에서 실제 소비처를 확인한 뒤 명시적으로 처리한다.
이 문서 변경으로 프로필 파일이나 Runner의 허용 타입을 변경하지 않는다.

### 실행 manifest v1

`--experiment <yaml>`을 추가한다. 파일 참조는 manifest의 디렉터리를 기준으로 해석한다.
`--experiment`와 명시적 `--profile`/`--dataset`은 함께 지정하지 않는다. 기존 두 옵션과
기본 smoke 호출은 위의 실제 소비처 때문에 계속 지원한다. 새 manifest가 구 CLI를
대체한다고 가정한 alias/shim은 추가하지 않는다.

```yaml
schema_version: 1
id: fixture-small-operating
infrastructure:
  id: fixture-small
  revision: fixture-v1
  expected:
    ecs:
      worker:
        cluster_identifier: fixture-cluster
        service_identifier: fixture-worker
        task_cpu: "256"
        task_memory: "512"
        desired_count: 1
        work_items_concurrency: 2
      api:  # API capacity도 실험 조건이면 지정한다.
        cluster_identifier: fixture-cluster
        service_identifier: fixture-api
        task_cpu: "256"
        task_memory: "512"
        desired_count: 1
    rds:
      db_instance_identifier: fixture-db
      db_instance_class: fixture-db-class
    sagemaker:
      endpoint_name: fixture-classifier
      production_variant_name: AllTraffic
      instance_type: fixture-instance-type
      desired_instance_count: 1
profile: ../profiles/operating.yaml
dataset: ../datasets/local.yaml
```

예시의 값은 검증 fixture이며 실제 small preset이나 운영 목표가 아니다. 필수 항목은
schema_version=1, 비어 있지 않은 id/revision, profile/dataset 참조,
infrastructure.expected.ecs.worker의 위 여섯 필드와 expected.rds/sagemaker다. 기존 측정 계약이
RDS·SageMaker 관측을 요구하므로 이 자원 입력도 필수로 받는다. expected.ecs.api는
선택 항목이며 지정하면 예시의 해당 필드를 모두 요구한다. 알려지지 않은 구조/키와
비정상 숫자(bool, NaN, Infinity, 음수·0 count)를 거절한다. CPU/memory는 양의 정수
또는 그 문자열을 받아 계획에서는 십진 문자열로 정규화한다. 실험 중 자원 개수를
0으로 내리는 시나리오는 이 schema에 포함하지 않는다.

실행 계획 JSON은 `schema_version`, `experiment: {id}`, `infrastructure`,
검증된 `profile`, `dataset: {id, test_case_count}`, `sources`를 가진다.
`sources`에는 experiment/profile/dataset 각각의 절대 path와 원본 byte의 SHA-256을 남긴다.
파일명만 같은 수정된 입력과 revision이 다른 구성을 구분한다. 검증한 입력과 해석한
Dataset snapshot을 실행 끝까지 재사용하고 결과 작성 시 원본 파일을 다시 읽지 않는다. 환경변수 치환을 마친
Profile을 계획에 포함하며 target URL의 credential/userinfo는 기존 검증으로 거절한다.
유한하지 않은 수치와 잘못된 중첩 object 타입은 참조 Profile 전체에서도 ConfigurationError로
거절한다. 계획은 실행 전에 확정하고 실행 중 수정하지 않는다. dry-run은 API/AWS/DB/k6를 호출하지 않는다.

### 관측값 대조

새 manifest 경로의 ECS snapshot은 `ecs.worker`, 선택적 `ecs.api`를 구분한다. 각 service의
active task definition과 desired/running/pending/rollout 상태를 읽어 해당 expected와 대조한다.
Worker 동시성은 Worker의 실제 container environment에서 읽는다. API의 capacity를
Worker snapshot으로 대신하지 않는다. RDS/SageMaker의 식별·구성값도 지정된 expected와
대조한다. instance count/endpoint 상태는 준비 완료 여부까지 확인한다.

manifest의 Worker/RDS/SageMaker 식별자는 capacity 조회와 CloudWatch dimension의 공통
입력이다. manifest 경로에서 legacy expected-count 환경변수는 사용하지 않으며, 자원 선택도
manifest를 우선한다. API 지표를 추가할 경우 Worker 지표와 역할이 다른 ID를 사용한다.
source/DLQ queue URL·QueueName과 API base URL 등 실행 연결 입력은 기존 배포 환경 입력을
계속 사용한다. 이런 연결 입력을 capacity preset이나 가짜 metric으로 추정하지 않는다.

manifest 없이 실행하는 기존 경로의 단일 `ecs` snapshot과 환경변수 입력은 현재 소비처를
위해 유지한다.
설정 불일치·조회 실패·미완료 rollout은 seed/k6 전에 명확한 expected/actual 차이로 중단한다.

### Workload와 튜플

기존 ramp_up_seconds/duration_seconds 형식에 더해, Profile workload에 설정 기반 `stages`를
허용한다. stages가 있으면 위 두 duration 입력과 혼용하지 않는다.

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

각 stage의 duration은 유한한 양수, target은 0 이상의 정수다. stage는 비어 있지 않아야 하고
최소 하나의 양수 target이 필요하다. workload.concurrent_test_runs는 target의 최댓값과
같아야 한다. 기존 단일 target 경로에서는 종전 의미와 같다. 실험 튜플의 C는 관측된
실사용자 수가 아니라 이 **최대 configured VU target**이다. 전체 부하 곡선과 그 의미를
결과에 함께 저장한다. 한 VU는 하나의 비동기 TestRun 완료를 기다리는 현재 모델을 유지한다.
운영과 스트레스는 이름별 분기 대신 같은 schema의 다른 설정이다. 단계형 경로에서
`gracefulRampDown`은 VU 수를 줄일 때 종료할 VU의 진행 중 iteration을 기다리는 최대 시간이고,
`gracefulStop`은 시나리오 전체 종료 시 진행 중 iteration을 기다리는 최대 시간이다.
단계형 `gracefulRampDown`과 공통 `gracefulStop`은
`completion_timeout_seconds + max(1, ceil(polling_interval_seconds))`초로 설정한다.
추가 여유는 완료 timeout 직후 실패 판정과 지표 기록을 마칠 시간을 주기 위한 것이다.
유예 시간 내 완료를 보장하지 않으며, 시간이 만료되면 k6가 진행 중 iteration을 강제 중단할 수 있다.
interrupted iteration이 남으면 별도 기록하고 단계형 경로에서 유효한 완료 측정으로 판정하지 않는다.
기존 `ramp_up_seconds`/`duration_seconds` 경로의 `gracefulRampDown`은 `0s`를 유지한다.
실제 종료 시간은 이 grace 때문에 선언된 부하 구간보다 길 수 있다.

기존 acceptance schema와 k6 exit 99의 측정 FAIL 의미를 유지한다. 검증 fixture에는
각 시나리오의 사전 고정된 기준을 사용한다. 운영 오류율과 한계/회복 판정의 실제 수치는
인프라와 사용자 모델이 생긴 뒤 정한다. 지금 구현은 임의의 한계 지점·원인을 자동 단정하지 않는다.

### 결과와 오류

완료 결과의 `started_at`, `finished_at`, `revisions`, `profile`, `dataset`, `acceptance`와
파일명은 실제 분석 소비처를 위해 유지한다. 기존 `result.experiment`는 (W,C,T)
객체로 유지하고 manifest 식별 정보는 `result.execution_plan.experiment` 아래에 둔다. 결과에 schema_version=2와 해석된 실행 계획,
인프라 구성 ID/revision, 시나리오 ID/type과 입력 출처를 추가한다. metric이 수집되지
않은 경우를 실제 정상 metric으로 채우지 않는다.

오류는 완료 결과로 위장하지 않고 별도 `failure.json`에 기록한다. 최소한
schema_version=1, run_id, status=ERROR, phase, error(type/message), available_artifacts,
preservation_errors와 확보된 입력/revision을 남긴다. 원래 실패를 보존 실패로 덮어쓰지 않는다.
완료 S3 upload의 필수 파일 검증은 유지하고, 부분 upload는 별도 상태/저장 경로로 구분한다.
새 schema_version=2 결과는 모든 완료 파일 뒤에 upload-complete.json을 마지막으로 게시한다.
분석 소비처는 새 결과의 완료 표식을 확인한다. 실제 소비 중인 과거 schema-less 결과는
종전 필수 파일 검증으로 읽되 새 결과의 누락 표식을 legacy로 간주하지 않는다.
측정 이후 upload만 실패하면 로컬 result의 성능 PASS/FAIL은 그대로 두고 별도 failure.json에
phase=upload를 남긴다. 입력 검증 이전에 최소 run ID·단계를 마련하고, 결과 디렉터리 충돌은
기존 파일을 덮어쓰지 않는다. 보존 자체가 실패하면 stderr에 원래 실패와 보존 실패를 함께 남긴다.

## Alternatives

1. 환경변수·CLI만 확장: 기존 연계는 쉽지만 여러 입력의 조합과 실제 revision을 한 번에 검토·보존하기 어렵다.
2. workload Profile에 CPU/memory/tier까지 추가: 한 파일은 간단하지만 기존 승인된 소유권을 깨므로 선택하지 않는다.
3. 단일 manifest + 기존 Profile/Dataset 참조: 새 입력 검증이 필요하지만 소유권과 재현성을 함께 유지하므로 채택한다.

## Consequences

단일 manifest로 입력 조합과 revision을 검토하고 재현할 수 있다. 반면 manifest와
참조 파일 사이의 검증·snapshot 보존이 추가로 필요하다. 기존 CLI·완료 결과 소비처는
조사한 범위에서 유지하며 새 경로를 되돌리는 범위는 optional `--experiment`와 추가
metadata에 한정된다. DB migration이나 인프라 자원 변경은 없다.

## Validation

- manifest schema/참조/finite 수치/중복 입력을 검증하고 dry-run에서 외부 호출이 없음을 검사한다.
- API/Worker/SageMaker/RDS 차이와 미완료 상태에서 seed/workload가 차단되는지 검사한다.
- 운영/스트레스의 선언된 k6 stages와 tuple 의미를 검사한다.
- 결과의 입력 출처와 strict complete/partial artifact 처리를 검사한다.
- Runner와 k6 연결, 정상·불일치·중간 오류, 프로세스 정리를 검증한다.
  테스트 대역을 사용하는 검증과 실환경 통합 검증의 결과를 구분한다.
