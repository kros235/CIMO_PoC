# tests/load/ts0009_mixed_load.py
"""
Test Scenario 0009 - 복합(실시간+배치 동시) 발송 부하 테스트 (Day 7 Phase 5 / 시나리오 C)

목적:
    실시간 발송(TS-0007)과 배치 발송(TS-0008)을 각각 따로 테스트해서는
    "두 흐름이 동시에 들어올 때 서로 영향을 주는가"를 확인할 수 없다.
    이 스크립트는 실시간을 계속 흘려보내는 도중에 배치를 투입해서,
    실시간 쪽이 얼마나 영향을 받는지 측정한다.

설계 변경 이력 (중요 - README 원안과 다름):
    원안의 C-3은 "실시간 토픽 vs 배치 토픽 격리 효과"였으나, 애초 작성 시점엔
    실제 코드 확인 결과 발송방법코드와 무관하게 단일 요청 토픽
    (topic.send.request)을 공유하고, 채널별로만 분배되는 구조였다
    (AM_ARCHITECTURE.md §16.11 참고). "실시간 토픽"과 "배치 토픽"이라는 구분
    자체가 없어 원안대로 테스트할 수 없었다.

    그래서 TC-0030을 "같은 채널을 실시간과 배치가 동시에 쓸 때, 그
    채널의 실시간 처리 지연이 얼마나 늘어나는가"로 재정의했다. 배치
    전량을 한 채널(BATCH_CHANNEL, 기본 SMS)에만 집중시키고, 실시간은
    5개 채널에 고르게 분배함으로써, 같은 테스트 실행 한 번 안에서
    "배치와 공유하는 채널"과 "배치가 없는 채널"의 실시간 지연을 직접
    비교할 수 있게 설계했다.

    ⭐️ Day 8 갱신: 위 진단(§16.12)을 근거로 실시간·배치 요청 라인을
    topic.send.request.realtime / topic.send.request.batch로 완전히
    분리했다(§16.13). 이 스크립트는 NiFi HTTP 엔드포인트(8090) 하나로
    요청을 보내는 방식은 그대로이며 — NiFi가 txId의 sendMethodCode를
    보고 내부적으로 알아서 실시간/배치 토픽으로 나눠 발행한다 — 코드
    수정 없이 그대로 재사용 가능하다. 이번 재실행은 분리 "이전"
    실측치(§16.12, TPS -38.3%, p95 +48.8%)와 "이후"를 비교하기 위한
    목적으로 사용한다. TC-0030(채널 공유 지연 비교)의 의미도 달라지는데,
    분리 전엔 "병목이 채널 공유 때문인지" 확인하는 용도였다면, 분리
    후엔 "모든 채널이 고르게 낮은 지연을 보이는지"를 확인하는 회귀
    검증 용도가 된다.

    이 측정은 SendRequestJob.java의 buildPostgresSink() 수정(커밋
    e32c499, requested_at/dispatched_at을 정확히 기록하도록 변경)이
    선행되어야 가능하다 - 그 전에는 실시간 경로의 두 컬럼이 항상
    NOW()로 같은 값이 찍혀 소요시간 계산이 불가능했다. (Day 8 분리
    이후에도 이 로직은 RequestPipelineBuilder.java로 그대로 이전되어
    동일하게 동작한다.)

규모 조정 (중요):
    README 원안(실시간 1,000 TPS + 배치 50만 건)은 이미 알려진 한계
    (§16.5 처리 한계 ~500TPS대, §16.9 100만 건 시 대량 503)를 다시
    재현할 뿐이라, 순수하게 "서로 영향을 주는가"만 깨끗하게 보기 위해
    아래로 축소했다:
      - 실시간: 기존 실측 달성 수준 (REALTIME_TARGET_TPS 기본 500)
      - 배치: 5만 건 (BATCH_COUNT 기본 50,000)

Test Cases:
    TC-0027  베이스라인 — 배치 없이 실시간만 짧게 실행, 기준 성능 확보
    TC-0028  복합 투입 — 실시간을 계속 실행하는 동시에 배치 투입
    TC-0029  실시간 성능 저하 비교 — TC-0028 실시간 성능이 TC-0027 대비
             ±20% 이내인지 확인 (README 원 완료기준)
    TC-0030  채널 공유 지연 비교 — TC-0028 실행 중 배치와 공유한 채널의
             실시간 지연 vs 공유 안 한 채널들의 실시간 지연 비교

How to run:
    # 축소 규모 (기본값, 권장 - 이번 실행분)
    python tests/load/ts0009_mixed_load.py

    # 규모 조정이 필요하면 환경변수로 오버라이드
    REALTIME_TARGET_TPS=300 BATCH_COUNT=10000 python tests/load/ts0009_mixed_load.py

Environment variables (모두 기본값 있음):
    REALTIME_TARGET_TPS     실시간 목표 TPS (기본 500 - 기존 실측 달성 수준)
    REALTIME_WORKERS        실시간 스레드 풀 크기 (기본 128)
    BASELINE_DURATION_SEC   TC-0027 지속 시간(초) (기본 30)
    MIXED_DURATION_SEC      TC-0028 실시간 지속 시간(초) (기본 90)

    BATCH_COUNT             배치 건수 (기본 50000)
    BATCH_WORKERS           배치 스레드 풀 크기 (기본 128)
    BATCH_CHANNEL           배치를 집중시킬 단일 채널 (기본 SMS)
    SCHEDULE_OFFSET_SEC     배치 예약시각 = 시작 +N초 (기본 30,
                            MIXED_DURATION_SEC 도중에 방출되도록)
    POST_STABILIZE_SEC      배치 방출 후 DB 반영 대기(초) (기본 60)

    MAX_DEGRADATION_RATIO   TC-0029 통과 기준 (기본 0.20 = ±20%)
"""

