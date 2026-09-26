# Backend CI Branch 검증과 Merge Gate

> Status: DRAFT
> Owner: Backend
> Scope: GitHub Issue #8

이 문서는 현재 Backend CI workflow와 dev/main ruleset 구성을 기록한다. 구현 판단은 Issue와 APPROVED 계약을 우선한다.

## Backend CI workflow

[Backend CI workflow](../../.github/workflows/backend-ci.yml)는 다음 event에서 검증한다.

| Event | 대상 | 동작 |
| --- | --- | --- |
| `pull_request` | base branch `dev`, `main` | workflow policy test를 실행하고, 관련 source/build/workflow 변경이면 `testFast`, `integrationTest`, `bootJar`, `verify` 실행 |
| `push` | `dev`, `main` | workflow policy test를 실행하고, 관련 source/build/workflow 변경이면 `testFast`, `integrationTest`, `bootJar`, `verify` 실행 |
| `workflow_dispatch` | 실행자가 선택한 branch | 입력 없이 수동 verification 실행. 전체 `testFast`, `integrationTest`, `bootJar`, `verify` 실행 |

`dev`는 개발 통합·안정화 branch이며 `main`은 안정된 기준선이다. 어느 branch의 push도 배포하지 않는다. `workflow_dispatch`도 수동 CI 검증 전용이다.

현재 workflow에는 deployment job, AWS credentials/OIDC 권한, ECS/ECR 호출, worker image sync 경로가 없다. 클라우드 리소스가 없으므로 staging은 향후 배포 환경을 가리키는 문서 개념이다. `main → staging runtime` 배포와 worker 동기화는 별도 Issue 범위다.

변경 감지는 `src/`, Gradle build 설정, `qodana.yaml`, Backend CI workflow를 대상으로 한다. 관련 변경이 없으면 Gradle 검증은 생략하지만 `workflow_tests`는 실행된다. 수동 실행은 전체 검증을 수행한다. CI의 `verify`는 `testFast`, `integrationTest`, `bootJar`가 모두 성공해야 통과한다. OpenAPI 명세는 별도 `OpenAPI contract` workflow가 검증한다.

## Required check와 현재 ruleset

Actions UI의 check 이름은 `Backend CI / verify`이고 ruleset status context는 `verify`다. Repository의 `dev`와 `main` ruleset은 모두 PR과 `verify` required check를 적용한다. main ruleset을 갱신할 때 기존 PR 보호, 삭제 방지, non-fast-forward 방지 규칙을 유지했다.

## 검증 범위

- `scripts/test-backend-ci-branch-policy.sh`는 dev/main PR·push, 수동 검증 경로, 배포 job 및 AWS 접근 부재, worker sync 비활성 상태를 검사한다.
- ECS 배포 전용 검증 스크립트는 현재 workflow에서 호출되지 않아 제거했다.
- PR check 실행에서 workflow policy test, `testFast`, `integrationTest`, `bootJar`, `verify`를 확인한다.

## 향후 배포

실제 클라우드 리소스가 다시 준비되면 `main → staging` runtime 배포 정책을 별도 Issue로 정의하고 구현한다. 해당 작업 전에는 GitHub Environment, OIDC, ECS/ECR 또는 worker sync 연결을 가정하지 않는다.
