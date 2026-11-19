package com.guardbench.testrun.infrastructure.messaging;

import static org.assertj.core.api.Assertions.assertThat;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.when;

import java.util.List;
import java.util.Map;
import java.util.UUID;

import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Test;

import com.guardbench.testrun.application.messaging.TestRunQueue;
import com.guardbench.testrun.application.port.out.PublishBatchResult;
import com.guardbench.testrun.application.port.out.SqsPublishPort.PublishBatchEntry;

import software.amazon.awssdk.services.sqs.SqsClient;
import software.amazon.awssdk.services.sqs.model.BatchResultErrorEntry;
import software.amazon.awssdk.services.sqs.model.SendMessageBatchRequest;
import software.amazon.awssdk.services.sqs.model.SendMessageBatchResponse;
import software.amazon.awssdk.services.sqs.model.SendMessageBatchResultEntry;

/**
 * SqsPublishAdapter가 SendMessageBatch 응답의 항목별 실패를 결과에 매핑하는지 검증한다.
 *
 * <p>ADR 0005: batch 안 한 항목의 실패가 나머지 항목 처리를 막지 않아야 한다.
 * 실제 SQS는 제어 문자가 든 본문을 {@code InvalidMessageContents}로 항목별 거부하지만
 * 테스트용 SQS 에뮬레이터(Floci)는 이를 거부하지 않는다(ADR 0015). 그래서 부분 실패는
 * SQS 응답을 직접 구성해 검증한다.
 */
class SqsPublishAdapterTest {

    private static final String QUEUE_URL = "http://localhost:4566/000000000000/gb-run-resolve";

    @Test
    @DisplayName("SQS가 batch 안 한 항목만 실패로 반환하면 그 항목만 실패이고 나머지는 성공이다")
    void failedEntryDoesNotBlockOthers() {
        SqsClient sqs = mock(SqsClient.class);
        UUID normal = UUID.randomUUID();
        UUID invalid = UUID.randomUUID();
        when(sqs.sendMessageBatch(any(SendMessageBatchRequest.class))).thenReturn(SendMessageBatchResponse.builder()
                .successful(SendMessageBatchResultEntry.builder()
                        .id(normal.toString()).messageId("m-1").md5OfMessageBody("x").build())
                .failed(BatchResultErrorEntry.builder()
                        .id(invalid.toString()).code("InvalidMessageContents").senderFault(true).build())
                .build());
        SqsPublishAdapter adapter = new SqsPublishAdapter(sqs, Map.of(TestRunQueue.RESOLVE, QUEUE_URL));

        PublishBatchResult result = adapter.publishBatch(TestRunQueue.RESOLVE, List.of(
                new PublishBatchEntry(normal, "{\"n\":1}"),
                new PublishBatchEntry(invalid, "{\"n\":2}\u0000")));

        assertThat(result.succeeded(normal)).isTrue();
        assertThat(result.succeeded(invalid)).isFalse();
    }
}
