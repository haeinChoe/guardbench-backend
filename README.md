# GuardBench Backend

> **Portfolio fork**  
> 이 저장소는 팀 프로젝트 `GuardBench/guardbench-backend`를 개인 포트폴리오용으로 fork한 저장소입니다.  
> 서비스의 설계·구현은 팀의 공동 결과물이며, 이 README는 실제 코드와 승인된 문서를 기준으로 Backend의 문제 해결 과정과 기술적 판단이 빠르게 드러나도록 재구성했습니다.

GuardBench는 **AI Application의 응답 행동을 반복 가능한 테스트 자산으로 검증하고, 배포 후보 간 Regression을 추적하는 테스트 플랫폼**입니다.

OpenAI-compatible AI Application을 실행한 뒤 Response Behavior Classifier가 자연어 응답을 `ALLOW | BLOCK`으로 정규화하고, Expected Result와 비교해 Assertion·Quality Gate·Regression 결과를 만듭니다.

---

## 왜 만들었나

AI Application의 안전성을 수동 프롬프트 테스트만으로 확인하면 다음 문제가 생깁니다.

- 같은 정책을 반복해서 검증하기 어렵습니다.
- Application이나 평가 설정이 바뀌었을 때 과거 결과와 비교하기 어렵습니다.
- 자연어 응답과 “기대했던 행동”을 분리해 기록하지 않으면 실패 원인을 추적하기 어렵습니다.
- 비동기 대량 실행에서는 retry, duplicate delivery, timeout이 테스트 결과 자체를 왜곡할 수 있습니다.

GuardBench Backend는 이를 위해 **테스트 정의와 실행 시점 Snapshot을 분리하고, 실행 결과를 저장한 뒤, Assertion과 Regression을 서로 다른 개념으로 계산**합니다.

## 핵심 흐름

```text
TestSuite / TestCase
        │
        │ TestRun 생성
        ▼
TestCaseSnapshot 고정
        │
        ▼
OpenAI-compatible AI Application 실행
        │
        ▼
Natural Language ApplicationResponse
        │
        ▼
Response Behavior Classifier
        │
        ▼
EvaluationResult (ALLOW | BLOCK)
        │
        ├── ExpectedResult와 비교 ──> AssertionResult
        │                              │
        │                              └──> QualityGateResult
        │
        └── 완료된 다른 TestRun의 저장 결과와 비교 ──> RegressionResult
```

### Assertion과 Regression을 분리한 이유

GuardBench에서 두 질문은 다릅니다.

```text
Assertion
"이번 Candidate가 기대한 정책을 만족했는가?"

Regression
"비교 가능한 과거 Run과 비교해 행동이 어떻게 바뀌었는가?"
```

따라서 Candidate가 기대값을 만족하는지와, 이전 버전 대비 변화가 생겼는지를 별도 결과로 다룹니다.

---

## Backend 설계

### 1. 실행 시점의 테스트 정의를 Snapshot으로 고정

`TestCase`는 계속 수정 가능한 자산입니다. 반면 과거 TestRun의 의미는 이후 편집 때문에 바뀌면 안 됩니다.

그래서 TestRun을 접수할 때 `TestCaseSnapshot`을 생성합니다.

```text
Mutable TestCase
      │
      │ TestRun 생성
      ▼
Immutable TestCaseSnapshot
      │
      └── execution / assertion / regression의 기준
```

원본 TestCase가 수정되거나 삭제되어도 과거 Snapshot과 실행 결과는 유지됩니다.

→ [핵심 도메인 모델](docs/domain/core-model.md)

### 2. AI Application과 평가기를 분리

Application Target은 자연어 응답을 생성할 뿐 `ALLOW | BLOCK`을 직접 결정하지 않습니다.

현재 MVP에서는:

- Application Target: OpenAI-compatible `HTTP_ENDPOINT`
- Evaluator: Amazon SageMaker Runtime의 Response Behavior Classifier
- Classifier output: `COMPLY | REFUSE`
- GuardBench normalization: `ALLOW | BLOCK`

