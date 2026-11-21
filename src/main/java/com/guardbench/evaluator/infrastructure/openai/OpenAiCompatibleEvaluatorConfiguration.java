package com.guardbench.evaluator.infrastructure.openai;

import java.net.http.HttpClient;
import java.time.Duration;

import com.guardbench.evaluator.infrastructure.classifier.ClassifierEvaluatorStore;
import com.guardbench.testrun.application.port.out.EvaluatorExecutionPort;

import org.springframework.boot.autoconfigure.condition.ConditionalOnProperty;
import org.springframework.boot.context.properties.EnableConfigurationProperties;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;

import tools.jackson.databind.ObjectMapper;

/**
 * Worker 모드에서 {@code guardbench.evaluator.provider=openai-compatible}이면 OpenAI-compatible
 * Response Behavior Classifier adapter를 조립한다.
 *
 * <p>HTTP client는 이 adapter 전용으로 만들고 bean으로 노출하지 않는다. Application Target용
 * {@code HttpClient} bean과 주입 대상이 섞이지 않게 하기 위해서다.
 */
@Configuration
@ConditionalOnProperty(name = "guardbench.worker.enabled", havingValue = "true")
@ConditionalOnProperty(name = "guardbench.evaluator.provider", havingValue = "openai-compatible")
@EnableConfigurationProperties(OpenAiCompatibleClassifierProperties.class)
class OpenAiCompatibleEvaluatorConfiguration {

    @Bean
    EvaluatorExecutionPort evaluatorExecutionPort(
            ClassifierEvaluatorStore evaluatorStore,
            OpenAiCompatibleClassifierProperties classifierProperties,
            ObjectMapper objectMapper
    ) {
        HttpClient httpClient = HttpClient.newBuilder()
                .connectTimeout(Duration.ofMillis(classifierProperties.connectTimeoutMs()))
                .followRedirects(HttpClient.Redirect.NEVER)
                .build();
        return new OpenAiCompatibleClassifierEvaluatorAdapter(
                httpClient, evaluatorStore, classifierProperties, objectMapper);
    }
}
