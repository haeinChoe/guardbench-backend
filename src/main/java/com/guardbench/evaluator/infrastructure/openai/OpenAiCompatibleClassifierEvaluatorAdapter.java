package com.guardbench.evaluator.infrastructure.openai;

import java.io.IOException;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.net.http.HttpTimeoutException;
import java.time.Duration;
import java.util.IllegalFormatException;
import java.util.Objects;

import com.guardbench.evaluator.infrastructure.classifier.ClassifierChatCompletion;
import com.guardbench.evaluator.infrastructure.classifier.ClassifierEvaluatorStore;
import com.guardbench.testrun.application.port.out.EvaluatorExecutionPort;
import com.guardbench.testrun.application.port.out.EvaluatorExecutionRequest;
import com.guardbench.testrun.application.port.out.EvaluatorExecutionResult;
import com.guardbench.testrun.application.port.out.EvaluatorFailureCode;

import tools.jackson.core.JacksonException;
import tools.jackson.databind.ObjectMapper;

/**
 * OpenAI-compatible chat completions server를 직접 호출하는 Response Behavior Classifier adapter다.
 *
 * <p>SageMaker adapter와 같은 요청·응답 계약({@link ClassifierChatCompletion})을 사용하고 전송 방식만
 * 다르다. 요청 body의 {@code model}은 TestRun이 고정한 evaluator reference의 {@code modelId}다.
 *
 * <p>timeout, 연결 실패, HTTP 오류와 classifier 출력 파싱 실패는 {@code ALLOW}/{@code BLOCK}으로 fallback하지
 * 않고 {@link EvaluatorFailureCode}로 실패를 전달한다. provider 원문 응답과 오류는 결과에 포함하지 않는다.
 */
final class OpenAiCompatibleClassifierEvaluatorAdapter implements EvaluatorExecutionPort {

    private static final String JSON_CONTENT_TYPE = "application/json";

    private final HttpClient httpClient;
    private final ClassifierEvaluatorStore evaluatorStore;
    private final OpenAiCompatibleClassifierProperties properties;
    private final ObjectMapper objectMapper;

    OpenAiCompatibleClassifierEvaluatorAdapter(
            HttpClient httpClient,
            ClassifierEvaluatorStore evaluatorStore,
            OpenAiCompatibleClassifierProperties properties,
            ObjectMapper objectMapper
    ) {
        this.httpClient = Objects.requireNonNull(httpClient, "http client must not be null");
        this.evaluatorStore = Objects.requireNonNull(evaluatorStore, "evaluator store must not be null");
        this.properties = Objects.requireNonNull(properties, "classifier properties must not be null");
        this.objectMapper = Objects.requireNonNull(objectMapper, "object mapper must not be null");
    }

    @Override
    public EvaluatorExecutionResult evaluate(EvaluatorExecutionRequest request) {
        Objects.requireNonNull(request, "evaluator request must not be null");
        URI endpoint = properties.chatCompletionsUri();
        if (endpoint == null || !properties.isConfigured()) {
            return EvaluatorExecutionResult.failed(EvaluatorFailureCode.EVALUATOR_CONFIGURATION_INVALID);
        }
        ClassifierEvaluatorStore.ClassifierEvaluator evaluator = evaluatorStore
                .findByReference(request.evaluatorReference().value())
                .orElse(null);
        if (evaluator == null) {
            return EvaluatorExecutionResult.failed(EvaluatorFailureCode.EVALUATOR_NOT_FOUND);
        }

        final String requestBody;
        try {
            requestBody = ClassifierChatCompletion.requestBody(
                    objectMapper, evaluator.modelId(), properties.prompt(), request);
        } catch (JacksonException | IllegalFormatException exception) {
            return EvaluatorExecutionResult.failed(EvaluatorFailureCode.EVALUATOR_CONFIGURATION_INVALID);
        }

        HttpRequest httpRequest = HttpRequest.newBuilder(endpoint)
                .timeout(Duration.ofMillis(properties.requestTimeoutMs()))
                .header("Content-Type", JSON_CONTENT_TYPE)
                .header("Accept", JSON_CONTENT_TYPE)
                .POST(HttpRequest.BodyPublishers.ofString(requestBody))
                .build();

        try {
            HttpResponse<byte[]> response = httpClient.send(httpRequest, HttpResponse.BodyHandlers.ofByteArray());
            EvaluatorFailureCode statusFailure = statusFailure(response.statusCode());
            if (statusFailure != null) {
                return EvaluatorExecutionResult.failed(statusFailure);
            }
            return ClassifierChatCompletion.parseResponse(objectMapper, response.body());
        } catch (HttpTimeoutException exception) {
            return EvaluatorExecutionResult.failed(EvaluatorFailureCode.PROVIDER_TIMEOUT);
        } catch (InterruptedException exception) {
            Thread.currentThread().interrupt();
            return EvaluatorExecutionResult.failed(EvaluatorFailureCode.PROVIDER_TIMEOUT);
        } catch (IOException exception) {
            return EvaluatorExecutionResult.failed(EvaluatorFailureCode.PROVIDER_UNAVAILABLE);
        }
    }

    /**
     * 408·429와 5xx는 재시도 가능한 일시 실패, 401·403은 권한 거부, 그 밖의 4xx(존재하지 않는 model의
     * 404 포함)는 구성 오류로 분류한다.
     */
    private static EvaluatorFailureCode statusFailure(int statusCode) {
        if (statusCode >= 200 && statusCode < 300) {
            return null;
        }
        if (statusCode == 401 || statusCode == 403) {
            return EvaluatorFailureCode.EVALUATOR_ACCESS_DENIED;
        }
        if (statusCode == 408 || statusCode == 429 || statusCode >= 500) {
            return EvaluatorFailureCode.PROVIDER_UNAVAILABLE;
        }
        if (statusCode >= 400) {
            return EvaluatorFailureCode.EVALUATOR_CONFIGURATION_INVALID;
        }
        return EvaluatorFailureCode.PROVIDER_RESPONSE_INVALID;
    }
}
