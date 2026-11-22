# 성능 테스트

> Status: APPROVED
> Owner: Backend
> Related Issue: #140, #187, #193, #200, #212, #24, #25, #26, #37, #39

## 목적과 경계

GuardBench 성능 Runner는 지정한 GuardBench API와 AI Target endpoint에 대해 재현 가능한
부하 시나리오와 Dataset을 실행하고 앱 수준 부하 결과를 기록한다.
Runner와 인프라의 소유권 경계는 [ADR 0017](decisions/0017-performance-runner-infrastructure-boundary.md),
테스트 유형·단계형 workload·실패 진단 계약은 [ADR 0016](decisions/0016-performance-experiment-plan.md)을 따른다.

| Backend 소유 | IaC 소유 |
| --- | --- |
| 테스트할 앱 버전 | 인프라 구성과 성능 환경의 생성·삭제 |
| 시나리오: 테스트 유형(`SMOKE`/`STRESS`/`SOAK`), workload, 앱 수준 acceptance | Runner 실행 기반(CodeBuild)과 단일 실행 진입점 |
| 데이터셋 (`performance/datasets/`) | 실행 전 capacity·queue 확인 |
| k6 스크립트와 k6 버전 (`performance/k6/VERSION`) | 인프라 지표·로그 수집과 인프라 수준 acceptance |
| Runner와 앱 수준 부하 결과 | 결과 보존, Runner 결과와 인프라 snapshot의 결합 `(W,C,T)` |

TestRun은 `POST /api/v1/test-runs`의 `202 Accepted`에서 끝나지 않고 비동기 Worker를 거쳐
`FINISHED`가 되므로, k6는 HTTP 생성 latency/오류율과 비동기 TestRun 완료 시간·실패율을 분리 측정한다.

```text
TestRun → controlled Application Target
        → Response Behavior Classifier
        → ALLOW/BLOCK 정규화 → Assertion → Metrics / Quality Gate
```

비교 가능한 실험에서는 고정된 synthetic Application Target 특성을 유지하며, target의 latency 변동이나
artificial throttling이 GuardBench 앱 측정에 섞이지 않도록 통제한다.

## Dataset과 시나리오

Dataset은 무엇을 실행할지, 시나리오는 어떤 부하 패턴과 앱 수준 기준으로 평가할지를 소유한다.

| 구분 | 책임 | 예시 |
| --- | --- | --- |
| Dataset | TestSuite/TestCase 내용과 고정된 TestCase 수 | `baseline-v1` (78건), `large-suite-491-v1` (491건) |
| 시나리오 | 테스트 유형, workload 곡선, target 유형, 앱 수준 acceptance | `FIXTURE-SMOKE` (`SMOKE`), `FIXTURE-STAGED-STRESS` (`STRESS`) |

- `performance/datasets/baseline-v1.yaml`(78건, `http-testset` SHA-256 고정)과
  `performance/datasets/large-suite-491-v1.yaml`(491건, `synthetic-testset`)을 immutable Dataset으로 관리한다.
- 시나리오 스키마:
  - `test`: `id`, `name`, `type`(`SMOKE` | `STRESS` | `SOAK`).
  - `workload`: `concurrent_test_runs`, `completion_timeout_seconds`, `polling_interval_seconds`,
    선택 `max_iterations_per_vu`, 그리고 `ramp_up_seconds`/`duration_seconds` 또는 단계형 `stages`.
  - `target`: `type: HTTP_ENDPOINT`만 선언한다. 환경별 URL·model·revision은 CLI 인자로만 받는다.
  - `acceptance`: `api.create_latency_ms`(`p50`/`p95`/`p99`), `api.error_rate`,
    `completion.max_seconds`, `completion.failure_rate`. 인프라 수준 기준은 시나리오에 두지 않으며
    알 수 없는 키는 `ConfigurationError`로 거절한다.

## 설치와 실행

