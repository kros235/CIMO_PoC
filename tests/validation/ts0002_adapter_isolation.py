# tests/validation/ts0002_adapter_isolation.py
"""
Test Scenario 0002 - Adapter Failure Isolation.

Verifies that when one Adapter fails (docker stop), messages to OTHER channels
continue processing normally and queued messages recover when the Adapter restarts.

Test Cases:
    TC-0004: Isolation
        - Stop SMS Adapter
        - Inject 20 messages across all 5 channels (4 per channel)
        - Expect: MMS/RCS/FAX/EMAIL terminal (DELIVERED),
                  SMS stuck (DISPATCHING or no row yet)
    TC-0005: Recovery
        - Restart SMS Adapter
        - Wait for SMS backlog to drain
        - Expect: all 4 SMS messages reach terminal within 30s

How to run:
    python tests/validation/ts0002_adapter_isolation.py

Environment variables:
    TARGET_CHANNEL          : which Adapter to fail (default "SMS")
    INJECT_PER_CHANNEL      : messages per channel (default 4 -> 20 total)
    OTHER_WAIT_SEC          : max wait for non-target channels (default 60)
    RECOVERY_WAIT_SEC       : max wait after Adapter restart (default 60)
"""

import os
import sys
import time
from pathlib import Path

# Make conftest.py helpers importable when run standalone
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
from lib.adapter_controller import AdapterController


# ─────────────────────────────────────────────────────────────
# Config
# ─────────────────────────────────────────────────────────────
SCENARIO_CODE  = "TS-0002"
SCENARIO_TITLE = "Adapter Failure Isolation"

TARGET_CHANNEL     = os.getenv("TARGET_CHANNEL",      "SMS")
INJECT_PER_CHANNEL = int(os.getenv("INJECT_PER_CHANNEL", "4"))
OTHER_WAIT_SEC     = int(os.getenv("OTHER_WAIT_SEC",    "60"))
RECOVERY_WAIT_SEC  = int(os.getenv("RECOVERY_WAIT_SEC", "60"))

ALL_CHANNELS = ["SMS", "MMS", "RCS", "FAX", "EMAIL"]


# ─────────────────────────────────────────────────────────────
# Helper: build channel-appropriate receiver
# (same logic as TS-0001, duplicated here to keep scenarios self-contained)
# ─────────────────────────────────────────────────────────────
def build_receiver(channel: str, index: int) -> str:
    if channel == "EMAIL":
        return f"test{(20000 + index):06d}@test.local"
    return f"010{(20000000 + index):08d}"


