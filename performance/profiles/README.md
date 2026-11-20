# Performance Profiles

성능 실험은 다음 네 개의 개념을 분리한다.

1. **Dataset / Dataset Size**: 한 TestRun이 처리할 TestCase 집합과 개수다. 현재 확정된 Dataset은 `baseline-v1` 78건뿐이며, `SMALL/MEDIUM/LARGE`의 경계값은 아직 정하지 않는다.
2. **Test Type**: 실험 목적이다. Runner는 `SMOKE`, `LOAD`, `PEAK`, `STRESS`, `SOAK`를 허용한다.
3. **Workload**: 시스템에 가하는 부하 값이다. Profile의 `concurrent_test_runs`, `ramp_up_seconds`, `duration_seconds`, `max_iterations_per_vu`, timeout/polling 값이 여기에 속한다.
4. **Infrastructure Capacity**: ECS task 수, CPU/memory, RDS 사양, SageMaker endpoint capacity처럼 시스템이 공급하는 configured resource와 dependency 상태다. Performance Profile이 소유하지 않고 Terraform/배포 설정이 소유한다.

따라서 Profile은 **Test Type + Workload + acceptance criteria + Application Target**을 정의한다. Dataset은 `performance/datasets/`에서 별도로 선택하고, Infrastructure Capacity는 배포 입력과 Infrastructure revision으로 별도 추적한다.

