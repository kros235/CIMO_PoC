package com.am.platform.operators;

import com.am.platform.model.SendMessage;
import com.am.platform.util.TxIdParser;
import org.apache.flink.api.common.functions.FilterFunction;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

/**
 * 발송 요청 메시지 검증 Operator.
 *
 * 검증 항목:
 *   1. txId 35자리 형식 및 sendMethodCode(01~05) 유효성
 *   2. channel 값 존재 여부 (SMS/MMS/RCS/FAX/EMAIL)
 *   3. receiver 값 존재 여부
 *   4. messageBody 값 존재 여부
 *   5. scheduledAt / sendMethodCode 정합성 (scheduledAt 존재 시 sendMethodCode는 01/02여야 함)
 *
 * 검증 실패 시: FilterFunction이 false를 반환하여 해당 메시지를 파이프라인에서 제거.
 * 실제 운영 환경에서는 별도의 DLQ 토픽으로 라우팅하도록 SideOutput으로 확장 가능.
 */
public class ValidationOperator implements FilterFunction<SendMessage> {

    private static final Logger LOG = LoggerFactory.getLogger(ValidationOperator.class);

    private static final java.util.Set<String> VALID_CHANNELS = new java.util.HashSet<>(
            java.util.Arrays.asList("SMS", "MMS", "RCS", "FAX", "EMAIL")
    );

    @Override
    public boolean filter(SendMessage msg) {
        if (msg == null) {
            LOG.warn("[ValidationOperator] null 메시지 수신, 제거");
            return false;
        }

        // 1. txId 검증
        if (!TxIdParser.isValid(msg.getTxId())) {
            LOG.warn("[ValidationOperator] txId 유효성 실패: txId={}", msg.getTxId());
            return false;
        }

        // 2. channel 검증
        if (msg.getChannel() == null || !VALID_CHANNELS.contains(msg.getChannel().toUpperCase())) {
            LOG.warn("[ValidationOperator] 유효하지 않은 channel: txId={}, channel={}",
                    msg.getTxId(), msg.getChannel());
            return false;
        }

        // 3. receiver 검증
        if (msg.getReceiver() == null || msg.getReceiver().trim().isEmpty()) {
            LOG.warn("[ValidationOperator] receiver 누락: txId={}", msg.getTxId());
            return false;
        }

        // 4. messageBody 검증
        if (msg.getMessageBody() == null || msg.getMessageBody().trim().isEmpty()) {
            LOG.warn("[ValidationOperator] messageBody 누락: txId={}", msg.getTxId());
            return false;
        }

        // 5. scheduledAt / sendMethodCode 정합성 검증
        //    불변 규칙(README §txId 구조): scheduledAt 값이 있으면 반드시 sendMethodCode가 01(배치) 또는 02(배치)여야 함.
        //    실시간(03)·준실시간(04~05) 건에 scheduledAt이 붙어 있으면 요청 자체가 잘못된 것으로 간주한다.
        boolean hasScheduledAt = msg.getScheduledAt() != null && !msg.getScheduledAt().trim().isEmpty();
        boolean isBatchCode = "01".equals(msg.getSendMethodCode()) || "02".equals(msg.getSendMethodCode());
        if (hasScheduledAt && !isBatchCode) {
            LOG.warn("[ValidationOperator] 불변 규칙 위반 - scheduledAt 존재하지만 sendMethodCode가 01/02가 아님: "
                    + "txId={}, sendMethodCode={}, scheduledAt={}",
                    msg.getTxId(), msg.getSendMethodCode(), msg.getScheduledAt());
            return false;
        }

        LOG.debug("[ValidationOperator] 검증 통과: txId={}, channel={}, sendMethodCode={}",
                msg.getTxId(), msg.getChannel(), msg.getSendMethodCode());
        return true;
    }
}