으로 역할을 분리했습니다.

이를 통해 **서비스 응답 자체와 평가 결과를 동일한 값으로 취급하지 않도록 경계**를 만들었습니다.

→ [Response Behavior Classifier Adapter](docs/integrations/sagemaker-classifier-adapter.md)

### 3. 비동기 실행은 SQS delivery와 business retry를 분리

TestRun은 SQS와 Worker를 이용해 비동기로 실행됩니다.

개발 환경에서 실제로 다음과 같은 terminal convergence 문제가 관측됐습니다.

```text
expected snapshots = 78
terminal executions = 76

SQS receive count ≠ Provider invocation count
finalization retry 누적
→ 일부 메시지 DLQ 이동
→ TestRun이 RUNNING에 잔류
```

이 문제를 분석하면서 lifecycle을 세 층으로 분리했습니다.

| Lifecycle | 의미 |
| --- | --- |
| SQS delivery | 메시지 전달, visibility timeout, redrive |
| Worker claim | 동일 Snapshot의 동시 실행 방지 |
| Provider execution | 실제 외부 Application/Evaluator 호출과 business retry |

핵심 원칙은 **메시지가 다시 전달됐다는 이유만으로 Provider 시도 횟수를 증가시키지 않는 것**입니다.

또한 `AlreadyHeld`, partial finalization 같은 정상 중간 상태를 business failure로 취급하지 않고, 모든 실행이 결국 terminal 상태로 수렴하는지를 별도 검증 대상으로 둡니다.

→ [비동기 신뢰성 및 테스트 원칙](docs/architecture/async-reliability-and-testing.md)

### 4. 실행 결과를 재호출하지 않고 저장 결과로 비교

Regression 시 외부 Application이나 Evaluator를 다시 호출하면 비교 시점이 달라져 재현성이 깨집니다.

GuardBench는 완료된 TestRun에 저장된:

- Snapshot 정의
- Application response
- Evaluator verdict
- Assertion 결과

를 기반으로 비교합니다.

따라서 Regression 조회 자체가 외부 AI Provider의 현재 상태에 의존하지 않습니다.

→ [TestRun Persistence](docs/architecture/testrun-persistence.md)

### 5. Domain · DB · API 계약을 같은 불변식으로 유지

실행 결과는 상태에 따라 허용되는 shape가 다릅니다.

| executionStatus | error |
| --- | --- |
| `SUCCEEDED` | `null` |
| `FAILED` | `{ stage, code, message }` 필수 |
| `TIMED_OUT` | error 필수, `code=PROVIDER_TIMEOUT` |
| `NOT_STARTED` | `null` |

이 규칙을 Java Domain뿐 아니라 PostgreSQL CHECK, Persistence Mapper, integration test, OpenAPI 문서까지 맞추도록 관리합니다.

이는 “DB에는 저장되지만 Domain으로 복원할 수 없는 상태”를 방지하기 위한 계약입니다.

→ [API 계약](docs/api/README.md)  
→ [Evaluation Contract](docs/domain/evaluation-contract.md)

---

## 포트폴리오에서 강조하는 Backend 문제 해결 영역

이 fork에서는 단순 기능 목록보다 다음 기술적 판단과 개선 과정을 중심으로 살펴볼 수 있습니다.

### 비동기 신뢰성과 수렴성

- SQS delivery retry와 Provider business retry 분리
- Worker claim lease를 통한 중복 실행 제어
- Outbox 기반 비동기 이벤트 전달
- duplicate delivery와 partial finalization을 고려한 idempotence
- 개별 상태 전이뿐 아니라 **eventual convergence**를 테스트 기준으로 정의

### 데이터와 실행 재현성

- TestRun 시점에 TestCaseSnapshot 고정
- 현재 편집 자산과 historical execution identity 분리
- 완료 Run의 stored result만으로 Regression 계산
- Quality Gate 판정 당시 threshold와 metric을 함께 보존

### 계약 정합성

