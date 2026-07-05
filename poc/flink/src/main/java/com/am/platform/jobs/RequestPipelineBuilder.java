package com.am.platform.jobs;

import com.am.platform.model.SendMessage;
import com.am.platform.operators.ChannelDispatchOperator;
import com.am.platform.operators.RateLimitOperator;
import com.am.platform.operators.ScheduleGateOperator;
import com.am.platform.operators.ValidationOperator;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.datatype.jsr310.JavaTimeModule;
import org.apache.flink.api.common.eventtime.WatermarkStrategy;
import org.apache.flink.api.common.serialization.SimpleStringSchema;
import org.apache.flink.connector.kafka.sink.KafkaRecordSerializationSchema;
import org.apache.flink.connector.kafka.sink.KafkaSink;
import org.apache.flink.connector.kafka.source.KafkaSource;
import org.apache.flink.connector.kafka.source.enumerator.initializer.OffsetsInitializer;
import org.apache.flink.streaming.api.datastream.DataStream;
import org.apache.flink.streaming.api.datastream.SingleOutputStreamOperator;
import org.apache.flink.streaming.api.environment.StreamExecutionEnvironment;

import java.sql.Timestamp;
import java.time.Instant;
import java.time.OffsetDateTime;

import org.apache.flink.connector.jdbc.JdbcConnectionOptions;
import org.apache.flink.connector.jdbc.JdbcExecutionOptions;
import org.apache.flink.connector.jdbc.JdbcSink;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

/**
 * ⭐️ 신규(Day 8): 발송 요청 처리 파이프라인의 공통 로직.
 *
 * 배경: 기존 SendRequestJob.java 하나가 실시간·배치 요청을 구분 없이 topic.send.request
 * 하나로 함께 받아 처리하고 있었다. §16.12 실측에서 배치가 몰리면 배치와 무관한 채널까지
 * 전부 동일하게 지연되는 현상이 확인되어(원인: 요청 인입 단계 공유), 실시간·배치를
 * SendRequestJob_Realtime / SendRequestJob_Batch 2개의 완전히 독립된 Flink Job으로
 * 분리했다(방안 A). 이 클래스는 그 둘이 공유하는 파이프라인 조립 로직만 모아둔 것으로,
 * 코드 중복 없이 "어떤 토픽을 구독하는지"만 다르게 하기 위해 존재한다.
 *
 * 파이프라인 (기존 SendRequestJob과 동일, 구독 토픽만 인자로 받음):
 *   {requestTopic}
 *     → ValidationOperator
 *     → ScheduleGateOperator (sendMethodCode 01/02 예약 발송건 게이트)
 *     → ChannelDispatchOperator
 *     → keyBy(channel) → RateLimitOperator
 *     → topic.send.dispatch.{channel}
 */
final class RequestPipelineBuilder {

    private static final Logger LOG = LoggerFactory.getLogger(RequestPipelineBuilder.class);

    private static final String TOPIC_DISPATCH_SMS   = "topic.send.dispatch.sms";
    private static final String TOPIC_DISPATCH_MMS   = "topic.send.dispatch.mms";
    private static final String TOPIC_DISPATCH_RCS   = "topic.send.dispatch.rcs";
    private static final String TOPIC_DISPATCH_FAX   = "topic.send.dispatch.fax";
    private static final String TOPIC_DISPATCH_EMAIL = "topic.send.dispatch.email";

    // ⭐️ 신규(Day 8 작업3): 채널 개수. RateLimitOperator는 채널별로 keyBy되므로,
    // 이 개수와 병렬도를 맞추면 일꾼(subtask) 하나당 채널 하나가 배정될 확률이
    // 높아진다(완전한 보장은 아니므로 배포 후 Flink UI로 실제 분배를 확인해야 함).
    private static final int CHANNEL_COUNT = 5; // SMS, MMS, RCS, FAX, EMAIL

    private static final String POSTGRES_URL  =
            System.getenv().getOrDefault("POSTGRES_URL",
                "jdbc:postgresql://postgres:5432/am_db");
    private static final String POSTGRES_USER =
            System.getenv().getOrDefault("POSTGRES_USER", "am_user");
    private static final String POSTGRES_PASS =
            System.getenv().getOrDefault("POSTGRES_PASSWORD", "am_password");

    private static final ObjectMapper MAPPER = new ObjectMapper()
            .registerModule(new JavaTimeModule());

