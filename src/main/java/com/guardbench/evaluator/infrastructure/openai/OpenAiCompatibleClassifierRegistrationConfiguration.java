package com.guardbench.evaluator.infrastructure.openai;

import com.guardbench.testrun.application.port.out.EvaluatorRegistration;

import org.springframework.boot.autoconfigure.condition.ConditionalOnProperty;
import org.springframework.boot.context.properties.EnableConfigurationProperties;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;

/**
 * {@code guardbench.evaluator.provider=openai-compatible}일 때 TestRun 접수(API 모드)와 Worker 실행 양쪽에서
 * 사용할 classifier provider/model 식별자를 조립한다.
 */
@Configuration
@ConditionalOnProperty(name = "guardbench.evaluator.provider", havingValue = "openai-compatible")
@EnableConfigurationProperties(OpenAiCompatibleClassifierProperties.class)
class OpenAiCompatibleClassifierRegistrationConfiguration {

    static final String PROVIDER_CODE = "OPENAI_COMPATIBLE";

    @Bean
    EvaluatorRegistration evaluatorRegistration(OpenAiCompatibleClassifierProperties classifierProperties) {
        return new EvaluatorRegistration(PROVIDER_CODE, classifierProperties.model());
    }
}