필수 도구는 Python 3.11 이상과 [performance/k6/VERSION](../performance/k6/VERSION)에 선언된 버전(`0.55.0`)의 `k6`다.

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r performance/requirements.txt
```

Runner CLI 입력:

| 옵션 | 필수 여부 | 설명 |
| --- | --- | --- |
| `--api-url` | 필수 | 부하를 보낼 GuardBench API base URL |
| `--target-url` | 필수 | TestRun이 평가할 AI Target endpoint URL |
| `--target-model` | 필수 | 평가할 Target의 model 식별자 |
| `--target-revision` | 선택 | 배포된 Target의 revision |
| `--scenario <yaml>` | 필수 | 시나리오 YAML 경로 |
| `--dataset <yaml>` | 필수 | Dataset YAML 경로 |
| `--result-dir <dir>` | 선택 | 산출물을 기록할 로컬 디렉터리 |
| `--dry-run` | 선택 | 입력 검증과 해석된 실행 계획 출력(외부 호출 없음) |

```bash
# 입력 검증과 실행 계획만 생성 (외부 API/k6 호출 없음)
python3 -m performance.runner.cli \
  --api-url https://<guardbench-api> \
  --target-url https://<openai-compatible-target>/v1/chat/completions \
  --target-model <model> \
  --target-revision <revision> \
  --scenario <scenario.yaml> \
  --dataset <dataset.yaml> \
  --result-dir <dir> \
  --dry-run

# 실제 실행: preflight(API health 및 진행 중 TestRun 없음 확인) → seed → k6 → report
python3 -m performance.runner.cli \
  --api-url https://<guardbench-api> \
  --target-url https://<openai-compatible-target>/v1/chat/completions \
  --target-model <model> \
  --target-revision <revision> \
  --scenario <scenario.yaml> \
  --dataset <dataset.yaml> \
  --result-dir <dir>
```

Internal ALB 뒤에 있는 대상 API/Target을 호출하는 환경에서는 Worker 설정의
`guardbench.http-endpoint.allowed-private-hostnames`에 전체 hostname을 정확히 등록하고
`allow-private-addresses: false`를 유지한다.

## 산출물과 판정

- **종료 코드**:
  - `0`: 앱 수준 acceptance `PASS` 또는 `--dry-run` 완료
  - `1`: 측정이 완료된 정상 실행 결과의 acceptance `FAIL` (k6 threshold breach `exit 99` 포함)
  - `2`: Runner 실행 오류 (`ConfigurationError`, `RunnerError`, `ApiError` 및 예기치 않은 예외)
- **`--dry-run` 산출물**: `--result-dir`에 `scenario.yaml`, `dataset.yaml`, `execution-plan.json`을 생성한다.
- **완료 실행 산출물**: `scenario.yaml`, `dataset.yaml`, `execution-plan.json`, `k6-summary.json`,
  `result.json`(`schema_version: 2`), `report.md` 6개 파일을 생성한다.
- **`result.json`과 `report.md`**:
  - 시나리오 ID·유형(`scenario`), Dataset(`id`, `test_case_count`, `suite_id`), 입력 파일 경로와 원본 byte
    SHA-256(`execution_plan.sources`), 해석된 연결 정보(`connection`), 해석된 `workload`(단계형 `stages` 포함),
    `(C, T)` 튜플(`tuple`: `concurrent_test_runs`, `test_case_count`)과 `C` 의미, k6 진단 요약, 앱 수준 판정(`acceptance`)을 기록한다.
  - `C`는 관측된 동시 사용자 수가 아니라 **최대 configured VU target**이며 한 VU가 TestRun 하나 완료를 기다린다.
    실험 튜플 `(W, C, T)`의 `W`와 인프라 수준 판정·최종 결합은 IaC가 소유한다.
  - 수집되지 않은 metric은 정상 수치로 채우지 않고 `null`로 남긴다.

## 실패 진단과 부분 결과 보존

- 실행 중 오류가 발생하면(`--dry-run` 제외) `failure.json`(`schema_version: 1`, `status: "ERROR"`,
  `phase`: `preflight|seed|k6|report`, `started_at`, `failed_at`, `error`, `inputs`,
  `available_artifacts`, `preservation_errors`)을 `--result-dir`에 기록한다.
- 이미 존재하는 `--result-dir`는 덮어쓰지 않으며, 보존이 실패해도 원래 예외를 덮지 않고 `stderr`에 함께 출력한다.
- URL userinfo, Bearer token, secret/key/password 패턴은 `failure.json`과 `stderr`에서 마스킹한다.
- 미생성 파일(`k6-summary.json`, `result.json`, `report.md` 등)은 오류가 난 시점에 없으면 생성하지 않는다.

## 로컬 Mock E2E 검증

실제 배포 API나 외부 인프라 없이 **실제 Python Runner와 실제 `k6` 프로세스**를 로컬 모의 HTTP API로
연결해 정상 완료(`0`), 측정 FAIL(`1`), 실행 오류 진단 보존(`2`)을 검증할 수 있다.
자세한 내용은 [LOCAL_E2E.md](../performance/runner/LOCAL_E2E.md)를 참고한다.

```bash
python3 -m performance.runner.local_e2e
python3 -m unittest discover -s performance -p 'test_*.py' -t .
```
