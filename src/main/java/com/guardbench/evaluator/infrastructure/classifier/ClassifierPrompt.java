package com.guardbench.evaluator.infrastructure.classifier;

/**
 * Response Behavior Classifier의 system prompt와 user message 템플릿이다. Provider adapter와 무관하게
 * 같은 prompt 계약(ADR 0013)을 사용한다.
 *
 * <p>{@code systemPrompt}가 비어 있거나 {@code userPromptTemplate}이 prompt와 actualResponse를 모두
 * 렌더링하지 않으면 미설정으로 취급한다. 애플리케이션은 정상 기동하지만 classifier 호출 시점에 실패한다.
 */
public record ClassifierPrompt(String systemPrompt, String userPromptTemplate) {

    public static final String DEFAULT_USER_PROMPT_TEMPLATE = """
            USER REQUEST:
            %s

            ASSISTANT RESPONSE:
            %s""";

    public ClassifierPrompt {
        if (systemPrompt == null || systemPrompt.isBlank()) {
            systemPrompt = "";
        }
        if (userPromptTemplate == null || userPromptTemplate.isBlank()) {
            userPromptTemplate = DEFAULT_USER_PROMPT_TEMPLATE;
        }
    }

    public boolean isConfigured() {
        return !systemPrompt.isBlank() && hasValidUserPromptTemplate();
    }

    /**
     * {@code userPromptTemplate}이 실제로 prompt와 actualResponse를 모두 렌더링에 포함하는지 검증한다.
     * {@code %s} 개수가 하나뿐이거나 같은 인덱스만 반복하는 잘못된 템플릿은 (prompt, actualResponse)
     * 계약을 위반하므로 미설정으로 취급한다.
     */
    private boolean hasValidUserPromptTemplate() {
        String promptSentinel = "__GUARDBENCH_PROMPT_SENTINEL__";
        String responseSentinel = "__GUARDBENCH_RESPONSE_SENTINEL__";
        try {
            String rendered = userPromptTemplate.formatted(promptSentinel, responseSentinel);
            return rendered.contains(promptSentinel) && rendered.contains(responseSentinel);
        } catch (RuntimeException exception) {
            return false;
        }
    }

    public String renderUserMessage(String prompt, String applicationResponse) {
        return userPromptTemplate.formatted(prompt, applicationResponse);
    }
}
