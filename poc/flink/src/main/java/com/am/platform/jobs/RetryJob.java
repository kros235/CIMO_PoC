package com.am.platform.jobs;

import com.am.platform.model.SendMessage;
import com.am.platform.model.SendResult;
import com.am.platform.util.ResultCodeClassifier;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.datatype.jsr310.JavaTimeModule;
import org.apache.flink.api.common.eventtime.WatermarkStrategy;
import org.apache.flink.api.common.serialization.SimpleStringSchema;
import org.apache.flink.api.common.state.ValueState;
import org.apache.flink.api.common.state.ValueStateDescriptor;
import org.apache.flink.api.common.typeinfo.Types;
import org.apache.flink.configuration.Configuration;
import org.apache.flink.connector.jdbc.JdbcConnectionOptions;
import org.apache.flink.connector.jdbc.JdbcExecutionOptions;
import org.apache.flink.connector.jdbc.JdbcSink;
import org.apache.flink.connector.kafka.sink.KafkaRecordSerializationSchema;
import org.apache.flink.connector.kafka.sink.KafkaSink;
import org.apache.flink.connector.kafka.source.KafkaSource;
import org.apache.flink.connector.kafka.source.enumerator.initializer.OffsetsInitializer;
import org.apache.flink.streaming.api.datastream.DataStream;
import org.apache.flink.streaming.api.datastream.SingleOutputStreamOperator;
import org.apache.flink.streaming.api.environment.StreamExecutionEnvironment;
import org.apache.flink.streaming.api.functions.KeyedProcessFunction;
import org.apache.flink.streaming.api.functions.sink.SinkFunction;
import org.apache.flink.util.Collector;
import org.apache.flink.util.OutputTag;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.sql.Timestamp;
import java.util.Properties;

/**
 * 재처리(Retry) Flink Job.
 *
 * 파이프라인:
 *   topic.send.retry
 *     → keyBy(txId)
 *     → RetryProcessFunction (지수 백오프 타이머 + 최대 재시도 횟수 관리)
 *         재시도 횟수 < 3 → 지수 백오프 대기 후 채널 dispatch 토픽으로 재발송
 *         재시도 횟수 >= 3 → topic.send.dlq 발행
 *
 * 지수 백오프 정책:
 *   1회차: 30초 대기
 *   2회차: 60초 대기
 *   3회차: 120초 대기
 *   3회 초과: DLQ 이동
 *
 * 환경변수:
 *   KAFKA_BOOTSTRAP_SERVERS (기본: kafka:9092)
 *   MAX_RETRY_COUNT         (기본: 3)
 */
public class RetryJob {

    private static final Logger LOG = LoggerFactory.getLogger(RetryJob.class);

    private static final String BOOTSTRAP_SERVERS =
            System.getenv().getOrDefault("KAFKA_BOOTSTRAP_SERVERS", "kafka:9092");
    private static final String GROUP_ID =
            System.getenv().getOrDefault("KAFKA_GROUP_ID_RETRY", "am-flink-retry-group");
    private static final int MAX_RETRY_COUNT =
            Integer.parseInt(System.getenv().getOrDefault("MAX_RETRY_COUNT", "3"));

    private static final String TOPIC_RETRY = "topic.send.retry";
    private static final String TOPIC_DLQ   = "topic.send.dlq";

    // ⭐️ 신규(Day 8 작업4): 최대 재시도 초과("완전 포기") 시 데이터베이스에
    // 직접 기록하기 위한 접속 정보. 기존엔 이 정보가 없어서, 재시도를 다
    // 써버린 건은 카프카(topic.send.dlq)로만 발행되고 데이터베이스는
    // 전혀 갱신되지 않았다(발송 상태 고착 문제·재시도 횟수 미기록 문제의
    // 나머지 절반 원인 - SendResultJob 쪽 절반은 이미 조치했음).
    private static final String POSTGRES_URL  =
            System.getenv().getOrDefault("POSTGRES_URL",
                "jdbc:postgresql://postgres:5432/am_db");
    private static final String POSTGRES_USER =
            System.getenv().getOrDefault("POSTGRES_USER", "am_user");
    private static final String POSTGRES_PASS =
            System.getenv().getOrDefault("POSTGRES_PASSWORD", "am_password");

    /** ⭐️ 신규(Day 8 작업4): 최대 재시도 초과로 완전 포기된 건을 데이터베이스
     *  기록 Sink로 흘려보내기 위한 side output 태그. */
    private static final OutputTag<SendResult> DLQ_TERMINAL_TAG =
            new OutputTag<SendResult>("dlq-terminal") {};