- Domain invariant와 DB CHECK constraint 정렬
- Persistence 복원 시 invalid state를 명시적 오류로 진단
- OpenAPI와 실제 상태별 nullable 규칙 동기화
- 공개 API와 내부 Domain Enum을 경계에서 명시적으로 변환

### 개발 품질과 검증

- ArchUnit으로 Context/architecture rule 검증
- Qodana JVM Community + SpotBugs 정적 분석
- PR에서는 변경 Java 범위 중심 분석
- 전체 정적 분석은 수동 workflow로 분리
- 단위/계약 테스트와 Testcontainers 기반 integration test 분리

---

## 기술 스택

| 영역 | 기술 |
| --- | --- |
| Language | Java 21 |
| Framework | Spring Boot |
| Build | Gradle |
| Database | PostgreSQL, Flyway |
| Messaging | Amazon SQS |
| AI Evaluation | Amazon SageMaker Runtime |
| Application Target | OpenAI-compatible HTTP API |
| Test | JUnit, Testcontainers, LocalStack, ArchUnit |
| Static Analysis | Qodana JVM Community, SpotBugs |
| CI | GitHub Actions |
| Observability | Amazon CloudWatch |
| Packaging | Docker |

---

## 검증 전략

테스트를 목적에 따라 분리했습니다.

```text
testFast
├─ unit test
├─ controller test
└─ contract test

integrationTest
├─ PostgreSQL Testcontainers
├─ SQS / LocalStack
├─ persistence integration
└─ async / E2E integration
```

PR CI에서는 `testFast`, `integrationTest`, `bootJar`를 독립적으로 실행하고 aggregate verification에서 결과를 확인합니다.

정적 분석은 개발 피드백 속도를 위해 PR 변경분 중심으로 수행하고, repository 전체 분석은 별도 수동 workflow로 실행할 수 있도록 분리했습니다.

---

## 로컬 실행

### 요구사항

- JDK 21
- Docker daemon
- 저장소에 포함된 Gradle Wrapper

로컬 환경 파일을 준비합니다.

```bash
cp .env.example .env
```

Spring Boot 실행:

```bash
./gradlew bootRun
```

`bootRun` 시 Spring Boot Docker Compose 지원이 `compose.yaml`의 PostgreSQL을 시작하고 Flyway migration을 적용합니다.

### 테스트

빠른 테스트:

```bash
./gradlew testFast
```

통합 테스트:

```bash
./gradlew integrationTest
```

전체 테스트:

```bash
./gradlew clean test
```

실행 가능한 JAR:

```bash
./gradlew bootJar
```

---

## 문서 탐색

| 문서 | 내용 |
| --- | --- |
| [문서 지도](docs/README.md) | 전체 문서 구조와 source-of-truth |
| [MVP 범위](docs/product/mvp-scope.md) | 현재 제품 범위와 비범위 |
| [핵심 도메인 모델](docs/domain/core-model.md) | Aggregate와 주요 invariant |
| [API 계약](docs/api/README.md) | REST API 공개 계약 |
| [OpenAPI](docs/api/openapi.yaml) | 기계 판독 가능한 API schema |
| [TestRun Persistence](docs/architecture/testrun-persistence.md) | 저장 구조와 historical identity |
| [비동기 신뢰성](docs/architecture/async-reliability-and-testing.md) | SQS/claim/retry/convergence 원칙 |
| [ADR](docs/decisions/README.md) | 주요 아키텍처 결정 기록 |
| [AI 개발 워크플로](docs/ai-development/workflow.md) | AI-assisted 개발 규칙 |
| [Codex 운영 규칙](AGENTS.md) | repository-level agent guardrail |

---

## Repository 관계

- **Portfolio fork:** `haeinChoe/guardbench-backend`
- **Original team repository:** `GuardBench/guardbench-backend`

이 fork는 팀 프로젝트 결과물을 개인 포트폴리오에서 기술적으로 설명하기 위한 저장소입니다. 원본 프로젝트의 상세 구현 계약은 코드와 `APPROVED` 상태의 GitHub 문서를 기준으로 확인할 수 있습니다.
