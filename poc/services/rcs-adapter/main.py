# poc/services/rcs-adapter/main.py
"""
RCS Mock Adapter
- 성공률: 90% (환경변수 RCS_SUCCESS_RATE로 오버라이드 가능)
- 기본 응답 지연: 60ms
- 구독 토픽: topic.send.dispatch.rcs
- 결과 발행: topic.send.result
- 포트: 8103
- 핵심 특이사항: RCS 실패 시 fallback 코드(50002)를 result에 포함
  → Flink RetryJob이 50002 코드를 감지하여 SMS 채널로 재발송 처리
"""

import logging
import os
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
logger = logging.getLogger("rcs-adapter")

# ──────────────────────────────────────────────
# Prometheus 메트릭
# ──────────────────────────────────────────────
rcs_send_total = Counter(
    "am_rcs_send_total",
    "RCS 발송 처리 총 건수",
    ["status"],
)
rcs_fallback_total = Counter(
    "am_rcs_fallback_total",
    "RCS→SMS fallback 발생 건수",
)
rcs_send_duration = Histogram(
    "am_rcs_send_duration_ms",
    "RCS 발송 처리 지연(ms)",
    buckets=[10, 30, 60, 100, 150, 200, 300, 500, 1000],
)

# RCS fallback 전용 결과 코드
RESULT_CODE_RCS_FALLBACK = "50002"  # RCS 미지원 단말 — SMS fallback 필요


# ──────────────────────────────────────────────
# RCS Adapter 구현
# ──────────────────────────────────────────────
class RcsAdapter(AdapterBase):
    channel_name   = "RCS"
    dispatch_topic = os.getenv("KAFKA_TOPIC_DISPATCH_RCS", "topic.send.dispatch.rcs")
    success_rate   = float(os.getenv("RCS_SUCCESS_RATE", "0.90"))
    base_delay_ms  = int(os.getenv("RCS_DELAY_MS", "60"))
    group_id       = "am-rcs-adapter-group"

    # RCS 미지원 단말 비율 (fallback 발생 확률)
    # 전체 발송 중 일부 단말이 RCS를 미지원하는 상황을 시뮬레이션
    FALLBACK_RATE = float(os.getenv("RCS_FALLBACK_RATE", "0.07"))  # 7%

    def _simulate_send(self, payload: dict) -> tuple[bool, str, str | None]:
        """
        RCS 발송 시뮬레이션.
        - fallback 발생 시: 결과코드 50002 반환
          → Flink RetryJob이 50002를 감지하면 channel=SMS로 재발송 토픽에 발행
        - 일반 실패: 50001 (재처리 대상) or 40003 (영구 실패)
        """
        import random, time

        jitter = random.uniform(0.8, 1.2)
        time.sleep((self.base_delay_ms * jitter) / 1000.0)

        rand = random.random()

        # RCS 미지원 단말 → fallback 처리
        if rand < self.FALLBACK_RATE:
            rcs_fallback_total.inc()
            rcs_send_total.labels(status="RETRYING").inc()
            logger.info(
                f"[RCSAdapter] RCS 미지원 단말 → SMS fallback | txId={payload.get('txId')}"
            )
            return False, RESULT_CODE_RCS_FALLBACK, "RCS 미지원 단말 — SMS fallback"

        # 정상 성공
        if rand < self.FALLBACK_RATE + self.success_rate:
            rcs_send_total.labels(status="DELIVERED").inc()
            return True, "10000", None

        # 일시적 오류 (재처리)
        if rand < self.FALLBACK_RATE + self.success_rate + 0.01:
            rcs_send_total.labels(status="RETRYING").inc()
            return False, "50001", "RCS 서버 일시 오류"

        # 영구 실패
        rcs_send_total.labels(status="FAILED").inc()
        return False, "40003", "RCS 수신 단말 오류"


# ──────────────────────────────────────────────
# FastAPI 앱
# ──────────────────────────────────────────────
app = FastAPI(title="AM RCS Mock Adapter", version="1.0.0")
metrics_app = make_asgi_app()
app.mount("/metrics", metrics_app)

_adapter: RcsAdapter = None


@app.on_event("startup")
def startup_event():
    global _adapter
    _adapter = RcsAdapter()
    t = threading.Thread(target=_adapter.start, daemon=True)
    t.start()
    logger.info("[RCSAdapter] FastAPI 기동 완료 — Kafka 폴링 스레드 시작")


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
        "adapter": "RCS Mock Adapter",
        "version": "1.0.0",
        "channel": "RCS",
        "dispatchTopic": os.getenv("KAFKA_TOPIC_DISPATCH_RCS", "topic.send.dispatch.rcs"),
        "resultTopic": os.getenv("KAFKA_TOPIC_RESULT", "topic.send.result"),
        "successRate": float(os.getenv("RCS_SUCCESS_RATE", "0.90")),
        "baseDelayMs": int(os.getenv("RCS_DELAY_MS", "60")),
        "fallbackRate": float(os.getenv("RCS_FALLBACK_RATE", "0.07")),
        "fallbackResultCode": RESULT_CODE_RCS_FALLBACK,
        "fallbackDescription": "50002 수신 시 Flink RetryJob이 SMS 채널로 재발송 처리",
    }


if __name__ == "__main__":
    port = int(os.getenv("ADAPTER_PORT", "8103"))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=False)