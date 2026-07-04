# tests/load/ts0008_batch_load.py
"""
Test Scenario 0008 - 배치성(예약) 발송 부하 테스트 (Day 7 Phase 4 / 시나리오 B)

목적:
    sendMethodCode=01/02 (배치/예약성) 발송 경로가
      1) 대량 건을 정해진 시간 안에 다 접수할 수 있는가 (처리 완료 시간)
      2) 각 건이 실제로 "예약한 시각" 근처에 정확히 발송되는가 (예약 정확도)
    를 검증한다. 이 기능(ScheduleGateOperator)은 Day 7 Phase 3(TS-0007 이후)에
    새로 추가된 기능이라, 이번이 최초의 정식 검증이다.

설계 배경 (처음 보는 사람을 위한 설명):
    실시간 발송(TS-0007)은 "지금 당장 보낼 수 있는가"만 보면 됐지만,
    배치 발송은 "지금 접수받고, 나중에 정확한 시각에 보낼 수 있는가"를
    봐야 해서 검증 항목이 다르다.
      1) 스모크: 소량으로 예약 큐잉 자체가 정상 동작하는지 먼저 확인
         (대량 테스트 전에 기본 동작을 확인해 시간 낭비 방지)
      2) 처리 완료 시간: 목표 건수를 접수하는 데 얼마나 걸리는가
      3) 예약 정확도: 접수된 건들이 예약 시각 기준 ±허용오차 이내에
         실제로 발송(status=DISPATCHING 전환)되는가

    예약 시각은 "테스트 시작 시점 + SCHEDULE_OFFSET_MIN분" 단일 값으로
    전체 건에 동일하게 부여한다. 대량 접수 자체가 몇 분씩 걸릴 수 있어서,
    접수가 다 끝나기 전에 예약 시각이 지나버리면 "몰아서 한꺼번에 발송되는"
    배치 특성을 제대로 테스트할 수 없기 때문에, 예상 접수 완료 시간보다
    충분히 여유 있게 잡아야 한다 (기본 10분, 필요 시 조정 가능).

Test Cases:
    TC-0023  스모크 — 소량(SMOKE_COUNT)으로 예약 큐잉 기본 동작 확인
        (실패 시 본 테스트를 걸지 않고 즉시 중단 — 의미 없는 대량 투입 방지)
    TC-0024  배치 처리 완료 시간 — BATCH_COUNT건 접수 완료까지 소요 시간
             <= MAX_COMPLETION_SEC (기본 900s = 15분)  (README B-2)
    TC-0025  예약 시각 정확도 — |dispatched_at - scheduled_at| <= 허용오차(초)
             인 비율 >= MIN_SCHEDULE_ACCURACY_RATIO (기본 99%)  (README B-3)

    ※ README B-4(TaskManager 4개 확장 재측정)는 별도 인프라 변경(docker-compose
      스케일 조정)이 필요해 본 스크립트 범위에서 제외한다 (시나리오 A의
      A-4/A-5와 동일하게 별도 항목으로 보류).

How to run:
    # 1) 스모크 검증 겸 축소 규모 (권장: 먼저 이걸로 스크립트 자체를 확인)
    BATCH_COUNT=10000 SCHEDULE_OFFSET_MIN=2 python tests/load/ts0008_batch_load.py

    # 2) 본 테스트 (README 원안: 100만 건, 예약시각 = 접수 시작 + 10분)
    python tests/load/ts0008_batch_load.py

Environment variables (모두 기본값 있음):
    SMOKE_COUNT                  스모크 건수 (기본 100)
    SMOKE_WORKERS                스모크 스레드 풀 크기 (기본 32)
    MIN_SMOKE_SUCCESS_RATE       스모크 통과 기준 성공률 (기본 0.99)

    BATCH_COUNT                  본 테스트 건수 (기본 1000000)
    BATCH_WORKERS                본 테스트 스레드 풀 크기 (기본 256)
    SCHEDULE_OFFSET_MIN          예약 시각 = 시작 시점 + N분 (기본 10)
    SEND_METHOD_CODE             "01" 또는 "02" (기본 "01")
    MAX_COMPLETION_SEC           TC-0024 통과 기준 접수 완료 시간(초) (기본 900)

    ACCURACY_TOLERANCE_SEC       TC-0025 허용 오차(초) (기본 60)
    MIN_SCHEDULE_ACCURACY_RATIO  TC-0025 통과 기준 비율 (기본 0.99)
    POST_SCHEDULE_WAIT_SEC       예약시각 도래 후 추가 안정화 대기(초) (기본 90)
"""

