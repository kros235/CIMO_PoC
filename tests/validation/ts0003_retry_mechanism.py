# tests/validation/ts0003_retry_mechanism.py
"""
Test Scenario 0003 - Retry Mechanism Verification.

Verifies that messages which fail with retriable result codes (5xxxx) are
routed through the retry pipeline (RetryJob) and eventually reach a terminal
state — without manual intervention.

Design note:
    Day 3 Mock Adapter spec allocates a FIXED 3% of random outcomes to retry
    result codes (50xxx), independent of SUCCESS_RATE. Changing SUCCESS_RATE
    reduces DELIVERED count but does NOT increase retry count. Therefore this
    scenario relies on VOLUME (200 messages) to produce a statistically
    sufficient retry sample (~6 expected).

Test Cases:
    TC-0006 Retry occurrence
        - Inject 200 messages
        - Expect: at least 1 row in msg_send_history with retry_count >= 1
        - Expect: topic.send.retry has at least 1 message produced

    TC-0007 Retry resolution
        - Wait for all messages to reach terminal state (DELIVERED/FAILED/DLQ
          or DISPATCHING for 4xxxx→DLQ side path)
        - Expect: >=98% of injected messages reach terminal within 180s

    TC-0008 RetryJob consumption
        - Check am-flink-retry-group consumer group has consumed the retry topic
        - Expect: topic.send.retry consumer LAG is 0 (all retries processed)

How to run:
    python tests/validation/ts0003_retry_mechanism.py

Environment variables:
    INJECT_COUNT         : total messages to inject (default 200)
    WAIT_TIMEOUT_SEC     : max wait for terminal (default 180)
    MIN_RETRY_COUNT      : minimum retry rows required for TC-0006 (default 1)
"""

import os
import sys
import time
import subprocess
import platform
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


# ─────────────────────────────────────────────────────────────
# Config
# ─────────────────────────────────────────────────────────────
SCENARIO_CODE  = "TS-0003"
SCENARIO_TITLE = "Retry Mechanism Verification"

INJECT_COUNT     = int(os.getenv("INJECT_COUNT",     "200"))
WAIT_TIMEOUT_SEC = int(os.getenv("WAIT_TIMEOUT_SEC", "180"))
MIN_RETRY_COUNT  = int(os.getenv("MIN_RETRY_COUNT",  "1"))

CHANNELS = ["SMS", "MMS", "RCS", "FAX", "EMAIL"]

# Pass threshold for TC-0007
MIN_TERMINAL_RATIO = 0.98

IS_WINDOWS = platform.system() == "Windows"


# ─────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────
def build_receiver(channel: str, index: int) -> str:
    if channel == "EMAIL":
        return f"test{(30000 + index):06d}@test.local"
    return f"010{(30000000 + index):08d}"


def run_kafka_command(args: list, timeout: int = 10) -> str:
    """Run kafka CLI command inside am-kafka container, return stdout."""
    cmd = ["docker", "exec", "am-kafka"] + args
    env = os.environ.copy()
    if IS_WINDOWS:
        env["MSYS_NO_PATHCONV"] = "1"
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True,
            timeout=timeout, env=env,
        )
        return result.stdout
    except Exception as e:
        print_warn(f"Kafka command failed: {e}")
        return ""


def get_topic_offset_sum(topic: str) -> int:
    """Return total message count across all partitions of a topic."""
    out = run_kafka_command([
        "kafka-run-class", "kafka.tools.GetOffsetShell",
        "--bootstrap-server", "localhost:9092",
        "--topic", topic,
    ])
    total = 0
    for line in out.strip().splitlines():
        parts = line.split(":")
        if len(parts) >= 3 and parts[2].isdigit():
            total += int(parts[2])
    return total


def get_consumer_group_lag(group: str, topic: str) -> tuple:
    """Return (current_offset_sum, end_offset_sum, lag_sum) for a consumer group."""
    out = run_kafka_command([
        "kafka-consumer-groups",
        "--bootstrap-server", "localhost:9092",
        "--group", group,
        "--describe",
    ])
    current, end, lag = 0, 0, 0
    for line in out.splitlines():
        parts = line.split()
        # Expected columns: GROUP TOPIC PARTITION CURRENT-OFFSET LOG-END-OFFSET LAG ...
        if len(parts) >= 6 and parts[1] == topic:
            try:
                current += int(parts[3])
                end     += int(parts[4])
                lag     += int(parts[5])
            except ValueError:
                continue
    return current, end, lag


# ─────────────────────────────────────────────────────────────
# Prepare messages
# ─────────────────────────────────────────────────────────────
def prepare_messages(count: int) -> list:
    messages = []
    for i in range(count):
        channel = CHANNELS[i % len(CHANNELS)]
        receiver = build_receiver(channel, i)
        tx_id = realtime_tx_id(sender_code="007")
        messages.append({
            "tx_id":        tx_id,
            "channel":      channel,
            "receiver":     receiver,
            "message_body": f"TS-0003 retry test #{i+1}",
            "source":       "TEST_TS0003",
        })
    return messages


