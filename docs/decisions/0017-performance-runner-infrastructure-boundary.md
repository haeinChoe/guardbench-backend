# 0017. 성능 Runner와 인프라의 경계

> Status: APPROVED
> Owner: Backend
> Last reviewed: 2026-11-22
> Canonical source: GitHub
> Origin: GitHub Issue #37

- ADR Status: ACCEPTED
- Decision date: 2026-11-22
- Related Issue: #37; 인프라 측 결정 haeinChoe/guardbench-iac#12

## Context

성능 Runner가 측정 대상 인프라를 직접 다루고 있다. Runner가 ECS·RDS·SageMaker의
capacity를 조회해 기대값과 대조하고, CloudWatch 지표를 수집하고, 실행 전후 SQS
queue를 확인하고, 결과를 S3에 보존한다. 기대 capacity와 자원 식별자는 실행 입력에
사람이 다시 적는데, 이 값의 원본은 IaC다.

그 결과 인프라가 없거나 바뀔 때마다 Backend 작업이 막혔고, 같은 값을 두 저장소에서
맞춰야 했다. 실행 기반도 바뀐다. Runner는 IaC가 관리하는 CodeBuild에서 실행되고,
실험 하나의 생성·실행·정리는 IaC 저장소의 단일 진입점이 지휘한다
(haeinChoe/guardbench-iac#12).

## Decision

### 소유권

Backend는 **무엇을 시험하는가**를, IaC는 **어디서·무엇으로 시험하는가**를 소유한다.
Backend 저장소의 코드·설정·workflow는 AWS 자원, 자격 증명, 인프라 구성 이름을 알지 않는다.

| Backend 소유 | IaC 소유 |
| --- | --- |
| 테스트할 앱 버전 | 인프라 구성과 성능 환경의 생성·삭제 |
| 시나리오: 테스트 유형(`SMOKE`/`STRESS`/`SOAK`), workload, 앱 수준 acceptance | Runner 실행 기반(CodeBuild)과 단일 실행 진입점 |
| 데이터셋 | 실행 전 capacity·queue 확인 |
| k6 스크립트와 k6 버전 | 인프라 지표·로그 수집과 인프라 수준 acceptance |
| Runner와 부하 결과 | 결과 보존, Runner 결과와 인프라 snapshot의 결합 |

### Runner 계약

Runner는 아래 입력만으로 실행된다.

| 입력 | 의미 |
| --- | --- |
| `--api-url` | 부하를 보낼 GuardBench API base URL |
| `--target-url` | TestRun이 평가할 AI Target endpoint |
| `--target-model`, `--target-revision` | 배포된 Target의 model과 revision. revision은 선택 |
| `--scenario <yaml>` | 테스트 유형, workload, 앱 수준 acceptance, Target 유형 |
| `--dataset <yaml>` | 시드할 TestCase |
| `--result-dir <dir>` | 결과를 쓸 로컬 디렉터리 |
| `--dry-run` | 입력 검증과 해석된 계획 출력. 외부 호출 없음 |

- 시나리오 파일은 환경별 연결 정보를 담지 않는다. 연결 정보는 위 CLI 입력으로만 받는다.
- Runner는 AWS SDK와 AWS 자격 증명을 사용하지 않는다. 결과를 원격에 업로드하지 않는다.
- 산출물은 `--result-dir` 아래에 쓴다.
  - 완료 시: `result.json`, `report.md`, k6 summary.
  - 오류 시: `failure.json`과 그 시점까지 확보한 자료.
- 종료 코드: `0` 측정 PASS, `1` 측정 FAIL, `2` 실행 오류.
- k6 버전은 Backend 저장소의 `performance/k6/VERSION` 한 곳에서 선언한다. 실행 기반은 이 버전을 설치한다.

### 결과와 판정

- `result.json`은 부하 결과와 앱 수준 판정만 담는다.
  - 판정 항목: API 생성 지연(p50/p95/p99), API 오류율, TestRun 완료 시간과 실패율.
  - 결과에는 시나리오 ID·유형, 데이터셋, 입력 파일 SHA-256, 해석된 workload, C와 T를 남긴다.
- 실험 튜플의 W(Worker 동시성)는 인프라 값이다. IaC가 Runner 결과와 인프라 snapshot을 결합할 때 (W,C,T)를 완성한다.
- C의 의미는 ADR 0016과 같다(최대 configured VU target).
- queue drain, DLQ, 자원 사용률 같은 인프라 수준 기준은 IaC가 판정한다.
- 실험의 최종 판정은 앱 수준 판정과 인프라 수준 판정을 함께 본다.

### ADR 0016과의 관계

**이 ADR이 ADR 0016에서 대체하는 범위:**
- 실행 입력의 인프라 구성 식별자와 기대 capacity
- 관측값 대조(capacity preflight)
- 결과의 인프라 식별 필드
- 결과의 원격 보존

**ADR 0016에서 그대로 유지하는 범위:** 테스트 유형과 목적, 단계형 workload와 C의 의미,
한 번에 실험 하나, 실패 진단 자료(`failure.json`) 계약.

## Alternatives

1. **실행 입력에 인프라 구성을 유지한다.**
   - IaC 값의 사본이 Backend에 남는다.
   - 인프라가 없거나 바뀌면 Backend 작업이 막히는 문제가 그대로라 선택하지 않는다.
2. **Backend workflow가 진입점이 되어 IaC를 호출한다.**
   - Runner는 인프라를 몰라도 된다.
   - 하지만 workflow가 인프라 구성 이름과 권한을 알아야 해서 결합이 workflow로 옮겨갈 뿐이므로 선택하지 않는다.
3. **Backend는 인프라를 모르고 IaC가 진입점을 맡는다.**
   - ADR 0016 일부를 대체하고 열린 구현을 정리해야 한다.
   - 대신 Backend 작업을 인프라 없이 끝까지 검증할 수 있어 채택한다.

## Consequences

**장점**
- Runner는 어떤 URL에도 실행할 수 있다.
- 로컬 mock E2E가 정식 실행 경로와 같은 입력으로 동작한다.
- 인프라 값은 IaC 한 곳에만 존재한다.

**비용**
- Runner에서 AWS 조회·지표 수집·업로드와 인프라 acceptance를 제거해야 한다.
- 성능 AWS workflow(Runner 이미지 발행, 분석 bundle)는 IaC 진입점으로 대체되어 제거된다.
- IaC는 capacity 확인, 지표·로그 수집, 결과 결합·보존을 새로 맡는다.
- Backend PR 버전을 성능 시험하려면 IaC 진입점에서 Backend commit을 지정해 실행한다.

**되돌리기:** 이 결정을 되돌리려면 새 ADR로 대체한다.

## Validation

- `performance/` 아래 Runner 코드와 Backend workflow에 AWS SDK, AWS 자원 식별자, 자격 증명 참조가 없다.
- Runner 단위 테스트와 로컬 mock E2E가 위 CLI 입력만으로 정상·측정 FAIL·실행 오류를 검증한다.
- IaC 진입점이 Backend commit을 받아 Runner를 실행한다. 그 결과에 Runner 결과와 인프라 snapshot이 함께 남는다.
