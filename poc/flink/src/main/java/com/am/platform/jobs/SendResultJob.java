package com.am.platform.jobs;

import com.am.platform.model.SendResult;
import com.am.platform.util.ResultCodeClassifier;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.datatype.jsr310.JavaTimeModule;
import com.mongodb.client.MongoClient;
import com.mongodb.client.MongoClients;
import com.mongodb.client.MongoCollection;
import com.mongodb.client.MongoDatabase;
import com.mongodb.client.model.Filters;
import com.mongodb.client.model.Updates;
import org.apache.flink.api.common.eventtime.WatermarkStrategy;
import org.apache.flink.api.common.functions.AggregateFunction;
import org.apache.flink.api.common.serialization.SimpleStringSchema;
import org.apache.flink.connector.jdbc.JdbcConnectionOptions;
import org.apache.flink.connector.jdbc.JdbcExecutionOptions;
import org.apache.flink.connector.jdbc.JdbcSink;
import org.apache.flink.connector.kafka.source.KafkaSource;
import org.apache.flink.connector.kafka.source.enumerator.initializer.OffsetsInitializer;
import org.apache.flink.streaming.api.datastream.DataStream;
import org.apache.flink.streaming.api.datastream.SingleOutputStreamOperator;
import org.apache.flink.streaming.api.environment.StreamExecutionEnvironment;
import org.apache.flink.streaming.api.functions.sink.SinkFunction;
import org.apache.flink.streaming.api.windowing.assigners.TumblingProcessingTimeWindows;
import org.apache.flink.streaming.api.windowing.time.Time;
import org.bson.Document;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.sql.Timestamp;
import java.time.Instant;

/**
 * 발송 결과 처리 Flink Job.
 *
 * 파이프라인:
 *   topic.send.result
 *     → ResultCodeClassifier (disposition 분류: STORE/RETRY/FALLBACK/DLQ)
 *     → 분기 처리:
 *         STORE    → PostgreSQL msg_send_history 업데이트 + MongoDB 업데이트
 *         RETRY    → topic.send.retry 발행 (RetryJob에서 처리)
 *         FALLBACK → topic.send.retry 발행 (channel=SMS 변경 포함)
 *         DLQ      → topic.send.dlq 발행
 *     → 1분 Tumbling Window 집계 → PostgreSQL msg_send_metrics 적재
 *
 * 환경변수:
 *   KAFKA_BOOTSTRAP_SERVERS  (기본: kafka:9092)
 *   POSTGRES_URL             (필수 환경변수, 예: jdbc:postgresql://postgres:5432/am_db)
 *   POSTGRES_USER            (필수 환경변수, 예: am_user)
 *   POSTGRES_PASSWORD        (필수 환경변수)
 *   MONGODB_URI              (필수 환경변수, 예: mongodb://admin:admin_password@mongodb:27017/am_db?authSource=admin)
 */
public class SendResultJob {

    private static final Logger LOG = LoggerFactory.getLogger(SendResultJob.class);

    private static final String BOOTSTRAP_SERVERS =
            System.getenv().getOrDefault("KAFKA_BOOTSTRAP_SERVERS", "kafka:9092");
    private static final String GROUP_ID =
            System.getenv().getOrDefault("KAFKA_GROUP_ID_RESULT", "am-flink-result-group");
    private static final String TOPIC_RESULT  = "topic.send.result";
    private static final String TOPIC_RETRY   = "topic.send.retry";
    private static final String TOPIC_DLQ     = "topic.send.dlq";

    private static final String POSTGRES_URL  =
            System.getenv().getOrDefault("POSTGRES_URL",
                "jdbc:postgresql://postgres:5432/am_db");
    private static final String POSTGRES_USER =
            System.getenv().getOrDefault("POSTGRES_USER", "am_user");
    private static final String POSTGRES_PASS =
            System.getenv().getOrDefault("POSTGRES_PASSWORD", "am_password");
    private static final String MONGODB_URI   =
            System.getenv().getOrDefault("MONGODB_URI",
                "mongodb://admin:admin_password@mongodb:27017/am_db?authSource=admin");

    private static final ObjectMapper MAPPER = new ObjectMapper()
            .registerModule(new JavaTimeModule());

