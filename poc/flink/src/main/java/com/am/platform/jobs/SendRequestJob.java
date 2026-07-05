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
 * 발송 요청 처리 Flink Job.
 *
 * 파이프라인:
 *   topic.send.request
 *     → ValidationOperator  (txId 검증, 채널 유효성, 필드 존재 여부)
 *     → ScheduleGateOperator (sendMethodCode 01/02 예약 발송건 게이트 — Day 7 Phase 3 신규)
 *     → ChannelDispatchOperator (채널 정규화, 상태 DISPATCHING)
 *     → keyBy(channel) → RateLimitOperator (채널별 TPS 제어)
 *     → topic.send.dispatch.{channel} (채널별 분배 발행)
 *
 * 예약 발송(sendMethodCode 01/02) 처리:
 *   - 미래 시각이 예약된 건은 ScheduleGateOperator가 붙잡아 두고, 즉시 status=SCHEDULED로
 *     DB에 1회 INSERT한다 (VOC 즉시 조회 가능). 예약 시각이 되면 자동으로 파이프라인에
 *     재투입되며, 이후 DB 반영은 INSERT가 아닌 UPDATE로 처리한다 (SendMessage.alreadyPersisted 참고).
 *
 * 환경변수:
 *   KAFKA_BOOTSTRAP_SERVERS  (기본: kafka:9092)
 *   KAFKA_GROUP_ID           (기본: am-flink-request-group)
 *   RATE_LIMIT_SMS/MMS/RCS/FAX/EMAIL (채널별 TPS)
 */
public class SendRequestJob {

    private static final Logger LOG = LoggerFactory.getLogger(SendRequestJob.class);

    private static final String BOOTSTRAP_SERVERS =
            System.getenv().getOrDefault("KAFKA_BOOTSTRAP_SERVERS", "kafka:9092");
    private static final String GROUP_ID =
            System.getenv().getOrDefault("KAFKA_GROUP_ID_REQUEST", "am-flink-request-group");
    private static final String TOPIC_REQUEST = "topic.send.request";

    // 채널별 dispatch 토픽
    private static final String TOPIC_DISPATCH_SMS   = "topic.send.dispatch.sms";
    private static final String TOPIC_DISPATCH_MMS   = "topic.send.dispatch.mms";
    private static final String TOPIC_DISPATCH_RCS   = "topic.send.dispatch.rcs";
    private static final String TOPIC_DISPATCH_FAX   = "topic.send.dispatch.fax";
    private static final String TOPIC_DISPATCH_EMAIL = "topic.send.dispatch.email";

    private static final String POSTGRES_URL  =
            System.getenv().getOrDefault("POSTGRES_URL",
                "jdbc:postgresql://postgres:5432/am_db");
    private static final String POSTGRES_USER =
            System.getenv().getOrDefault("POSTGRES_USER", "am_user");
    private static final String POSTGRES_PASS =
            System.getenv().getOrDefault("POSTGRES_PASSWORD", "am_password");

    private static final ObjectMapper MAPPER = new ObjectMapper()
            .registerModule(new JavaTimeModule());

