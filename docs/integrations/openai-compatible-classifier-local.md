# 로컬 OpenAI-compatible Classifier 설정

> Status: DRAFT
> Owner: Backend
> Last reviewed: 2026-11-21
> Related Issue: [#12](https://github.com/haeinChoe/guardbench-backend/issues/12)

이 문서는 로컬 실험 브랜치의 OpenAI-compatible Response Behavior Classifier 설정을 설명한다.
[ADR 0013](../decisions/0013-response-behavior-classifier.md)과
[SageMaker Adapter](sagemaker-classifier-adapter.md)의 승인된 배포 계약을 변경하지 않는다.

## Provider 선택

서버의 `guardbench.evaluator.provider` 설정으로 분류기 adapter를 선택한다.
TestRun 생성 요청에는 provider·model·endpoint·prompt를 추가하지 않는다.

| 설정값 | 동작 |
| --- | --- |
| 미설정 또는 `sagemaker` | 기존 SageMaker Runtime Adapter와 `SAGEMAKER` registration 사용 |
| `openai-compatible` | JDK HttpClient로 chat completions URL을 직접 호출하고 `OPENAI_COMPATIBLE` registration 사용 |

API와 Worker에는 같은 provider/model 설정을 전달한다. Worker 실행 adapter는
`guardbench.worker.enabled=true`일 때 조립한다. OpenAI-compatible provider를 선택하면
SageMaker client를 조립하지 않는다. 지원하지 않는 provider 값은 registration 조립에 실패한다.

## 환경 입력

| 환경변수 | 용도 |
| --- | --- |
| `GUARDBENCH_EVALUATOR_PROVIDER` | `openai-compatible` 선택. 기본값은 `sagemaker` |
| `OPENAI_COMPATIBLE_CLASSIFIER_URL` | 전체 chat completions URL. 예: `http://127.0.0.1:8000/v1/chat/completions` |
| `OPENAI_COMPATIBLE_CLASSIFIER_MODEL` | 서버에 실제 서빙된 model 이름 |
| `OPENAI_COMPATIBLE_CLASSIFIER_SYSTEM_PROMPT` | 별도로 준비한 classifier system prompt |
| `OPENAI_COMPATIBLE_CLASSIFIER_USER_PROMPT_TEMPLATE` | 선택 사항. 비어 있으면 기본 사용자 메시지 형식 사용 |

URL은 절대 HTTP/HTTPS 주소다. Adapter가 `/v1/chat/completions`를 자동으로 붙이지 않으므로
전체 경로를 설정한다. model과 system prompt는 저장소에 임의의 값을 넣지 않고 배포 환경에서
주입한다. URL·model·prompt가 유효하지 않으면 classifier 실행은
`EVALUATOR_CONFIGURATION_INVALID`로 실패한다.

## 요청과 실패 처리

TestCase prompt와 ApplicationResponse를 함께 보내며 요청에는 `model`, `messages`,
`temperature: 0`, `max_tokens: 8`, `chat_template_kwargs.enable_thinking: false`를 포함한다.
라벨 정규화와 prompt 처리는 기존 SageMaker adapter와 공통 구현을 사용한다.
분류 실패를 ALLOW/BLOCK으로 대체하지 않는다.

- 연결 timeout 기본값은 5초, 요청 timeout 기본값은 15초다. 두 설정의 합은 45초 claim lease 미만이어야 한다.
- Adapter 자체 재시도는 없으며 transient failure의 재시도는 Worker claim retry가 담당한다.
- HTTP 401·403은 `EVALUATOR_ACCESS_DENIED`, 408·429·5xx는 `PROVIDER_UNAVAILABLE`, 나머지 4xx는 `EVALUATOR_CONFIGURATION_INVALID`로 처리한다.
- timeout은 `PROVIDER_TIMEOUT`, 연결 오류는 `PROVIDER_UNAVAILABLE`, 잘못된 응답은 `PROVIDER_RESPONSE_INVALID`로 처리한다.

로컬 compose·서버 기동, API key 인증과 실제 vLLM 연결 검증은 이 안내에 포함하지 않는다.
