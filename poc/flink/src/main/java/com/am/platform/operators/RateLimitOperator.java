package com.am.platform.operators;

import com.am.platform.model.SendMessage;
import org.apache.flink.api.common.state.MapState;
import org.apache.flink.api.common.state.MapStateDescriptor;
import org.apache.flink.api.common.typeinfo.Types;
import org.apache.flink.configuration.Configuration;
import org.apache.flink.streaming.api.functions.KeyedProcessFunction;
import org.apache.flink.util.Collector;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

/**
 * 채널별 TPS Rate Limiting Operator.
 *
 * 동작 방식:
 *   - KeyedStream(채널별)로 파티셔닝된 스트림에서 동작
 *   - 1초 슬라이딩 윈도우 내 처리 건수를 Flink MapState로 관리
 *   - 채널별 TPS 한도 초과 시 ProcessingTime 타이머로 지연 처리
 *   - 타이머 만료 시 누적된 메시지를 순서대로 방출
 *
 * 채널별 기본 TPS 한도 (환경변수 오버라이드 가능):
 *   SMS   : 500 TPS
 *   MMS   : 200 TPS
 *   RCS   : 300 TPS
 *   FAX   : 100 TPS
 *   EMAIL : 400 TPS
 */
public class RateLimitOperator extends KeyedProcessFunction<String, SendMessage, SendMessage> {

    private static final Logger LOG = LoggerFactory.getLogger(RateLimitOperator.class);

    /** 1초(ms) 슬라이딩 윈도우 크기 */
    private static final long WINDOW_SIZE_MS = 1000L;

    /** 채널별 TPS 한도 */
    private final java.util.Map<String, Integer> channelTpsLimit;

    /** 현재 윈도우 내 채널별 처리 건수 상태 */
    private transient MapState<String, Integer> countState;

    /** 현재 윈도우 시작 시각 (ms) */
    private transient MapState<String, Long> windowStartState;

    /** TPS 초과 시 대기 중인 메시지 큐 */
    private transient MapState<String, java.util.List<SendMessage>> pendingQueueState;

    public RateLimitOperator() {
        this.channelTpsLimit = new java.util.HashMap<>();
        channelTpsLimit.put("SMS",   getEnvInt("RATE_LIMIT_SMS",   500));
        channelTpsLimit.put("MMS",   getEnvInt("RATE_LIMIT_MMS",   200));
        channelTpsLimit.put("RCS",   getEnvInt("RATE_LIMIT_RCS",   300));
        channelTpsLimit.put("FAX",   getEnvInt("RATE_LIMIT_FAX",   100));
        channelTpsLimit.put("EMAIL", getEnvInt("RATE_LIMIT_EMAIL", 400));
    }

    @Override
    public void open(Configuration parameters) {
        countState = getRuntimeContext().getMapState(
                new MapStateDescriptor<>("rate-count", Types.STRING, Types.INT));

        windowStartState = getRuntimeContext().getMapState(
                new MapStateDescriptor<>("rate-window-start", Types.STRING, Types.LONG));

        pendingQueueState = getRuntimeContext().getMapState(
                new MapStateDescriptor<>(
                        "rate-pending-queue",
                        Types.STRING,
                        Types.LIST(Types.GENERIC(SendMessage.class))));
    }

    @Override
    public void processElement(
            SendMessage msg,
            Context ctx,
            Collector<SendMessage> out) throws Exception {

        String channel = msg.getChannel();
        long now = ctx.timerService().currentProcessingTime();
        int limit = channelTpsLimit.getOrDefault(channel, 500);

        // 윈도우 초기화 (첫 메시지 또는 윈도우 만료)
        Long windowStart = windowStartState.get(channel);
        if (windowStart == null || now - windowStart >= WINDOW_SIZE_MS) {
            windowStartState.put(channel, now);
            countState.put(channel, 0);
        }

        Integer currentCount = countState.get(channel);
        if (currentCount == null) currentCount = 0;

        if (currentCount < limit) {
            // TPS 한도 이내 → 즉시 방출
            countState.put(channel, currentCount + 1);
            out.collect(msg);
        } else {
            // TPS 한도 초과 → 다음 윈도우 시작 시 타이머 등록 후 큐에 적재
            long nextWindow = windowStartState.get(channel) + WINDOW_SIZE_MS;
            ctx.timerService().registerProcessingTimeTimer(nextWindow);

            java.util.List<SendMessage> queue = pendingQueueState.get(channel);
            if (queue == null) queue = new java.util.ArrayList<>();
            queue.add(msg);
            pendingQueueState.put(channel, queue);

            LOG.debug("[RateLimitOperator] TPS 초과 대기: channel={}, count={}, limit={}, txId={}",
                    channel, currentCount, limit, msg.getTxId());
        }
    }

    @Override
    public void onTimer(long timestamp, OnTimerContext ctx, Collector<SendMessage> out) throws Exception {
        // 타이머 만료 시 대기 큐에서 이번 윈도우 한도만큼 방출
        for (String channel : pendingQueueState.keys()) {
            java.util.List<SendMessage> queue = pendingQueueState.get(channel);
            if (queue == null || queue.isEmpty()) continue;

            int limit = channelTpsLimit.getOrDefault(channel, 500);
            int released = 0;

            java.util.List<SendMessage> remaining = new java.util.ArrayList<>();
            for (SendMessage msg : queue) {
                if (released < limit) {
                    out.collect(msg);
                    released++;
                } else {
                    remaining.add(msg);
                }
            }
            pendingQueueState.put(channel, remaining);

            // 아직 남은 메시지가 있으면 다음 윈도우 타이머 재등록
            if (!remaining.isEmpty()) {
                ctx.timerService().registerProcessingTimeTimer(timestamp + WINDOW_SIZE_MS);
            }

            LOG.debug("[RateLimitOperator] 타이머 방출: channel={}, released={}, remaining={}",
                    channel, released, remaining.size());
        }

        // 현재 윈도우 카운트 리셋
        for (String channel : countState.keys()) {
            countState.put(channel, 0);
        }
        // 윈도우 시작 시각 갱신
        for (String channel : windowStartState.keys()) {
            windowStartState.put(channel, timestamp);
        }
    }

    /** 환경변수에서 정수값 읽기 (없으면 기본값 사용) */
    private static int getEnvInt(String key, int defaultValue) {
        String val = System.getenv(key);
        if (val == null || val.trim().isEmpty()) return defaultValue;
        try {
            return Integer.parseInt(val.trim());
        } catch (NumberFormatException e) {
            return defaultValue;
        }
    }
}