    // 채널별 dispatch 토픽 (재발송 시 사용)
    private static final java.util.Map<String, String> DISPATCH_TOPIC_MAP =
            new java.util.HashMap<String, String>() {{
                put("SMS",   "topic.send.dispatch.sms");
                put("MMS",   "topic.send.dispatch.mms");
                put("RCS",   "topic.send.dispatch.rcs");
                put("FAX",   "topic.send.dispatch.fax");
                put("EMAIL", "topic.send.dispatch.email");
            }};

    /** 지수 백오프 대기 시간 (ms): 1회=30s, 2회=60s, 3회=120s */
    private static final long[] BACKOFF_MS = {30_000L, 60_000L, 120_000L};

    private static final ObjectMapper MAPPER = new ObjectMapper()
            .registerModule(new JavaTimeModule());

    public static void main(String[] args) throws Exception {
        StreamExecutionEnvironment env = StreamExecutionEnvironment.getExecutionEnvironment();
        env.enableCheckpointing(30_000L);

        // ── Kafka Source ───────────────────────────────────────────────────────
        KafkaSource<String> kafkaSource = KafkaSource.<String>builder()
                .setBootstrapServers(BOOTSTRAP_SERVERS)
                .setTopics(TOPIC_RETRY)
                .setGroupId(GROUP_ID)
                .setStartingOffsets(OffsetsInitializer.latest())
                .setValueOnlyDeserializer(new SimpleStringSchema())
                .build();

        DataStream<String> rawStream = env.fromSource(
                kafkaSource,
                WatermarkStrategy.noWatermarks(),
                "KafkaSource-Retry");

        // ── JSON 역직렬화 ──────────────────────────────────────────────────────
        // retry 토픽에는 SendResult 형식의 JSON이 들어옴
        DataStream<SendResult> retryStream = rawStream
                .map(json -> {
                    try {
                        return MAPPER.readValue(json, SendResult.class);
                    } catch (Exception e) {
                        LOG.error("[RetryJob] JSON 파싱 실패: {}", json, e);
                        return null;
                    }
                })
                .filter(r -> r != null)
                .name("JsonDeserializer-Retry");

        // ── RetryProcessFunction (txId 기반 keyed 처리) ───────────────────────
        SingleOutputStreamOperator<RetryEvent> retryOutput = retryStream
                .keyBy(SendResult::getTxId)
                .process(new RetryProcessFunction())
                .name("RetryProcessFunction");

        // 재발송 or DLQ 토픽으로 동적 라우팅
        retryOutput
                .sinkTo(buildDynamicKafkaSink())
                .name("KafkaSink-RetryOrDLQ");

        // ⭐️ 신규(Day 8 작업4): 최대 재시도 초과("완전 포기") 건을 데이터베이스에
        // 최종 기록. 기존엔 이 경로가 SendResultJob을 거치지 않고 곧바로
        // 카프카로만 발행되어, 재시도 횟수·최종 상태가 데이터베이스에 전혀
        // 반영되지 않았다(발송 상태 고착 문제·재시도 횟수 미기록 문제의
        // 나머지 절반 원인).
        retryOutput.getSideOutput(DLQ_TERMINAL_TAG)
                .addSink(buildDlqTerminalPostgresSink())
                .name("PostgresSink-DlqTerminal");

        LOG.info("[RetryJob] Job 시작: bootstrapServers={}, maxRetry={}",
                BOOTSTRAP_SERVERS, MAX_RETRY_COUNT);

        env.execute("AM-RetryJob");
    }