    public static void main(String[] args) throws Exception {
        StreamExecutionEnvironment env = StreamExecutionEnvironment.getExecutionEnvironment();

        // Checkpoint 설정 (정확히 1회 처리 보장)
        env.enableCheckpointing(30_000L); // 30초 주기

        // ── Kafka Source 설정 ─────────────────────────────────────────────────
        KafkaSource<String> kafkaSource = KafkaSource.<String>builder()
                .setBootstrapServers(BOOTSTRAP_SERVERS)
                .setTopics(TOPIC_REQUEST)
                .setGroupId(GROUP_ID)
                .setStartingOffsets(OffsetsInitializer.latest())
                .setValueOnlyDeserializer(new SimpleStringSchema())
                .build();

        DataStream<String> rawStream = env.fromSource(
                kafkaSource,
                WatermarkStrategy.noWatermarks(),
                "KafkaSource-SendRequest");

        // ── JSON 역직렬화 ──────────────────────────────────────────────────────
        DataStream<SendMessage> msgStream = rawStream
                .map(json -> {
                    try {
                        return MAPPER.readValue(json, SendMessage.class);
                    } catch (Exception e) {
                        LOG.error("[SendRequestJob] JSON 파싱 실패: {}", json, e);
                        return null;
                    }
                })
                .filter(msg -> msg != null)
                .name("JsonDeserializer");

        // ── ValidationOperator ────────────────────────────────────────────────
        SingleOutputStreamOperator<SendMessage> validatedStream = msgStream
                .filter(new ValidationOperator())
                .name("ValidationOperator");

        // ── ScheduleGateOperator (예약 발송 게이트, Day 7 Phase 3 신규) ──────────
        // txId는 유일하므로 keyBy(txId)로 파티셔닝해도 키당 최대 1건만 대기하게 되어 안전하다.
        SingleOutputStreamOperator<SendMessage> gatedStream = validatedStream
                .keyBy(SendMessage::getTxId)
                .process(new ScheduleGateOperator())
                .name("ScheduleGateOperator");

        // 예약 대기 등록 시점에 즉시 발행되는 side output → SCHEDULED 상태로 DB 최초 1회 INSERT
        DataStream<SendMessage> scheduledLogStream =
                gatedStream.getSideOutput(ScheduleGateOperator.SCHEDULED_LOG_TAG);
        scheduledLogStream
                .addSink(buildScheduledLogPostgresSink())
                .name("PostgresSink-ScheduledLog");

        // ── ChannelDispatchOperator ───────────────────────────────────────────
        SingleOutputStreamOperator<SendMessage> dispatchedStream = gatedStream
                .map(new ChannelDispatchOperator())
                .name("ChannelDispatchOperator");

        // ── RateLimitOperator (채널별 keyBy 후 TPS 제어) ──────────────────────
        SingleOutputStreamOperator<SendMessage> rateLimitedStream = dispatchedStream
                .keyBy(SendMessage::getChannel)
                .process(new RateLimitOperator())
                .name("RateLimitOperator");

        // ── 채널별 Kafka Sink 분배 ─────────────────────────────────────────────
        // SMS
        rateLimitedStream
                .filter(msg -> "SMS".equals(msg.getChannel()))
                .sinkTo(buildKafkaSink(TOPIC_DISPATCH_SMS))
                .name("KafkaSink-SMS");

        // MMS
        rateLimitedStream
                .filter(msg -> "MMS".equals(msg.getChannel()))
                .sinkTo(buildKafkaSink(TOPIC_DISPATCH_MMS))
                .name("KafkaSink-MMS");

        // RCS
        rateLimitedStream
                .filter(msg -> "RCS".equals(msg.getChannel()))
                .sinkTo(buildKafkaSink(TOPIC_DISPATCH_RCS))
                .name("KafkaSink-RCS");

        // FAX
        rateLimitedStream
                .filter(msg -> "FAX".equals(msg.getChannel()))
                .sinkTo(buildKafkaSink(TOPIC_DISPATCH_FAX))
                .name("KafkaSink-FAX");

        // EMAIL
        rateLimitedStream
                .filter(msg -> "EMAIL".equals(msg.getChannel()))
                .sinkTo(buildKafkaSink(TOPIC_DISPATCH_EMAIL))
                .name("KafkaSink-EMAIL");

        // PostgreSQL 발송 요청 이력 반영
        // - alreadyPersisted=false (실시간/준실시간, 또는 예약시각이 이미 지난 배치건): 최초 INSERT
        // - alreadyPersisted=true  (예약 대기를 거쳐온 건, ScheduleGateOperator가 SCHEDULED로 이미 INSERT함): UPDATE만 수행
        rateLimitedStream
                .filter(msg -> !msg.isAlreadyPersisted())
                .addSink(buildPostgresSink())
                .name("PostgresSink-Request");

        rateLimitedStream
                .filter(SendMessage::isAlreadyPersisted)
                .addSink(buildPostgresReleaseUpdateSink())
                .name("PostgresSink-ScheduledRelease");

        LOG.info("[SendRequestJob] Job 시작: bootstrapServers={}, groupId={}",
                BOOTSTRAP_SERVERS, GROUP_ID);

        env.execute("AM-SendRequestJob");
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
                    // ⭐️ 수정 (TS-0009 준비 - 채널 공유 시 지연 측정을 위해 필요):
                    // 기존엔 requested_at에 NOW()(=이 INSERT가 실행되는 시각,
                    // 즉 채널 게이트 통과 완료 시각과 항상 동일)를 넣고 있어
                    // "요청→통과 소요시간"을 전혀 계산할 수 없었다.
                    // 클라이언트가 보낸 실제 요청 시각(requestedAt)을 사용하고,
                    // dispatched_at(=NOW(), 이 INSERT 실행 시각)을 함께 기록하여
                    // dispatched_at - requested_at 으로 소요시간을 계산할 수 있게 한다.
                    // (배치/예약 경로의 buildScheduledLogPostgresSink()와 동일한 방식)
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
     * (ScheduleGateOperator의 side output, 즉 예약 대기가 시작되는 시점에만 호출됨)
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
                    ps.setString(3, msg.getStatus()); // ScheduleGateOperator가 "SCHEDULED"로 설정해둠
                    ps.setString(4, msg.getSender());
                    ps.setString(5, msg.getReceiver());
                    ps.setInt(6, msg.getRetryCount());
                    ps.setString(7, msg.getSource());
                    // OffsetDateTime.parse(): UTC(...Z)와 시간대 포함(...+09:00) 형식 모두 지원
                    // (Instant.parse()는 UTC(Z)만 지원해 +09:00 형식에서 예외가 발생하던 버그 수정)
                    ps.setTimestamp(8, Timestamp.from(OffsetDateTime.parse(msg.getScheduledAt()).toInstant()));
                    // requestedAt이 요청 전문에 있으면 그 값을, 없으면 현재 시각(=예약 접수 시각)을 사용
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
     * INSERT가 아닌 UPDATE — 해당 tx_id row는 buildScheduledLogPostgresSink()에서 이미 생성됨.
     */
    private static org.apache.flink.streaming.api.functions.sink.SinkFunction<SendMessage>
            buildPostgresReleaseUpdateSink() {
        return JdbcSink.sink(
                "UPDATE msg_send_history SET status = ?, dispatched_at = NOW() WHERE tx_id = ?",
                (ps, msg) -> {
                    ps.setString(1, msg.getStatus()); // ChannelDispatchOperator가 "DISPATCHING"으로 갱신해둠
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
    private static KafkaSink<SendMessage> buildKafkaSink(String topic) {
        
        org.apache.flink.api.common.serialization.SerializationSchema<SendMessage> serializer =
                value -> {
                    try {
                        return MAPPER.writeValueAsBytes(value);
                    } catch (Exception e) {
                        LOG.error("[SendRequestJob] 직렬화 실패: {}", value, e);
                        return new byte[0];
                    }
                };

        return KafkaSink.<SendMessage>builder()
                .setBootstrapServers(BOOTSTRAP_SERVERS)
                .setRecordSerializer(
                        KafkaRecordSerializationSchema.<SendMessage>builder()
                                .setTopic(topic)
                                .setValueSerializationSchema(serializer)
                                .build())
                .build();
    }
}