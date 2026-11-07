# 0015. SQS 통합 테스트 에뮬레이터를 Floci로 교체

> Status: DRAFT
> Owner: Backend
> Last reviewed: 2026-11-07
> Canonical source: GitHub
> Origin: GitHub Issue #14
> AI assistance: 이 문서의 초안은 LLM의 도움으로 작성되었으며 사람의 검토가 필요합니다.

- ADR Status: PROPOSED
- Decision date: 2026-11-07
- Related Issue: #14

## Context

SQS 통합 테스트 7개(21개 케이스)는 Testcontainers로 LocalStack을 띄워 실행한다. 운영 코드는 LocalStack에 의존하지 않고, 테스트만 의존한다.

LocalStack은 2026.03.0부터 이미지를 하나로 통합하고 auth token이 있어야 시작되도록 바꿨다. Community 저장소도 아카이브됐다. 지금 테스트는 예전 태그(`3.0` 6개, `4.0` 1개)에 고정해 토큰 없이 돌아가지만, 이 상태로는 보안 패치나 버그 수정을 받을 수 없다.

[Floci](https://github.com/floci-io/floci)는 MIT 라이선스 AWS 에뮬레이터다. 계정이나 토큰 없이 쓸 수 있고, LocalStack과 같은 4566 포트와 엔드포인트 형식을 제공한다.

## Decision

- SQS 통합 테스트의 에뮬레이터를 `floci/floci:2.4.0`으로 교체한다. 모듈은 `io.floci:testcontainers-floci-aws:2.17.0`을 쓴다.
- 이미지 태그는 stable 버전으로 고정한다. `latest`와 `nightly`는 쓰지 않는다.
- 교체하면서 드러난 두 가지 차이는 아래처럼 처리한다.

### 차이 1: visibility timeout 정밀도 (테스트 가정 수정)

visibility timeout을 1초로 두고 재전달 간격을 측정했다.

| 에뮬레이터 | 재전달 간격 (중앙값) |
| --- | --- |
| LocalStack 3.0 / 4.0 | 2.01초 |
| Floci 2.4.0 | 1.01초 |

LocalStack은 visibility 만료를 약 2초 단위로만 처리한다. Floci는 지정한 값에 맞춰 재전달하며, 실제 SQS 동작에 더 가깝다.

`SqsReliabilityConvergenceIntegrationTest`는 #149 순서를 재현하려고 일부러 `visibility timeout(1s) < claim lease(3s)`로 설정한다. 이 설정에서 Worker attempt 3회를 모두 쓰려면 최소 `(3 - 1) × 3s = 6s`가 필요하다. 그런데 메시지가 DLQ로 가기 전까지 버틸 수 있는 시간은 `maxReceiveCount(5) × 1s = 5s`뿐이다.

- LocalStack: 실제 재전달 간격이 2초라서 약 10초를 버틴다. 그래서 테스트가 통과했다.
- Floci: 5초 만에 DLQ로 이동한다. 그래서 Scenario 1과 4가 terminal 상태로 수렴하지 못하고 매번 실패했다.

결국 이 테스트는 LocalStack의 부정확한 타이머 덕분에 통과하고 있었다.

테스트의 visibility timeout을 2초로 바꾼다. 그러면 `visibility(2s) < lease(3s)`라는 #149 재현 조건을 유지하면서 `5 × 2s = 10s > 6s`도 만족한다. 이 관계는 테스트 주석에 남긴다.

운영 설정은 영향을 받지 않는다. 운영값은 visibility 90초, claim lease 45초다. visibility가 lease보다 길어서 재전달될 때마다 lease가 이미 만료돼 있으므로, 5번 수신 안에 attempt 3회를 모두 쓸 수 있다.

### 차이 2: 제어 문자 본문 검증 (Floci 호환성 공백)

실제 SQS와 LocalStack은 `\u0000` 같은 제어 문자가 든 본문을 `InvalidMessageContents`로 항목별 거부한다. Floci는 이를 그대로 받아들인다.

그래서 batch 부분 실패를 검증하던 통합 테스트 케이스를 단위 테스트(`SqsPublishAdapterTest`)로 옮긴다. 단위 테스트는 SQS 응답의 `Failed` 항목을 직접 구성해서, 어댑터가 실패 항목만 실패로 매핑하는지 검증한다. 실제 SQS의 제어 문자 거부 동작 자체는 더 이상 에뮬레이터로 검증하지 않는다.

### 동일하게 동작함을 확인한 SQS 기능

아래 기능은 별도 실측으로 LocalStack 3.0, 4.0과 같은 결과를 확인했다.

- `maxReceiveCount` 기반 DLQ 이동 시점
- `ReceiveMessage`의 `VisibilityTimeout` 지정
- `ChangeMessageVisibility`
- 큐 단위 visibility timeout
- long polling 대기
- `ApproximateReceiveCount`

## Alternatives

- **LocalStack 예전 태그 유지**: 지금 당장은 비용이 없다. 하지만 패치를 받을 수 없고, 위의 visibility 부정확성도 그대로 남는다.
- **LocalStack 유료 또는 Hobby 플랜과 auth token 사용**: CI secret 관리가 필요하고, 상업적 사용 조건이 붙는다.
- **LocalEmu 등 LocalStack Community 포크**: 아카이브된 코드를 이어받은 것이라 visibility 정밀도 문제를 그대로 물려받을 가능성이 높다. 직접 확인하지는 않았다.
- **모든 SQS 테스트를 실제 AWS에서 실행**: no-cloud CI 정책과 맞지 않는다.

## Consequences

장점

- 토큰이나 계정 없이 계속 업데이트를 받을 수 있다.
- visibility 타이밍이 실제 SQS에 더 가까워져, 에뮬레이터 특성에 기대는 테스트 가정이 드러난다.
- 자원을 적게 쓴다. 이미지는 1,085MB에서 280MB로, 실행 중 메모리는 343MB에서 25MB로 줄었다.
- 컨테이너 종료가 빠르다. 4.7초에서 0.3초로 줄었다.

비용과 위험

- 제어 문자 거부처럼 Floci가 실제 SQS를 덜 정확하게 흉내 내는 부분이 있다. 버전을 올릴 때 같은 측정 스크립트로 다시 확인해야 한다.
- 2026년에 시작된 신규 프로젝트이고, Testcontainers 모듈도 Testcontainers 공식이 아니라 Floci 프로젝트가 직접 관리한다.
- 컨테이너 시작은 1.2초에서 약 2.5초로 오히려 느려졌다.
- 테스트 시간 합계는 약 10% 줄었지만, Gradle 전체 실행 시간은 줄지 않았다. 원인은 확인하지 못했다.

되돌리기

- 테스트 코드와 `build.gradle`만 바뀌므로, PR revert로 LocalStack 고정 태그 상태로 돌아갈 수 있다.
- 다만 visibility 2초 설정은 LocalStack에서도 통과하므로 되돌리지 않아도 된다.

## Validation

- `./gradlew testFast integrationTest` 통과
- `SqsReliabilityConvergenceIntegrationTest`를 Floci에서 반복 실행해 실패 없음 확인
- 테스트 코드와 `build.gradle`에 `localstack` 참조가 남지 않음
