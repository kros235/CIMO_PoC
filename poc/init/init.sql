-- init.sql
-- AM Platform POC PostgreSQL 초기화 스크립트

-- 1. 발송 이력 메인 테이블 (msg_send_history)
CREATE TABLE IF NOT EXISTS msg_send_history (
    id BIGSERIAL PRIMARY KEY,
    tx_id UUID NOT NULL,
    request_id VARCHAR(100),
    channel VARCHAR(20) NOT NULL,
    sender VARCHAR(50) NOT NULL,
    receiver VARCHAR(50) NOT NULL,
    status VARCHAR(20) NOT NULL DEFAULT 'PENDING',
    result_code VARCHAR(10),
    retry_count SMALLINT DEFAULT 0,
    source VARCHAR(30),
    scheduled_at TIMESTAMPTZ,
    requested_at TIMESTAMPTZ,
    dispatched_at TIMESTAMPTZ,
    delivered_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- VOC 조회 최적화 인덱스 (수신번호 기준)
CREATE INDEX IF NOT EXISTS idx_send_history_receiver ON msg_send_history(receiver, requested_at DESC);
-- 트랜잭션 ID 조회 인덱스
CREATE INDEX IF NOT EXISTS idx_send_history_tx_id ON msg_send_history(tx_id);
-- 상태별 조회 인덱스 (재처리 대상 필터링 용도)
CREATE INDEX IF NOT EXISTS idx_send_history_status ON msg_send_history(status, channel);

-- 2. 집계 지표 테이블 (msg_send_metrics)
CREATE TABLE IF NOT EXISTS msg_send_metrics (
    id BIGSERIAL PRIMARY KEY,
    metric_time TIMESTAMPTZ NOT NULL,
    channel VARCHAR(20) NOT NULL,
    total_count INTEGER DEFAULT 0,
    success_count INTEGER DEFAULT 0,
    fail_count INTEGER DEFAULT 0,
    retry_count INTEGER DEFAULT 0,
    avg_latency_ms INTEGER DEFAULT 0,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- 시계열 집계 인덱스
CREATE INDEX IF NOT EXISTS idx_send_metrics_time ON msg_send_metrics(metric_time DESC, channel);

-- 3. 스케줄 정보 테이블 (msg_batch_schedule)
CREATE TABLE IF NOT EXISTS msg_batch_schedule (
    id BIGSERIAL PRIMARY KEY,
    batch_id VARCHAR(50) NOT NULL,
    channel VARCHAR(20) NOT NULL,
    total_requests INTEGER DEFAULT 0,
    status VARCHAR(20) NOT NULL DEFAULT 'SCHEDULED',
    scheduled_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- 4. DLQ 테이블 (msg_dlq - 최종 실패건 저장)
CREATE TABLE IF NOT EXISTS msg_dlq (
    id BIGSERIAL PRIMARY KEY,
    tx_id UUID NOT NULL,
    channel VARCHAR(20),
    error_reason TEXT,
    payload JSONB,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- 5. 발송처 코드 관리 테이블 (ref_sender_code)
CREATE TABLE IF NOT EXISTS ref_sender_code (
    sender_code VARCHAR(10) PRIMARY KEY,
    sender_name VARCHAR(50) NOT NULL,
    description TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- 기초 데이터 삽입
INSERT INTO ref_sender_code (sender_code, sender_name, description) VALUES
('001', 'CI_CAMPAIGN', 'CI Campaign 발송'),
('002', 'RATER', 'Rater 실시간 발송')
ON CONFLICT (sender_code) DO NOTHING;