    private RequestPipelineBuilder() {}

    /**
     * 실시간/배치 Job 공통 파이프라인을 조립한다. env.execute()는 호출하지 않으며,
     * 각 Job의 main()에서 Job 이름을 지정해 직접 실행해야 한다.
     *
     * @param env              Flink 실행 환경 (Job별로 각자 생성)
     * @param bootstrapServers Kafka bootstrap servers
     * @param groupId          Kafka Consumer Group ID (Job별로 반드시 달라야 함)
     * @param requestTopic     구독할 요청 토픽 (topic.send.request.realtime 또는 .batch)
     * @param jobLabel         로그 구분용 라벨 (예: "Realtime", "Batch")
     */
    static void build(StreamExecutionEnvironment env,
                       String bootstrapServers,
                       String groupId,
                       String requestTopic,
                       String jobLabel) {

        // Checkpoint 설정 (정확히 1회 처리 보장)
        env.enableCheckpointing(30_000L); // 30초 주기

        // ── Kafka Source 설정 ─────────────────────────────────────────────────
        KafkaSource<String> kafkaSource = KafkaSource.<String>builder()
                .setBootstrapServers(bootstrapServers)
                .setTopics(requestTopic)
                .setGroupId(groupId)
                .setStartingOffsets(OffsetsInitializer.latest())
                .setValueOnlyDeserializer(new SimpleStringSchema())
                .build();

        DataStream<String> rawStream = env.fromSource(
                kafkaSource,
                WatermarkStrategy.noWatermarks(),
                "KafkaSource-SendRequest-" + jobLabel);

        // ── JSON 역직렬화 ──────────────────────────────────────────────────────
        DataStream<SendMessage> msgStream = rawStream
                .map(json -> {
                    try {
                        return MAPPER.readValue(json, SendMessage.class);
                    } catch (Exception e) {
                        LOG.error("[SendRequestJob_{}] JSON 파싱 실패: {}", jobLabel, json, e);
                        return null;
                    }
                })
                .filter(msg -> msg != null)
                .name("JsonDeserializer-" + jobLabel);

        // ── ValidationOperator ────────────────────────────────────────────────
        SingleOutputStreamOperator<SendMessage> validatedStream = msgStream
                .filter(new ValidationOperator())
                .name("ValidationOperator-" + jobLabel);

        // ── ScheduleGateOperator (예약 발송 게이트) ──────────────────────────
        // txId는 유일하므로 keyBy(txId)로 파티셔닝해도 키당 최대 1건만 대기하게 되어 안전하다.
        SingleOutputStreamOperator<SendMessage> gatedStream = validatedStream
                .keyBy(SendMessage::getTxId)
                .process(new ScheduleGateOperator())
                .name("ScheduleGateOperator-" + jobLabel);

        // 예약 대기 등록 시점에 즉시 발행되는 side output → SCHEDULED 상태로 DB 최초 1회 INSERT
        DataStream<SendMessage> scheduledLogStream =
                gatedStream.getSideOutput(ScheduleGateOperator.SCHEDULED_LOG_TAG);
        scheduledLogStream
                .addSink(buildScheduledLogPostgresSink())
                .name("PostgresSink-ScheduledLog-" + jobLabel);

        // ── ChannelDispatchOperator ───────────────────────────────────────────
        SingleOutputStreamOperator<SendMessage> dispatchedStream = gatedStream
                .map(new ChannelDispatchOperator())
                .name("ChannelDispatchOperator-" + jobLabel);

        // ── RateLimitOperator (채널별 keyBy 후 TPS 제어) ──────────────────────
        // ⭐️ 변경(Day 8 작업3): 병렬도를 Job 기본값(3)이 아니라 채널 개수(5)로
        // 명시적으로 맞춘다. 실측 결과(Flink UI Subtask Metrics), 일꾼 3명에게
        // 채널 5개를 나눠주다 보니 한 일꾼이 채널 3개를 떠맡는 불균형이 발생해,
        // 그 일꾼이 맡은 채널들만 순서대로 밀려서 늦게 처리되는 문제가 확인됐다.
        // 일꾼 수를 채널 수와 똑같이 맞추면 "채널 하나당 일꾼 하나"로 배정될
        // 확률이 크게 높아진다(완전한 보장은 아니며, 배포 후 Flink UI의
        // Subtask Metrics 화면으로 실제로 고르게 나뉘었는지 확인이 필요하다).
        SingleOutputStreamOperator<SendMessage> rateLimitedStream = dispatchedStream
                .keyBy(SendMessage::getChannel)
                .process(new RateLimitOperator())
                .name("RateLimitOperator-" + jobLabel)
                .setParallelism(CHANNEL_COUNT);

        // ── 채널별 Kafka Sink 분배 ─────────────────────────────────────────────
        // ⭐️ 변경: 위 RateLimitOperator와 병렬도를 맞춰야 Flink가 두 단계를
        // 하나로 묶어서(Chaining) 실행할 수 있어 불필요한 네트워크 재분배를
        // 피할 수 있다.
        rateLimitedStream
                .filter(msg -> "SMS".equals(msg.getChannel()))
                .sinkTo(buildKafkaSink(bootstrapServers, TOPIC_DISPATCH_SMS))
                .name("KafkaSink-SMS-" + jobLabel)
                .setParallelism(CHANNEL_COUNT);

        rateLimitedStream
                .filter(msg -> "MMS".equals(msg.getChannel()))
                .sinkTo(buildKafkaSink(bootstrapServers, TOPIC_DISPATCH_MMS))
                .name("KafkaSink-MMS-" + jobLabel)
                .setParallelism(CHANNEL_COUNT);

        rateLimitedStream
                .filter(msg -> "RCS".equals(msg.getChannel()))
                .sinkTo(buildKafkaSink(bootstrapServers, TOPIC_DISPATCH_RCS))
                .name("KafkaSink-RCS-" + jobLabel)
                .setParallelism(CHANNEL_COUNT);

        rateLimitedStream
                .filter(msg -> "FAX".equals(msg.getChannel()))
                .sinkTo(buildKafkaSink(bootstrapServers, TOPIC_DISPATCH_FAX))
                .name("KafkaSink-FAX-" + jobLabel)
                .setParallelism(CHANNEL_COUNT);

        rateLimitedStream
                .filter(msg -> "EMAIL".equals(msg.getChannel()))
                .sinkTo(buildKafkaSink(bootstrapServers, TOPIC_DISPATCH_EMAIL))
                .name("KafkaSink-EMAIL-" + jobLabel)
                .setParallelism(CHANNEL_COUNT);

        // PostgreSQL 발송 요청 이력 반영
        // - alreadyPersisted=false (실시간/준실시간, 또는 예약시각이 이미 지난 배치건): 최초 INSERT
        // - alreadyPersisted=true  (예약 대기를 거쳐온 건, ScheduleGateOperator가 SCHEDULED로 이미 INSERT함): UPDATE만 수행
        rateLimitedStream
                .filter(msg -> !msg.isAlreadyPersisted())
                .addSink(buildPostgresSink())
                .name("PostgresSink-Request-" + jobLabel)
                .setParallelism(CHANNEL_COUNT);

        rateLimitedStream
                .filter(SendMessage::isAlreadyPersisted)
                .addSink(buildPostgresReleaseUpdateSink())
                .name("PostgresSink-ScheduledRelease-" + jobLabel)
                .setParallelism(CHANNEL_COUNT);

        LOG.info("[SendRequestJob_{}] Job 구성 완료: bootstrapServers={}, groupId={}, requestTopic={}",
                jobLabel, bootstrapServers, groupId, requestTopic);
    }