import os
import sys
import time
import threading
from pathlib import Path
from datetime import timedelta

SCRIPT_DIR         = Path(__file__).resolve().parent
LOAD_LIB_DIR       = SCRIPT_DIR / "lib"
TESTS_DIR          = SCRIPT_DIR.parent
VALIDATION_DIR     = TESTS_DIR / "validation"
VALIDATION_LIB_DIR = VALIDATION_DIR / "lib"
REPORTS_DIR        = SCRIPT_DIR / "reports"  # 기존 디렉토리 (TS-0007/0008과 공유) - 신규 아님

for _p in (str(LOAD_LIB_DIR), str(VALIDATION_DIR), str(VALIDATION_LIB_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

REPORTS_DIR.mkdir(parents=True, exist_ok=True)

# 기존 코드 재사용 (수정 없음)
from conftest import (                                              # noqa: E402
    banner, print_pass, print_fail, print_info, print_warn,
    configure_logging, TestCaseResult, TestScenarioResult,
    now_kst, timestamp_slug, _json_default,
)
from nifi_client import NiFiClient                                  # noqa: E402
from db_checker import DBChecker                                    # noqa: E402
from load_injector import LoadInjector, build_realtime_messages, build_batch_messages  # noqa: E402

import json


# ─────────────────────────────────────────────────────────────
# Config
# ─────────────────────────────────────────────────────────────
SCENARIO_CODE     = "TS-0009"
SCENARIO_TITLE    = "Mixed Realtime+Batch Load Test"
SCENARIO_TITLE_KO = "복합(실시간+배치 동시) 발송 부하 테스트"

TC_TITLES_KO = {
    "TC-0027": "베이스라인 — 배치 없이 실시간 단독 성능 확보",
    "TC-0028": "복합 투입 — 실시간 실행 중 배치 동시 투입",
    "TC-0029": "실시간 성능 저하 비교 (기준선 대비 ±20% 이내)",
    "TC-0030": "채널 공유 지연 비교 (배치 공유 채널 vs 미공유 채널)",
}

ALL_CHANNELS = ["SMS", "MMS", "RCS", "FAX", "EMAIL"]

REALTIME_TARGET_TPS   = int(os.getenv("REALTIME_TARGET_TPS", "500"))
REALTIME_WORKERS      = int(os.getenv("REALTIME_WORKERS", "128"))
BASELINE_DURATION_SEC = int(os.getenv("BASELINE_DURATION_SEC", "30"))
MIXED_DURATION_SEC    = int(os.getenv("MIXED_DURATION_SEC", "90"))

BATCH_COUNT         = int(os.getenv("BATCH_COUNT", "50000"))
BATCH_WORKERS       = int(os.getenv("BATCH_WORKERS", "128"))
BATCH_CHANNEL       = os.getenv("BATCH_CHANNEL", "SMS")
SCHEDULE_OFFSET_SEC = int(os.getenv("SCHEDULE_OFFSET_SEC", "30"))
POST_STABILIZE_SEC  = int(os.getenv("POST_STABILIZE_SEC", "60"))

MAX_DEGRADATION_RATIO = float(os.getenv("MAX_DEGRADATION_RATIO", "0.20"))


def save_load_report(scenario_code: str, payload: dict) -> Path:
    ts = timestamp_slug()
    output_path = REPORTS_DIR / f"{scenario_code}_{ts}.json"
    if "meta" not in payload:
        payload["meta"] = {}
    payload["meta"]["scenario_code"] = scenario_code
    payload["meta"]["timestamp_kst"] = now_kst().isoformat(timespec="seconds")
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2, default=_json_default)
    return output_path


# ─────────────────────────────────────────────────────────────
# 실시간 부하 실행 (TC-0027 베이스라인 / TC-0028 복합 공용)
# ─────────────────────────────────────────────────────────────
def run_realtime(target_tps: int, duration_sec: int, workers: int, source_tag: str) -> dict:
    messages = build_realtime_messages(count=target_tps * duration_sec, channels=ALL_CHANNELS)
    for m in messages:
        m["source"] = source_tag  # 이번 테스트 결과만 DB에서 구분 조회하기 위한 태그

    injector = LoadInjector(max_workers=workers)
    stats = injector.run_at_target_tps(messages, target_tps=target_tps, duration_sec=duration_sec)
    return stats


# ─────────────────────────────────────────────────────────────
# TC-0027: 베이스라인 (실시간 단독)
# ─────────────────────────────────────────────────────────────
def run_tc_0027() -> tuple:
    tc = TestCaseResult(
        "TC-0027",
        f"베이스라인 (실시간 {REALTIME_TARGET_TPS} TPS x {BASELINE_DURATION_SEC}s, 배치 없음)",
    )
    try:
        print_info(f"베이스라인 실시간 실행 중... ({REALTIME_TARGET_TPS} TPS x {BASELINE_DURATION_SEC}s)")
        stats = run_realtime(
            REALTIME_TARGET_TPS, BASELINE_DURATION_SEC, REALTIME_WORKERS,
            source_tag="LOADTEST_MIXED_BASELINE",
        )
        print_info(f"베이스라인 결과: {stats}")
        tc.finish_pass(details=stats)
    except Exception as e:
        tc.finish_fail(f"exception: {type(e).__name__}: {e}")
        stats = {}
    return tc, stats


# ─────────────────────────────────────────────────────────────
# TC-0028: 복합 투입 (실시간 계속 + 배치 동시 투입)
# ─────────────────────────────────────────────────────────────
def run_tc_0028() -> tuple:
    tc = TestCaseResult(
        "TC-0028",
        f"복합 투입 (실시간 {REALTIME_TARGET_TPS} TPS x {MIXED_DURATION_SEC}s "
        f"+ 배치 {BATCH_COUNT}건 → {BATCH_CHANNEL} 채널 집중, "
        f"배치 예약시각 = 시작 +{SCHEDULE_OFFSET_SEC}s)",
    )

    realtime_result = {}
    realtime_error = []

    def _realtime_worker():
        try:
            realtime_result.update(
                run_realtime(
                    REALTIME_TARGET_TPS, MIXED_DURATION_SEC, REALTIME_WORKERS,
                    source_tag="LOADTEST_MIXED_RT",
                )
            )
        except Exception as e:
            realtime_error.append(str(e))

    try:
        start = now_kst()
        rt_thread = threading.Thread(target=_realtime_worker, daemon=True)
        rt_thread.start()
        print_info(f"실시간 스레드 시작 ({MIXED_DURATION_SEC}s 동안 계속 실행)")

        # 배치는 실시간이 도는 도중(SCHEDULE_OFFSET_SEC 후)에 방출되도록 예약
        scheduled_at_iso = (start + timedelta(seconds=SCHEDULE_OFFSET_SEC)).isoformat(timespec="seconds")
        print_info(
            f"배치 메시지 {BATCH_COUNT}건 생성 중... "
            f"(channel={BATCH_CHANNEL} 단일 집중, scheduled_at={scheduled_at_iso})"
        )
        batch_messages = build_batch_messages(
            count=BATCH_COUNT,
            scheduled_at_iso=scheduled_at_iso,
            channels=[BATCH_CHANNEL],  # ⭐️ 한 채널에만 집중 (TC-0030 비교를 위해 의도적)
            send_method_code="01",
        )
        for m in batch_messages:
            m["source"] = "LOADTEST_MIXED_BATCH"

        batch_injector = LoadInjector(max_workers=BATCH_WORKERS)
        batch_stats = batch_injector.run_burst(batch_messages)
        print_info(f"배치 접수 결과: {batch_stats}")

        rt_thread.join(timeout=MIXED_DURATION_SEC + 60)

        if realtime_error:
            tc.finish_fail(f"실시간 스레드 예외: {realtime_error[0]}")
            return tc, {}, batch_stats

        print_info(f"복합 실행 중 실시간 결과: {realtime_result}")
        tc.finish_pass(details={"realtime": realtime_result, "batch": batch_stats})
        return tc, realtime_result, batch_stats

    except Exception as e:
        tc.finish_fail(f"exception: {type(e).__name__}: {e}")
        return tc, {}, {}


# ─────────────────────────────────────────────────────────────
# TC-0029: 실시간 성능 저하 비교
# ─────────────────────────────────────────────────────────────
def run_tc_0029(baseline: dict, mixed: dict) -> TestCaseResult:
    tc = TestCaseResult(
        "TC-0029",
        f"실시간 성능 저하 비교 (기준선 대비 ±{MAX_DEGRADATION_RATIO:.0%} 이내)",
    )
    try:
        base_tps = baseline.get("achieved_tps", 0)
        mixed_tps = mixed.get("achieved_tps", 0)
        base_p95 = baseline.get("latency_p95_ms", 0)
        mixed_p95 = mixed.get("latency_p95_ms", 0)

        if base_tps == 0:
            tc.finish_fail("베이스라인 achieved_tps가 0 — 비교 불가")
            return tc

        tps_degradation = (base_tps - mixed_tps) / base_tps
        p95_degradation = (mixed_p95 - base_p95) / base_p95 if base_p95 > 0 else 0

        details = {
            "baseline_tps": base_tps,
            "mixed_tps": mixed_tps,
            "tps_degradation_ratio": round(tps_degradation, 4),
            "baseline_p95_ms": base_p95,
            "mixed_p95_ms": mixed_p95,
            "p95_degradation_ratio": round(p95_degradation, 4),
        }
        print_info(
            f"TPS 저하율: {tps_degradation:.1%} (기준 {base_tps} → 복합중 {mixed_tps}), "
            f"p95 저하율: {p95_degradation:.1%} (기준 {base_p95}ms → 복합중 {mixed_p95}ms)"
        )

        if abs(tps_degradation) <= MAX_DEGRADATION_RATIO and abs(p95_degradation) <= MAX_DEGRADATION_RATIO:
            tc.finish_pass(details=details)
        else:
            tc.finish_fail(
                f"저하율 초과 (TPS {tps_degradation:.1%} 또는 p95 {p95_degradation:.1%} "
                f"> 허용 {MAX_DEGRADATION_RATIO:.0%})",
                details=details,
            )
    except Exception as e:
        tc.finish_fail(f"exception: {type(e).__name__}: {e}")
    return tc


# ─────────────────────────────────────────────────────────────
# TC-0030: 채널 공유 지연 비교
# db_checker.py는 공용 라이브러리라 무수정, 이미 열어둔 커넥션을 재사용해
# 이 스크립트 전용 쿼리를 직접 실행한다 (TS-0008과 동일한 패턴).
# ─────────────────────────────────────────────────────────────
def run_tc_0030(db: DBChecker) -> TestCaseResult:
    tc = TestCaseResult(
        "TC-0030",
        f"채널 공유 지연 비교 ({BATCH_CHANNEL} 채널 vs 나머지 채널, 실시간 메시지 기준)",
    )
    try:
        wait_sec = SCHEDULE_OFFSET_SEC + POST_STABILIZE_SEC
        print_info(f"배치 방출 + 안정화 대기 ({wait_sec}s)...")
        time.sleep(wait_sec)

        with db._cursor() as cur:
            cur.execute(
                """
                SELECT channel,
                       COUNT(*) AS cnt,
                       AVG(EXTRACT(EPOCH FROM (dispatched_at - requested_at)) * 1000) AS avg_ms,
                       PERCENTILE_CONT(0.95) WITHIN GROUP (
                           ORDER BY EXTRACT(EPOCH FROM (dispatched_at - requested_at)) * 1000
                       ) AS p95_ms
                FROM msg_send_history
                WHERE source = 'LOADTEST_MIXED_RT'
                  AND dispatched_at IS NOT NULL
                  AND requested_at IS NOT NULL
                GROUP BY channel
                ORDER BY channel
                """
            )
            rows = cur.fetchall()

        by_channel = {r["channel"]: {"count": r["cnt"], "avg_ms": float(r["avg_ms"] or 0),
                                      "p95_ms": float(r["p95_ms"] or 0)} for r in rows}
        print_info(f"채널별 실시간 통과 지연: {by_channel}")

        if BATCH_CHANNEL not in by_channel:
            tc.finish_fail(f"{BATCH_CHANNEL} 채널의 실시간 데이터가 없음", details=by_channel)
            return tc

        other_channels = [c for c in ALL_CHANNELS if c != BATCH_CHANNEL and c in by_channel]
        if not other_channels:
            tc.finish_fail("비교할 다른 채널 데이터가 없음", details=by_channel)
            return tc

        shared_avg = by_channel[BATCH_CHANNEL]["avg_ms"]
        other_avg = sum(by_channel[c]["avg_ms"] for c in other_channels) / len(other_channels)

        ratio = (shared_avg / other_avg) if other_avg > 0 else float("inf")
        details = {
            "batch_channel": BATCH_CHANNEL,
            "shared_channel_avg_ms": round(shared_avg, 1),
            "other_channels_avg_ms": round(other_avg, 1),
            "ratio": round(ratio, 2),
            "by_channel": by_channel,
        }
        print_info(
            f"배치 공유 채널({BATCH_CHANNEL}) 평균 지연 {shared_avg:.1f}ms vs "
            f"나머지 채널 평균 {other_avg:.1f}ms (비율 {ratio:.2f}배)"
        )

        # README 원 기준(토픽 격리, 저하 없음)에 준해 "2배 이내"를 잠정 기준으로 적용
        if ratio <= 2.0:
            tc.finish_pass(details=details)
        else:
            tc.finish_fail(
                f"배치와 공유한 채널이 나머지 대비 {ratio:.2f}배 느림 (기준 2.0배 이내)",
                details=details,
            )
    except Exception as e:
        tc.finish_fail(f"exception: {type(e).__name__}: {e}")
    return tc


# ─────────────────────────────────────────────────────────────
# Scenario driver
# ─────────────────────────────────────────────────────────────
def main() -> int:
    configure_logging(verbose=False)
    banner(f"{SCENARIO_CODE} - {SCENARIO_TITLE_KO} ({SCENARIO_TITLE})")
    print_info(
        f"Config: REALTIME={REALTIME_TARGET_TPS}TPS  "
        f"BASELINE_DURATION={BASELINE_DURATION_SEC}s  MIXED_DURATION={MIXED_DURATION_SEC}s  "
        f"BATCH={BATCH_COUNT}건→{BATCH_CHANNEL}채널  SCHEDULE_OFFSET={SCHEDULE_OFFSET_SEC}s"
    )

    scenario = TestScenarioResult(SCENARIO_CODE, SCENARIO_TITLE)
    nifi = NiFiClient()
    db = DBChecker()

    try:
        if not nifi.health_check():
            print_fail("NiFi 8090 health check failed")
            return 2
        print_pass("NiFi 8090 reachable")
        db.connect()
        print_pass("PostgreSQL connection OK")

        print()
        banner(f"TC-0027 execution - {TC_TITLES_KO['TC-0027']}", char="-")
        tc27, baseline_stats = run_tc_0027()
        scenario.add_tc(tc27)

        print()
        banner(f"TC-0028 execution - {TC_TITLES_KO['TC-0028']}", char="-")
        tc28, mixed_rt_stats, batch_stats = run_tc_0028()
        scenario.add_tc(tc28)

        print()
        banner(f"TC-0029 execution - {TC_TITLES_KO['TC-0029']}", char="-")
        scenario.add_tc(run_tc_0029(baseline_stats, mixed_rt_stats))

        print()
        banner(f"TC-0030 execution - {TC_TITLES_KO['TC-0030']}", char="-")
        scenario.add_tc(run_tc_0030(db))

    finally:
        scenario.finish()
        nifi.close()
        db.close()

    scenario.print_summary(title_ko=SCENARIO_TITLE_KO)
    report_path = save_load_report(SCENARIO_CODE, {
        "summary": scenario.to_dict(),
        "config": {
            "realtime_target_tps": REALTIME_TARGET_TPS,
            "baseline_duration_sec": BASELINE_DURATION_SEC,
            "mixed_duration_sec": MIXED_DURATION_SEC,
            "batch_count": BATCH_COUNT,
            "batch_channel": BATCH_CHANNEL,
            "schedule_offset_sec": SCHEDULE_OFFSET_SEC,
            "max_degradation_ratio": MAX_DEGRADATION_RATIO,
        },
    })
    print_info(f"JSON report saved: {report_path}")
    return 0 if scenario.fail_count == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
