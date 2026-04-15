package com.am.platform.jobs;

import com.am.platform.model.SendMessage;
import com.am.platform.operators.ChannelDispatchOperator;
import com.am.platform.operators.RateLimitOperator;
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
 *     → ChannelDispatchOperator (채널 정규화, 상태 DISPATCHING)
 *     → keyBy(channel) → RateLimitOperator (채널별 TPS 제어)
 *     → topic.send.dispatch.{channel} (채널별 분배 발행)
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

        // ── ChannelDispatchOperator ───────────────────────────────────────────
        SingleOutputStreamOperator<SendMessage> dispatchedStream = validatedStream
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

        // PostgreSQL 발송 요청 이력 INSERT
        rateLimitedStream.addSink(buildPostgresSink()).name("PostgresSink-Request");

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
                " retry_count, source, requested_at) " +
                "VALUES (?, ?, ?, ?, ?, ?, ?, NOW()) " +
                "ON CONFLICT (tx_id) DO NOTHING",
                (ps, msg) -> {
                    ps.setString(1, msg.getTxId());
                    ps.setString(2, msg.getChannel());
                    ps.setString(3, msg.getStatus());
                    ps.setString(4, msg.getSender());
                    ps.setString(5, msg.getReceiver());
                    ps.setInt(6, msg.getRetryCount() != null ? msg.getRetryCount() : 0);
                    ps.setString(7, msg.getSource());
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