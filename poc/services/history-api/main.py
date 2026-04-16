"""
AM Platform — VOC / History API
포트: 8200
역할: txId 또는 수신번호 기준으로 전 구간 발송 이력 조회.
      E2E 파이프라인 구간별 상태와 에러 로그를 함께 반환.
"""
import os
import time
import asyncio
from datetime import datetime, timezone
from typing import Optional, List

import asyncpg
from fastapi import FastAPI, HTTPException, Query
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse
from prometheus_client import Counter, Histogram, generate_latest, CONTENT_TYPE_LATEST
from starlette.responses import Response
from dotenv import load_dotenv

load_dotenv()

# ── 환경변수 (절대경로 금지, 모두 환경변수 기반) ─────────────
PG_HOST     = os.getenv("POSTGRES_HOST", "postgres")
PG_PORT     = int(os.getenv("POSTGRES_PORT", "5432"))
PG_DB       = os.getenv("POSTGRES_DB", "am_db")
PG_USER     = os.getenv("POSTGRES_USER", "am_user")
PG_PASSWORD = os.getenv("POSTGRES_PASSWORD", "am_password")

app = FastAPI(
    title="AM Platform — History API",
    description="txId / 수신번호 기준 발송 이력 조회 및 E2E 구간 추적",
    version="1.0.0",
)

# ── Prometheus 메트릭 ────────────────────────────────────────
_REQUEST_COUNT = Counter(
    "history_api_requests_total",
    "VOC API 요청 수",
    ["endpoint"],
)
_REQUEST_LATENCY = Histogram(
    "history_api_latency_ms",
    "VOC API 응답 지연 (ms)",
    ["endpoint"],
    buckets=[50, 100, 300, 500, 1000, 3000],
)

# ── DB 커넥션 풀 ─────────────────────────────────────────────
_pool: Optional[asyncpg.Pool] = None

async def get_pool() -> asyncpg.Pool:
    global _pool
    if _pool is None:
        _pool = await asyncpg.create_pool(
            host=PG_HOST, port=PG_PORT,
            database=PG_DB, user=PG_USER, password=PG_PASSWORD,
            min_size=2, max_size=10,
        )
    return _pool

@app.on_event("startup")
async def startup():
    await get_pool()

@app.on_event("shutdown")
async def shutdown():
    if _pool:
        await _pool.close()

# ── 공통 직렬화 헬퍼 ─────────────────────────────────────────
def _row_to_dict(row) -> dict:
    d = dict(row)
    for k, v in d.items():
        if isinstance(v, datetime):
            d[k] = v.isoformat()
    return d

# ── E2E 구간 추론 헬퍼 ───────────────────────────────────────
# 파이프라인 구간 정의:
#   0: NiFi 수신       (requested_at 존재)
#   1: Kafka 발행      (requested_at 존재 → Kafka 도달 추정)
#   2: Flink 처리      (status != PENDING)
#   3: Adapter 발송    (dispatched_at 존재)
#   4: 결과 수신       (result_code 존재)
#   5: DB 저장         (delivered_at 존재)
PIPELINE_STAGES = [
    {"id": "nifi",    "label": "NiFi 수신",    "icon": "🔄"},
    {"id": "kafka",   "label": "Kafka 발행",   "icon": "📦"},
    {"id": "flink",   "label": "Flink 처리",   "icon": "⚡"},
    {"id": "adapter", "label": "Adapter 발송", "icon": "📡"},
    {"id": "result",  "label": "결과 수신",    "icon": "✅"},
    {"id": "db",      "label": "DB 저장",      "icon": "🗄️"},
]

def _infer_pipeline_status(row: dict) -> List[dict]:
    """
    DB 레코드 한 건을 바탕으로 각 파이프라인 구간의 통과 여부를 추론.
    실제 운영에서는 각 구간마다 별도 이벤트 로그 테이블이 있지만,
    PoC에서는 msg_send_history 의 타임스탬프 컬럼으로 추론.
    """
    stages = []
    status = row.get("status", "PENDING")
    result_code = row.get("result_code")
    requested_at = row.get("requested_at")
    dispatched_at = row.get("dispatched_at")
    delivered_at = row.get("delivered_at")

    # 구간별 통과 여부
    passed = {
        "nifi":    requested_at is not None,
        "kafka":   requested_at is not None,          # NiFi 통과 시 Kafka 발행 완료로 간주
        "flink":   status not in ("PENDING",),
        "adapter": dispatched_at is not None,
        "result":  result_code is not None,
        "db":      delivered_at is not None or status in ("DELIVERED", "FAILED", "DLQ"),
    }

    # 실패 구간 탐지: status=FAILED 이고 dispatcher_at 없으면 Flink 단계 실패
    error_stage = None
    error_msg   = None
    if status == "FAILED":
        if dispatched_at is None:
            error_stage = "flink"
            error_msg   = f"Flink 처리 실패 — result_code: {result_code}"
        else:
            error_stage = "result"
            error_msg   = f"발송 실패 — result_code: {result_code}"
    elif status == "DLQ":
        error_stage = "db"
        error_msg   = f"최종 실패 → DLQ 이동 — result_code: {result_code}"

    for s in PIPELINE_STAGES:
        sid = s["id"]
        stage_status = "success"
        if not passed[sid]:
            stage_status = "pending"
        if sid == error_stage:
            stage_status = "error"

        stages.append({
            **s,
            "status":    stage_status,   # success | pending | error
            "error_log": error_msg if sid == error_stage else None,
        })

    return stages

