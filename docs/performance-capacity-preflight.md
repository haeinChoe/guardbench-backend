# Manifest capacity preflight 구현 제안

> Status: DRAFT
> Owner: Backend
> Related Issue: #22 (선행 #20, #21)
> AI assistance: 구현 검토가 필요한 초안이다.

`--experiment` 실행은 [ADR 0016](decisions/0016-performance-experiment-plan.md)의
`infrastructure.expected`와 독립적으로 조회한 AWS 값을 대조한다.
`--dry-run`은 기존처럼 외부 서비스를 호출하지 않는다.

- ECS Worker와 선택적 API를 각 cluster/service로 조회한다. CPU/memory, desired/running,
  pending=0, ACTIVE service를 확인한다. 하나의 PRIMARY/COMPLETED deployment가 service와
  같은 task definition을 사용하고 desired/running/pending 수도 준비 완료 상태여야 한다.
  Worker의 W는 실제 task definition container environment에서 읽고 API에서는 읽지 않는다.
- RDS는 instance identifier/class와 `available` 상태를 확인한다. RDS 튜닝 기능은 추가하지 않는다.
- SageMaker는 endpoint/production variant/instance type, desired/current instance count와
  `InService` 상태를 확인한다. 현재 count를 desired count 대신 채택하지 않는다.
- 조회 실패, 누락, 불일치, 미완료 배포는 seed와 k6 실행 전에 차단한다. 오류는 자원 경로와
  expected/actual을 표시한다. AWS 오류를 설정값으로 채우거나 자원을 수정하지 않는다.

관측 snapshot은 `infrastructure_capacity.ecs.worker`와 선택적 `ecs.api`, `rds`, `sagemaker`에
기록한다. 성공 대조는 `preflight.capacity.expected/actual`에 남는다. ECS task definition ARN과
배포 상태도 snapshot에 포함한다. (W,C,T)는 Worker의 관측 W/desired count를 사용한다.
API까지 포함한 보고서 구성과 결과 provenance 확장은 후속 #24 범위다.

Manifest의 Worker/RDS/SageMaker 식별자는 capacity 조회와 CloudWatch dimensions의 공통
입력이며 기존 자원 환경변수보다 우선한다. ECS ARN 입력은 CloudWatch 이름 dimension으로
변환한다. SQS QueueName/URL과 API base URL은 환경 입력을 유지한다. 환경을 전역 수정하지 않는다.
현재 metric 정의는 Worker ECS를 측정한다. API metric을 Worker ID로 생성하지 않는다.

기존 manifest 없는 `collect()`와 단일 `ecs` snapshot, 환경 기반 expected count 검증은
`bin/run-performance`, `bin/verify-runtime`, Docker ENTRYPOINT 및 기존 Runner/분석 소비처를
위해 유지한다. 공개 API, DB schema, migration, production dependency는 변경하지 않는다.
구 경로 제거는 소비처 전환과 별도 계약 대체가 승인될 때 검토한다.

검증은 `python3 -m unittest discover -s performance/runner -p 'test_*.py' -q`로 실행한다.
AWS client 대역의 역할별 응답/조회 인자, 누락·오류, 모든 비교 필드 차이에서 seed/k6 차단,
legacy 경로, dry-run 경계를 확인한다. 실제 AWS, IAM/VPC, quota, 실제 k6 성능은 검증하지 않았다.
되돌리기는 이 optional manifest preflight 변경을 revert하는 것으로 가능하며 데이터 변경은 없다.
