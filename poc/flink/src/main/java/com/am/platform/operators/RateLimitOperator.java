package com.am.platform.operators;

import com.am.platform.model.SendMessage;
import org.apache.flink.api.common.state.ListState;
import org.apache.flink.api.common.state.ListStateDescriptor;
import org.apache.flink.api.common.state.ValueState;
import org.apache.flink.api.common.state.ValueStateDescriptor;
import org.apache.flink.configuration.Configuration;
import org.apache.flink.streaming.api.functions.KeyedProcessFunction;
import org.apache.flink.util.Collector;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.util.ArrayList;
import java.util.List;

/**
 * 채널별 TPS Rate Limiting Operator.
 *
 * 동작 방식:
 *   - KeyedStream(채널별)로 파티셔닝된 스트림에서 동작
 *   - 1초 슬라이딩 윈도우 내 처리 건수를 Flink ValueState로 관리
 *     (Day 8 작업 3: 기존엔 MapState<채널, ...>였으나, 이 Operator가 이미
 *     keyBy(채널) 뒤에서 동작해 State가 자동으로 채널별로 나뉘므로
 *     ValueState/ListState로 단순화함)
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

    // ⭐️ 변경(Day 8 작업 3 - RateLimitOperator 큐 자료구조 개선):
    // 이 Operator는 앞단에서 이미 keyBy(채널)을 거쳐서 들어온다. 즉 Flink가
    // "지금 처리 중인 메시지가 어느 채널 것인지"를 이미 자동으로 구분해서
    // State를 채널별로 따로 관리해준다. 그런데 기존 코드는 State 값 안에서
    // 채널 이름을 또 한 번 Key로 사용하는 MapState<String, ...> 구조를 썼다 —
    // 이미 채널별로 나뉘어 있는 걸 코드에서 한 번 더 수동으로 나눈 셈이라
    // 불필요한 중복이었다. 단순한 ValueState/ListState로 교체한다.

    /** 현재 윈도우 내 처리 건수 (이 State는 현재 채널(Key)에 대해서만 값을 가짐) */
    private transient ValueState<Integer> countState;

    /** 현재 윈도우 시작 시각 (ms, 이 State도 현재 채널(Key)에 대해서만 값을 가짐) */
    private transient ValueState<Long> windowStartState;

    // ⭐️ 변경: 기존엔 MapState<String, List<SendMessage>> 로, 메시지 1건을
    // 대기열에 추가할 때마다 그 채널의 전체 목록을 get()으로 통째로 읽고
    // 항목을 추가한 뒤 put()으로 전체를 다시 쓰는 구조였다. 대기열에 항목이
    // n개 쌓여 있으면 1건을 추가하는 데 n에 비례하는 시간이 걸렸고, 이게
    // 누적되면 대기열 길이의 제곱에 비례하는 비용이 되어 대기열이 길어질수록
    // 점점 느려지는 원인이었다(AM_ARCHITECTURE.md 16번 챕터 7-1번 항목에서
    // 진단 근거 기록). ListState.add()는 항목 하나만 추가하고 전체를 다시
    // 쓰지 않으므로, 대기열이 길어져도 추가 비용이 항상 일정하다.
    /** TPS 초과 시 대기 중인 메시지 큐 (현재 채널(Key)의 대기열만 담음) */
    private transient ListState<SendMessage> pendingQueueState;

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
        countState = getRuntimeContext().getState(
                new ValueStateDescriptor<>("rate-count", Integer.class));

        windowStartState = getRuntimeContext().getState(
                new ValueStateDescriptor<>("rate-window-start", Long.class));

        pendingQueueState = getRuntimeContext().getListState(
                new ListStateDescriptor<>("rate-pending-queue", SendMessage.class));
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
        // ⭐️ 변경: windowStartState.get(channel) → windowStartState.value()
        // (State가 이미 현재 채널(Key)에 대해서만 값을 가지므로 채널을 다시
        // 인자로 넘길 필요가 없다)
        Long windowStart = windowStartState.value();
        if (windowStart == null || now - windowStart >= WINDOW_SIZE_MS) {
            windowStartState.update(now);
            countState.update(0);
        }

        Integer currentCount = countState.value();
        if (currentCount == null) currentCount = 0;

        if (currentCount < limit) {
            // TPS 한도 이내 → 즉시 방출
            countState.update(currentCount + 1);
            out.collect(msg);
        } else {
            // TPS 한도 초과 → 다음 윈도우 시작 시 타이머 등록 후 큐에 적재
            long nextWindow = windowStartState.value() + WINDOW_SIZE_MS;
            ctx.timerService().registerProcessingTimeTimer(nextWindow);

            // ⭐️ 변경: 기존엔 get()으로 전체 목록을 읽고, 항목 추가 후
            // put()으로 전체를 다시 썼다(대기열 길이에 비례하는 비용).
            // ListState.add()는 항목 하나만 추가하므로 대기열 길이와
            // 무관하게 항상 일정한 비용으로 동작한다.
            pendingQueueState.add(msg);

            LOG.debug("[RateLimitOperator] TPS 초과 대기: channel={}, count={}, limit={}, txId={}",
                    channel, currentCount, limit, msg.getTxId());
        }
    }

    @Override
    public void onTimer(long timestamp, OnTimerContext ctx, Collector<SendMessage> out) throws Exception {
        // ⭐️ 변경: 기존 코드는 pendingQueueState.keys()로 "저장된 모든 채널"을
        // 순회했다. 하지만 이 Operator는 keyBy(채널) 뒤에서 동작하므로,
        // onTimer 역시 Flink에 의해 타이머를 등록한 채널(Key) 하나에 대해서만
        // 호출된다. 즉 순회할 필요 없이 현재 채널의 대기열만 처리하면 된다
        // (기존 순회문은 사실상 매번 항목이 1개뿐인 목록을 순회하는 것과
        // 같아서 틀린 동작은 아니었지만, 불필요한 코드였다).
        String channel = ctx.getCurrentKey();
        int limit = channelTpsLimit.getOrDefault(channel, 500);

        int released = 0;
        List<SendMessage> remaining = new ArrayList<>();
        for (SendMessage msg : pendingQueueState.get()) {
            if (released < limit) {
                out.collect(msg);
                released++;
            } else {
                remaining.add(msg);
            }
        }

        // ListState는 항목 단위 추가(add)만 지원하고 중간 삭제는 지원하지
        // 않으므로, 남은 메시지 목록을 갱신할 때는 update()로 통째로
        // 교체한다. 이 교체는 "메시지 1건이 들어올 때마다"가 아니라
        // "채널당 1초에 한 번(타이머가 만료될 때만)" 일어나므로, 기존
        // 코드에도 있던 수준의 비용이며 이번 개선과는 무관하다. 이번에
        // 없앤 비용은 processElement에서 "메시지 1건 추가마다" 전체 목록을
        // 다시 썼던 부분이다.
        pendingQueueState.update(remaining);

        // ⭐️ 버그 수정(TS-0008 TC-0025 예약 정확도 진단 중 발견, 기존 로직 유지):
        // 새 윈도우는 이 타이머 시각(timestamp)부터 시작되고, 그 안에서 이미
        // released건을 내보냈다. 이 사실을 기록하지 않고 count를 0으로만
        // 리셋하면, 이 직후 도착하는 신규 메시지가 "이번 윈도우엔 아직
        // 아무것도 안 나갔다"고 착각해 한도를 무시하고 즉시 통과할 수 있다.
        windowStartState.update(timestamp);
        countState.update(released);

        // 아직 남은 메시지가 있으면 다음 윈도우 타이머 재등록
        if (!remaining.isEmpty()) {
            ctx.timerService().registerProcessingTimeTimer(timestamp + WINDOW_SIZE_MS);
        }

        LOG.debug("[RateLimitOperator] 타이머 방출: channel={}, released={}, remaining={}",
                channel, released, remaining.size());
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