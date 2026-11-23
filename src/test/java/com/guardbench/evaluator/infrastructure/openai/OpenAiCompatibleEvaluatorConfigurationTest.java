package com.guardbench.evaluator.infrastructure.openai;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;
import static org.mockito.Mockito.mock;

import java.util.Map;

import com.guardbench.evaluator.infrastructure.classifier.ClassifierEvaluatorStore;
import com.guardbench.testrun.application.port.out.EvaluatorExecutionPort;
import com.guardbench.testrun.application.port.out.EvaluatorRegistration;

import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Nested;
import org.junit.jupiter.api.Test;
import org.springframework.context.annotation.AnnotationConfigApplicationContext;
import org.springframework.core.env.MapPropertySource;

import tools.jackson.databind.ObjectMapper;
import tools.jackson.databind.json.JsonMapper;

/** {@code guardbench.evaluator.provider}에 따라 OpenAI-compatible classifier 조립 여부를 검증한다. */
class OpenAiCompatibleEvaluatorConfigurationTest {

    @Nested
    @DisplayName("Provider 선택")
    class ProviderSelection {

        @Test
        @DisplayName("provider가 openai-compatible이고 worker가 활성화되면 OpenAI-compatible adapter와 registration을 조립한다")
        void assemblesAdapterAndRegistrationWhenSelected() {
            try (var context = createContext(Map.of(
                    "guardbench.evaluator.provider", "openai-compatible",
                    "guardbench.worker.enabled", "true",
                    "guardbench.openai-compatible.classifier.model", "classifier"))) {
                assertThat(context.getBean(EvaluatorExecutionPort.class))
                        .isInstanceOf(OpenAiCompatibleClassifierEvaluatorAdapter.class);
                assertThat(context.getBean(EvaluatorRegistration.class))
                        .isEqualTo(new EvaluatorRegistration("OPENAI_COMPATIBLE", "classifier"));
            }
        }

        @Test
        @DisplayName("API 모드에서는 registration만 조립하고 EvaluatorExecutionPort는 없다")
        void assemblesOnlyRegistrationInApiMode() {
            try (var context = createContext(Map.of(
                    "guardbench.evaluator.provider", "openai-compatible",
                    "guardbench.worker.enabled", "false"))) {
                assertThat(context.getBeanNamesForType(EvaluatorExecutionPort.class)).isEmpty();
                assertThat(context.getBean(EvaluatorRegistration.class).providerCode())
                        .isEqualTo("OPENAI_COMPATIBLE");
            }
        }

        @Test
        @DisplayName("provider가 설정되지 않으면 OpenAI-compatible 구성 요소를 조립하지 않는다")
        void skipsWhenProviderUnset() {
            try (var context = createContext(Map.of("guardbench.worker.enabled", "true"))) {
                assertThat(context.getBeanNamesForType(EvaluatorExecutionPort.class)).isEmpty();
                assertThat(context.getBeanNamesForType(EvaluatorRegistration.class)).isEmpty();
            }
        }
    }

    @Nested
    @DisplayName("Provider 호출 한도")
    class CallLimit {

        @Test
        @DisplayName("기본 설정은 연결 5초, 응답 대기 15초를 적용한다")
        void appliesDefaultTimeouts() {
            var properties = new OpenAiCompatibleClassifierProperties(null, null, null, null, 0L, 0L);

            assertThat(properties.connectTimeoutMs()).isEqualTo(5_000L);
            assertThat(properties.requestTimeoutMs()).isEqualTo(15_000L);
        }

        @Test
        @DisplayName("연결과 응답 대기 한도의 합이 claim lease 이상이면 기동을 거부한다")
        void rejectsTimeoutsBeyondClaimLease() {
            assertThatThrownBy(() -> new OpenAiCompatibleClassifierProperties(
                    null, null, null, null, 5_000L, 40_000L))
                    .isInstanceOf(IllegalArgumentException.class)
                    .hasMessageContaining("45s execution claim lease");
        }
    }

    private static AnnotationConfigApplicationContext createContext(Map<String, String> properties) {
        AnnotationConfigApplicationContext context = new AnnotationConfigApplicationContext();
        context.getEnvironment().getPropertySources().addFirst(
                new MapPropertySource("test", Map.copyOf(properties)));
        context.registerBean(ClassifierEvaluatorStore.class, () -> mock(ClassifierEvaluatorStore.class));
        context.registerBean(ObjectMapper.class, () -> JsonMapper.builder().build());
        context.register(
                OpenAiCompatibleClassifierRegistrationConfiguration.class,
                OpenAiCompatibleEvaluatorConfiguration.class);
        context.refresh();
        return context;
    }
}