    public static void main(String[] args) throws Exception {
        StreamExecutionEnvironment env = StreamExecutionEnvironment.getExecutionEnvironment();
        env.enableCheckpointing(30_000L);

        // ── Kafka Source ───────────────────────────────────────────────────────
        KafkaSource<String> kafkaSource = KafkaSource.<String>builder()
                .setBootstrapServers(BOOTSTRAP_SERVERS)
                .setTopics(TOPIC_RESULT)
                .setGroupId(GROUP_ID)
                .setStartingOffsets(OffsetsInitializer.latest())
                .setValueOnlyDeserializer(new SimpleStringSchema())
                .build();

        DataStream<String> rawStream = env.fromSource(
                kafkaSource,
                WatermarkStrategy.noWatermarks(),
                "KafkaSource-SendResult");

        // ── JSON 역직렬화 + disposition 분류 ──────────────────────────────────
        DataStream<SendResult> resultStream = rawStream
                .map(json -> {
                    try {
                        SendResult result = MAPPER.readValue(json, SendResult.class);
                        // disposition 분류
                        result.setDisposition(
                                ResultCodeClassifier.classify(result.getResultCode()));
                        return result;
                    } catch (Exception e) {
                        LOG.error("[SendResultJob] JSON 파싱 실패: {}", json, e);
                        return null;
                    }
                })
                .filter(r -> r != null)
                .name("JsonDeserializer-Result");

        // ── STORE: PostgreSQL 이력 업데이트 ───────────────────────────────────
        SingleOutputStreamOperator<SendResult> storeStream = resultStream
                .filter(r -> ResultCodeClassifier.DISPOSITION_STORE.equals(r.getDisposition()))
                .name("Filter-STORE");

        storeStream.addSink(buildPostgresSink()).name("PostgresSink-History");
        storeStream.addSink(buildMongoSink()).name("MongoSink-History");

        // ── RETRY / FALLBACK: retry 토픽 발행 ────────────────────────────────
        resultStream
                .filter(r -> ResultCodeClassifier.DISPOSITION_RETRY.equals(r.getDisposition())
                          || ResultCodeClassifier.DISPOSITION_FALLBACK.equals(r.getDisposition()))
                .map(r -> {
                    // fallback: 채널을 SMS로 변경
                    if (ResultCodeClassifier.DISPOSITION_FALLBACK.equals(r.getDisposition())) {
                        r.setChannel("SMS");
                        LOG.info("[SendResultJob] RCS→SMS fallback 전환: txId={}", r.getTxId());
                    }
                    return MAPPER.writeValueAsString(r);
                })
                .sinkTo(buildKafkaSink(TOPIC_RETRY))
                .name("KafkaSink-Retry");

        // ── DLQ: dlq 토픽 발행 ───────────────────────────────────────────────
        resultStream
                .filter(r -> ResultCodeClassifier.DISPOSITION_DLQ.equals(r.getDisposition()))
                .map(r -> MAPPER.writeValueAsString(r))
                .sinkTo(buildKafkaSink(TOPIC_DLQ))
                .name("KafkaSink-DLQ");

        // ── 1분 Tumbling Window 집계 → PostgreSQL msg_send_metrics ────────────
        resultStream
                .keyBy(SendResult::getChannel)
                .window(TumblingProcessingTimeWindows.of(Time.minutes(1)))
                .aggregate(new ChannelMetricsAggregator())
                .addSink(buildMetricsSink())
                .name("MetricsSink-PostgreSQL");

        LOG.info("[SendResultJob] Job 시작: bootstrapServers={}", BOOTSTRAP_SERVERS);
        env.execute("AM-SendResultJob");
    }