# ─────────────────────────────────────────────────────────────
# TC-0004: Isolation - target channel stopped, others must still process
# ─────────────────────────────────────────────────────────────
def run_tc_0004(nifi: NiFiClient, db: DBChecker, ctrl: AdapterController) -> TestCaseResult:
    tc = TestCaseResult(
        "TC-0004",
        f"Isolation ({TARGET_CHANNEL} stopped, other channels keep running)",
    )

    # Track tx_ids by channel for per-channel verification
    tx_ids_by_channel: dict[str, list] = {ch: [] for ch in ALL_CHANNELS}
    all_tx_ids: list = []

    try:
        # Step 1: stop target Adapter
        print_info(f"Stopping {TARGET_CHANNEL} Adapter...")
        ctrl.stop(TARGET_CHANNEL)
        if ctrl.is_running(TARGET_CHANNEL):
            tc.finish_fail(f"{TARGET_CHANNEL} still running after stop")
            return tc
        print_pass(f"{TARGET_CHANNEL} Adapter is stopped")

        # Verify other Adapters still running
        status_map = ctrl.list_all()
        running_others = [ch for ch, st in status_map.items()
                          if ch != TARGET_CHANNEL and st == "running"]
        print_info(f"Other Adapters running: {running_others}")
        if len(running_others) < 4:
            print_warn(f"Expected 4 other Adapters running, found {len(running_others)}")

        # Step 2: inject INJECT_PER_CHANNEL messages per channel (20 total)
        total = INJECT_PER_CHANNEL * len(ALL_CHANNELS)
        print_info(f"Injecting {total} messages ({INJECT_PER_CHANNEL} per channel)...")

        for i in range(total):
            channel = ALL_CHANNELS[i % len(ALL_CHANNELS)]
            receiver = build_receiver(channel, i)
            tx_id = realtime_tx_id(sender_code="007")
            result = nifi.send_one(
                tx_id=tx_id,
                channel=channel,
                receiver=receiver,
                message_body=f"TS-0002 TC-0004 isolation test msg #{i+1}",
                source="TEST_TS0002_TC0004",
            )
            if not result["success"]:
                tc.finish_fail(
                    f"NiFi injection failed: HTTP {result['http_status']} - {result['error']}",
                    details={"failed_tx_id": tx_id, "channel": channel},
                )
                return tc
            tx_ids_by_channel[channel].append(tx_id)
            all_tx_ids.append(tx_id)

        print_info(f"Injection done: {len(all_tx_ids)} messages")

        # Step 3: wait for OTHER channels' messages to reach terminal
        other_tx_ids = [t for ch, ts in tx_ids_by_channel.items()
                        if ch != TARGET_CHANNEL for t in ts]
        print_info(
            f"Waiting up to {OTHER_WAIT_SEC}s for {len(other_tx_ids)} "
            f"non-{TARGET_CHANNEL} messages to reach terminal state..."
        )

        def _progress(elapsed_sec, found, terminal):
            print(f"    [{elapsed_sec:>5.1f}s]  found={found:>3d}/{len(other_tx_ids)}  terminal={terminal:>3d}")

        other_result = db.wait_until_processed(
            tx_ids=other_tx_ids,
            timeout_sec=OTHER_WAIT_SEC,
            poll_interval_sec=3.0,
            progress_callback=_progress,
        )

        # Step 4: check target channel DID NOT reach terminal yet
        target_tx_ids = tx_ids_by_channel[TARGET_CHANNEL]
        target_status = db.count_by_tx_ids(target_tx_ids)
        target_delivered = target_status["by_status"].get("DELIVERED", 0)

        # Evaluate
        other_terminal_ratio = other_result["terminal_count"] / len(other_tx_ids) if other_tx_ids else 1.0
        print_info(f"Other channels terminal: {other_result['terminal_count']}/{len(other_tx_ids)} "
                   f"({other_terminal_ratio:.0%})")
        print_info(f"Target ({TARGET_CHANNEL}) DELIVERED: {target_delivered}/{len(target_tx_ids)} "
                   f"(must be 0 for isolation to hold)")
        print_info(f"Target status breakdown: {target_status['by_status']}")

        # PASS conditions:
        #   (a) >=90% of non-target channel messages reached terminal
        #   (b) target channel has 0 DELIVERED (adapter stopped -> no delivery)
        issues = []
        if other_terminal_ratio < 0.90:
            issues.append(f"Other channels only {other_terminal_ratio:.0%} terminal (expected >=90%)")
        if target_delivered > 0:
            issues.append(
                f"Target {TARGET_CHANNEL} has {target_delivered} DELIVERED "
                f"despite Adapter being stopped (isolation broken)"
            )

        if not issues:
            tc.finish_pass(details={
                "target_channel":          TARGET_CHANNEL,
                "target_delivered":        target_delivered,
                "target_status_breakdown": target_status["by_status"],
                "other_terminal_ratio":    round(other_terminal_ratio, 3),
                "other_by_status":         other_result["by_status"],
                "all_tx_ids_count":        len(all_tx_ids),
            })
        else:
            tc.finish_fail("; ".join(issues), details={
                "target_delivered":     target_delivered,
                "other_terminal_ratio": round(other_terminal_ratio, 3),
                "other_by_status":      other_result["by_status"],
            })

    except Exception as e:
        tc.finish_fail(f"exception: {type(e).__name__}: {e}")
    finally:
        # Expose state for TC-0005
        tc.details["_target_tx_ids"] = tx_ids_by_channel[TARGET_CHANNEL]
        tc.details["_all_tx_ids"]    = all_tx_ids

    return tc