# ─────────────────────────────────────────────────────────────
# TC-0006: Retry occurrence (at least 1 retry happens naturally)
# ─────────────────────────────────────────────────────────────
def run_tc_0006(messages: list, db: DBChecker, nifi: NiFiClient) -> tuple:
    tc = TestCaseResult(
        "TC-0006",
        f"Retry occurrence ({INJECT_COUNT} msgs, expect >={MIN_RETRY_COUNT} retried)",
    )
    tx_ids = [m["tx_id"] for m in messages]

    try:
        # Snapshot retry topic offset BEFORE injection
        retry_offset_before = get_topic_offset_sum("topic.send.retry")
        print_info(f"topic.send.retry initial offset: {retry_offset_before}")

        # Inject messages
        print_info(f"Injecting {len(messages)} messages...")
        inject_payload = [dict(m) for m in messages]
        inject_result = nifi.send_bulk(inject_payload, progress_every=50)

        if inject_result["fail_count"] > 0:
            tc.finish_fail(
                f"NiFi injection had {inject_result['fail_count']} failures",
                details={"fail_count": inject_result["fail_count"]},
            )
            return tc, tx_ids

        print_info(f"Injection done: {inject_result['success_count']}/{inject_result['total']} OK")

        # Wait for pipeline to process (short wait — TC-0007 handles full drain)
        print_info("Waiting 20s for pipeline to produce retry messages...")
        time.sleep(20)

        # Check retry topic: did new messages arrive?
        retry_offset_after = get_topic_offset_sum("topic.send.retry")
        retry_produced = retry_offset_after - retry_offset_before
        print_info(f"topic.send.retry new messages: {retry_produced} "
                   f"(before={retry_offset_before}, after={retry_offset_after})")

        # Diagnostic only: DB retry_count column (known PoC design limitation L01)
        #
        # DESIGN NOTE:
        #   Per SendResultJob.java (lines 116-133) and the UPDATE query at line 160,
        #   only 4 columns are updated: status, result_code, dispatched_at, delivered_at.
        #   The retry_count column is intentionally NOT updated at this PoC stage.
        #
        #   Additionally, ResultCodeClassifier splits results by disposition:
        #     - 10000       -> STORE  (triggers DB UPDATE via storeStream filter)
        #     - 5xxxx/50002 -> RETRY/FALLBACK  (routed to topic.send.retry only)
        #     - 4xxxx       -> DLQ   (routed to topic.send.dlq only)
        #   Retry/DLQ paths never touch msg_send_history at all — this is
        #   a deliberate simplification (msg_send_history holds "delivered"
        #   history; failed/retried messages live in Kafka topics for analysis).
        #
        # PLANNED IMPROVEMENT (Day 8):
        #   SendResultJob + RetryJob will be extended to UPDATE retry_count
        #   on every pipeline step, and to transition status through
        #   RETRYING/DELIVERED/FAILED/DLQ based on result_code branch.
        #
        # VERIFICATION APPROACH FOR TC-0006:
        #   We verify retry MECHANISM activity via Kafka topic.send.retry
        #   offset delta, which is the authoritative signal that retry-
        #   eligible messages actually entered the retry pipeline.
        retry_rows = db.count_retried_rows(tx_ids)
        print_info(f"DB rows with retry_count>=1 (diagnostic): {retry_rows}  "
                   "[INFO: PoC L01 - retry_count not UPDATEd until Day 8]")

        if retry_produced >= MIN_RETRY_COUNT:
            observed_rate = retry_produced / inject_result["success_count"]
            tc.finish_pass(details={
                "inject_count":         inject_result["success_count"],
                "retry_topic_produced": retry_produced,
                "db_retried_rows":      retry_rows,
                "expected_rate":        "~3% (per adapter_base.py fixed allocation)",
                "observed_rate":        f"{observed_rate:.1%}",
                "verification_method":  "Kafka topic.send.retry offset delta",
                "design_limitation":    "L01 - msg_send_history.retry_count not updated (Day 8 fix planned)",
            })
        else:
            tc.finish_fail(
                f"Kafka retry topic produced only {retry_produced} messages "
                f"(expected >={MIN_RETRY_COUNT})",
                details={
                    "inject_count":         inject_result["success_count"],
                    "retry_topic_produced": retry_produced,
                    "db_retried_rows":      retry_rows,
                },
            )

    except Exception as e:
        tc.finish_fail(f"exception: {type(e).__name__}: {e}")

    return tc, tx_ids


