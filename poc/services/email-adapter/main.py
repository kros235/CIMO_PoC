# poc/services/email-adapter/main.py
"""
Email Mock Adapter
- 성공률: 98% (환경변수 EMAIL_SUCCESS_RATE로 오버라이드 가능)
- 기본 응답 지연: 30ms (이메일은 비동기 특성상 빠른 수신 확인)
- 구독 토픽: topic.send.dispatch.email
- 결과 발행: topic.send.result
- 포트: 8105
- 특이사항: 이메일 주소 형식 검증, subject 필드 존재 여부 확인
  → 바운스(존재하지 않는 이메일) 시뮬레이션 포함
"""

import logging
import os
import re
import sys
import threading

import uvicorn
from fastapi import FastAPI
from prometheus_client import Counter, Histogram, make_asgi_app

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from base.adapter_base import AdapterBase

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
)
logger = logging.getLogger("email-adapter")

# 이메일 형식 검증 정규식
EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

# ──────────────────────────────────────────────
# Prometheus 메트릭
# ──────────────────────────────────────────────
email_send_total = Counter(
    "am_email_send_total",
    "Email 발송 처리 총 건수",
    ["status", "fail_reason"],
)
email_send_duration = Histogram(
    "am_email_send_duration_ms",
    "Email 발송 처리 지연(ms)",
    buckets=[5, 10, 20, 30, 50, 80, 120, 200, 400],
)

# Email 전용 결과 코드
RESULT_CODE_EMAIL_BOUNCE    = "40007"  # 이메일 바운스 (존재하지 않는 주소)
RESULT_CODE_EMAIL_SPAM      = "40008"  # 스팸 필터 차단
RESULT_CODE_EMAIL_RETRY     = "50004"  # SMTP 서버 일시 오류 (재시도)


# ──────────────────────────────────────────────
# Email Adapter 구현
# ──────────────────────────────────────────────
class EmailAdapter(AdapterBase):
    channel_name   = "EMAIL"
    dispatch_topic = os.getenv("KAFKA_TOPIC_DISPATCH_EMAIL", "topic.send.dispatch.email")
    success_rate   = float(os.getenv("EMAIL_SUCCESS_RATE", "0.98"))
    base_delay_ms  = int(os.getenv("EMAIL_DELAY_MS", "30"))
    group_id       = "am-email-adapter-group"

    def _simulate_send(self, payload: dict) -> tuple[bool, str, str | None]:
        """
        Email 발송 시뮬레이션.
        - 이메일 주소 형식 검증
        - subject 필드 존재 여부 확인 (없으면 경고만 출력, 발송 계속)
        - 바운스/스팸 차단 등 Email 특유의 실패 시뮬레이션
        """
        import random, time

        receiver = payload.get("receiver", "")
        subject  = payload.get("subject", "")

        # 이메일 주소 형식 검증
        if not EMAIL_PATTERN.match(receiver):
            logger.error(
                f"[EmailAdapter] 이메일 주소 형식 오류 | "
                f"txId={payload.get('txId')} | receiver={receiver}"
            )
            email_send_total.labels(status="FAILED", fail_reason="invalid_address").inc()
            return False, "40001", f"이메일 주소 형식 오류: {receiver}"

        # subject 없음 경고
        if not subject:
            logger.warning(
                f"[EmailAdapter] 제목(subject) 없음 | txId={payload.get('txId')}"
            )

        # 지연 시뮬레이션
        jitter = random.uniform(0.8, 1.2)
        time.sleep((self.base_delay_ms * jitter) / 1000.0)

        rand = random.random()
        if rand < self.success_rate:
            email_send_total.labels(status="DELIVERED", fail_reason="none").inc()
            return True, "10000", None
        elif rand < self.success_rate + 0.008:
            # 바운스 (존재하지 않는 이메일 주소) — 영구 실패
            email_send_total.labels(status="FAILED", fail_reason="bounce").inc()
            logger.info(f"[EmailAdapter] 이메일 바운스 | receiver={receiver}")
            return False, RESULT_CODE_EMAIL_BOUNCE, "이메일 주소 없음 (바운스)"
        elif rand < self.success_rate + 0.012:
            # 스팸 필터 — 영구 실패
            email_send_total.labels(status="FAILED", fail_reason="spam").inc()
            return False, RESULT_CODE_EMAIL_SPAM, "수신 측 스팸 필터 차단"
        else:
            # SMTP 일시 오류 — 재처리
            email_send_total.labels(status="RETRYING", fail_reason="smtp_error").inc()
            return False, RESULT_CODE_EMAIL_RETRY, "SMTP 서버 일시 오류"


# ──────────────────────────────────────────────
# FastAPI 앱
# ──────────────────────────────────────────────
app = FastAPI(title="AM Email Mock Adapter", version="1.0.0")
metrics_app = make_asgi_app()
app.mount("/metrics", metrics_app)

_adapter: EmailAdapter = None


@app.on_event("startup")
def startup_event():
    global _adapter
    _adapter = EmailAdapter()
    t = threading.Thread(target=_adapter.start, daemon=True)
    t.start()
    logger.info("[EmailAdapter] FastAPI 기동 완료 — Kafka 폴링 스레드 시작")


@app.on_event("shutdown")
def shutdown_event():
    if _adapter:
        _adapter.stop()


@app.get("/health")
def health():
    if _adapter is None:
        return {"status": "initializing"}
    return _adapter.get_health()


@app.get("/info")
def info():
    return {
        "adapter": "Email Mock Adapter",
        "version": "1.0.0",
        "channel": "EMAIL",
        "dispatchTopic": os.getenv("KAFKA_TOPIC_DISPATCH_EMAIL", "topic.send.dispatch.email"),
        "resultTopic": os.getenv("KAFKA_TOPIC_RESULT", "topic.send.result"),
        "successRate": float(os.getenv("EMAIL_SUCCESS_RATE", "0.98")),
        "baseDelayMs": int(os.getenv("EMAIL_DELAY_MS", "30")),
        "emailSpecificCodes": {
            "40007": "이메일 바운스 (영구 실패)",
            "40008": "스팸 필터 차단 (영구 실패)",
            "50004": "SMTP 서버 일시 오류 (재처리 대상)",
        },
    }


if __name__ == "__main__":
    port = int(os.getenv("ADAPTER_PORT", "8105"))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=False)