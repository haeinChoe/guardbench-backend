# Performance Profiles

성능 실험은 다음 네 개의 개념을 분리한다.

1. **Dataset / Dataset Size**: 한 TestRun이 처리할 TestCase 집합과 개수다. 실제 Dataset은 `baseline-v1` 78건이고 별도 `large-suite-491-v1` 491건 파일도 있다. 두 Dataset의 내용·버전은 고정하며, `SMALL/MEDIUM/LARGE`의 경계값은 아직 정하지 않는다.
2. **Test Type**: 실험 목적이다. 사용할 유형은 `SMOKE`·`STRESS`·`SOAK`이며 Runner는 이 세 유형만 허용한다.
3. **Workload**: 시스템에 가하는 부하 값이다. Profile의 `concurrent_test_runs`, `ramp_up_seconds`/`duration_seconds` 또는 단계형 `stages`, `max_iterations_per_vu`, timeout/polling 값이 여기에 속한다.
4. **Infrastructure Capacity**: ECS task 수, CPU/memory, RDS 사양, SageMaker endpoint capacity처럼 시스템이 공급하는 configured resource와 dependency 상태다. Performance Profile이 소유하지 않고 Terraform/배포 설정이 소유한다.

따라서 Profile은 **Test Type + Workload + acceptance criteria + Application Target**을 정의한다. Dataset은 `performance/datasets/`에서 별도로 선택하고, Infrastructure Capacity는 배포 입력과 Infrastructure revision으로 별도 추적한다.

ECS capacity 입력도 환경별 배포 설정에 속한다. dev는 `api_cpu`/`api_memory`를 사용하고,
Performance는 dev와 분리된 CPU/memory 입력(예: `performance_api_cpu`/`performance_api_memory`)을
사용한다. Performance 입력은 dev Task Definition에 전달되지 않는다. 실제 Terraform 연결은
[guardbench-iac Issue #41](https://github.com/GuardBench/guardbench-iac/issues/41),
실행 전 적용값 검증은 [#200](https://github.com/GuardBench/guardbench-backend/issues/200)의
Backend 계약을 따른다.

Runner는 Profile에서 capacity를 읽지 않는다. [ADR 0016](../../docs/decisions/0016-performance-experiment-plan.md)의
manifest 경로는 `infrastructure.expected`와 AWS 조회값을 대조하고 snapshot을
`result.json.infrastructure_capacity.ecs.worker`와 선택적 `ecs.api`에 구분해 기록한다.
이 snapshot은 인프라 수준 이름이 아닌 실행 전 AWS configured capacity의 조회 증거다.

MVP의 Capacity 실험 범위는 주요 변경 대상과 고정·관찰 대상을 구분한다.

| 구분 | 대상 | 처리 원칙 |
| --- | --- | --- |
| 주요 Capacity 실험 대상 | Performance Backend ECS, SageMaker classifier | configured capacity를 변경·비교 |
| 고정·관찰 대상 | Performance 전용 RDS, SQS | 실행 기간 동안 구성을 고정하고 병목·backlog를 관찰 |

RDS instance identifier/class는 snapshot에 계속 기록하지만 RDS instance class sweep, sizing,
storage/IOPS/connection pool 튜닝은 MVP 실험 범위가 아니다. RDS와 SQS는 Profile/Workload에
포함하지 않으며, ECS/SageMaker와 동일한 주요 capacity tuning 축으로 해석하지 않는다.

현재 TestRun은 Application Target 응답을 SageMaker Response Behavior Classifier로 분류한 뒤 Assertion/Quality Gate까지 비동기로 처리한다. Profile은 classifier 설정을 입력으로 받지 않으며 classifier endpoint/prompt 등은 Backend 배포 설정이 소유한다.

## 현재 프로필 파일

Runner는 기본 프로필 없이 experiment manifest(`--experiment`)가 참조하는 Profile만 실행한다.
legacy 기본 실행용 `smoke.yaml`(`SMOKE`)과 `concurrent-10.yaml`(`LOAD`)은 제거했다
([ADR 0016 Amendments](../../docs/decisions/0016-performance-experiment-plan.md#amendments)).
현재 이 디렉터리에는 실제 운영 프로필 파일이 없고, 검증용 fixture는 `performance/fixtures/profiles/`에 있다.

입력 검증은 `SMOKE`·`STRESS`·`SOAK`만 허용한다. 허용 유형이 있다는 사실이 해당 프로필의
구현 완료를 뜻하지 않는다. 검증용 fixture 파일과 실제 실행 프로필을 구분한다.
Profile 이름은 Dataset Size나 Infrastructure Capacity를 의미하지 않는다.

사용할 `SMOKE`·`STRESS`·`SOAK`의 목적은 ADR 0016에 정의한다. 유형별 부하 수치·시간·판정 기준과
신규 프로필은 #23에서 구현한다. Dataset Size의 `SMALL/MEDIUM/LARGE` 경계는 별도 미정이며
인프라 구성 수준과 같은 축이 아니다. 새 Profile은 타입 이름별 분기 없이 같은 schema로 표현한다.

## 실험 진행 규칙

1. Dataset과 Profile/Workload, 고정 대상인 RDS/SQS 구성을 유지한 상태에서 Smoke로 실행 경계를 검증한다.
2. 다음 Workload 값은 직전 실행의 completion time, SQS backlog/drain, ECS/RDS/SageMaker 지표를 근거로 정한다.
3. Workload 증가에 따른 포화 구간을 찾은 뒤, 동일 Dataset/Workload와 고정된 RDS/SQS 구성을 유지하고 ECS/SageMaker Capacity만 변경해 효과를 비교한다.
4. Dataset Size 자체의 영향을 보고 싶을 때만 Dataset을 별도 축으로 변경한다.

즉 `Workload 증가`와 `ECS/SageMaker Capacity 증가`를 같은 실험에서 동시에 바꾸지 않는다. 그래야 병목 원인을 구분할 수 있다.
