# tests/load/ts0007_realtime_load.py
"""
Test Scenario 0007 - 실시간 발송 2,000 TPS 부하 테스트 (Day 7 Phase 2 / 시나리오 A)

목적:
    sendMethodCode=03 (실시간) 발송 경로가 목표 처리량(2,000 TPS)에서
    "받아들이는 속도", "성공률", "지연시간", "DB 적재 정합성" 4가지를
    동시에 충족하는지 검증한다.

설계 배경 (왜 이렇게 나눠서 보는가 - 처음 보는 사람을 위한 설명):
    "2,000 TPS 를 처리한다"는 한 문장에는 사실 4가지 다른 질문이 섞여 있다.
      1) 워밍업: 시스템이 정상 가동 상태인가? (작은 부하로 먼저 확인)
      2) 속도:   진짜로 초당 2,000건을 받아낼 수 있는가? (achieved_tps)
      3) 성공률: 그 와중에 요청이 거부되거나 에러나지 않는가? (success_rate)
      4) 지연:   응답이 너무 늦어지지 않는가? (p95 latency)
      5) 정합성: 받아낸 요청이 실제로 DB에 끝까지 잘 쌓이는가? (DB 도달률)
    이 5가지를 따로따로 측정해야, "느려서 실패"와 "터져서 실패"를
    구분할 수 있다. 그래서 TC 를 5개로 나눈다.

Test Cases:
    TC-0018  워밍업 — 도구·시스템 정상 가동 확인
        250 TPS x 10초 (총 2,500건). 성공률 >= 99% 면 본 부하로 진입.
        (워밍업 실패 시 본 부하를 걸지 않고 즉시 중단 — 의미 없는 측정 방지)

    TC-0019  목표 TPS 달성 — achieved_tps >= 1,900 (목표 2,000의 95%)
    TC-0020  성공률 — HTTP 성공률 >= 99% (메인 부하 구간)
    TC-0021  지연시간 — p95 지연 <= 1,000ms (메인 부하 구간)
        (TC-0019~0021 은 동일한 메인 부하 1회 실행 결과를 서로 다른
         기준으로 채점한다 — 부하를 3번 걸 필요가 없다)

    TC-0022  DB 도달률 — 안정화 대기 후 msg_send_history 도달률 >= 95%
        메인 부하 종료 후 STABILIZE_SEC(기본 60초) 만큼 기다린 다음,
        투입한 txId 가 실제로 DB 에 몇 % 들어왔는지 확인한다.
        (받아들인 것과 DB 에 쌓이는 것은 다른 구간이라 따로 검증)

확정된 설정값 (사용자 confirm 완료):
    - 시나리오 파일 위치: tests/load/ (Day 6 검증 자산과 분리)
    - TC 설계/임계치: 권장안 그대로 (위 TC-0018~0022)
    - 메인 부하 워커 수: 256
    - run_all.py 통합: 하지 않음 (완전 격리, 단일 실행 스크립트)
      → 리포트는 tests/validation/reports/ 가 아니라 tests/load/reports/ 에 저장

How to run:
    python tests/load/ts0007_realtime_load.py

Environment variables (모두 기본값 있음 - 그대로 실행 가능):
    WARMUP_TPS              워밍업 목표 TPS (기본 250)
    WARMUP_DURATION_SEC     워밍업 지속 시간(초) (기본 10)
    WARMUP_WORKERS          워밍업 스레드 풀 크기 (기본 64)
    MIN_WARMUP_SUCCESS_RATE 워밍업 통과 기준 성공률 (기본 0.99)

    MAIN_TPS                메인 부하 목표 TPS (기본 2000)
    MAIN_DURATION_SEC       메인 부하 지속 시간(초) (기본 30)
    MAIN_WORKERS            메인 부하 스레드 풀 크기 (기본 256, 사용자 확정값)
    MIN_ACHIEVED_TPS        TC-0019 통과 기준 achieved_tps (기본 1900)
    MIN_SUCCESS_RATE        TC-0020 통과 기준 성공률 (기본 0.99)
    MAX_P95_LATENCY_MS      TC-0021 통과 기준 p95 지연(ms) (기본 1000)

    STABILIZE_SEC           메인 부하 종료 후 DB 적재 대기 시간(초) (기본 60)
    MIN_DB_ARRIVAL_RATIO    TC-0022 통과 기준 DB 도달률 (기본 0.95)
"""

import os
import sys
from pathlib import Path

