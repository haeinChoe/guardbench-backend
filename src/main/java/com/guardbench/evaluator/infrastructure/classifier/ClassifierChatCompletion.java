package com.guardbench.evaluator.infrastructure.classifier;

import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

import com.guardbench.testrun.application.port.out.EvaluatorExecutionRequest;
import com.guardbench.testrun.application.port.out.EvaluatorExecutionResult;
import com.guardbench.testrun.application.port.out.EvaluatorFailureCode;

import tools.jackson.core.JacksonException;
import tools.jackson.databind.JsonNode;
import tools.jackson.databind.ObjectMapper;

/**
 * Response Behavior Classifier의 OpenAI-compatible chat completions 요청·응답 계약(ADR 0013)이다.
 *
 * <p>Provider adapter는 전송 방식만 다르고 요청 JSON, {@code COMPLY -> ALLOW}, {@code REFUSE -> BLOCK}
 * 정규화와 파싱 실패 처리는 이 클래스를 공유한다. 예상하지 못한 출력은 {@code ALLOW}/{@code BLOCK}으로
 * fallback하지 않고 {@link EvaluatorFailureCode#PROVIDER_RESPONSE_INVALID}로 실패한다.
 */
public final class ClassifierChatCompletion {

    private static final String ALLOW_ACTION = "ALLOW";
    private static final String BLOCK_ACTION = "BLOCK";
    private static final String COMPLY_LABEL = "COMPLY";
    private static final String REFUSE_LABEL = "REFUSE";
    private static final double TEMPERATURE = 0;
    private static final int MAX_TOKENS = 8;

    private ClassifierChatCompletion() {
    }

    /**
     * classifier 요청 body를 만든다. {@code model}이 {@code null}이면 {@code model} 필드를 생략한다
     * (SageMaker endpoint는 endpoint name으로 모델을 식별한다).
     */
    public static String requestBody(
            ObjectMapper objectMapper,
            String model,
            ClassifierPrompt prompt,
            EvaluatorExecutionRequest request
    ) {
        String userMessage = prompt.renderUserMessage(request.prompt(), request.applicationResponse());
        Map<String, Object> body = new LinkedHashMap<>();
        if (model != null) {
            body.put("model", model);
        }
        body.put("messages", List.of(
                Map.of("role", "system", "content", prompt.systemPrompt()),
                Map.of("role", "user", "content", userMessage)));
        body.put("temperature", TEMPERATURE);
        body.put("max_tokens", MAX_TOKENS);
        body.put("chat_template_kwargs", Map.of("enable_thinking", false));
        return objectMapper.writeValueAsString(body);
    }

    public static EvaluatorExecutionResult parseResponse(ObjectMapper objectMapper, byte[] body) {
        if (body == null || body.length == 0) {
            return invalidProviderResponse();
        }
        try {
            JsonNode root = objectMapper.readTree(body);
            JsonNode choices = root == null || !root.isObject() ? null : root.get("choices");
            if (choices == null || !choices.isArray() || choices.isEmpty()) {
                return invalidProviderResponse();
            }
            JsonNode firstChoice = choices.get(0);
            JsonNode message = firstChoice == null || !firstChoice.isObject() ? null : firstChoice.get("message");
            JsonNode content = message == null || !message.isObject() ? null : message.get("content");
            if (content == null || !content.isTextual()) {
                return invalidProviderResponse();
            }
            return toResult(content.asText());
        } catch (JacksonException exception) {
            return invalidProviderResponse();
        }
    }

    private static EvaluatorExecutionResult toResult(String text) {
        String label = normalizeLabel(text);
        if (label.equals(COMPLY_LABEL)) {
            return EvaluatorExecutionResult.succeeded(ALLOW_ACTION);
        }
        if (label.equals(REFUSE_LABEL)) {
            return EvaluatorExecutionResult.succeeded(BLOCK_ACTION);
        }
        return invalidProviderResponse();
    }

    /**
     * 모델이 라벨 앞뒤에 따옴표나 구두점을 섞어 반환하는 경우를 관용적으로 허용한다(예: {@code "COMPLY"},
     * {@code COMPLY.}). 라벨 중간 문자는 건드리지 않으므로 {@code COMPLY REFUSE} 같은 값은 계속
     * 유효하지 않은 것으로 처리한다.
     */
    private static String normalizeLabel(String text) {
        return text.strip()
                .replaceAll("^[\"'`]+|[\"'`.,!?]+$", "")
                .toUpperCase();
    }

    private static EvaluatorExecutionResult invalidProviderResponse() {
        return EvaluatorExecutionResult.failed(EvaluatorFailureCode.PROVIDER_RESPONSE_INVALID);
    }
}
