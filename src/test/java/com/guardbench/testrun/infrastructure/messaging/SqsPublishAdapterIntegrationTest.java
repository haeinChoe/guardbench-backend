package com.guardbench.testrun.infrastructure.messaging;

import static org.assertj.core.api.Assertions.assertThat;

import java.net.URI;
import java.util.List;
import java.util.Map;
import java.util.UUID;

import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Test;
import io.floci.testcontainers.FlociContainer;

import com.guardbench.testrun.application.messaging.TestRunQueue;
import com.guardbench.testrun.application.port.out.PublishBatchResult;
import com.guardbench.testrun.application.port.out.SqsPublishPort.PublishBatchEntry;

import software.amazon.awssdk.auth.credentials.AwsBasicCredentials;
import software.amazon.awssdk.auth.credentials.StaticCredentialsProvider;
import software.amazon.awssdk.regions.Region;
import software.amazon.awssdk.services.sqs.SqsClient;
import software.amazon.awssdk.services.sqs.model.CreateQueueRequest;
import software.amazon.awssdk.services.sqs.model.Message;
import software.amazon.awssdk.services.sqs.model.ReceiveMessageRequest;

/**
 * SqsPublishAdapter의 SendMessageBatch 발행 통합 테스트다.
 *
 * <p>batch 안 한 항목만 실패하는 경우(ADR 0005)는 {@link SqsPublishAdapterTest}가 검증한다.
 * 테스트용 SQS 에뮬레이터(Floci)는 실제 SQS가 항목별로 거부하는 제어 문자 본문을
 * 받아들이므로 이 통합 테스트로는 재현할 수 없다(ADR 0015).
 */
class SqsPublishAdapterIntegrationTest {

    private static final FlociContainer FLOCI = new FlociContainer(
            "floci/floci:2.4.0");

    private static SqsClient sqs;
    private static String queueUrl;

    @BeforeAll
    static void setup() {
        FLOCI.start();
        sqs = SqsClient.builder()
                .endpointOverride(URI.create(FLOCI.getEndpoint().toString()))
                .region(Region.of(FLOCI.getRegion()))
                .credentialsProvider(StaticCredentialsProvider.create(
                        AwsBasicCredentials.create(FLOCI.getAccessKey(), FLOCI.getSecretKey())))
                .build();
        queueUrl = sqs.createQueue(CreateQueueRequest.builder()
                .queueName("sqs-publish-adapter-test").build()).queueUrl();
    }

    @AfterAll
    static void teardown() {
        if (sqs != null) {
            sqs.close();
        }
        FLOCI.stop();
    }

    @BeforeEach
    void drainQueue() {
        while (true) {
            List<Message> batch = sqs.receiveMessage(ReceiveMessageRequest.builder()
                    .queueUrl(queueUrl).maxNumberOfMessages(10).waitTimeSeconds(1).build()).messages();
            if (batch.isEmpty()) {
                return;
            }
            for (Message message : batch) {
                sqs.deleteMessage(r -> r.queueUrl(queueUrl).receiptHandle(message.receiptHandle()));
            }
        }
    }

    @Test
    @DisplayName("정상 batch는 SendMessageBatch로 전송되고 모든 eventId가 성공으로 반환된다")
    void publishesWholeBatchSuccessfully() {
        SqsPublishAdapter adapter = new SqsPublishAdapter(sqs, Map.of(TestRunQueue.RESOLVE, queueUrl));

        PublishBatchEntry first = new PublishBatchEntry(UUID.randomUUID(), samplePayload("1"));
        PublishBatchEntry second = new PublishBatchEntry(UUID.randomUUID(), samplePayload("2"));

        PublishBatchResult result = adapter.publishBatch(TestRunQueue.RESOLVE, List.of(first, second));

        assertThat(result.succeeded(first.eventId())).isTrue();
        assertThat(result.succeeded(second.eventId())).isTrue();

        List<Message> delivered = receiveAll();
        assertThat(delivered).hasSize(2);
    }

    private static List<Message> receiveAll() {
        List<Message> collected = new java.util.ArrayList<>();
        for (int attempt = 0; attempt < 5; attempt++) {
            List<Message> batch = sqs.receiveMessage(ReceiveMessageRequest.builder()
                    .queueUrl(queueUrl).maxNumberOfMessages(10).waitTimeSeconds(1).build()).messages();
            collected.addAll(batch);
            if (batch.isEmpty()) {
                break;
            }
        }
        return collected;
    }

    private static String samplePayload(String testRunId) {
        return "{\"eventId\":\"" + UUID.randomUUID() + "\",\"eventType\":\"TestRunRequested\",\"schemaVersion\":1,\"testRunId\":"
                + testRunId + ",\"occurredAt\":\"2026-08-26T00:00:00Z\"}";
    }
}