# ─────────────────────────────────────────────────────────────
# API 엔드포인트
# ─────────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    return {"status": "ok", "service": "history-api", "timestamp": datetime.now(timezone.utc).isoformat()}

@app.get("/metrics")
async def metrics():
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

@app.get("/api/v1/history/tx/{tx_id}")
async def get_history_by_tx(tx_id: str):
    """
    트랜잭션 ID 기준 전 구간 이력 조회.
    응답에 pipeline_stages 포함 — 어느 구간에서 문제가 발생했는지 시각화용.
    """
    if len(tx_id) != 35:
        raise HTTPException(status_code=400, detail="txId는 35자리여야 합니다.")

    start = time.monotonic()
    _REQUEST_COUNT.labels(endpoint="tx").inc()

    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT tx_id, request_id, channel, sender, receiver,
                   status, result_code, retry_count, source,
                   scheduled_at, requested_at, dispatched_at,
                   delivered_at, created_at
            FROM msg_send_history
            WHERE tx_id = $1
            ORDER BY created_at DESC
            LIMIT 1
            """,
            tx_id,
        )

    elapsed_ms = (time.monotonic() - start) * 1000
    _REQUEST_LATENCY.labels(endpoint="tx").observe(elapsed_ms)

    if not row:
        raise HTTPException(status_code=404, detail=f"txId {tx_id} 에 해당하는 이력이 없습니다.")

    data = _row_to_dict(row)
    data["pipeline_stages"] = _infer_pipeline_status(data)
    data["query_elapsed_ms"] = round(elapsed_ms, 1)
    return data


@app.get("/api/v1/history/receiver/{phone}")
async def get_history_by_receiver(
    phone: str,
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
):
    """
    수신번호 기준 발송 이력 목록 조회.
    VOC 상담원이 고객 전화번호를 입력해서 최근 발송 내역을 확인하는 용도.
    """
    start = time.monotonic()
    _REQUEST_COUNT.labels(endpoint="receiver").inc()

    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT tx_id, channel, sender, status, result_code,
                   retry_count, requested_at, dispatched_at, delivered_at
            FROM msg_send_history
            WHERE receiver = $1
            ORDER BY requested_at DESC NULLS LAST
            LIMIT $2 OFFSET $3
            """,
            phone, limit, offset,
        )
        total = await conn.fetchval(
            "SELECT COUNT(*) FROM msg_send_history WHERE receiver = $1",
            phone,
        )

    elapsed_ms = (time.monotonic() - start) * 1000
    _REQUEST_LATENCY.labels(endpoint="receiver").observe(elapsed_ms)

    return {
        "phone": phone,
        "total": total,
        "limit": limit,
        "offset": offset,
        "query_elapsed_ms": round(elapsed_ms, 1),
        "items": [_row_to_dict(r) for r in rows],
    }


@app.get("/api/v1/metrics/success-rate")
async def get_success_rate():
    """
    채널별 실시간 성공률 (최근 1시간 기준).
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT channel,
                   COUNT(*) AS total,
                   SUM(CASE WHEN result_code = '10000' THEN 1 ELSE 0 END) AS success,
                   SUM(CASE WHEN result_code LIKE '5%'  THEN 1 ELSE 0 END) AS retried,
                   SUM(CASE WHEN result_code LIKE '4%'  THEN 1 ELSE 0 END) AS failed
            FROM msg_send_history
            WHERE requested_at >= NOW() - INTERVAL '1 hour'
            GROUP BY channel
            ORDER BY channel
            """,
        )
    result = []
    for r in rows:
        total = r["total"] or 0
        success = r["success"] or 0
        result.append({
            "channel":      r["channel"],
            "total":        total,
            "success":      success,
            "retried":      r["retried"] or 0,
            "failed":       r["failed"] or 0,
            "success_rate": round(success / total * 100, 2) if total > 0 else 0.0,
        })
    return {"window": "1h", "channels": result}


@app.get("/api/v1/metrics/tps")
async def get_tps():
    """
    최근 1분간 채널별 TPS (건/초).
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT channel, COUNT(*) AS cnt
            FROM msg_send_history
            WHERE requested_at >= NOW() - INTERVAL '1 minute'
            GROUP BY channel
            """,
        )
    return {
        "window_seconds": 60,
        "channels": [
            {"channel": r["channel"], "tps": round(r["cnt"] / 60, 2)}
            for r in rows
        ],
    }


# ── Static 파일 (E2E 추적 UI) 마운트 ─────────────────────────
app.mount("/ui", StaticFiles(directory="static", html=True), name="static")

@app.get("/", response_class=HTMLResponse)
async def root():
    return """
    <html><body>
    <p>AM Platform History API</p>
    <ul>
      <li><a href="/docs">API 문서 (Swagger)</a></li>
      <li><a href="/ui/trace.html">E2E 이력 추적 UI</a></li>
      <li><a href="/health">Health Check</a></li>
    </ul>
    </body></html>
    """