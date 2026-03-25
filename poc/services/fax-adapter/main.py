# poc/services/fax-adapter/main.py
"""
FAX Mock Adapter
- 성공률: 85% (환경변수 FAX_SUCCESS_RATE로 오버라이드 가능)
- 기본 응답 지연: 200ms (팩스 특성상 다른 채널 대비 높은 지연)
- 구독 토픽: topic.send.dispatch.fax
- 결과 발행: topic.send.result
- 포트: 8104
- 특이사항: 수신 팩스 번호(수신자 팩스기 상태) 시뮬레이션
  → 통화 중(50003), 전원 꺼짐(40005) 등 FAX 전용 실패 코드 포함
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
logger = logging.getLogger("fax-adapter")

# ──────────────────────────────────────────────
# Prometheus 메트릭
# ──────────────────────────────────────────────
fax_send_total = Counter(
    "am_fax_send_total",
    "FAX 발송 처리 총 건수",
    ["status", "fail_reason"],
)
fax_send_duration = Histogram(
    "am_fax_send_duration_ms",
    "FAX 발송 처리 지연(ms)",
    buckets=[50, 100, 150, 200, 300, 500, 800, 1200, 2000],
)

# FAX 전용 결과 코드
RESULT_CODE_FAX_BUSY      = "50003"  # 수신 팩스 통화 중 (재시도 필요)
RESULT_CODE_FAX_NO_ANSWER = "40005"  # 수신 팩스 응답 없음 (영구 실패)
RESULT_CODE_FAX_PAPER_OUT = "40006"  # 수신 팩스 용지 없음


# ──────────────────────────────────────────────
# FAX Adapter 구현
# ──────────────────────────────────────────────
class FaxAdapter(AdapterBase):
    channel_name   = "FAX"
    dispatch_topic = os.getenv("KAFKA_TOPIC_DISPATCH_FAX", "topic.send.dispatch.fax")
    success_rate   = float(os.getenv("FAX_SUCCESS_RATE", "0.85"))
    base_delay_ms  = int(os.getenv("FAX_DELAY_MS", "200"))
    group_id       = "am-fax-adapter-group"

    def _simulate_send(self, payload: dict) -> tuple[bool, str, str | None]:
        """
        FAX 발송 시뮬레이션.
        팩스 특성상 지연이 크며, 통화 중/용지 없음 등 FAX 특유의 실패 상황을 시뮬레이션한다.
        """
        import random, time

        # FAX는 지연 편차가 큼 (200ms ~ 2000ms)
        jitter = random.uniform(0.5, 3.0)
        delay_s = (self.base_delay_ms * jitter) / 1000.0
        time.sleep(delay_s)

        rand = random.random()
        if rand < self.success_rate:
            fax_send_total.labels(status="DELIVERED", fail_reason="none").inc()
            return True, "10000", None
        elif rand < self.success_rate + 0.06:
            # 통화 중 → 재처리
            fax_send_total.labels(status="RETRYING", fail_reason="busy").inc()
            logger.info(f"[FaxAdapter] 수신 팩스 통화 중 | txId={payload.get('txId')}")
            return False, RESULT_CODE_FAX_BUSY, "수신 팩스 통화 중"
        elif rand < self.success_rate + 0.10:
            # 용지 없음 → 영구 실패
            fax_send_total.labels(status="FAILED", fail_reason="paper_out").inc()
            return False, RESULT_CODE_FAX_PAPER_OUT, "수신 팩스 용지 없음"
        else:
            # 응답 없음 → 영구 실패
            fax_send_total.labels(status="FAILED", fail_reason="no_answer").inc()
            return False, RESULT_CODE_FAX_NO_ANSWER, "수신 팩스 응답 없음"


# ──────────────────────────────────────────────
# FastAPI 앱
# ──────────────────────────────────────────────
app = FastAPI(title="AM FAX Mock Adapter", version="1.0.0")
metrics_app = make_asgi_app()
app.mount("/metrics", metrics_app)

_adapter: FaxAdapter = None


@app.on_event("startup")
def startup_event():
    global _adapter
    _adapter = FaxAdapter()
    t = threading.Thread(target=_adapter.start, daemon=True)
    t.start()
    logger.info("[FaxAdapter] FastAPI 기동 완료 — Kafka 폴링 스레드 시작")


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
        "adapter": "FAX Mock Adapter",
        "version": "1.0.0",
        "channel": "FAX",
        "dispatchTopic": os.getenv("KAFKA_TOPIC_DISPATCH_FAX", "topic.send.dispatch.fax"),
        "resultTopic": os.getenv("KAFKA_TOPIC_RESULT", "topic.send.result"),
        "successRate": float(os.getenv("FAX_SUCCESS_RATE", "0.85")),
        "baseDelayMs": int(os.getenv("FAX_DELAY_MS", "200")),
        "faxSpecificCodes": {
            "50003": "수신 팩스 통화 중 (재처리 대상)",
            "40005": "수신 팩스 응답 없음",
            "40006": "수신 팩스 용지 없음",
        },
    }


if __name__ == "__main__":
    port = int(os.getenv("ADAPTER_PORT", "8104"))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=False)