    // ── PostgreSQL 이력 업데이트 Sink ─────────────────────────────────────────
    private static SinkFunction<SendResult> buildPostgresSink() {
        return JdbcSink.sink(
                "UPDATE msg_send_history SET " +
                "  status = ?, result_code = ?, result_message = ?, " +
                "  dispatched_at = ?, delivered_at = ?, updated_at = NOW() " +
                "WHERE tx_id = ?",
                (ps, result) -> {
                    ps.setString(1, "DELIVERED".equals(result.getDisposition()) ? "DELIVERED" : "FAILED");
                    ps.setString(2, result.getResultCode());
                    ps.setString(3, result.getResultMessage());
                    ps.setTimestamp(4, result.getDispatchedAt() != null
                            ? Timestamp.from(Instant.parse(result.getDispatchedAt())) : null);
                    ps.setTimestamp(5, result.getDeliveredAt() != null
                            ? Timestamp.from(Instant.parse(result.getDeliveredAt())) : null);
                    ps.setString(6, result.getTxId());
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

    // ── MongoDB 이력 업데이트 Sink ─────────────────────────────────────────────
    private static SinkFunction<SendResult> buildMongoSink() {
        return new SinkFunction<SendResult>() {
            private transient MongoClient mongoClient;
            private transient MongoDatabase db;

            @Override
            public void invoke(SendResult result, Context context) {
                if (mongoClient == null) {
                    mongoClient = MongoClients.create(MONGODB_URI);
                    db = mongoClient.getDatabase("am_db");
                }
                // 월별 컬렉션명 계산
                String yearMonth = java.time.YearMonth.now().toString().replace("-", "");
                MongoCollection<Document> collection =
                        db.getCollection("send_histories_" + yearMonth);

                // sends 배열 내 해당 txId의 도큐먼트 업데이트
                collection.updateOne(
                        Filters.and(
                                Filters.eq("customerId", result.getCustomerId()),
                                Filters.eq("sends.txId", result.getTxId())),
                        Updates.combine(
                                Updates.set("sends.$.status",
                                        ResultCodeClassifier.isSuccess(result.getResultCode())
                                                ? "DELIVERED" : "FAILED"),
                                Updates.set("sends.$.resultCode", result.getResultCode()),
                                Updates.set("sends.$.deliveredAt", result.getDeliveredAt()),
                                Updates.set("sends.$.dispatchedAt", result.getDispatchedAt()),
                                Updates.currentDate("updatedAt")));

                LOG.debug("[SendResultJob] MongoDB 업데이트 완료: txId={}", result.getTxId());
            }
        };
    }

    // ── 집계 결과 PostgreSQL Sink ─────────────────────────────────────────────
    private static SinkFunction<ChannelMetrics> buildMetricsSink() {
        return JdbcSink.sink(
                "INSERT INTO msg_send_metrics " +
                "(metric_time, channel, total_count, success_count, fail_count, success_rate, created_at) " +
                "VALUES (NOW(), ?, ?, ?, ?, ?, NOW()) " +
                "ON CONFLICT (metric_time, channel) DO UPDATE SET " +
                "  total_count   = EXCLUDED.total_count, " +
                "  success_count = EXCLUDED.success_count, " +
                "  fail_count    = EXCLUDED.fail_count, " +
                "  success_rate  = EXCLUDED.success_rate",
                (ps, m) -> {
                    ps.setString(1, m.channel);
                    ps.setLong(2, m.totalCount);
                    ps.setLong(3, m.successCount);
                    ps.setLong(4, m.failCount);
                    ps.setDouble(5, m.totalCount > 0
                            ? (double) m.successCount / m.totalCount * 100.0 : 0.0);
                },
                JdbcExecutionOptions.builder().withBatchSize(50).build(),
                new JdbcConnectionOptions.JdbcConnectionOptionsBuilder()
                        .withUrl(POSTGRES_URL)
                        .withDriverName("org.postgresql.Driver")
                        .withUsername(POSTGRES_USER)
                        .withPassword(POSTGRES_PASS)
                        .build()
        );
    }

    // ── KafkaSink 헬퍼 ───────────────────────────────────────────────────────
    private static org.apache.flink.connector.kafka.sink.KafkaSink<String> buildKafkaSink(
            String topic) {
        return org.apache.flink.connector.kafka.sink.KafkaSink.<String>builder()
                .setBootstrapServers(BOOTSTRAP_SERVERS)
                .setRecordSerializer(
                        org.apache.flink.connector.kafka.sink.KafkaRecordSerializationSchema
                                .builder()
                                .setTopic(topic)
                                .setValueSerializationSchema(new SimpleStringSchema())
                                .build())
                .build();
    }

    // ── 1분 집계용 Accumulator ────────────────────────────────────────────────
    static class ChannelMetrics {
        String channel;
        long totalCount;
        long successCount;
        long failCount;
    }

    static class ChannelMetricsAggregator
            implements AggregateFunction<SendResult, ChannelMetrics, ChannelMetrics> {

        @Override
        public ChannelMetrics createAccumulator() {
            return new ChannelMetrics();
        }

        @Override
        public ChannelMetrics add(SendResult result, ChannelMetrics acc) {
            acc.channel = result.getChannel();
            acc.totalCount++;
            if (ResultCodeClassifier.isSuccess(result.getResultCode())) {
                acc.successCount++;
            } else {
                acc.failCount++;
            }
            return acc;
        }

        @Override
        public ChannelMetrics getResult(ChannelMetrics acc) {
            return acc;
        }

        @Override
        public ChannelMetrics merge(ChannelMetrics a, ChannelMetrics b) {
            a.channel = b.channel;
            a.totalCount += b.totalCount;
            a.successCount += b.successCount;
            a.failCount += b.failCount;
            return a;
        }
    }
}