# poc/services/sms-adapter/main.py
"""
SMS Mock Adapter
- 발송방법코드: 03(실시간), 01/02(배치), 04/05(준실시간) 모두 처리
- 성공률: 95% (환경변수 SMS_SUCCESS_RATE로 오버라이드 가능)
- 기본 응답 지연: 50ms (환경변수 SMS_DELAY_MS로 오버라이드 가능)
- 구독 토픽: topic.send.dispatch.sms
- 결과 발행: topic.send.result
- 포트: 8101
"""

import logging
import os
import sys
import threading

import uvicorn
from fastapi import FastAPI
from prometheus_client import Counter, Histogram, make_asgi_app

# base 모듈 경로 추가 (컨테이너 내 공유 볼륨 마운트 기준)
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from base.adapter_base import AdapterBase

# 로깅 설정
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
)
logger = logging.getLogger("sms-adapter")

# ──────────────────────────────────────────────
# Prometheus 메트릭 정의
# ──────────────────────────────────────────────
sms_send_total = Counter(
    "am_sms_send_total",
    "SMS 발송 처리 총 건수",
    ["status"],  # label: DELIVERED / FAILED / RETRYING
)
sms_send_duration = Histogram(
    "am_sms_send_duration_ms",
    "SMS 발송 처리 지연(ms)",
    buckets=[10, 30, 50, 80, 100, 150, 200, 300, 500, 1000],
)


# ──────────────────────────────────────────────
# SMS Adapter 구현
# ──────────────────────────────────────────────
class SmsAdapter(AdapterBase):
    channel_name   = "SMS"
    dispatch_topic = os.getenv("KAFKA_TOPIC_DISPATCH_SMS", "topic.send.dispatch.sms")
    success_rate   = float(os.getenv("SMS_SUCCESS_RATE", "0.95"))
    base_delay_ms  = int(os.getenv("SMS_DELAY_MS", "50"))
    group_id       = "am-sms-adapter-group"

    def _simulate_send(self, payload: dict) -> tuple[bool, str, str | None]:
        """
        SMS 발송 시뮬레이션.
        - 80byte 초과 시 MMS 전환 권고 메시지 로그 출력 (실제 발송은 정상 처리)
        - 수신번호 형식 간이 검증 (01로 시작하는 10~11자리)
        """
        import random, time

        receiver = payload.get("receiver", "")
        body = payload.get("body", "")

        # 수신번호 형식 검증 (간이)
        cleaned = receiver.replace("-", "").replace(" ", "")
        if not (cleaned.startswith("01") and 10 <= len(cleaned) <= 11):
            logger.warning(
                f"[SMSAdapter] 수신번호 형식 의심 | txId={payload.get('txId')} | receiver={receiver}"
            )

        # SMS 80byte 초과 경고 (발송은 계속)
        if len(body.encode("euc-kr", errors="ignore")) > 80:
            logger.info(
                f"[SMSAdapter] 본문 80byte 초과 — MMS 권고 | txId={payload.get('txId')} | len={len(body)}"
            )

        # 지연 시뮬레이션
        jitter = random.uniform(0.8, 1.2)
        time.sleep((self.base_delay_ms * jitter) / 1000.0)

        rand = random.random()
        if rand < self.success_rate:
            sms_send_total.labels(status="DELIVERED").inc()
            return True, "10000", None
        elif rand < self.success_rate + 0.02:
            sms_send_total.labels(status="RETRYING").inc()
            return False, "50001", "통신사 일시 오류"
        else:
            sms_send_total.labels(status="FAILED").inc()
            return False, "40003", "수신 단말 전원 꺼짐"


# ──────────────────────────────────────────────
# FastAPI 앱 (헬스체크 + Prometheus 메트릭)
# ──────────────────────────────────────────────
app = FastAPI(title="AM SMS Mock Adapter", version="1.0.0")

# Prometheus 메트릭 엔드포인트 마운트
metrics_app = make_asgi_app()
app.mount("/metrics", metrics_app)

_adapter: SmsAdapter = None


@app.on_event("startup")
def startup_event():
    global _adapter
    _adapter = SmsAdapter()
    # Kafka 폴링 루프는 별도 스레드에서 실행
    t = threading.Thread(target=_adapter.start, daemon=True)
    t.start()
    logger.info("[SMSAdapter] FastAPI 기동 완료 — Kafka 폴링 스레드 시작")


@app.on_event("shutdown")
def shutdown_event():
    if _adapter:
        _adapter.stop()


@app.get("/health")
def health():
    """
    헬스체크 엔드포인트.
    Day3 완료 기준: GET /health → 200 OK
    """
    if _adapter is None:
        return {"status": "initializing"}
    return _adapter.get_health()


@app.get("/info")
def info():
    """Adapter 상세 정보"""
    return {
        "adapter": "SMS Mock Adapter",
        "version": "1.0.0",
        "channel": "SMS",
        "dispatchTopic": os.getenv("KAFKA_TOPIC_DISPATCH_SMS", "topic.send.dispatch.sms"),
        "resultTopic": os.getenv("KAFKA_TOPIC_RESULT", "topic.send.result"),
        "successRate": float(os.getenv("SMS_SUCCESS_RATE", "0.95")),
        "baseDelayMs": int(os.getenv("SMS_DELAY_MS", "50")),
        "kafkaBroker": os.getenv("KAFKA_BOOTSTRAP_SERVERS", "kafka:9092"),
    }


if __name__ == "__main__":
    port = int(os.getenv("ADAPTER_PORT", "8101"))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=False)