package com.guardbench.evaluator.infrastructure.sagemaker;

import com.guardbench.evaluator.infrastructure.classifier.ClassifierPrompt;

import org.springframework.boot.context.properties.ConfigurationProperties;

/**
 * SageMaker Response Behavior Classifier 호출 설정이다.
 *
 * <p>최종 endpoint name과 classifier system prompt 최종 버전은 별도 실험으로 확정 중이며(#173), 이
 * 설정은 실험 결과값을 배포 시점에 주입할 수 있는 placeholder다. {@code endpointName}이 비어 있거나
 * {@code userPromptTemplate}이 prompt와 actualResponse를 모두 포함하지 않으면 애플리케이션은 정상
 * 기동하지만 classifier를 실제로 호출하는 시점(TestRun 접수의 evaluator reference 등록, Worker의
 * evaluate 호출)에 실패한다. 값이 확정되기 전에 운영 환경에서 classifier를 활성화하지 않는다.
 */
@ConfigurationProperties(prefix = "guardbench.sagemaker.classifier")
record SageMakerClassifierProperties(
        String endpointName,
        String systemPrompt,
        String userPromptTemplate
) {

    static final String UNCONFIGURED_ENDPOINT_NAME = "UNCONFIGURED";

    SageMakerClassifierProperties {
        if (endpointName == null || endpointName.isBlank()) {
            endpointName = UNCONFIGURED_ENDPOINT_NAME;
        }
    }

    ClassifierPrompt prompt() {
        return new ClassifierPrompt(systemPrompt, userPromptTemplate);
    }

    boolean isConfigured() {
        return !UNCONFIGURED_ENDPOINT_NAME.equals(endpointName) && prompt().isConfigured();
    }
}