    /**
     * 재처리 처리 함수.
     * - 재시도 횟수 상태를 Flink ValueState로 관리
     * - 지수 백오프 타이머 등록 후 타이머 만료 시 재발송 or DLQ 분류
     */
    static class RetryProcessFunction
            extends KeyedProcessFunction<String, SendResult, RetryEvent> {

        private transient ValueState<Integer> retryCountState;
        private transient ValueState<String>  pendingResultState;

        @Override
        public void open(Configuration params) {
            retryCountState = getRuntimeContext().getState(
                    new ValueStateDescriptor<>("retry-count", Types.INT));
            pendingResultState = getRuntimeContext().getState(
                    new ValueStateDescriptor<>("pending-result", Types.STRING));
        }

        @Override
        public void processElement(
                SendResult result,
                Context ctx,
                Collector<RetryEvent> out) throws Exception {

            Integer count = retryCountState.value();
            if (count == null) count = result.getRetryCount();

            if (count >= MAX_RETRY_COUNT) {
                // 최대 재시도 횟수 초과 → DLQ
                LOG.warn("[RetryJob] 최대 재시도 초과 DLQ 이동: txId={}, retryCount={}",
                        result.getTxId(), count);
                out.collect(new RetryEvent(TOPIC_DLQ,
                        MAPPER.writeValueAsString(result)));
                // ⭐️ 신규(Day 8 작업4): 데이터베이스 기록용 side output.
                // result의 retryCount는 아직 이번 시도 횟수(count)로 갱신되기
                // 전이므로, 실제로 시도한 횟수를 정확히 남기기 위해 count로
                // 명시적으로 설정한다.
                result.setRetryCount(count);
                ctx.output(DLQ_TERMINAL_TAG, result);
                retryCountState.clear();
                pendingResultState.clear();
                return;
            }

            // 지수 백오프 타이머 등록
            int attempt = count; // 0-based index for BACKOFF_MS
            long delay = attempt < BACKOFF_MS.length
                    ? BACKOFF_MS[attempt] : BACKOFF_MS[BACKOFF_MS.length - 1];
            long fireAt = ctx.timerService().currentProcessingTime() + delay;

            result.setRetryCount(count + 1);
            retryCountState.update(count + 1);
            pendingResultState.update(MAPPER.writeValueAsString(result));

            ctx.timerService().registerProcessingTimeTimer(fireAt);

            LOG.info("[RetryJob] 재처리 타이머 등록: txId={}, attempt={}, delayMs={}, fireAt={}",
                    result.getTxId(), count + 1, delay, fireAt);
        }

        @Override
        public void onTimer(
                long timestamp,
                OnTimerContext ctx,
                Collector<RetryEvent> out) throws Exception {

            String pendingJson = pendingResultState.value();
            if (pendingJson == null) return;

            SendResult result = MAPPER.readValue(pendingJson, SendResult.class);

            // 재발송용 SendMessage 조립
            SendMessage retryMsg = new SendMessage();
            retryMsg.setTxId(result.getTxId());
            retryMsg.setChannel(result.getChannel());
            retryMsg.setReceiver(result.getReceiver());
            retryMsg.setCustomerId(result.getCustomerId());
            retryMsg.setRetryCount(result.getRetryCount());
            retryMsg.setStatus("RETRYING");

            // 채널에 맞는 dispatch 토픽 선택
            String dispatchTopic = DISPATCH_TOPIC_MAP.getOrDefault(
                    result.getChannel(), "topic.send.dispatch.sms");

            out.collect(new RetryEvent(dispatchTopic,
                    MAPPER.writeValueAsString(retryMsg)));

            LOG.info("[RetryJob] 재발송 처리: txId={}, channel={}, retryCount={}, topic={}",
                    result.getTxId(), result.getChannel(), result.getRetryCount(), dispatchTopic);

            pendingResultState.clear();
        }
    }

    /**
     * 동적 토픽 라우팅을 위한 이벤트 래퍼.
     * targetTopic 에 따라 dispatch.* 또는 dlq 토픽으로 발행된다.
     */
    static class RetryEvent implements java.io.Serializable {
        private static final long serialVersionUID = 1L;
        final String targetTopic;
        final String payload;

        RetryEvent(String targetTopic, String payload) {
            this.targetTopic = targetTopic;
            this.payload = payload;
        }
    }

    /**
     * ⭐️ 신규(Day 8 작업4): 최대 재시도 초과로 완전 포기된 건의 최종 상태를
     * 데이터베이스에 기록하는 Sink. SendResultJob의 buildDlqPostgresSink()와
     * 동일하게 status를 "DLQ"로 남긴다.
     */
    private static SinkFunction<SendResult> buildDlqTerminalPostgresSink() {
        return JdbcSink.sink(
                "UPDATE msg_send_history SET " +
                "  status = 'DLQ', result_code = ?, retry_count = ?, channel = ? " +
                "WHERE tx_id = ?",
                (ps, result) -> {
                    ps.setString(1, result.getResultCode());
                    ps.setInt(2, result.getRetryCount());
                    ps.setString(3, result.getChannel());
                    ps.setString(4, result.getTxId());
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
     * RetryEvent.targetTopic에 따라 동적으로 토픽을 선택하는 KafkaSink.
     */
    private static KafkaSink<RetryEvent> buildDynamicKafkaSink() {

        org.apache.flink.api.common.serialization.SerializationSchema<RetryEvent> serializer =
                event -> {
                    try {
                        return event.payload.getBytes();
                    } catch (Exception e) {
                        return new byte[0];
                    }
                };

        return KafkaSink.<RetryEvent>builder()
                .setBootstrapServers(BOOTSTRAP_SERVERS)
                .setRecordSerializer(
                        KafkaRecordSerializationSchema.<RetryEvent>builder()
                                .setTopicSelector((org.apache.flink.connector.kafka.sink.TopicSelector<RetryEvent>) event -> event.targetTopic)
                                .setValueSerializationSchema(serializer)
                                .build())
                .build();
    }
}