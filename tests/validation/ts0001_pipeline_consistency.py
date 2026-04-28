# tests/validation/ts0001_pipeline_consistency.py
"""
Test Scenario 0001 - Pipeline Consistency Verification.

Verifies that messages injected via NiFi (port 8090) are reliably persisted
to PostgreSQL msg_send_history with no loss.

Test Cases:
    TC-0001 Basic consistency   : N messages injected -> N rows in DB (>= 99%)
    TC-0002 Individual matching : each injected txId found in DB by tx_id lookup
    TC-0003 Channel distribution: 5 channels are evenly distributed (each within tolerance)

How to run:
    python tests/validation/ts0001_pipeline_consistency.py

Environment variables:
    INJECT_COUNT        : total messages to inject (default 100)
    WAIT_TIMEOUT_SEC    : max seconds to wait for DB terminal state (default 180)
    CHANNEL_TOLERANCE   : per-channel distribution tolerance vs even split (default 0.10 = +/- 10%)
"""

import os
import sys
import time
import random
from pathlib import Path

# Ensure conftest.py helpers are importable when run standalone
SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from conftest import (
    banner, print_pass, print_fail, print_info, print_warn,
    configure_logging, save_json_report,
    TestCaseResult, TestScenarioResult,
)
from lib.tx_generator import realtime_tx_id
from lib.nifi_client import NiFiClient
from lib.db_checker import DBChecker


# ─────────────────────────────────────────────────────────────
# Config
# ─────────────────────────────────────────────────────────────
SCENARIO_CODE  = "TS-0001"
SCENARIO_TITLE = "Pipeline Consistency Verification"
# ─── 추가: 한국어 제목 (콘솔 출력용) ───
SCENARIO_TITLE_KO = "파이프라인 정합성 검증"
TC_TITLES_KO = {
    "TC-0001": "기본 정합성 — 주입 건수 대비 적재 건수 일치",
    "TC-0002": "개별 txId 매칭 — 주입한 txId 100% DB 적재 확인",
    "TC-0003": "채널 분배 균등성 — 채널별 분배 편차 허용오차 내",
}
# ─── 추가 끝 ───

INJECT_COUNT      = int(os.getenv("INJECT_COUNT", "100"))
WAIT_TIMEOUT_SEC  = int(os.getenv("WAIT_TIMEOUT_SEC", "180"))
CHANNEL_TOLERANCE = float(os.getenv("CHANNEL_TOLERANCE", "0.10"))

# 5 channels in round-robin allocation for TC-0003
CHANNELS = ["SMS", "MMS", "RCS", "FAX", "EMAIL"]

# Pass/fail thresholds
MIN_CONSISTENCY_RATIO = 0.99   # TC-0001: at least 99% of injected must reach DB


# ─────────────────────────────────────────────────────────────
# Message preparation
# ─────────────────────────────────────────────────────────────
def prepare_test_messages(count: int) -> list:
    """
    Build `count` test messages with:
      - unique txId (35-digit, real-time code 03)
      - round-robin channel assignment (ensures even distribution input)
      - channel-appropriate receiver format per message:
          * EMAIL     -> email address (xxx@test.local)
          * otherwise -> mobile phone number (010xxxxxxxx)
    """
    messages = []
    for i in range(count):
        channel = CHANNELS[i % len(CHANNELS)]

        # CHANGE: receiver format must match channel semantics.
        # EMAIL Adapter validates email format and returns 40001 on mismatch,
        # which causes DLQ routing instead of DELIVERED terminal state.
        if channel == "EMAIL":
            receiver = f"test{(10000 + i):06d}@test.local"
        else:
            receiver = f"010{(10000000 + i):08d}"

        tx_id = realtime_tx_id(sender_code="007")
        messages.append({
            "tx_id":        tx_id,
            "channel":      channel,
            "receiver":     receiver,
            "message_body": f"TS-0001 consistency test message #{i+1}",
            "source":       "TEST_TS0001",
        })
    return messages


