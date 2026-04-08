package com.am.platform.operators;

import com.am.platform.model.SendMessage;
import com.am.platform.util.TxIdParser;
import org.apache.flink.api.common.functions.MapFunction;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

/**
 * 채널 분배 Operator.
 *
 * 역할:
 *   - channel 값을 대문자로 정규화
 *   - sendMethodCode에 따른 발송 유형 태깅 (REALTIME / BATCH / NEAR_RT)
 *   - 채널 정규화 완료 후 status를 PENDING → DISPATCHING 으로 변경
 *   - RCS fallback은 RetryJob에서 처리하므로 여기서는 channel 그대로 유지
 *
 * 실제 dispatch 토픽 선택은 SendRequestJob에서 채널 값 기반으로 수행한다.
 */
public class ChannelDispatchOperator implements MapFunction<SendMessage, SendMessage> {

    private static final Logger LOG = LoggerFactory.getLogger(ChannelDispatchOperator.class);

    @Override
    public SendMessage map(SendMessage msg) {
        // 채널 정규화 (소문자 입력 허용)
        if (msg.getChannel() != null) {
            msg.setChannel(msg.getChannel().toUpperCase());
        }

        // sendMethodCode 기반 발송 유형 태깅 (source 필드 활용)
        String sendType = TxIdParser.getSendType(msg.getTxId());
        // 기존 source가 없으면 sendType으로 설정 (있으면 유지)
        if (msg.getSource() == null || msg.getSource().trim().isEmpty()) {
            msg.setSource(sendType);
        }

        // 상태 전이: PENDING → DISPATCHING
        msg.setStatus("DISPATCHING");

        LOG.debug("[ChannelDispatchOperator] 채널 분배 완료: txId={}, channel={}, sendType={}",
                msg.getTxId(), msg.getChannel(), sendType);

        return msg;
    }
}