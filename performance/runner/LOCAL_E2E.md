# 로컬 mock E2E 검증 harness

배포된 Backend나 외부 인프라 없이 **실제 Python Runner와 실제 `k6` 프로세스**를
로컬 모의 HTTP API로 연결해 정상 완료, 측정 FAIL, 실행 오류 진단 보존을 검증합니다.

## 실행 명령

저장소 루트에서 실행합니다 (`performance/requirements.txt`와 `k6` 필요):

```bash
python3 -m performance.runner.local_e2e
```

전체 테스트 묶음을 함께 실행하려면:

```bash
python3 -m unittest discover -s performance -p 'test_*.py' -t .
```

- `k6` 실행 파일(`PATH` 또는 `K6_BIN`)이 없으면 오류 메시지를 출력하고 종료 코드 `2`로 종료합니다.
- `k6` 선언 버전은 [`performance/k6/VERSION`](../k6/VERSION)에 있습니다.

## 실제 실행 구성 요소 vs 테스트 대역 범위

| 구분 | 항목 |
| --- | --- |
| **실제 실행 구성 요소** | `performance/runner/plan.py` 입력 해석(`smoke.yaml`, `two-cases.yaml`, CLI 연결 인자), `cli.main`/`cli.execute` phase orchestration, `api.py` HTTP 호출, 실제 `k6` 바이너리 및 `performance/k6/test-run.js`, `acceptance.py` 판정, `results.py` 보고서 생성, `failure.py` 실패 보존 |
| **테스트 대역 범위** | `local_e2e.py` 내부의 로컬 `ThreadingHTTPServer` 모의 API (`/api/v1/test-suites`, `/api/v1/test-runs`, `/api/v1/test-runs/<id>`, `/api/v1/test-runs/<id>/results`) 및 고정 모의 Target URL |

Runner CLI에는 테스트 우회 옵션이 없으며, 모든 모의 서버 구성은 `performance/runner/local_e2e.py` 내부에서만 수행합니다.

## 검증 시나리오와 종료 정리

1. **`normal_completion`** (종료 코드 `0`): 입력 해석 → preflight 통과 → TestSuite seed → 실제 `k6`가 모의 API로 `POST /api/v1/test-runs`(202) 및 polling(`RUNNING` → `FINISHED`/`COMPLETED`) 수행 → `scenario.yaml`, `dataset.yaml`, `execution-plan.json`, `k6-summary.json`, `result.json`(`schema_version: 2`, `tuple`), `report.md` 생성 확인 (`failure.json` 없음).
2. **`measurement_fail`** (종료 코드 `1`): 모의 API가 TestRun `executionOutcome: ERROR`를 반환할 때 `k6` threshold 위반(exit `99`) 후에도 `result.json`(`acceptance.status: "FAIL"`)과 `report.md`를 생성하고 `failure.json`을 만들지 않는지 확인.
3. **`execution_error`** (종료 코드 `2`): seed와 실제 `k6` 실행 이후 `report` 진단 조회에서 모의 API 오류가 발생할 때 `failure.json`(`phase: "report"`, `suite_id`, `error.type: "ApiError"`)과 `k6-summary.json`·입력 스냅샷만 보존하고 `result.json`·`report.md`를 만들지 않는지 확인.

모든 시나리오는 성공·실패와 관계없이 종료 시 모의 HTTP 서버 종료, 잔여 `k6` 자식 프로세스 점검, 임시 디렉터리 삭제를 확인합니다.

> **주의**: 본 harness는 로컬 기능·연결 검증용이며 실제 성능 Baseline 검증이 아닙니다.
