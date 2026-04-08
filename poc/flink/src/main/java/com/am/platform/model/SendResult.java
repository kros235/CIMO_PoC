package com.am.platform.model;

import com.fasterxml.jackson.annotation.JsonIgnoreProperties;
import com.fasterxml.jackson.annotation.JsonProperty;
import java.io.Serializable;

/**
 * Kafka topic.send.result 에서 수신하는 발송 결과 메시지 모델.
 * Mock Adapter가 발행하는 JSON 구조와 1:1 매핑된다.
 *
 * 결과코드 체계:
 *   10000         = 성공 (DELIVERED)
 *   40001~40008   = 영구 실패 → DLQ
 *   50001~50004   = 재처리 가능 → retry 토픽
 *   50002         = RCS fallback → SMS 채널 변경 후 재발송
 */
@JsonIgnoreProperties(ignoreUnknown = true)
public class SendResult implements Serializable {

    private static final long serialVersionUID = 1L;

    /** 35자리 트랜잭션 ID */
    @JsonProperty("txId")
    private String txId;

    /** 발송 채널 */
    @JsonProperty("channel")
    private String channel;

    /**
     * 결과코드
     *   10000        = 성공
     *   40001        = 수신번호 오류
     *   40002        = 타임아웃 (영구)
     *   40003        = 네트워크 오류 (영구)
     *   40004        = MMS 첨부 용량 초과
     *   40005        = FAX 무응답
     *   40006        = FAX 용지 없음
     *   40007        = Email 바운스
     *   40008        = 스팸 차단
     *   50001        = 통신사/SMTP 일시 오류
     *   50002        = RCS 미지원 단말 (SMS fallback 트리거)
     *   50003        = FAX 수신 통화 중
     *   50004        = Email SMTP 일시 오류
     */
    @JsonProperty("resultCode")
    private String resultCode;

    /** 결과 설명 메시지 */
    @JsonProperty("resultMessage")
    private String resultMessage;

    /** 실발송 완료 시각 (ISO-8601) */
    @JsonProperty("dispatchedAt")
    private String dispatchedAt;

    /** 결과 수신 시각 (ISO-8601) */
    @JsonProperty("deliveredAt")
    private String deliveredAt;

    /** 고객 ID */
    @JsonProperty("customerId")
    private String customerId;

    /** 수신번호 */
    @JsonProperty("receiver")
    private String receiver;

    /** 현재 재시도 횟수 */
    @JsonProperty("retryCount")
    private int retryCount;

    /** Disposition 분류 결과 (STORE / RETRY / FALLBACK / DLQ) */
    @JsonProperty("disposition")
    private String disposition;

    public SendResult() {}

    // ── Getters & Setters ──────────────────────────────────────────────────────

    public String getTxId() { return txId; }
    public void setTxId(String txId) { this.txId = txId; }

    public String getChannel() { return channel; }
    public void setChannel(String channel) { this.channel = channel; }

    public String getResultCode() { return resultCode; }
    public void setResultCode(String resultCode) { this.resultCode = resultCode; }

    public String getResultMessage() { return resultMessage; }
    public void setResultMessage(String resultMessage) { this.resultMessage = resultMessage; }

    public String getDispatchedAt() { return dispatchedAt; }
    public void setDispatchedAt(String dispatchedAt) { this.dispatchedAt = dispatchedAt; }

    public String getDeliveredAt() { return deliveredAt; }
    public void setDeliveredAt(String deliveredAt) { this.deliveredAt = deliveredAt; }

    public String getCustomerId() { return customerId; }
    public void setCustomerId(String customerId) { this.customerId = customerId; }

    public String getReceiver() { return receiver; }
    public void setReceiver(String receiver) { this.receiver = receiver; }

    public int getRetryCount() { return retryCount; }
    public void setRetryCount(int retryCount) { this.retryCount = retryCount; }

    public String getDisposition() { return disposition; }
    public void setDisposition(String disposition) { this.disposition = disposition; }

    @Override
    public String toString() {
        return "SendResult{txId='" + txId + "', channel='" + channel
                + "', resultCode='" + resultCode + "', disposition='" + disposition
                + "', retryCount=" + retryCount + "}";
    }
}