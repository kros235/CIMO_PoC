package com.am.platform.operators;

import com.am.platform.model.SendMessage;
import org.apache.flink.api.common.state.ValueState;
import org.apache.flink.api.common.state.ValueStateDescriptor;
import org.apache.flink.api.common.typeinfo.TypeInformation;
import org.apache.flink.configuration.Configuration;
import org.apache.flink.streaming.api.functions.KeyedProcessFunction;
import org.apache.flink.util.Collector;
import org.apache.flink.util.OutputTag;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.time.Instant;
import java.time.format.DateTimeParseException;

/**
 * 예약 발송(sendMethodCode 01/02) 게이트 Operator.
 *
 * 반드시 keyBy(SendMessage::getTxId) 로 파티셔닝된 스트림에서 사용해야 한다.
 * (txId는 유일하므로, 키당 대기 메시지는 항상 최대 1건이다.)
 *
 * 동작 방식:
 *   1. sendMethodCode가 03/04/05 이거나, scheduledAt이 없거나, scheduledAt이 이미 지난 시각이면
 *      → 즉시 통과 (기존과 동일하게 실시간 처리)
 *   2. sendMethodCode가 01/02 이고 scheduledAt이 미래 시각이면
 *      → 상태(status)를 SCHEDULED로 바꿔 side output(SCHEDULED_LOG_TAG)으로 1회 즉시 발행
 *        (SendRequestJob에서 이 side output을 받아 DB에 최초 1회 INSERT → VOC 즉시 조회 가능)
 *      → 메시지를 Flink 상태(State)에 보관하고, scheduledAt 시각에 ProcessingTimeTimer 등록
 *      → 타이머가 울리면(onTimer) 메시지에 alreadyPersisted=true 표시 후 메인 출력으로 흘려보냄
 *        (SendRequestJob에서 이 플래그를 보고 INSERT 대신 UPDATE를 사용)
 *
 * 주의: PoC 환경은 이벤트 타임 워터마크를 사용하지 않으므로(SendRequestJob의
 * WatermarkStrategy.noWatermarks() 참고), RateLimitOperator와 동일하게
 * ProcessingTime 기준 타이머를 사용한다. 즉 "예약 시각"은 서버(컨테이너)의
 * 현재 시각(System time) 기준으로 판단된다.
 */
public class ScheduleGateOperator extends KeyedProcessFunction<String, SendMessage, SendMessage> {

    private static final Logger LOG = LoggerFactory.getLogger(ScheduleGateOperator.class);

    /** 예약 대기 중 즉시 이력 기록(SCHEDULED)이 필요한 메시지를 내보내는 side output 태그 */
    public static final OutputTag<SendMessage> SCHEDULED_LOG_TAG =
            new OutputTag<SendMessage>("scheduled-log") {};

    /** 예약 대기 중인 메시지 보관 상태 (키=txId 당 최대 1건) */
    private transient ValueState<SendMessage> pendingState;

    @Override
    public void open(Configuration parameters) {
        pendingState = getRuntimeContext().getState(
                new ValueStateDescriptor<>("schedule-pending-message",
                        TypeInformation.of(SendMessage.class)));
    }

    @Override
    public void processElement(SendMessage msg, Context ctx, Collector<SendMessage> out) throws Exception {
        // 채널 값을 여기서 미리 대문자로 정규화해둔다.
        // (ChannelDispatchOperator도 동일 작업을 하지만, 이 게이트가 그보다 앞단이라
        //  SCHEDULED_LOG_TAG로 즉시 내보내는 이력에도 정규화된 채널값이 들어가야
        //  이후 UPDATE 시점 값과 일관성이 유지된다.)
        if (msg.getChannel() != null) {
            msg.setChannel(msg.getChannel().toUpperCase());
        }

        Long triggerAtMillis = parseFutureScheduledAt(msg, ctx.timerService().currentProcessingTime());

        if (triggerAtMillis == null) {
            // 실시간/준실시간 건이거나, 예약시각이 이미 지난 배치건 → 즉시 통과
            out.collect(msg);
            return;
        }

        // 예약 대기 등록
        msg.setStatus("SCHEDULED");
        pendingState.update(msg);
        ctx.timerService().registerProcessingTimeTimer(triggerAtMillis);
        ctx.output(SCHEDULED_LOG_TAG, msg);

        LOG.info("[ScheduleGateOperator] 예약 대기 등록: txId={}, scheduledAt={}, triggerAtMillis={}",
                msg.getTxId(), msg.getScheduledAt(), triggerAtMillis);
    }

    @Override
    public void onTimer(long timestamp, OnTimerContext ctx, Collector<SendMessage> out) throws Exception {
        SendMessage msg = pendingState.value();
        if (msg == null) {
            // 정상적으로는 발생하지 않지만, 방어적으로 로그만 남기고 종료
            LOG.warn("[ScheduleGateOperator] onTimer 호출되었으나 대기 메시지 없음: timestamp={}", timestamp);
            return;
        }

        msg.setAlreadyPersisted(true); // 이미 SCHEDULED로 1회 INSERT된 건 → 이후엔 UPDATE만
        pendingState.clear();
        out.collect(msg);

        LOG.info("[ScheduleGateOperator] 예약 시각 도래, 처리 재개: txId={}", msg.getTxId());
    }

    /**
     * 예약 대기가 필요한 "미래 시각" 건인지 판단한다.
     *
     * @return 예약 대기가 필요하면 트리거 시각(epoch millis), 아니면 null(즉시 처리)
     */
    private Long parseFutureScheduledAt(SendMessage msg, long nowMillis) {
        boolean isBatchCode = "01".equals(msg.getSendMethodCode()) || "02".equals(msg.getSendMethodCode());
        String scheduledAt = msg.getScheduledAt();

        if (!isBatchCode || scheduledAt == null || scheduledAt.trim().isEmpty()) {
            return null;
        }

        try {
            long triggerAtMillis = Instant.parse(scheduledAt.trim()).toEpochMilli();
            if (triggerAtMillis <= nowMillis) {
                // 이미 지난 예약시각 → 즉시 처리로 폴백
                LOG.debug("[ScheduleGateOperator] 예약시각이 이미 지남, 즉시 처리: txId={}, scheduledAt={}",
                        msg.getTxId(), scheduledAt);
                return null;
            }
            return triggerAtMillis;
        } catch (DateTimeParseException e) {
            // ISO-8601 형식이 아닌 경우: ValidationOperator를 통과했다는 것은 값 자체는 있다는 뜻이므로,
            // 파싱 실패 시 안전하게 즉시 처리로 폴백하고 경고 로그를 남긴다. (요청 자체를 버리지 않음)
            LOG.warn("[ScheduleGateOperator] scheduledAt 파싱 실패, 즉시 처리로 폴백: txId={}, scheduledAt={}",
                    msg.getTxId(), scheduledAt);
            return null;
        }
    }
}
