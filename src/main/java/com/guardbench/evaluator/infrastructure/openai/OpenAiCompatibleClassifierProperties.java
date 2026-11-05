package com.guardbench.evaluator.infrastructure.openai;

import java.net.URI;
import java.util.Locale;

import com.guardbench.evaluator.infrastructure.classifier.ClassifierPrompt;

import org.springframework.boot.context.properties.ConfigurationProperties;

/**
 * OpenAI-compatible chat completions server(예: 로컬 vLLM)에 서빙된 Response Behavior Classifier 호출
 * 설정이다.
 *
 * <p>{@code chatCompletionsUrl}, {@code model}, {@code systemPrompt}가 비어 있거나 템플릿이 잘못되면
 * 애플리케이션은 정상 기동하지만 classifier 호출 시점에 {@code EVALUATOR_CONFIGURATION_INVALID}로 실패한다.
 *
 * <p>호출 한도는 SageMaker adapter와 같은 원칙을 따른다(ADR 0005). 연결과 응답 대기를 합한 시간이 45초
 * claim lease 미만이어야 하며, adapter 자체 재시도는 없다. transient 실패 재시도는 Worker claim retry가
 * 전담한다.
 */
@ConfigurationProperties(prefix = "guardbench.openai-compatible.classifier")
record OpenAiCompatibleClassifierProperties(
        String chatCompletionsUrl,
        String model,
        String systemPrompt,
        String userPromptTemplate,
        long connectTimeoutMs,
        long requestTimeoutMs
) {

    static final String UNCONFIGURED_MODEL = "UNCONFIGURED";
    private static final long CLAIM_LEASE_MS = 45_000L;
    private static final long DEFAULT_CONNECT_TIMEOUT_MS = 5_000L;
    private static final long DEFAULT_REQUEST_TIMEOUT_MS = 15_000L;

    OpenAiCompatibleClassifierProperties {
        if (chatCompletionsUrl == null || chatCompletionsUrl.isBlank()) {
            chatCompletionsUrl = "";
        }
        if (model == null || model.isBlank()) {
            model = UNCONFIGURED_MODEL;
        }
        if (connectTimeoutMs <= 0) {
            connectTimeoutMs = DEFAULT_CONNECT_TIMEOUT_MS;
        }
        if (requestTimeoutMs <= 0) {
            requestTimeoutMs = DEFAULT_REQUEST_TIMEOUT_MS;
        }
        if (connectTimeoutMs + requestTimeoutMs >= CLAIM_LEASE_MS) {
            throw new IllegalArgumentException(
                    "guardbench.openai-compatible.classifier connect and request timeouts must stay below "
                            + "the 45s execution claim lease");
        }
    }

    ClassifierPrompt prompt() {
        return new ClassifierPrompt(systemPrompt, userPromptTemplate);
    }

    boolean isConfigured() {
        return chatCompletionsUri() != null
                && !UNCONFIGURED_MODEL.equals(model)
                && prompt().isConfigured();
    }

    /** 설정된 URL이 절대 http(s) URL이 아니면 {@code null}을 반환한다. */
    URI chatCompletionsUri() {
        if (chatCompletionsUrl.isBlank()) {
            return null;
        }
        try {
            URI uri = URI.create(chatCompletionsUrl);
            String scheme = uri.getScheme() == null ? "" : uri.getScheme().toLowerCase(Locale.ROOT);
            if (!scheme.equals("http") && !scheme.equals("https") || uri.getHost() == null) {
                return null;
            }
            return uri;
        } catch (IllegalArgumentException exception) {
            return null;
        }
    }
}