# ─────────────────────────────────────────────────────────────
# 경로 설정 (절대경로 금지 — 이 파일 위치 기준 상대 경로만 사용)
#   tests/load/ts0007_realtime_load.py
#     -> tests/load/lib/            (LoadInjector, 메시지 생성 헬퍼)
#     -> tests/validation/          (conftest 공통 유틸 재사용)
#     -> tests/validation/lib/      (NiFiClient, DBChecker)
# ─────────────────────────────────────────────────────────────
SCRIPT_DIR         = Path(__file__).resolve().parent              # tests/load
LOAD_LIB_DIR       = SCRIPT_DIR / "lib"                            # tests/load/lib
TESTS_DIR          = SCRIPT_DIR.parent                            # tests
VALIDATION_DIR     = TESTS_DIR / "validation"                      # tests/validation
VALIDATION_LIB_DIR = VALIDATION_DIR / "lib"                        # tests/validation/lib
REPORTS_DIR        = SCRIPT_DIR / "reports"                        # tests/load/reports (신규)

for _p in (str(LOAD_LIB_DIR), str(VALIDATION_DIR), str(VALIDATION_LIB_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# 신규 디렉토리 생성 (사전 안내 완료 - tests/load/reports/)
REPORTS_DIR.mkdir(parents=True, exist_ok=True)

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
from load_injector import LoadInjector, build_realtime_messages     # noqa: E402

import json


# ─────────────────────────────────────────────────────────────
# Config (모두 환경변수로 override 가능, 기본값으로 즉시 실행 가능)
# ─────────────────────────────────────────────────────────────
SCENARIO_CODE      = "TS-0007"
SCENARIO_TITLE     = "Realtime 2,000 TPS Load Test"
SCENARIO_TITLE_KO  = "실시간 발송 2,000 TPS 부하 테스트"

TC_TITLES_KO = {
    "TC-0018": "워밍업 — 도구·시스템 정상 가동 확인",
    "TC-0019": "목표 TPS 달성 — achieved_tps 임계치 확인",
    "TC-0020": "성공률 — 메인 부하 구간 HTTP 성공률 확인",
    "TC-0021": "지연시간 — 메인 부하 구간 p95 지연 임계치 확인",
    "TC-0022": "DB 도달률 — 안정화 대기 후 msg_send_history 도달률 확인",
}

CHANNELS = ["SMS", "MMS", "RCS", "FAX", "EMAIL"]

# 워밍업
WARMUP_TPS              = int(os.getenv("WARMUP_TPS", "250"))
WARMUP_DURATION_SEC     = int(os.getenv("WARMUP_DURATION_SEC", "10"))
WARMUP_WORKERS          = int(os.getenv("WARMUP_WORKERS", "64"))
MIN_WARMUP_SUCCESS_RATE = float(os.getenv("MIN_WARMUP_SUCCESS_RATE", "0.99"))

# 메인 부하
MAIN_TPS            = int(os.getenv("MAIN_TPS", "2000"))
MAIN_DURATION_SEC   = int(os.getenv("MAIN_DURATION_SEC", "30"))
MAIN_WORKERS        = int(os.getenv("MAIN_WORKERS", "256"))     # 사용자 확정값
MIN_ACHIEVED_TPS    = float(os.getenv("MIN_ACHIEVED_TPS", "1900"))
MIN_SUCCESS_RATE    = float(os.getenv("MIN_SUCCESS_RATE", "0.99"))
MAX_P95_LATENCY_MS  = float(os.getenv("MAX_P95_LATENCY_MS", "1000"))

# 안정화 대기 + DB 도달률
STABILIZE_SEC        = int(os.getenv("STABILIZE_SEC", "60"))
MIN_DB_ARRIVAL_RATIO = float(os.getenv("MIN_DB_ARRIVAL_RATIO", "0.95"))


# ─────────────────────────────────────────────────────────────
# 리포트 저장 (tests/load/reports/ 에 저장 — Day 6 validation/reports/ 와 분리)
#
# 왜 conftest.save_json_report() 를 그대로 쓰지 않는가:
#   conftest.save_json_report() 는 REPORTS_DIR 이
#   tests/validation/reports/ 로 고정되어 있다. Day 7 부하 테스트 리포트는
#   "완전 격리" 결정에 따라 tests/load/reports/ 에 따로 쌓아야 하므로,
#   동일한 직렬화 로직(now_kst, timestamp_slug, _json_default)만 재사용하고
#   저장 경로만 다르게 한 얇은 래퍼를 둔다. (conftest.py 자체는 무수정)
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
# TC-0018: 워밍업
# ─────────────────────────────────────────────────────────────
def run_tc_0018() -> TestCaseResult:
    tc = TestCaseResult(
        "TC-0018",
        f"워밍업 ({WARMUP_TPS} TPS x {WARMUP_DURATION_SEC}s, "
        f"성공률 >= {MIN_WARMUP_SUCCESS_RATE:.0%})",
    )

    try:
        count = WARMUP_TPS * WARMUP_DURATION_SEC
        messages = build_realtime_messages(count=count, channels=CHANNELS)
        print_info(f"워밍업 메시지 {count}건 생성 (채널 라운드로빈: {CHANNELS})")

        injector = LoadInjector(max_workers=WARMUP_WORKERS)
        stats = injector.run_at_target_tps(
            messages, target_tps=WARMUP_TPS, duration_sec=WARMUP_DURATION_SEC,
        )
        print_info(f"워밍업 결과: {stats}")

        if stats["success_rate"] >= MIN_WARMUP_SUCCESS_RATE:
            tc.finish_pass(details=stats)
        else:
            tc.finish_fail(
                f"워밍업 성공률 {stats['success_rate']:.1%} "
                f"< 기준 {MIN_WARMUP_SUCCESS_RATE:.0%}",
                details=stats,
            )

    except Exception as e:
        tc.finish_fail(f"exception: {type(e).__name__}: {e}")

    return tc


# ─────────────────────────────────────────────────────────────
# 메인 부하 1회 실행 (TC-0019~0021 공유)
# ─────────────────────────────────────────────────────────────
def run_main_load() -> tuple:
    """
    메인 부하를 1회 실행하고 (stats, tx_ids) 를 반환한다.
    TC-0019/0020/0021 이 동일한 결과를 서로 다른 기준으로 채점하므로
    부하는 여기서 단 한 번만 건다.
    """
    count = MAIN_TPS * MAIN_DURATION_SEC
    messages = build_realtime_messages(count=count, channels=CHANNELS)
    tx_ids = [m["tx_id"] for m in messages]

    print_info(
        f"메인 부하 메시지 {count}건 생성 "
        f"(target_tps={MAIN_TPS}, duration={MAIN_DURATION_SEC}s, workers={MAIN_WORKERS})"
    )

    def _progress(sec_index, submitted, success, fail):
        print(f"    [{sec_index:>3}s]  submitted={submitted:>7}  "
              f"success={success:>7}  fail={fail:>5}")

    injector = LoadInjector(max_workers=MAIN_WORKERS)
    stats = injector.run_at_target_tps(
        messages,
        target_tps=MAIN_TPS,
        duration_sec=MAIN_DURATION_SEC,
        progress_callback=_progress,
    )
    print_info(f"메인 부하 결과: {stats}")

    return stats, tx_ids


def run_tc_0019(stats: dict) -> TestCaseResult:
    tc = TestCaseResult(
        "TC-0019",
        f"목표 TPS 달성 (achieved_tps >= {MIN_ACHIEVED_TPS})",
    )
    achieved = stats["achieved_tps"]
    if achieved >= MIN_ACHIEVED_TPS:
        tc.finish_pass(details={
            "achieved_tps": achieved,
            "target_tps": MAIN_TPS,
            "tps_achievement_ratio": stats.get("tps_achievement_ratio"),
        })
    else:
        tc.finish_fail(
            f"achieved_tps {achieved} < 기준 {MIN_ACHIEVED_TPS}",
            details={"achieved_tps": achieved, "target_tps": MAIN_TPS},
        )
    return tc


def run_tc_0020(stats: dict) -> TestCaseResult:
    tc = TestCaseResult(
        "TC-0020",
        f"성공률 (success_rate >= {MIN_SUCCESS_RATE:.0%})",
    )
    rate = stats["success_rate"]
    if rate >= MIN_SUCCESS_RATE:
        tc.finish_pass(details={
            "success_rate": rate, "fail": stats["fail"],
            "fail_reasons": stats.get("fail_reasons", {}),
        })
    else:
        tc.finish_fail(
            f"success_rate {rate:.1%} < 기준 {MIN_SUCCESS_RATE:.0%}",
            details={
                "success_rate": rate, "fail": stats["fail"],
                "fail_reasons": stats.get("fail_reasons", {}),
            },
        )
    return tc


def run_tc_0021(stats: dict) -> TestCaseResult:
    tc = TestCaseResult(
        "TC-0021",
        f"지연시간 (p95 <= {MAX_P95_LATENCY_MS:.0f}ms)",
    )
    p95 = stats["latency_p95_ms"]
    if p95 <= MAX_P95_LATENCY_MS:
        tc.finish_pass(details={
            "latency_p50_ms": stats["latency_p50_ms"],
            "latency_p95_ms": p95,
            "latency_p99_ms": stats["latency_p99_ms"],
            "latency_max_ms": stats["latency_max_ms"],
        })
    else:
        tc.finish_fail(
            f"p95 {p95}ms > 기준 {MAX_P95_LATENCY_MS:.0f}ms",
            details={
                "latency_p50_ms": stats["latency_p50_ms"],
                "latency_p95_ms": p95,
                "latency_p99_ms": stats["latency_p99_ms"],
            },
        )
    return tc


# ─────────────────────────────────────────────────────────────
# TC-0022: DB 도달률 (안정화 대기 후 확인)
# ─────────────────────────────────────────────────────────────
def run_tc_0022(tx_ids: list, db: DBChecker) -> TestCaseResult:
    tc = TestCaseResult(
        "TC-0022",
        f"DB 도달률 (안정화 {STABILIZE_SEC}s 후 >= {MIN_DB_ARRIVAL_RATIO:.0%})",
    )

    try:
        print_info(f"메인 부하 종료. DB 적재 안정화 대기 {STABILIZE_SEC}s...")
        import time
        time.sleep(STABILIZE_SEC)

        result = db.count_by_tx_ids(tx_ids)
        arrival_ratio = result["total_found"] / result["total_queried"] if result["total_queried"] else 0.0

        print_info(
            f"DB 도달: {result['total_found']}/{result['total_queried']} "
            f"({arrival_ratio:.1%})  by_status={result['by_status']}"
        )

        if arrival_ratio >= MIN_DB_ARRIVAL_RATIO:
            tc.finish_pass(details={
                "total_queried":  result["total_queried"],
                "total_found":    result["total_found"],
                "arrival_ratio":  round(arrival_ratio, 4),
                "by_status":      result["by_status"],
            })
        else:
            tc.finish_fail(
                f"DB 도달률 {arrival_ratio:.1%} < 기준 {MIN_DB_ARRIVAL_RATIO:.0%}",
                details={
                    "total_queried": result["total_queried"],
                    "total_found":   result["total_found"],
                    "missing":       result["missing"],
                    "by_status":     result["by_status"],
                },
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
        f"Config: WARMUP={WARMUP_TPS}TPSx{WARMUP_DURATION_SEC}s  "
        f"MAIN={MAIN_TPS}TPSx{MAIN_DURATION_SEC}s(workers={MAIN_WORKERS})  "
        f"STABILIZE={STABILIZE_SEC}s"
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

        # TC-0018: 워밍업 (실패 시 본 부하 진입 안 함 — 무의미한 측정 방지)
        print()
        banner(f"TC-0018 execution - {TC_TITLES_KO['TC-0018']}", char="-")
        tc18 = run_tc_0018()
        scenario.add_tc(tc18)

        if not tc18.passed:
            print_warn("워밍업 실패 — 메인 부하(TC-0019~0022)를 건너뜁니다.")
            scenario.finish()
            scenario.print_summary(title_ko=SCENARIO_TITLE_KO)
            report_path = save_load_report(SCENARIO_CODE, {"summary": scenario.to_dict()})
            print_info(f"JSON report saved: {report_path}")
            return 1

        # TC-0019~0021: 메인 부하 (1회 실행, 3개 기준으로 채점)
        print()
        banner("메인 부하 실행 (TC-0019~0021 공유)", char="-")
        main_stats, main_tx_ids = run_main_load()

        banner(f"TC-0019 execution - {TC_TITLES_KO['TC-0019']}", char="-")
        scenario.add_tc(run_tc_0019(main_stats))

        banner(f"TC-0020 execution - {TC_TITLES_KO['TC-0020']}", char="-")
        scenario.add_tc(run_tc_0020(main_stats))

        banner(f"TC-0021 execution - {TC_TITLES_KO['TC-0021']}", char="-")
        scenario.add_tc(run_tc_0021(main_stats))

        # TC-0022: DB 도달률 (안정화 대기 포함)
        print()
        banner(f"TC-0022 execution - {TC_TITLES_KO['TC-0022']}", char="-")
        scenario.add_tc(run_tc_0022(main_tx_ids, db))

    finally:
        scenario.finish()
        nifi.close()
        db.close()

    scenario.print_summary(title_ko=SCENARIO_TITLE_KO)

    report_path = save_load_report(SCENARIO_CODE, {
        "summary": scenario.to_dict(),
        "config": {
            "warmup_tps": WARMUP_TPS,
            "warmup_duration_sec": WARMUP_DURATION_SEC,
            "warmup_workers": WARMUP_WORKERS,
            "min_warmup_success_rate": MIN_WARMUP_SUCCESS_RATE,
            "main_tps": MAIN_TPS,
            "main_duration_sec": MAIN_DURATION_SEC,
            "main_workers": MAIN_WORKERS,
            "min_achieved_tps": MIN_ACHIEVED_TPS,
            "min_success_rate": MIN_SUCCESS_RATE,
            "max_p95_latency_ms": MAX_P95_LATENCY_MS,
            "stabilize_sec": STABILIZE_SEC,
            "min_db_arrival_ratio": MIN_DB_ARRIVAL_RATIO,
        },
    })
    print_info(f"JSON report saved: {report_path}")

    return 0 if scenario.fail_count == 0 else 1


if __name__ == "__main__":
    sys.exit(main())