    /**
     * 발송 요청 이력을 PostgreSQL에 INSERT하는 Sink.
     */
    private static org.apache.flink.streaming.api.functions.sink.SinkFunction<SendMessage>
            buildPostgresSink() {
        return JdbcSink.sink(
                "INSERT INTO msg_send_history " +
                "(tx_id, channel, status, sender, receiver, " +
                " retry_count, source, requested_at, dispatched_at) " +
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, NOW()) " +
                "ON CONFLICT (tx_id) DO NOTHING",
                (ps, msg) -> {
                    ps.setString(1, msg.getTxId());
                    ps.setString(2, msg.getChannel());
                    ps.setString(3, msg.getStatus());
                    ps.setString(4, msg.getSender());
                    ps.setString(5, msg.getReceiver());
                    ps.setInt(6, msg.getRetryCount());
                    ps.setString(7, msg.getSource());
                    ps.setTimestamp(8, msg.getRequestedAt() != null && !msg.getRequestedAt().trim().isEmpty()
                            ? Timestamp.from(OffsetDateTime.parse(msg.getRequestedAt()).toInstant())
                            : Timestamp.from(Instant.ofEpochMilli(System.currentTimeMillis())));
                },
                JdbcExecutionOptions.builder()
                        .withBatchSize(100)
                        .withBatchIntervalMs(200)
                        .withMaxRetries(3)
                        .build(),
                new JdbcConnectionOptions.JdbcConnectionOptionsBuilder()
                        .withUrl(POSTGRES_URL)
                        .withDriverName("org.postgresql.Driver")
                        .withUsername(POSTGRES_USER)
                        .withPassword(POSTGRES_PASS)
                        .build()
        );
    }

    /**
     * 예약 발송 대기 등록 시점에 SCHEDULED 상태로 1회 INSERT하는 Sink.
     */
    private static org.apache.flink.streaming.api.functions.sink.SinkFunction<SendMessage>
            buildScheduledLogPostgresSink() {
        return JdbcSink.sink(
                "INSERT INTO msg_send_history " +
                "(tx_id, channel, status, sender, receiver, " +
                " retry_count, source, scheduled_at, requested_at) " +
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) " +
                "ON CONFLICT (tx_id) DO NOTHING",
                (ps, msg) -> {
                    ps.setString(1, msg.getTxId());
                    ps.setString(2, msg.getChannel());
                    ps.setString(3, msg.getStatus());
                    ps.setString(4, msg.getSender());
                    ps.setString(5, msg.getReceiver());
                    ps.setInt(6, msg.getRetryCount());
                    ps.setString(7, msg.getSource());
                    ps.setTimestamp(8, Timestamp.from(OffsetDateTime.parse(msg.getScheduledAt()).toInstant()));
                    ps.setTimestamp(9, msg.getRequestedAt() != null && !msg.getRequestedAt().trim().isEmpty()
                            ? Timestamp.from(OffsetDateTime.parse(msg.getRequestedAt()).toInstant())
                            : Timestamp.from(Instant.ofEpochMilli(System.currentTimeMillis())));
                },
                JdbcExecutionOptions.builder()
                        .withBatchSize(100)
                        .withBatchIntervalMs(200)
                        .withMaxRetries(3)
                        .build(),
                new JdbcConnectionOptions.JdbcConnectionOptionsBuilder()
                        .withUrl(POSTGRES_URL)
                        .withDriverName("org.postgresql.Driver")
                        .withUsername(POSTGRES_USER)
                        .withPassword(POSTGRES_PASS)
                        .build()
        );
    }

    /**
     * 예약 시각 도래로 파이프라인에 재투입된 건의 상태를 갱신하는 Sink.
     */
    private static org.apache.flink.streaming.api.functions.sink.SinkFunction<SendMessage>
            buildPostgresReleaseUpdateSink() {
        return JdbcSink.sink(
                "UPDATE msg_send_history SET status = ?, dispatched_at = NOW() WHERE tx_id = ?",
                (ps, msg) -> {
                    ps.setString(1, msg.getStatus());
                    ps.setString(2, msg.getTxId());
                },
                JdbcExecutionOptions.builder()
                        .withBatchSize(100)
                        .withBatchIntervalMs(200)
                        .withMaxRetries(3)
                        .build(),
                new JdbcConnectionOptions.JdbcConnectionOptionsBuilder()
                        .withUrl(POSTGRES_URL)
                        .withDriverName("org.postgresql.Driver")
                        .withUsername(POSTGRES_USER)
                        .withPassword(POSTGRES_PASS)
                        .build()
        );
    }

    /**
     * SendMessage를 JSON 직렬화하여 지정 토픽으로 발행하는 KafkaSink 생성.
     */
    private static KafkaSink<SendMessage> buildKafkaSink(String bootstrapServers, String topic) {

        org.apache.flink.api.common.serialization.SerializationSchema<SendMessage> serializer =
                value -> {
                    try {
                        return MAPPER.writeValueAsBytes(value);
                    } catch (Exception e) {
                        LOG.error("[RequestPipelineBuilder] 직렬화 실패: {}", value, e);
                        return new byte[0];
                    }
                };

        return KafkaSink.<SendMessage>builder()
                .setBootstrapServers(bootstrapServers)
                .setRecordSerializer(
                        KafkaRecordSerializationSchema.<SendMessage>builder()
                                .setTopic(topic)
                                .setValueSerializationSchema(serializer)
                                .build())
                .build();
    }
}
