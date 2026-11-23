package com.guardbench.evaluator.infrastructure.sagemaker;

import java.util.IllegalFormatException;
import java.util.Objects;

import com.guardbench.evaluator.infrastructure.classifier.ClassifierChatCompletion;
import com.guardbench.evaluator.infrastructure.classifier.ClassifierEvaluatorStore;
import com.guardbench.testrun.application.port.out.EvaluatorExecutionPort;
import com.guardbench.testrun.application.port.out.EvaluatorExecutionRequest;
import com.guardbench.testrun.application.port.out.EvaluatorExecutionResult;
import com.guardbench.testrun.application.port.out.EvaluatorFailureCode;

import software.amazon.awssdk.core.SdkBytes;
import software.amazon.awssdk.core.exception.SdkException;
import software.amazon.awssdk.services.sagemakerruntime.SageMakerRuntimeClient;
import software.amazon.awssdk.services.sagemakerruntime.model.InvokeEndpointRequest;
import software.amazon.awssdk.services.sagemakerruntime.model.InvokeEndpointResponse;

import tools.jackson.core.JacksonException;
import tools.jackson.databind.ObjectMapper;

/**
 * (prompt, Application 자연어 응답)을 SageMaker endpoint에 서빙된 텍스트 모델로 {@code COMPLY | REFUSE}
 * 이진 분류하는 Response Behavior Classifier adapter다.
 *
 * <p>endpoint는 DJL LMI(vLLM) 컨테이너의 OpenAI-compatible chat completions 스키마를 사용한다.
 * classifier는 도메인 정책(SAFE/UNSAFE), 응답 품질 또는 PASS/FAIL을 판단하지 않는다. 실제 응답이 핵심
 * 요청을 수행했는지(COMPLY) 또는 거부했는지(REFUSE)만 관측하며, 이 값을 기존 코어와 호환되는
 * {@code COMPLY -> ALLOW}, {@code REFUSE -> BLOCK}로 정규화해 반환한다. 요청·응답 계약은
 * {@link ClassifierChatCompletion}을 따른다.
 *
 * <p>SageMaker 호출 실패, timeout, throttling과 classifier 출력 파싱 실패는 {@code ALLOW}/{@code BLOCK}로
 * 임의 fallback하지 않고 {@link EvaluatorFailureCode}로 실패를 전달한다.
 */
public final class SageMakerClassifierEvaluatorAdapter implements EvaluatorExecutionPort {

    private final SageMakerRuntimeClient client;
    private final ClassifierEvaluatorStore evaluatorStore;
    private final SageMakerClassifierProperties properties;
    private final ObjectMapper objectMapper;

    public SageMakerClassifierEvaluatorAdapter(
            SageMakerRuntimeClient client,
            ClassifierEvaluatorStore evaluatorStore,
            SageMakerClassifierProperties properties,
            ObjectMapper objectMapper
    ) {
        this.client = Objects.requireNonNull(client, "SageMakerRuntimeClient must not be null");
        this.evaluatorStore = Objects.requireNonNull(evaluatorStore, "evaluator store must not be null");
        this.properties = Objects.requireNonNull(properties, "classifier properties must not be null");
        this.objectMapper = Objects.requireNonNull(objectMapper, "object mapper must not be null");
    }

    @Override
    public EvaluatorExecutionResult evaluate(EvaluatorExecutionRequest request) {
        Objects.requireNonNull(request, "evaluator request must not be null");
        if (!properties.isConfigured()) {
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
            requestBody = ClassifierChatCompletion.requestBody(objectMapper, null, properties.prompt(), request);
        } catch (JacksonException | IllegalFormatException exception) {
            return EvaluatorExecutionResult.failed(EvaluatorFailureCode.EVALUATOR_CONFIGURATION_INVALID);
        }

        try {
            // modelId는 SageMaker 맥락에서 endpoint name이다.
            InvokeEndpointResponse response = client.invokeEndpoint(InvokeEndpointRequest.builder()
                    .endpointName(evaluator.modelId())
                    .contentType("application/json")
                    .accept("application/json")
                    .body(SdkBytes.fromUtf8String(requestBody))
                    .build());
            if (response == null || response.body() == null) {
                return EvaluatorExecutionResult.failed(EvaluatorFailureCode.PROVIDER_RESPONSE_INVALID);
            }
            return ClassifierChatCompletion.parseResponse(objectMapper, response.body().asByteArray());
        } catch (SdkException exception) {
            return EvaluatorExecutionResult.failed(SageMakerFailureCodeMapper.map(exception));
        }
    }
}
