package com.guardbench.evaluator.infrastructure.classifier;

import java.util.List;
import java.util.Optional;

import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.stereotype.Repository;

/** TestRun이 고정한 Response Behavior Classifier reference를 조회한다. */
@Repository
public class ClassifierEvaluatorStore {

    private final JdbcTemplate jdbcTemplate;

    public ClassifierEvaluatorStore(JdbcTemplate jdbcTemplate) {
        this.jdbcTemplate = jdbcTemplate;
    }

    public Optional<ClassifierEvaluator> findByReference(String referenceId) {
        List<ClassifierEvaluator> rows = jdbcTemplate.query(
                """
                SELECT reference_id, provider_code, model_id
                FROM evaluator_reference
                WHERE reference_id = ?
                """,
                (resultSet, rowNumber) -> new ClassifierEvaluator(
                        resultSet.getString("reference_id"),
                        resultSet.getString("provider_code"),
                        resultSet.getString("model_id")),
                referenceId);
        return rows.stream().findFirst();
    }

    /**
     * {@code modelId}의 의미는 provider마다 다르다. SageMaker는 endpoint name, OpenAI-compatible
     * provider는 요청 body의 {@code model} 값을 저장한다. 컬럼 이름은 provider-neutral {@code model_id}를
     * 유지한다.
     */
    public record ClassifierEvaluator(
            String referenceId,
            String providerCode,
            String modelId
    ) {
    }
}
