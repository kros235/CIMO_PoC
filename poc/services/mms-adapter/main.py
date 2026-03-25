# poc/services/mms-adapter/main.py
"""
MMS Mock Adapter
- 성공률: 93% (환경변수 MMS_SUCCESS_RATE로 오버라이드 가능)
- 기본 응답 지연: 80ms (환경변수 MMS_DELAY_MS로 오버라이드 가능)
- 구독 토픽: topic.send.dispatch.mms
- 결과 발행: topic.send.result
- 포트: 8102
- 특이사항: 첨부파일(attachments) 유무 및 크기 검증 로그 출력
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
logger = logging.getLogger("mms-adapter")

# ──────────────────────────────────────────────
# Prometheus 메트릭
# ──────────────────────────────────────────────
mms_send_total = Counter(
    "am_mms_send_total",
    "MMS 발송 처리 총 건수",
    ["status"],
)
mms_send_duration = Histogram(
    "am_mms_send_duration_ms",
    "MMS 발송 처리 지연(ms)",
    buckets=[20, 50, 80, 120, 160, 200, 300, 500, 800, 1500],
)
mms_attachment_size = Histogram(
    "am_mms_attachment_count",
    "MMS 첨부파일 개수",
    buckets=[0, 1, 2, 3, 5, 10],
)


# ──────────────────────────────────────────────
# MMS Adapter 구현
# ──────────────────────────────────────────────
class MmsAdapter(AdapterBase):
    channel_name   = "MMS"
    dispatch_topic = os.getenv("KAFKA_TOPIC_DISPATCH_MMS", "topic.send.dispatch.mms")
    success_rate   = float(os.getenv("MMS_SUCCESS_RATE", "0.93"))
    base_delay_ms  = int(os.getenv("MMS_DELAY_MS", "80"))
    group_id       = "am-mms-adapter-group"

    # MMS 첨부파일 최대 허용 크기 (기본 2MB)
    MAX_ATTACHMENT_MB = float(os.getenv("MMS_MAX_ATTACHMENT_MB", "2.0"))

    def _simulate_send(self, payload: dict) -> tuple[bool, str, str | None]:
        """
        MMS 발송 시뮬레이션.
        - 첨부파일 개수 및 크기 로그 출력
        - 첨부파일 MAX_ATTACHMENT_MB 초과 시 실패 처리
        """
        import random, time

        attachments = payload.get("attachments", [])
        mms_attachment_size.observe(len(attachments))

        # 첨부파일 크기 검증 (size_mb 필드가 있는 경우)
        for att in attachments:
            size_mb = att.get("size_mb", 0)
            if size_mb > self.MAX_ATTACHMENT_MB:
                logger.warning(
                    f"[MMSAdapter] 첨부파일 크기 초과 | txId={payload.get('txId')} "
                    f"| size={size_mb}MB > max={self.MAX_ATTACHMENT_MB}MB"
                )
                mms_send_total.labels(status="FAILED").inc()
                return False, "40004", f"첨부파일 크기 초과 ({size_mb}MB)"

        # 지연 시뮬레이션 (MMS는 SMS보다 느림)
        jitter = random.uniform(0.8, 1.2)
        time.sleep((self.base_delay_ms * jitter) / 1000.0)

        rand = random.random()
        if rand < self.success_rate:
            mms_send_total.labels(status="DELIVERED").inc()
            return True, "10000", None
        elif rand < self.success_rate + 0.02:
            mms_send_total.labels(status="RETRYING").inc()
            return False, "50001", "통신사 MMS 서버 일시 오류"
        else:
            mms_send_total.labels(status="FAILED").inc()
            return False, "40003", "MMS 수신 단말 오류"


# ──────────────────────────────────────────────
# FastAPI 앱
# ──────────────────────────────────────────────
app = FastAPI(title="AM MMS Mock Adapter", version="1.0.0")
metrics_app = make_asgi_app()
app.mount("/metrics", metrics_app)

_adapter: MmsAdapter = None


@app.on_event("startup")
def startup_event():
    global _adapter
    _adapter = MmsAdapter()
    t = threading.Thread(target=_adapter.start, daemon=True)
    t.start()
    logger.info("[MMSAdapter] FastAPI 기동 완료 — Kafka 폴링 스레드 시작")


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
        "adapter": "MMS Mock Adapter",
        "version": "1.0.0",
        "channel": "MMS",
        "dispatchTopic": os.getenv("KAFKA_TOPIC_DISPATCH_MMS", "topic.send.dispatch.mms"),
        "resultTopic": os.getenv("KAFKA_TOPIC_RESULT", "topic.send.result"),
        "successRate": float(os.getenv("MMS_SUCCESS_RATE", "0.93")),
        "baseDelayMs": int(os.getenv("MMS_DELAY_MS", "80")),
        "maxAttachmentMb": float(os.getenv("MMS_MAX_ATTACHMENT_MB", "2.0")),
    }


if __name__ == "__main__":
    port = int(os.getenv("ADAPTER_PORT", "8102"))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=False)