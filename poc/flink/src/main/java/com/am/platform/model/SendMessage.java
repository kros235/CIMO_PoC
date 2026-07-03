package com.am.platform.model;

import com.fasterxml.jackson.annotation.JsonIgnoreProperties;
import com.fasterxml.jackson.annotation.JsonProperty;
import java.io.Serializable;

/**
 * Kafka topic.send.request 에서 수신하는 발송 요청 메시지 모델.
 * NiFi가 발행하는 JSON 구조와 1:1 매핑된다.
 *
 * txId 구조 (35자리):
 *   messageId(13) + sendMethodCode(2) + dayOfYear(3) + senderCode(3) + sequence(14)
 */
@JsonIgnoreProperties(ignoreUnknown = true)
public class SendMessage implements Serializable {

    private static final long serialVersionUID = 1L;

    /** 35자리 트랜잭션 ID (상류 발송 시스템이 생성, NiFi가 검증) */
    @JsonProperty("txId")
    private String txId;

    /**
     * 발송방법코드 2자리
     *  01~02: 배치성
     *  03:    온라인(실시간)
     *  04~05: 준실시간
     */
    @JsonProperty("sendMethodCode")
    private String sendMethodCode;

    /** 발송 채널 (SMS / MMS / RCS / FAX / EMAIL) */
    @JsonProperty("channel")
    private String channel;

    /** 발신번호 */
    @JsonProperty("sender")
    private String sender;

    /** 수신번호 (전화번호 또는 이메일) */
    @JsonProperty("receiver")
    private String receiver;

    /** 메시지 본문 */
    @JsonProperty("messageBody")
    private String messageBody;

    /** 고객 ID */
    @JsonProperty("customerId")
    private String customerId;

    /** 발송처 코드 3자리 */
    @JsonProperty("senderCode")
    private String senderCode;

    /** 예약 발송 시각 (ISO-8601, nullable — null이면 즉시 발송) */
    @JsonProperty("scheduledAt")
    private String scheduledAt;

    /** 발송 요청 시각 (ISO-8601) */
    @JsonProperty("requestedAt")
    private String requestedAt;

    /** 발송 소스 시스템 (CI, AB_CAMPAIGN, PARTNER_CRM 등) */
    @JsonProperty("source")
    private String source;

    /** 캠페인 ID (nullable) */
    @JsonProperty("campaignId")
    private String campaignId;

    /** 템플릿 ID (nullable) */
    @JsonProperty("templateId")
    private String templateId;

    /** 현재 처리 상태 (PENDING / SENT / DELIVERED / FAILED / DLQ) */
    @JsonProperty("status")
    private String status;

    /** 재시도 횟수 (최초 요청 시 0) */
    @JsonProperty("retryCount")
    private int retryCount;

    /**
     * Flink 파이프라인 내부 전용 플래그 (Kafka로 발행되지 않음, @JsonIgnore).
     * true = 이 메시지는 예약 대기 중 이미 DB에 1회 INSERT됨(SCHEDULED 상태) →
     *        최종 이력 반영 시 INSERT가 아닌 UPDATE를 사용해야 함.
     * false(기본값) = 아직 DB에 없음 → 최초 INSERT 필요.
     */
    @com.fasterxml.jackson.annotation.JsonIgnore
    private boolean alreadyPersisted = false;

    public SendMessage() {}

    // ── Getters & Setters ──────────────────────────────────────────────────────

    public String getTxId() { return txId; }
    public void setTxId(String txId) { this.txId = txId; }

    public String getSendMethodCode() { return sendMethodCode; }
    public void setSendMethodCode(String sendMethodCode) { this.sendMethodCode = sendMethodCode; }

    public String getChannel() { return channel; }
    public void setChannel(String channel) { this.channel = channel; }

    public String getSender() { return sender; }
    public void setSender(String sender) { this.sender = sender; }

    public String getReceiver() { return receiver; }
    public void setReceiver(String receiver) { this.receiver = receiver; }

    public String getMessageBody() { return messageBody; }
    public void setMessageBody(String messageBody) { this.messageBody = messageBody; }

    public String getCustomerId() { return customerId; }
    public void setCustomerId(String customerId) { this.customerId = customerId; }

    public String getSenderCode() { return senderCode; }
    public void setSenderCode(String senderCode) { this.senderCode = senderCode; }

    public String getScheduledAt() { return scheduledAt; }
    public void setScheduledAt(String scheduledAt) { this.scheduledAt = scheduledAt; }

    public String getRequestedAt() { return requestedAt; }
    public void setRequestedAt(String requestedAt) { this.requestedAt = requestedAt; }

    public String getSource() { return source; }
    public void setSource(String source) { this.source = source; }

    public String getCampaignId() { return campaignId; }
    public void setCampaignId(String campaignId) { this.campaignId = campaignId; }

    public String getTemplateId() { return templateId; }
    public void setTemplateId(String templateId) { this.templateId = templateId; }

    public String getStatus() { return status; }
    public void setStatus(String status) { this.status = status; }

    public int getRetryCount() { return retryCount; }
    public void setRetryCount(int retryCount) { this.retryCount = retryCount; }

    public boolean isAlreadyPersisted() { return alreadyPersisted; }
    public void setAlreadyPersisted(boolean alreadyPersisted) { this.alreadyPersisted = alreadyPersisted; }

    @Override
    public String toString() {
        return "SendMessage{txId='" + txId + "', channel='" + channel
                + "', receiver='" + receiver + "', sendMethodCode='" + sendMethodCode
                + "', retryCount=" + retryCount + "}";
    }
}