ECS capacity 입력도 환경별 배포 설정에 속한다. dev는 `api_cpu`/`api_memory`를 사용하고,
Performance는 dev와 분리된 CPU/memory 입력(예: `performance_api_cpu`/`performance_api_memory`)을
사용한다. Performance 입력은 dev Task Definition에 전달되지 않는다. 실제 Terraform 연결은
[guardbench-iac Issue #41](https://github.com/GuardBench/guardbench-iac/issues/41),
실행 전 적용값 검증은 [#200](https://github.com/GuardBench/guardbench-backend/issues/200)의
Backend 계약을 따른다.

Runner는 Profile에서 capacity를 읽지 않는다. 실행 대상 Performance Service의 active Task
Definition을 조회해 `result.json.infrastructure_capacity.ecs.task_cpu`와
`ecs.task_memory`에 실제 적용된 CPU/memory를 기록한다. 이 snapshot은 입력 의도나 capacity
level 이름이 아니라 AWS configured capacity의 실행 시점 증거다.

MVP의 Capacity 실험 범위는 주요 변경 대상과 고정·관찰 대상을 구분한다.

| 구분 | 대상 | 처리 원칙 |
| --- | --- | --- |
| 주요 Capacity 실험 대상 | Performance Backend ECS, SageMaker classifier | configured capacity를 변경·비교 |
| 고정·관찰 대상 | Performance 전용 RDS, SQS | 실행 기간 동안 구성을 고정하고 병목·backlog를 관찰 |

RDS instance identifier/class는 snapshot에 계속 기록하지만 RDS instance class sweep, sizing,
storage/IOPS/connection pool 튜닝은 MVP 실험 범위가 아니다. RDS와 SQS는 Profile/Workload에
포함하지 않으며, ECS/SageMaker와 동일한 주요 capacity tuning 축으로 해석하지 않는다.

현재 TestRun은 Application Target 응답을 SageMaker Response Behavior Classifier로 분류한 뒤 Assertion/Quality Gate까지 비동기로 처리한다. Profile은 classifier 설정을 입력으로 받지 않으며 classifier endpoint/prompt 등은 Backend 배포 설정이 소유한다.

## 현재 확정된 Profile

`smoke.yaml`이 현재 유일하게 수치까지 확정된 canonical Profile이다.

- Test Type: `SMOKE`
- Dataset: 별도 선택, 현재 기본값 `baseline-v1` 78 TestCases
- concurrent TestRuns: `1`
- max iterations per VU: `1`
- completion timeout: `300s`
- polling interval: `2s`

`smoke.yaml`은 현재 유일한 실행 가능한 canonical Profile이다. Profile 이름은 Dataset Size나 Infrastructure Capacity를 의미하지 않는다.

LOAD/PEAK/STRESS/SOAK의 구체적인 workload 값과 Dataset Size의 `SMALL/MEDIUM/LARGE` 경계는 사전에 고정하지 않는다. Smoke 결과와 이후 단계적 실험 결과를 보고 정한다. 새 Profile은 Runner 핵심 로직에 타입별 분기문을 추가하지 않고 동일 schema로 추가한다.

## 실험 진행 규칙

1. Dataset과 Profile/Workload, 고정 대상인 RDS/SQS 구성을 유지한 상태에서 Smoke로 실행 경계를 검증한다.
2. 다음 Workload 값은 직전 실행의 completion time, SQS backlog/drain, ECS/RDS/SageMaker 지표를 근거로 정한다.
3. Workload 증가에 따른 포화 구간을 찾은 뒤, 동일 Dataset/Workload와 고정된 RDS/SQS 구성을 유지하고 ECS/SageMaker Capacity만 변경해 효과를 비교한다.
4. Dataset Size 자체의 영향을 보고 싶을 때만 Dataset을 별도 축으로 변경한다.

즉 `Workload 증가`와 `ECS/SageMaker Capacity 증가`를 같은 실험에서 동시에 바꾸지 않는다. 그래야 병목 원인을 구분할 수 있다.

## 단계형 workload (#23)

[ADR 0016](../../docs/decisions/0016-performance-experiment-plan.md)의 APPROVED/ACCEPTED
실행 계약에 따른 단계형 부하 지원이다. 아래 fixture의 숫자는 기능 검증용이며 실제 운영
preset이나 승인된 목표 수치로 해석하지 않는다.
기존 smoke/launcher의 `ramp_up_seconds` + `duration_seconds` 입력은 계속 지원한다.
새로운 `stages`는 두 입력과 함께 쓸 수 없다.

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

`stages`는 순서가 있는 비어 있지 않은 배열이다. 각 duration은 유한한 양수이고 target은
0 이상의 정수다. 양수 target이 최소 하나 있어야 하고, 최상위 `concurrent_test_runs`는
stage target의 최댓값과 같아야 한다. 알 수 없는 stage 키도 실행 전에 거절한다.
k6 `ramping-vus`의 각 stage는 지정한 duration 동안 이전 target에서 새 target으로
선형 이동한다. 일정 부하는 같은 target의 stage를 연속 선언해 표현한다. 실험 튜플의 C는
최대 configured VU target이며 관측된 사용자 수·실제 동시 Run 수를 보장하는 값이 아니다.
한 VU는 TestRun 한 건을 생성하고 FINISHED까지 polling한다. 시나리오 이름별 실행 분기는 없다.

단계형 경로의 `gracefulRampDown`과 일반 종료 `gracefulStop`은
`completion_timeout_seconds + max(1, ceil(polling_interval_seconds))`의 제한된 여유 시간을
두어 timeout 직후 실패 판정과 `completionFailures`/`iterationsCompleted` 집계를 마칠 시간을 준다.
유예 시간 내 완료를 보장하지 않으며 시간이 만료되면 진행 중 iteration이 강제 중단될 수 있다.
또한 polling loop는 `startedAt + completion_timeout_seconds`를 마감 시각으로 삼아 대기 시간을
남은 예산 안으로 제한한다. 하강 구간에서도 진행 중 polling을 기다리므로 실제 종료 시각은 선언한
stage duration의 합보다 늦을 수 있다. 새 workload의 `test_run_iterations_started`와
`test_run_iterations_completed` Counter는 실제 TestRun iteration이 강제 중단 없이 끝까지
실행됐는지 센다. 반복 상한 때문에 건너뛴 k6 iteration은 제외하며, 접수 실패 또는 완료/timeout
polling을 반영한 뒤에만 completed를 증가시킨다(TestRun 성공 여부는 `api.create_error_rate`와
`completion.failure_rate`가 별도로 판정한다). `completion.iterations` 판정은
`workload.stages`를 선언한 단계형 Profile에만 적용해 `started = completed > 0`을 요구하고
`interrupted = started - completed`를 결과와 report에 남긴다. 기존
`ramp_up_seconds`/`duration_seconds` 기반 승인 Profile에는 이 새 판정을 강제하지 않으며,
기존 보관 artifact도 다시 작성하지 않는다.

ADR 0016이 선택한 테스트 유형은 `SMOKE`·`STRESS`·`SOAK`다. 현재 이 변경은 단계형
실행 기반과 `LOAD` 운영·`STRESS` 검증 fixture를 제공한다. 세 유형별 실제 프로필의
부하 수치·시간·판정 기준, SOAK 프로필 구현과 기존 유형의 전환 처리는 완료된 것으로
간주하지 않으며 #23의 후속 구현 범위다.

검증용 곡선은 [`staged-operating.yaml`](../fixtures/profiles/staged-operating.yaml)과
[`staged-stress.yaml`](../fixtures/profiles/staged-stress.yaml)에 있다. 두 fixture는 동일 schema의
서로 다른 부하/acceptance 입력이며 숫자는 짧은 로컬 기능 검증용이다. `small` 자원값,
운영 목표, 한계 지점 또는 실제 AWS 성능 baseline을 뜻하지 않는다. Profile은 infrastructure
capacity를 바꾸지 않는다. acceptance는 실행 전에 고정하고 실행 후에 바꾸지 않는다.

```bash
python3 -m unittest performance.runner.test_workload_stages -v
```

k6가 설치되어 있으면 위 테스트는 실제 k6 inspect와 임시 모의 HTTP API를 실행한다.
정상 하강에서 polling 완료와 강제 중단에서 acceptance FAIL을 확인하고 서버/프로세스를 정리한다.
k6가 없으면 실제 실행 테스트는 명시적으로 skip되며 Python schema/판정 테스트만 동작한다.
실환경 성능 검증은 별도다. 최종 Runner/AWS 대역 연결은 후속 #26 검증에서 다룬다.