# ─────────────────────────────────────────────────────────────
# TC-0005: Recovery - restart target Adapter, backlog drains
# ─────────────────────────────────────────────────────────────
def run_tc_0005(target_tx_ids: list, nifi: NiFiClient, db: DBChecker,
                ctrl: AdapterController) -> TestCaseResult:
    tc = TestCaseResult(
        "TC-0005",
        f"Recovery ({TARGET_CHANNEL} restarted, backlog drains)",
    )

    try:
        if not target_tx_ids:
            tc.finish_fail("No target tx_ids from TC-0004 to verify recovery against")
            return tc

        # Step 1: restart target Adapter
        print_info(f"Starting {TARGET_CHANNEL} Adapter...")
        ctrl.start(TARGET_CHANNEL)
        # Small buffer for the Adapter to re-subscribe to Kafka
        time.sleep(3)
        if not ctrl.is_running(TARGET_CHANNEL):
            tc.finish_fail(f"{TARGET_CHANNEL} Adapter did not reach running state")
            return tc
        print_pass(f"{TARGET_CHANNEL} Adapter is running again")

        # Step 2: wait for backlog (target messages) to reach terminal
        print_info(
            f"Waiting up to {RECOVERY_WAIT_SEC}s for "
            f"{len(target_tx_ids)} {TARGET_CHANNEL} messages to drain..."
        )

        def _progress(elapsed_sec, found, terminal):
            print(f"    [{elapsed_sec:>5.1f}s]  found={found:>3d}/{len(target_tx_ids)}  terminal={terminal:>3d}")

        recovery_result = db.wait_until_processed(
            tx_ids=target_tx_ids,
            timeout_sec=RECOVERY_WAIT_SEC,
            poll_interval_sec=3.0,
            progress_callback=_progress,
        )

        # Strict recovery check: only count DELIVERED rows.
        #
        # NOTE: db_checker.TERMINAL_STATUSES includes DISPATCHING (to accommodate
        # 4xxxx -> DLQ routing in other scenarios). However for TC-0005 specifically,
        # DISPATCHING means the message is STILL waiting for the Adapter — not
        # recovered. A true recovery means the Adapter consumed the backlog and
        # produced a DELIVERED result. So we poll again using DELIVERED count only.
        print_info("Polling for DELIVERED count (strict recovery criterion)...")
        deadline = time.monotonic() + RECOVERY_WAIT_SEC
        delivered = 0
        last_status = {}
        while time.monotonic() < deadline:
            status = db.count_by_tx_ids(target_tx_ids)
            last_status = status["by_status"]
            delivered = last_status.get("DELIVERED", 0)
            elapsed = RECOVERY_WAIT_SEC - (deadline - time.monotonic())
            print(f"    [{elapsed:>5.1f}s]  DELIVERED={delivered:>2d}/{len(target_tx_ids)}  "
                  f"status={last_status}")
            if delivered >= len(target_tx_ids):
                break
            time.sleep(3.0)

        ratio = delivered / len(target_tx_ids) if target_tx_ids else 0.0
        elapsed_total = RECOVERY_WAIT_SEC - (deadline - time.monotonic())
        print_info(f"Recovery result (DELIVERED): {delivered}/{len(target_tx_ids)} "
                   f"({ratio:.0%}, elapsed={elapsed_total:.1f}s)")
        print_info(f"Final status breakdown: {last_status}")

        if ratio >= 0.95:
            tc.finish_pass(details={
                "target_channel":   TARGET_CHANNEL,
                "backlog_count":    len(target_tx_ids),
                "delivered_count":  delivered,
                "recovery_ratio":   round(ratio, 3),
                "recovery_elapsed": round(elapsed_total, 1),
                "final_status":     last_status,
            })
        else:
            tc.finish_fail(
                f"Only {delivered}/{len(target_tx_ids)} ({ratio:.0%}) delivered after recovery (expected >=95%)",
                details={
                    "backlog_count":   len(target_tx_ids),
                    "delivered_count": delivered,
                    "final_status":    last_status,
                    "recovery_elapsed": round(elapsed_total, 1),
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
    banner(f"{SCENARIO_CODE} - {SCENARIO_TITLE}")
    print_info(
        f"Config: TARGET_CHANNEL={TARGET_CHANNEL}  "
        f"INJECT_PER_CHANNEL={INJECT_PER_CHANNEL}  "
        f"OTHER_WAIT_SEC={OTHER_WAIT_SEC}  RECOVERY_WAIT_SEC={RECOVERY_WAIT_SEC}"
    )

    scenario = TestScenarioResult(SCENARIO_CODE, SCENARIO_TITLE)

    nifi = NiFiClient()
    db = DBChecker()
    ctrl = AdapterController()

    try:
        # Pre-flight checks
        if not nifi.health_check():
            print_fail("NiFi 8090 health check failed")
            return 2
        print_pass("NiFi 8090 reachable")

        db.connect()
        print_pass("PostgreSQL connection OK")

        # Verify ALL 5 Adapters running at start
        initial_status = ctrl.list_all()
        print_info(f"Initial Adapter status: {initial_status}")
        not_running = [ch for ch, st in initial_status.items() if st != "running"]
        if not_running:
            print_fail(f"Some Adapters not running before test: {not_running}")
            print_info("Start them with: docker compose -f poc/docker/docker-compose.adapters.yml up -d")
            return 2
        print_pass("All 5 Adapters running at start")

        # TC-0004: Isolation
        print()
        banner("TC-0004 execution", char="-")
        tc1 = run_tc_0004(nifi, db, ctrl)
        scenario.add_tc(tc1)

        # TC-0005: Recovery (always attempt, even if TC-0004 failed,
        # because the target Adapter is stopped and must be restored)
        print()
        banner("TC-0005 execution", char="-")
        target_tx_ids = tc1.details.get("_target_tx_ids", [])
        tc2 = run_tc_0005(target_tx_ids, nifi, db, ctrl)
        scenario.add_tc(tc2)

    finally:
        # Safety net: ensure target Adapter is restored no matter what
        try:
            if not ctrl.is_running(TARGET_CHANNEL):
                print_warn(f"Cleanup: starting {TARGET_CHANNEL} Adapter...")
                ctrl.start(TARGET_CHANNEL)
        except Exception as e:
            print_warn(f"Cleanup failed: {e}")

        scenario.finish()
        nifi.close()
        db.close()

    scenario.print_summary()

    # Clean up internal tracking keys before saving JSON
    for tc in scenario.tc_results:
        tc.details = {k: v for k, v in tc.details.items() if not k.startswith("_")}

    report_path = save_json_report(SCENARIO_CODE, {
        "summary": scenario.to_dict(),
        "config": {
            "target_channel":      TARGET_CHANNEL,
            "inject_per_channel":  INJECT_PER_CHANNEL,
            "other_wait_sec":      OTHER_WAIT_SEC,
            "recovery_wait_sec":   RECOVERY_WAIT_SEC,
        },
    })
    print_info(f"JSON report saved: {report_path}")

    return 0 if scenario.fail_count == 0 else 1


if __name__ == "__main__":
    sys.exit(main())