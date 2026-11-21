# 로컬 mock E2E 검증 harness

실제 AWS 인프라나 배포된 Backend 없이 **실제 Python Runner와 실제 `k6` 프로세스**를 끝까지 연결해
정상 실행, capacity 불일치 차단, 중간 실행 오류 진단 보존을 검증합니다.

## 실행 명령

저장소 루트에서 실행합니다 (`performance/requirements.txt`와 `k6` 필요):

```bash
python3 -m performance.runner.local_e2e
```

단위·통합 테스트 묶음으로 함께 실행하려면:

```bash
python3 -m unittest discover -s performance -p 'test_*.py' -t .
```

- AWS credential(`AWS_ACCESS_KEY_ID` 등)이나 AWS endpoint, 실제 DB/SQS/ECS가 필요하지 않습니다.
- `k6` 실행 파일(`PATH` 또는 `K6_BIN`)이 없으면 명확한 오류 메시지를 출력하고 종료 코드 `2`로 종료합니다.

## 실제 실행 구성 요소 vs 테스트 대역 범위

| 구분 | 항목 |
| --- | --- |
| **실제 실행 구성 요소** | `performance/runner/plan.py` 입력 해석(`smoke.yaml`, `profiles/smoke.yaml`, `datasets/two-cases.yaml`), `cli.execute` 전체 phase orchestration, `capacity.py` strict capacity 대조, `api.py` HTTP 호출, 실제 `k6` 바이너리 및 `performance/k6/test-run.js`, `acceptance.py` 판정, `results.py` 보고서 생성, `failure.py` 실패 보존 |
| **테스트 대역 범위** | `local_e2e.py` 내부의 로컬 `ThreadingHTTPServer` 모의 API (`/api/v1/test-suites`, `/api/v1/test-runs`, `/api/v1/test-runs/<id>`, `/results`), fake `boto3` client (`ecs`, `rds`, `sagemaker`, `sqs`, `cloudwatch`), `PERFORMANCE_RESULTS_BUCKET` 미설정으로 S3 업로드 생략 |

운영 Runner `cli.py`에는 preflight 우회 옵션이나 테스트 대역을 두지 않으며, 모든 대역 주입은
`performance/runner/local_e2e.py` 내부에서만 수행합니다.

## 검증 시나리오와 종료 정리

1. **`normal_completion`**: manifest 실행 계획 해석 → preflight 통과 → TestSuite seed → 실제 `k6`가 모의 API로 `POST /api/v1/test-runs`(202) 및 polling(`RUNNING` → `FINISHED`/`COMPLETED`) 수행 → queue drain → fake CloudWatch metric 수집 → `result.json`(`schema_version: 2`, `execution_plan.experiment.id`, `scenario`), `report.md` 등 8개 artifact 생성 확인.
2. **`capacity_mismatch`**: manifest expected와 다른 fake ECS capacity 응답에서 `preflight` 단계에 즉시 차단되어 seed/k6가 시작되지 않고 `failure.json`(`phase: "preflight"`)이 남는지 확인.
3. **`mid_run_failure`**: seed와 실제 `k6` 실행 이후 `drain` 단계에서 모의 API 오류가 발생할 때 `failure.json`(`phase: "drain"`, `suite_id`, `error`)과 이미 생성된 `k6-summary.json`·입력 파일이 보존되는지 확인.

모든 시나리오는 성공·실패와 관계없이 종료 시 모의 HTTP 서버 종료, 잔여 `k6` 자식 프로세스 점검, 임시 디렉터리 삭제를 확인합니다.

> **주의**: 본 harness는 로컬 기능·연결 검증용이며, 실제 AWS 인프라·IAM·네트워크·성능 Baseline 검증이 아닙니다.