import os
import sys
import time
from pathlib import Path
from datetime import timedelta

# ─────────────────────────────────────────────────────────────
# 경로 설정 (절대경로 금지 — 이 파일 위치 기준 상대 경로만 사용)
#   tests/load/ts0008_batch_load.py
#     -> tests/load/lib/            (LoadInjector, 메시지 생성 헬퍼)
#     -> tests/validation/          (conftest 공통 유틸 재사용)
#     -> tests/validation/lib/      (NiFiClient, DBChecker)
# ─────────────────────────────────────────────────────────────
SCRIPT_DIR         = Path(__file__).resolve().parent              # tests/load
LOAD_LIB_DIR       = SCRIPT_DIR / "lib"                            # tests/load/lib
TESTS_DIR          = SCRIPT_DIR.parent                            # tests
VALIDATION_DIR     = TESTS_DIR / "validation"                      # tests/validation
VALIDATION_LIB_DIR = VALIDATION_DIR / "lib"                        # tests/validation/lib
REPORTS_DIR        = SCRIPT_DIR / "reports"                        # tests/load/reports (기존 - TS-0007과 공유)

for _p in (str(LOAD_LIB_DIR), str(VALIDATION_DIR), str(VALIDATION_LIB_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

REPORTS_DIR.mkdir(parents=True, exist_ok=True)  # 이미 존재 (TS-0007이 생성) - 신규 디렉토리 아님

# 기존 코드 재사용 (수정 없음) — Day 6 conftest 공통 유틸
from conftest import (                                              # noqa: E402
    banner, print_pass, print_fail, print_info, print_warn,
    configure_logging, TestCaseResult, TestScenarioResult,
    now_kst, timestamp_slug, _json_default,
)
# 기존 코드 재사용 (수정 없음) — Day 6 검증 라이브러리
from nifi_client import NiFiClient                                  # noqa: E402
from db_checker import DBChecker                                    # noqa: E402
# 기존 코드 재사용 (수정 없음) — Day 7 Phase 1 부하 주입기
# (build_batch_messages / run_burst 는 Phase 1 부터 "시나리오 A/B/C 공용"으로
#  준비되어 있었으나 지금까지 사용된 적이 없었다 - 이번이 최초 사용)
from load_injector import LoadInjector, build_batch_messages         # noqa: E402

import json


# ─────────────────────────────────────────────────────────────
# Config (모두 환경변수로 override 가능, 기본값 = README 원안 100만 건)
# ─────────────────────────────────────────────────────────────
SCENARIO_CODE      = "TS-0008"
SCENARIO_TITLE     = "Batch Scheduled Send Load Test"
SCENARIO_TITLE_KO  = "배치성(예약) 발송 부하 테스트"

TC_TITLES_KO = {
    "TC-0023": "스모크 — 예약 큐잉 기본 동작 확인",
    "TC-0024": "배치 처리 완료 시간 — 접수 완료 소요 시간 확인",
    "TC-0025": "예약 시각 정확도 — 허용오차 이내 발송 비율 확인",
}

CHANNELS = ["SMS", "MMS", "RCS", "FAX", "EMAIL"]

# 스모크
SMOKE_COUNT             = int(os.getenv("SMOKE_COUNT", "100"))
SMOKE_WORKERS           = int(os.getenv("SMOKE_WORKERS", "32"))
MIN_SMOKE_SUCCESS_RATE  = float(os.getenv("MIN_SMOKE_SUCCESS_RATE", "0.99"))

# 본 테스트
BATCH_COUNT           = int(os.getenv("BATCH_COUNT", "1000000"))
BATCH_WORKERS         = int(os.getenv("BATCH_WORKERS", "256"))
SCHEDULE_OFFSET_MIN   = int(os.getenv("SCHEDULE_OFFSET_MIN", "10"))
SEND_METHOD_CODE      = os.getenv("SEND_METHOD_CODE", "01")
MAX_COMPLETION_SEC    = float(os.getenv("MAX_COMPLETION_SEC", "900"))

# 예약 정확도
ACCURACY_TOLERANCE_SEC      = float(os.getenv("ACCURACY_TOLERANCE_SEC", "60"))
MIN_SCHEDULE_ACCURACY_RATIO = float(os.getenv("MIN_SCHEDULE_ACCURACY_RATIO", "0.99"))
POST_SCHEDULE_WAIT_SEC      = float(os.getenv("POST_SCHEDULE_WAIT_SEC", "90"))


# ─────────────────────────────────────────────────────────────
# 리포트 저장 (tests/load/reports/ — TS-0007과 동일한 얇은 래퍼 재사용 패턴)
# conftest.py 는 무수정, 저장 경로만 다르게 하기 위해 동일 로직을 재구현한다.
# ─────────────────────────────────────────────────────────────
def save_load_report(scenario_code: str, payload: dict) -> Path:
    ts = timestamp_slug()
    filename = f"{scenario_code}_{ts}.json"
    output_path = REPORTS_DIR / filename

    if "meta" not in payload:
        payload["meta"] = {}
    payload["meta"]["scenario_code"] = scenario_code
    payload["meta"]["timestamp_kst"] = now_kst().isoformat(timespec="seconds")
    payload["meta"]["reports_dir"]   = str(REPORTS_DIR.relative_to(TESTS_DIR.parent))

    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2, default=_json_default)

    return output_path


# ─────────────────────────────────────────────────────────────
# TC-0023: 스모크 — 소량으로 예약 큐잉 기본 동작 확인
# ─────────────────────────────────────────────────────────────
def run_tc_0023(db: DBChecker) -> TestCaseResult:
    tc = TestCaseResult(
        "TC-0023",
        f"스모크 ({SMOKE_COUNT}건, 성공률 >= {MIN_SMOKE_SUCCESS_RATE:.0%}, "
        f"SCHEDULED 상태 즉시 확인)",
    )

    try:
        # 스모크는 접수 직후(=아직 예약시각 전) 곧바로 확인해야 하므로
        # 본 테스트 예약시각(SCHEDULE_OFFSET_MIN)과 무관하게 충분히 먼
        # 미래(5분 후)로 별도 고정한다 - 스모크 도중 시각이 지나버려
        # 이미 발송되면 "SCHEDULED 상태 확인"이라는 스모크 취지와 안 맞는다.
        smoke_scheduled_at = (now_kst() + timedelta(minutes=5)).isoformat(timespec="seconds")

        messages = build_batch_messages(
            count=SMOKE_COUNT,
            scheduled_at_iso=smoke_scheduled_at,
            channels=CHANNELS,
            send_method_code=SEND_METHOD_CODE,
        )
        tx_ids = [m["tx_id"] for m in messages]
        print_info(
            f"스모크 메시지 {SMOKE_COUNT}건 생성 "
            f"(sendMethodCode={SEND_METHOD_CODE}, scheduled_at={smoke_scheduled_at})"
        )

        injector = LoadInjector(max_workers=SMOKE_WORKERS)
        stats = injector.run_burst(messages)
        print_info(f"스모크 접수 결과: {stats}")

        if stats["success_rate"] < MIN_SMOKE_SUCCESS_RATE:
            tc.finish_fail(
                f"스모크 성공률 {stats['success_rate']:.1%} "
                f"< 기준 {MIN_SMOKE_SUCCESS_RATE:.0%}",
                details=stats,
            )
            return tc

        # 접수 직후 DB에 SCHEDULED 상태로 즉시 반영되는지 확인
        # (§16.6.1: 요청 즉시 INSERT, VOC 조회 가능해야 함 - Day 7 Phase 3 설계)
        time.sleep(5)  # NiFi → Kafka → Flink 파이프라인 반영 대기 (짧은 마진)
        result = db.count_by_tx_ids(tx_ids)
        scheduled_found = result["by_status"].get("SCHEDULED", 0)

        print_info(
            f"DB 확인: {result['total_found']}/{result['total_queried']} 도달, "
            f"by_status={result['by_status']}"
        )

        if scheduled_found == 0:
            tc.finish_fail(
                "접수 5초 후에도 SCHEDULED 상태 row가 하나도 없음 "
                "(예약 게이트가 즉시 INSERT하지 않는 것으로 보임)",
                details={"http_stats": stats, "db_result": result},
            )
        else:
            tc.finish_pass(details={
                "http_stats": stats,
                "scheduled_found": scheduled_found,
                "db_by_status": result["by_status"],
            })

    except Exception as e:
        tc.finish_fail(f"exception: {type(e).__name__}: {e}")

    return tc


# ─────────────────────────────────────────────────────────────
# 본 테스트 1회 실행 (TC-0024/0025 공유)
# ─────────────────────────────────────────────────────────────
def run_main_batch() -> tuple:
    """
    본 배치를 1회 투입하고 (stats, tx_ids, scheduled_at_iso) 를 반환한다.
    TC-0024/0025 가 동일한 결과를 서로 다른 기준으로 채점하므로
    투입은 여기서 단 한 번만 한다.
    """
    scheduled_at_iso = (
        now_kst() + timedelta(minutes=SCHEDULE_OFFSET_MIN)
    ).isoformat(timespec="seconds")

    print_info(
        f"본 배치 메시지 {BATCH_COUNT}건 생성 중... "
        f"(sendMethodCode={SEND_METHOD_CODE}, scheduled_at={scheduled_at_iso}, "
        f"workers={BATCH_WORKERS})"
    )
    messages = build_batch_messages(
        count=BATCH_COUNT,
        scheduled_at_iso=scheduled_at_iso,
        channels=CHANNELS,
        send_method_code=SEND_METHOD_CODE,
    )
    tx_ids = [m["tx_id"] for m in messages]

    def _progress(done, success, fail):
        print(f"    [진행]  done={done:>9}  success={success:>9}  fail={fail:>5}")

    injector = LoadInjector(max_workers=BATCH_WORKERS)
    stats = injector.run_burst(messages, progress_every=max(1000, BATCH_COUNT // 50),
                                progress_callback=_progress)
    print_info(f"본 배치 접수 결과: {stats}")

    return stats, tx_ids, scheduled_at_iso


def run_tc_0024(stats: dict) -> TestCaseResult:
    tc = TestCaseResult(
        "TC-0024",
        f"배치 처리 완료 시간 ({BATCH_COUNT}건 접수 <= {MAX_COMPLETION_SEC:.0f}s)",
    )
    elapsed = stats["wall_elapsed_sec"]
    if elapsed <= MAX_COMPLETION_SEC:
        tc.finish_pass(details={
            "wall_elapsed_sec": elapsed,
            "success_rate": stats["success_rate"],
            "achieved_tps": stats["achieved_tps"],
        })
    else:
        tc.finish_fail(
            f"처리 완료 시간 {elapsed}s > 기준 {MAX_COMPLETION_SEC:.0f}s",
            details={"wall_elapsed_sec": elapsed, "achieved_tps": stats["achieved_tps"]},
        )
    return tc


# ─────────────────────────────────────────────────────────────
# TC-0025: 예약 시각 정확도
#
# db_checker.py 는 공용 라이브러리라 이번 테스트 전용 쿼리를 추가하지 않고,
# DBChecker 가 이미 열어둔 커넥션(db.conn)을 재사용해 이 스크립트 안에서만
# 쓰는 쿼리를 직접 실행한다 (기존 코드 무수정 원칙).
# ─────────────────────────────────────────────────────────────
def run_tc_0025(tx_ids: list, scheduled_at_iso: str, db: DBChecker) -> TestCaseResult:
    tc = TestCaseResult(
        "TC-0025",
        f"예약 시각 정확도 (|dispatched_at - scheduled_at| <= {ACCURACY_TOLERANCE_SEC:.0f}s "
        f"인 비율 >= {MIN_SCHEDULE_ACCURACY_RATIO:.0%})",
    )

    try:
        # 예약 시각 + 안정화 대기시간까지 남은 시간만큼 대기
        from datetime import datetime
        scheduled_dt = datetime.fromisoformat(scheduled_at_iso)
        now = now_kst()
        wait_sec = (scheduled_dt - now).total_seconds() + POST_SCHEDULE_WAIT_SEC
        if wait_sec > 0:
            print_info(
                f"예약 시각({scheduled_at_iso}) + 안정화 {POST_SCHEDULE_WAIT_SEC:.0f}s "
                f"까지 대기 ({wait_sec:.0f}s)..."
            )
            time.sleep(wait_sec)

        with db._cursor() as cur:
            cur.execute(
                """
                SELECT tx_id,
                       scheduled_at,
                       dispatched_at,
                       status,
                       EXTRACT(EPOCH FROM (dispatched_at - scheduled_at)) AS deviation_sec
                FROM msg_send_history
                WHERE tx_id = ANY(%s)
                """,
                (list(tx_ids),),
            )
            rows = cur.fetchall()

        total_queried   = len(tx_ids)
        total_found     = len(rows)
        dispatched_rows = [r for r in rows if r["dispatched_at"] is not None]
        still_scheduled = total_found - len(dispatched_rows)

        within_tolerance = sum(
            1 for r in dispatched_rows
            if r["deviation_sec"] is not None and abs(r["deviation_sec"]) <= ACCURACY_TOLERANCE_SEC
        )
        accuracy_ratio = (
            within_tolerance / len(dispatched_rows) if dispatched_rows else 0.0
        )

        print_info(
            f"DB 확인: {total_found}/{total_queried} 도달, "
            f"dispatched={len(dispatched_rows)}, still_scheduled={still_scheduled}, "
            f"허용오차 이내={within_tolerance} ({accuracy_ratio:.1%})"
        )

        details = {
            "total_queried":       total_queried,
            "total_found":         total_found,
            "dispatched_count":    len(dispatched_rows),
            "still_scheduled":     still_scheduled,
            "within_tolerance":    within_tolerance,
            "accuracy_ratio":      round(accuracy_ratio, 4),
            "tolerance_sec":       ACCURACY_TOLERANCE_SEC,
        }

        if still_scheduled > 0:
            print_warn(
                f"{still_scheduled}건이 안정화 대기 이후에도 아직 SCHEDULED 상태 "
                f"(아직 발송 안 됨) — 정확도 계산은 발송 완료건만 대상으로 함"
            )

        if accuracy_ratio >= MIN_SCHEDULE_ACCURACY_RATIO and total_found > 0:
            tc.finish_pass(details=details)
        else:
            tc.finish_fail(
                f"예약 정확도 {accuracy_ratio:.1%} < 기준 {MIN_SCHEDULE_ACCURACY_RATIO:.0%}",
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
        f"Config: SMOKE={SMOKE_COUNT}건  "
        f"BATCH={BATCH_COUNT}건(workers={BATCH_WORKERS}, "
        f"schedule_offset={SCHEDULE_OFFSET_MIN}min)  "
        f"MAX_COMPLETION={MAX_COMPLETION_SEC:.0f}s"
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

        # TC-0023: 스모크 (실패 시 본 테스트 진입 안 함 — 대량 투입 시간 낭비 방지)
        print()
        banner(f"TC-0023 execution - {TC_TITLES_KO['TC-0023']}", char="-")
        tc23 = run_tc_0023(db)
        scenario.add_tc(tc23)

        if not tc23.passed:
            print_warn("스모크 실패 — 본 배치 테스트(TC-0024~0025)를 건너뜁니다.")
            scenario.finish()
            scenario.print_summary(title_ko=SCENARIO_TITLE_KO)
            report_path = save_load_report(SCENARIO_CODE, {"summary": scenario.to_dict()})
            print_info(f"JSON report saved: {report_path}")
            return 1

        # TC-0024/0025: 본 배치 (1회 투입, 2개 기준으로 채점)
        print()
        banner("본 배치 투입 실행 (TC-0024~0025 공유)", char="-")
        main_stats, main_tx_ids, scheduled_at_iso = run_main_batch()

        banner(f"TC-0024 execution - {TC_TITLES_KO['TC-0024']}", char="-")
        scenario.add_tc(run_tc_0024(main_stats))

        print()
        banner(f"TC-0025 execution - {TC_TITLES_KO['TC-0025']}", char="-")
        scenario.add_tc(run_tc_0025(main_tx_ids, scheduled_at_iso, db))

    finally:
        scenario.finish()
        nifi.close()
        db.close()

    scenario.print_summary(title_ko=SCENARIO_TITLE_KO)

    report_path = save_load_report(SCENARIO_CODE, {
        "summary": scenario.to_dict(),
        "config": {
            "smoke_count": SMOKE_COUNT,
            "smoke_workers": SMOKE_WORKERS,
            "min_smoke_success_rate": MIN_SMOKE_SUCCESS_RATE,
            "batch_count": BATCH_COUNT,
            "batch_workers": BATCH_WORKERS,
            "schedule_offset_min": SCHEDULE_OFFSET_MIN,
            "send_method_code": SEND_METHOD_CODE,
            "max_completion_sec": MAX_COMPLETION_SEC,
            "accuracy_tolerance_sec": ACCURACY_TOLERANCE_SEC,
            "min_schedule_accuracy_ratio": MIN_SCHEDULE_ACCURACY_RATIO,
            "post_schedule_wait_sec": POST_SCHEDULE_WAIT_SEC,
        },
    })
    print_info(f"JSON report saved: {report_path}")

    return 0 if scenario.fail_count == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