# ─────────────────────────────────────────────────────────────
# TC-0001 : Basic consistency (N injected -> N in DB terminal state)
# ─────────────────────────────────────────────────────────────
def run_tc_0001(messages: list, db: DBChecker, nifi: NiFiClient) -> TestCaseResult:
    tc = TestCaseResult("TC-0001", f"Basic consistency ({INJECT_COUNT} injected)")
    try:
        # Step 1: inject all messages via NiFi (sequential)
        print_info(f"Injecting {len(messages)} messages via NiFi...")
        # Make a shallow copy per message for send_bulk (it pops keys)
        inject_payload = [dict(m) for m in messages]
        inject_result = nifi.send_bulk(inject_payload, progress_every=25)

        injected_ok = inject_result["success_count"]
        print_info(
            f"Injection done: {injected_ok}/{inject_result['total']} OK "
            f"(avg {inject_result['avg_elapsed_ms']}ms)"
        )

        if inject_result["fail_count"] > 0:
            tc.finish_fail(
                f"NiFi injection had {inject_result['fail_count']} failures",
                details={"inject_result": inject_result},
            )
            return tc

        # Step 2: wait until all reach terminal state in DB
        print_info(f"Waiting up to {WAIT_TIMEOUT_SEC}s for DB terminal state...")
        tx_ids = [m["tx_id"] for m in messages]

        def _progress(elapsed_sec, found, terminal):
            print(f"    [{elapsed_sec:>5.1f}s]  found={found:>4d}/{len(tx_ids)}  terminal={terminal:>4d}")

        db_result = db.wait_until_processed(
            tx_ids=tx_ids,
            timeout_sec=WAIT_TIMEOUT_SEC,
            poll_interval_sec=3.0,
            progress_callback=_progress,
        )

        # Step 3: evaluate consistency ratio
        terminal = db_result["terminal_count"]
        ratio = terminal / len(tx_ids) if tx_ids else 0.0
        print_info(
            f"DB result: terminal={terminal}/{len(tx_ids)} "
            f"(ratio={ratio:.1%}, timed_out={db_result['timed_out']})"
        )
        print_info(f"  status breakdown: {db_result['by_status']}")

        if ratio >= MIN_CONSISTENCY_RATIO:
            tc.finish_pass(details={
                "inject_ok":       injected_ok,
                "db_terminal":     terminal,
                "consistency":     round(ratio, 4),
                "by_status":       db_result["by_status"],
                "elapsed_sec":     db_result["elapsed_sec"],
                "timed_out":       db_result["timed_out"],
                "missing_samples": db_result["missing_tx_ids"],
            })
        else:
            tc.finish_fail(
                f"consistency {ratio:.1%} < threshold {MIN_CONSISTENCY_RATIO:.0%}",
                details={
                    "inject_ok":       injected_ok,
                    "db_terminal":     terminal,
                    "consistency":     round(ratio, 4),
                    "by_status":       db_result["by_status"],
                    "elapsed_sec":     db_result["elapsed_sec"],
                    "timed_out":       db_result["timed_out"],
                    "missing_samples": db_result["missing_tx_ids"],
                },
            )

    except Exception as e:
        tc.finish_fail(f"exception: {type(e).__name__}: {e}")

    return tc


# ─────────────────────────────────────────────────────────────
# TC-0002 : Individual matching (each injected txId found in DB)
# ─────────────────────────────────────────────────────────────
def run_tc_0002(messages: list, db: DBChecker) -> TestCaseResult:
    tc = TestCaseResult("TC-0002", "Individual txId matching (100% of injected found in DB)")
    try:
        tx_ids = [m["tx_id"] for m in messages]
        result = db.count_by_tx_ids(tx_ids)

        found   = result["total_found"]
        missing = result["missing"]
        total   = result["total_queried"]

        print_info(f"Injected: {total}  Found in DB: {found}  Missing: {missing}")

        if missing == 0:
            tc.finish_pass(details={
                "total_injected": total,
                "total_found":    found,
                "missing":        0,
            })
        else:
            tc.finish_fail(
                f"{missing} of {total} txIds not found in DB",
                details={
                    "total_injected":   total,
                    "total_found":      found,
                    "missing":          missing,
                    "missing_samples":  result["missing_tx_ids"],
                },
            )

    except Exception as e:
        tc.finish_fail(f"exception: {type(e).__name__}: {e}")

    return tc