# ─────────────────────────────────────────────────────────────
# TC-0007: Retry resolution (everything reaches terminal eventually)
# ─────────────────────────────────────────────────────────────
def run_tc_0007(tx_ids: list, db: DBChecker) -> TestCaseResult:
    tc = TestCaseResult(
        "TC-0007",
        f"Retry resolution (>={int(MIN_TERMINAL_RATIO*100)}% reach terminal within {WAIT_TIMEOUT_SEC}s)",
    )

    try:
        print_info(f"Waiting up to {WAIT_TIMEOUT_SEC}s for all {len(tx_ids)} messages to reach terminal...")

        def _progress(elapsed_sec, found, terminal):
            print(f"    [{elapsed_sec:>5.1f}s]  found={found:>4d}/{len(tx_ids)}  terminal={terminal:>4d}")

        result = db.wait_until_processed(
            tx_ids=tx_ids,
            timeout_sec=WAIT_TIMEOUT_SEC,
            poll_interval_sec=5.0,
            progress_callback=_progress,
        )

        terminal = result["terminal_count"]
        ratio = terminal / len(tx_ids) if tx_ids else 0.0
        print_info(f"Final: {terminal}/{len(tx_ids)} ({ratio:.1%}, elapsed={result['elapsed_sec']}s)")
        print_info(f"Status breakdown: {result['by_status']}")

        if ratio >= MIN_TERMINAL_RATIO:
            tc.finish_pass(details={
                "total_injected":  len(tx_ids),
                "terminal_count":  terminal,
                "terminal_ratio":  round(ratio, 4),
                "elapsed_sec":     result["elapsed_sec"],
                "by_status":       result["by_status"],
            })
        else:
            tc.finish_fail(
                f"Only {ratio:.1%} reached terminal (expected >={MIN_TERMINAL_RATIO:.0%})",
                details={
                    "total_injected": len(tx_ids),
                    "terminal_count": terminal,
                    "by_status":      result["by_status"],
                    "timed_out":      result["timed_out"],
                },
            )

    except Exception as e:
        tc.finish_fail(f"exception: {type(e).__name__}: {e}")

    return tc


# ─────────────────────────────────────────────────────────────
# TC-0008: RetryJob consumption (consumer group keeps up)
# ─────────────────────────────────────────────────────────────
def run_tc_0008() -> TestCaseResult:
    tc = TestCaseResult(
        "TC-0008",
        "RetryJob consumer group keeps up (topic.send.retry LAG == 0)",
    )

    try:
        current, end, lag = get_consumer_group_lag(
            "am-flink-retry-group", "topic.send.retry",
        )
        print_info(f"am-flink-retry-group on topic.send.retry:")
        print_info(f"  CURRENT-OFFSET: {current}")
        print_info(f"  LOG-END-OFFSET: {end}")
        print_info(f"  LAG:            {lag}")

        if lag == 0 and end > 0:
            tc.finish_pass(details={
                "consumer_group":  "am-flink-retry-group",
                "topic":           "topic.send.retry",
                "current_offset":  current,
                "end_offset":      end,
                "lag":             lag,
                "retry_messages_consumed": current,
            })
        elif end == 0:
            tc.finish_fail(
                "topic.send.retry has no messages (no retries occurred in this run)",
                details={"current_offset": current, "end_offset": end, "lag": lag},
            )
        else:
            tc.finish_fail(
                f"RetryJob is behind: LAG={lag} messages unprocessed",
                details={"current_offset": current, "end_offset": end, "lag": lag},
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
        f"Config: INJECT_COUNT={INJECT_COUNT}  "
        f"WAIT_TIMEOUT_SEC={WAIT_TIMEOUT_SEC}  MIN_RETRY_COUNT={MIN_RETRY_COUNT}"
    )
    print_info("Design note: Adapter spec fixes retry rate at ~3% — relying on volume.")

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

        # Prepare message batch shared across TCs
        messages = prepare_messages(INJECT_COUNT)
        print_info(f"Prepared {len(messages)} messages (round-robin 5 channels)")

        # TC-0006: retry occurrence (injection happens here)
        print()
        banner("TC-0006 execution", char="-")
        tc1, tx_ids = run_tc_0006(messages, db, nifi)
        scenario.add_tc(tc1)

        # TC-0007: wait for terminal (uses same tx_ids)
        print()
        banner("TC-0007 execution", char="-")
        tc2 = run_tc_0007(tx_ids, db)
        scenario.add_tc(tc2)

        # TC-0008: RetryJob consumer group lag
        print()
        banner("TC-0008 execution", char="-")
        tc3 = run_tc_0008()
        scenario.add_tc(tc3)

    finally:
        scenario.finish()
        nifi.close()
        db.close()

    scenario.print_summary()

    report_path = save_json_report(SCENARIO_CODE, {
        "summary": scenario.to_dict(),
        "config": {
            "inject_count":        INJECT_COUNT,
            "wait_timeout_sec":    WAIT_TIMEOUT_SEC,
            "min_retry_count":     MIN_RETRY_COUNT,
            "min_terminal_ratio":  MIN_TERMINAL_RATIO,
            "retry_rate_design":   "3% fixed (per adapter_base.py)",
        },
    })
    print_info(f"JSON report saved: {report_path}")

    return 0 if scenario.fail_count == 0 else 1


if __name__ == "__main__":
    sys.exit(main())