# ─────────────────────────────────────────────────────────────
# TC-0003 : Channel distribution (each channel within tolerance)
# ─────────────────────────────────────────────────────────────
def run_tc_0003(messages: list, db: DBChecker) -> TestCaseResult:
    tc = TestCaseResult(
        "TC-0003",
        f"Channel distribution evenness (tolerance +/-{int(CHANNEL_TOLERANCE * 100)}%)",
    )
    try:
        tx_ids = [m["tx_id"] for m in messages]
        by_channel = db.count_by_channel(tx_ids)
        total_in_db = sum(by_channel.values())

        # Expected share per channel (round-robin over injection, so equal)
        expected_share = 1.0 / len(CHANNELS)
        tolerance = CHANNEL_TOLERANCE

        print_info(f"Distribution (total {total_in_db} rows):")
        violations = []
        for ch in CHANNELS:
            count = by_channel.get(ch, 0)
            share = (count / total_in_db) if total_in_db > 0 else 0.0
            deviation = abs(share - expected_share)
            marker = "OK" if deviation <= tolerance else "OUT-OF-BAND"
            print(f"    {ch:6s}  count={count:>4d}  share={share:.1%}  ({marker})")
            if deviation > tolerance:
                violations.append({
                    "channel":      ch,
                    "count":        count,
                    "share":        round(share, 4),
                    "expected":     round(expected_share, 4),
                    "deviation":    round(deviation, 4),
                })

        if not violations:
            tc.finish_pass(details={
                "total_in_db": total_in_db,
                "by_channel":  by_channel,
                "tolerance":   tolerance,
            })
        else:
            tc.finish_fail(
                f"{len(violations)} channel(s) outside tolerance {tolerance:.0%}",
                details={
                    "total_in_db": total_in_db,
                    "by_channel":  by_channel,
                    "tolerance":   tolerance,
                    "violations":  violations,
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
    print_info(f"Config: INJECT_COUNT={INJECT_COUNT}  WAIT_TIMEOUT_SEC={WAIT_TIMEOUT_SEC}  CHANNEL_TOLERANCE={CHANNEL_TOLERANCE}")

    scenario = TestScenarioResult(SCENARIO_CODE, SCENARIO_TITLE)

    # Pre-flight checks
    nifi = NiFiClient()
    db = DBChecker()
    try:
        if not nifi.health_check():
            print_fail("NiFi 8090 health check failed. Is Phase 0 (deploy_flow.py) applied?")
            return 2
        print_pass("NiFi 8090 reachable")

        db.connect()
        print_pass("PostgreSQL connection OK")

        # Prepare message set ONCE, shared across TC-0001/0002/0003 (same batch)
        messages = prepare_test_messages(INJECT_COUNT)
        print_info(f"Prepared {len(messages)} messages (round-robin across {CHANNELS})")

        # TC-0001 injects + verifies ratio
        print()
        banner(f"TC-0001 execution - {TC_TITLES_KO['TC-0001']}", char="-")
        tc1 = run_tc_0001(messages, db, nifi)
        scenario.add_tc(tc1)

        # TC-0002 only reads (no new injection)
        print()
        banner(f"TC-0002 execution - {TC_TITLES_KO['TC-0002']}", char="-")
        tc2 = run_tc_0002(messages, db)
        scenario.add_tc(tc2)

        # TC-0003 only reads
        print()
        banner(f"TC-0003 execution - {TC_TITLES_KO['TC-0003']}", char="-")
        tc3 = run_tc_0003(messages, db)
        scenario.add_tc(tc3)

    finally:
        scenario.finish()
        nifi.close()
        db.close()

    # Print summary
    scenario.print_summary(title_ko=SCENARIO_TITLE_KO)

    # Save JSON report
    report_path = save_json_report(SCENARIO_CODE, {
        "summary": scenario.to_dict(),
        "config": {
            "inject_count":       INJECT_COUNT,
            "wait_timeout_sec":   WAIT_TIMEOUT_SEC,
            "channel_tolerance":  CHANNEL_TOLERANCE,
            "min_consistency":    MIN_CONSISTENCY_RATIO,
            "channels":           CHANNELS,
        },
    })
    print_info(f"JSON report saved: {report_path.relative_to(Path.cwd()) if report_path.is_relative_to(Path.cwd()) else report_path}")

    # Exit code: 0 if all pass, 1 if any fail
    return 0 if scenario.fail_count == 0 else 1


if __name__ == "__main__":
    sys.exit(main())