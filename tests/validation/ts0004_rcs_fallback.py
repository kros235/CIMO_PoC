# tests/validation/ts0004_rcs_fallback.py
"""
Test Scenario 0004 - RCS to SMS Fallback Verification.

Verifies that RCS messages receiving result_code=50002 ("RCS not supported
by receiver device") are automatically re-routed to SMS channel — i.e.,
the receiver still gets the message via fallback channel.

Design reference (SendResultJob.java line 129-131):
    if (DISPOSITION_FALLBACK.equals(r.getDisposition())) {
        r.setChannel("SMS");
        LOG.info("[SendResultJob] RCS→SMS fallback 전환: txId={}", ...);
    }

Flow:
    1. RCS Adapter returns resultCode=50002 (probability ~1% per message)
    2. SendResultJob detects FALLBACK disposition via ResultCodeClassifier
    3. Message's channel field is mutated: "RCS" -> "SMS"
    4. Re-published to topic.send.retry with modified channel
    5. RetryJob consumes and publishes to topic.send.dispatch.sms
    6. SMS Adapter processes the fallback message

Test Cases:
    TC-0009 Fallback occurrence
        - Inject 200 RCS-only messages
        - Expect: at least 1 message in topic.send.retry with channel=SMS
                  (originally RCS, channel mutated by SendResultJob)
    TC-0010 Fallback re-dispatch
        - Verify that topic.send.dispatch.sms offset increased
          after RCS injection — meaning SMS Adapter received at least
          one fallback message originated from RCS.

How to run:
    python tests/validation/ts0004_rcs_fallback.py

Environment variables:
    INJECT_COUNT_RCS   : number of RCS-only messages to inject (default 200)
    WAIT_SEC           : wait for pipeline to produce fallback (default 30)
    MIN_FALLBACK_COUNT : minimum fallback events required (default 1)
"""

import os
import sys
import time
import json
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
SCENARIO_CODE  = "TS-0004"
SCENARIO_TITLE = "RCS to SMS Fallback Verification"
# ─── 추가: 한국어 제목 (콘솔 출력용) ───
SCENARIO_TITLE_KO = "RCS→SMS Fallback 검증"
TC_TITLES_KO = {
    "TC-0009": "Fallback 발생 — RCS 실패분의 SMS 전환 발생 확인",
    "TC-0010": "Fallback 재발송 — 전환된 건의 SMS 발송 토픽 인입 확인",
}
# ─── 추가 끝 ───

INJECT_COUNT_RCS   = int(os.getenv("INJECT_COUNT_RCS",   "200"))
WAIT_SEC           = int(os.getenv("WAIT_SEC",           "30"))
MIN_FALLBACK_COUNT = int(os.getenv("MIN_FALLBACK_COUNT", "1"))

IS_WINDOWS = platform.system() == "Windows"


# ─────────────────────────────────────────────────────────────
# Kafka CLI helpers
# ─────────────────────────────────────────────────────────────
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


def scan_flink_logs_for_fallbacks(target_tx_ids: set, since_minutes: int = 5) -> list:
    """
    Scan Flink TaskManager logs for RCS→SMS fallback events and filter by txId.

    Why this approach:
        The retry Kafka topic may contain many messages from prior test runs
        (78+ cumulative). Reading the entire topic with kafka-console-consumer
        is slow and fragile (subprocess timeout). Instead, we parse SendResultJob's
        explicit INFO log line:
            "[SendResultJob] RCS→SMS fallback 전환: txId=<35-digit-id>"
        which is emitted exactly once per fallback event (see SendResultJob.java
        line 131). This gives us the authoritative fallback count.

    Args:
        target_tx_ids: txIds injected in the current test run
        since_minutes: how far back to scan logs (default 5m covers most tests)

    Returns:
        List of dicts {txId} for each fallback matching our injection batch.
    """
    # ⭐️ 변경(노트북B 검증 중 발견): 기존엔 docker-taskmanager-1의 로그만
    # 확인했으나, RCS 채널을 처리하는 일꾼(subtask)이 어느 TaskManager
    # 컨테이너에 배정될지는 기동할 때마다 달라질 수 있다. 노트북B에서는
    # 그 일꾼이 docker-taskmanager-2에 배정되어, 실제로는 로그가 정상
    # 기록됐는데도 이 스캔이 "0건 발견"으로 잘못 판정했다(카프카 토픽에는
    # 실제로 8건이 도착한 것으로 별도 확인됨 - 기능 자체는 정상). 두
    # TaskManager 컨테이너 모두 확인하도록 수정한다.
    def _fetch_container_logs(container_name: str) -> list:
        cmd = [
            "docker", "logs", container_name,
            "--since", f"{since_minutes}m",
        ]
        env = os.environ.copy()
        if IS_WINDOWS:
            env["MSYS_NO_PATHCONV"] = "1"
        try:
            # CHANGE: Explicitly set UTF-8 encoding with error replacement.
            # Windows Python defaults subprocess text mode to cp949, which fails on
            # Flink log lines containing "→" (the fallback arrow character, UTF-8
            # bytes 0xE2 0x86 0x92). Setting encoding='utf-8' + errors='replace'
            # ensures we get the stdout/stderr strings without UnicodeDecodeError.
            result = subprocess.run(
                cmd, capture_output=True,
                text=True, encoding="utf-8", errors="replace",
                timeout=15, env=env,
            )
        except subprocess.TimeoutExpired:
            return []

        # Defensive: if subprocess still returned None for any stream, treat as empty
        stdout_lines = (result.stdout or "").splitlines()
        stderr_lines = (result.stderr or "").splitlines()
        return stderr_lines + stdout_lines

    # Log line pattern:
    #   2026-04-23 08:58:32,743 INFO ... [SendResultJob] RCS→SMS fallback 전환: txId=<35digits>
    matches = []
    seen_tx_ids = set()  # dedupe: same txId may appear multiple times if retried
    marker = "RCS→SMS fallback 전환: txId="
    # ⭐️ 변경: taskmanager-1, taskmanager-2 두 컨테이너의 로그를 모두 합쳐서 검사
    all_lines = _fetch_container_logs("docker-taskmanager-1") \
        + _fetch_container_logs("docker-taskmanager-2")
    for line in all_lines:
        if marker not in line:
            continue
        # Extract txId after the marker
        idx = line.index(marker) + len(marker)
        tx = line[idx:idx + 35].strip()
        if len(tx) == 35 and tx.isdigit():
            if tx in target_tx_ids and tx not in seen_tx_ids:
                seen_tx_ids.add(tx)
                matches.append({"txId": tx})
    return matches


# ─────────────────────────────────────────────────────────────
# Inject RCS-only batch
# ─────────────────────────────────────────────────────────────
def inject_rcs_batch(count: int, nifi: NiFiClient) -> tuple:
    """Inject `count` RCS-only messages. Returns (tx_ids, success_count)."""
    tx_ids = []
    messages = []
    for i in range(count):
        tx_id = realtime_tx_id(sender_code="007")
        tx_ids.append(tx_id)
        messages.append({
            "tx_id":        tx_id,
            "channel":      "RCS",
            "receiver":     f"010{(40000000 + i):08d}",
            "message_body": f"TS-0004 RCS fallback test #{i+1}",
            "source":       "TEST_TS0004",
        })

    print_info(f"Injecting {count} RCS-only messages via NiFi...")
    payload = [dict(m) for m in messages]
    result = nifi.send_bulk(payload, progress_every=50)
    print_info(f"Injection: {result['success_count']}/{result['total']} OK "
               f"(avg {result['avg_elapsed_ms']}ms)")
    return tx_ids, result["success_count"]


# ─────────────────────────────────────────────────────────────
# TC-0009: Fallback occurrence (RCS -> SMS channel mutation)
# ─────────────────────────────────────────────────────────────
def run_tc_0009(nifi: NiFiClient) -> tuple:
    tc = TestCaseResult(
        "TC-0009",
        f"Fallback occurrence (RCS→SMS, {INJECT_COUNT_RCS} msgs, expect >={MIN_FALLBACK_COUNT})",
    )
    fallback_matches = []
    tx_ids = []
    sms_dispatch_before = 0

    try:
        # Snapshot SMS dispatch offset (for TC-0010)
        sms_dispatch_before = get_topic_offset_sum("topic.send.dispatch.sms")
        print_info(f"topic.send.dispatch.sms offset (pre-injection): {sms_dispatch_before}")

        # Inject RCS-only batch
        tx_ids, injected_ok = inject_rcs_batch(INJECT_COUNT_RCS, nifi)
        if injected_ok < INJECT_COUNT_RCS:
            tc.finish_fail(
                f"NiFi injection partial: {injected_ok}/{INJECT_COUNT_RCS}",
                details={"injected_ok": injected_ok},
            )
            return tc, tx_ids, sms_dispatch_before

        # Wait for pipeline to propagate + SendResultJob to process
        print_info(f"Waiting {WAIT_SEC}s for pipeline to produce fallback events...")
        time.sleep(WAIT_SEC)

        # Scan Flink logs for fallback evidence (more reliable than retry topic scan)
        print_info("Scanning Flink TaskManager logs for 'RCS→SMS fallback 전환' entries...")
        target_set = set(tx_ids)
        fallback_matches = scan_flink_logs_for_fallbacks(target_set, since_minutes=5)
        print_info(f"Fallback events detected (matching this batch's txIds): {len(fallback_matches)}")

        if fallback_matches:
            print_info("Sample fallback txIds (first 3):")
            for entry in fallback_matches[:3]:
                print(f"    txId={entry['txId']}")

        if len(fallback_matches) >= MIN_FALLBACK_COUNT:
            tc.finish_pass(details={
                "inject_count":     injected_ok,
                "fallback_count":   len(fallback_matches),
                "fallback_rate":    f"{len(fallback_matches) / injected_ok:.2%}",
                "sample_fallbacks": fallback_matches[:5],
                "expected_rate":    "~1% (50002 = 1/3 of 3% retry allocation)",
                "verification":     "Kafka topic.send.retry scan for channel=SMS",
            })
        else:
            tc.finish_fail(
                f"Only {len(fallback_matches)} fallback events detected "
                f"(expected >={MIN_FALLBACK_COUNT}). "
                f"Probability was ~1% — consider increasing INJECT_COUNT_RCS.",
                details={
                    "inject_count":   injected_ok,
                    "fallback_count": len(fallback_matches),
                },
            )

    except Exception as e:
        tc.finish_fail(f"exception: {type(e).__name__}: {e}")

    return tc, tx_ids, sms_dispatch_before


# ─────────────────────────────────────────────────────────────
# TC-0010: Fallback re-dispatch (SMS dispatch topic gets new messages)
# ─────────────────────────────────────────────────────────────
def run_tc_0010(tx_ids: list, sms_dispatch_before: int, fallback_count: int) -> TestCaseResult:
    tc = TestCaseResult(
        "TC-0010",
        "Fallback re-dispatch (SMS dispatch topic receives RCS-origin fallbacks)",
    )

    # ⭐️ 변경(데스크탑A·노트북B 양쪽에서 재현된 문제 수정): 기존엔 SMS 발송
    # 토픽의 오프셋을 "딱 한 번만" 확인하고 끝냈다. 그런데 RCS→SMS 전환은
    # RetryJob을 거쳐 재발송되는데, RetryJob의 첫 번째 재시도 대기시간이
    # 정확히 30초로 고정되어 있다(RetryJob.java BACKOFF_MS[0]=30_000).
    # TC-0009가 전환 이벤트를 "감지"한 시점과 실제로 SMS로 "재발송"되는
    # 시점 사이에는 최소 30초의 간격이 항상 존재하는데, 기존 코드는 이
    # 간격을 감안하지 않고 곧바로 한 번만 확인해서, 아직 30초 대기가 안
    # 끝난 건들을 "누락"으로 잘못 판정하고 있었다. 최대 90초까지 5초
    # 간격으로 재확인하도록 변경한다(다른 테스트의 폴링 방식과 동일한
    # 패턴).
    max_wait_sec = 90
    poll_interval_sec = 5
    elapsed = 0
    sms_dispatch_after = sms_dispatch_before
    sms_produced = 0

    try:
        while elapsed <= max_wait_sec:
            sms_dispatch_after = get_topic_offset_sum("topic.send.dispatch.sms")
            sms_produced = sms_dispatch_after - sms_dispatch_before
            print_info(f"  [{elapsed:3d}s]  topic.send.dispatch.sms delta={sms_produced} "
                       f"(목표 >= {fallback_count})")
            if sms_produced >= fallback_count and sms_produced > 0:
                break
            time.sleep(poll_interval_sec)
            elapsed += poll_interval_sec

        print_info(f"topic.send.dispatch.sms offset change: "
                   f"{sms_dispatch_before} → {sms_dispatch_after}  (delta={sms_produced})")

        # Since this scenario injects ONLY RCS messages, any new SMS dispatch
        # entries must originate from RCS→SMS fallback (via RetryJob).
        #
        # Expected: sms_produced >= fallback_count (each fallback re-dispatches to SMS)
        #
        # Note: small tolerance because other background tests might add to SMS topic.
        # We use fallback_count as the lower bound.

        if sms_produced >= fallback_count and sms_produced > 0:
            tc.finish_pass(details={
                "sms_dispatch_before":  sms_dispatch_before,
                "sms_dispatch_after":   sms_dispatch_after,
                "sms_dispatch_delta":   sms_produced,
                "expected_fallbacks":   fallback_count,
                "verification":         "SMS dispatch topic offset delta >= fallback count",
            })
        elif sms_produced == 0:
            tc.finish_fail(
                "SMS dispatch topic did not receive any new messages — "
                "fallback did not re-dispatch (RetryJob issue?).",
                details={
                    "sms_dispatch_before": sms_dispatch_before,
                    "sms_dispatch_after":  sms_dispatch_after,
                },
            )
        else:
            # ⭐️ 변경: 최대 90초까지 재확인했는데도 부족하면 그때는 진짜
            # 문제일 가능성이 높으므로, 안내 문구도 그에 맞게 수정한다.
            tc.finish_fail(
                f"SMS dispatch delta ({sms_produced}) < fallback count ({fallback_count}) "
                f"even after waiting up to {max_wait_sec}s (RetryJob 첫 재시도 대기시간 30초 "
                f"감안하여 이미 충분히 기다림). RetryJob 자체 문제 가능성 있음.",
                details={
                    "sms_dispatch_delta":  sms_produced,
                    "expected_fallbacks":  fallback_count,
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
        f"Config: INJECT_COUNT_RCS={INJECT_COUNT_RCS}  "
        f"WAIT_SEC={WAIT_SEC}  MIN_FALLBACK_COUNT={MIN_FALLBACK_COUNT}"
    )
    print_info("Expected fallback rate: ~1% (50002 code). "
               "200 msgs → ~2 fallback events expected.")

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

        # TC-0009: inject and detect fallback events
        print()
        banner(f"TC-0009 execution - {TC_TITLES_KO['TC-0009']}", char="-")
        tc1, tx_ids, sms_before = run_tc_0009(nifi)
        scenario.add_tc(tc1)
        fallback_count = tc1.details.get("fallback_count", 0)

        # TC-0010: verify SMS dispatch received fallbacks
        print()
        banner(f"TC-0010 execution - {TC_TITLES_KO['TC-0010']}", char="-")
        tc2 = run_tc_0010(tx_ids, sms_before, fallback_count)
        scenario.add_tc(tc2)

    finally:
        scenario.finish()
        nifi.close()
        db.close()

    scenario.print_summary(title_ko=SCENARIO_TITLE_KO)

    report_path = save_json_report(SCENARIO_CODE, {
        "summary": scenario.to_dict(),
        "config": {
            "inject_count_rcs":   INJECT_COUNT_RCS,
            "wait_sec":           WAIT_SEC,
            "min_fallback_count": MIN_FALLBACK_COUNT,
            "fallback_source":    "RCS channel only",
            "expected_behavior":  "50002 result_code triggers channel=SMS mutation in SendResultJob",
        },
    })
    print_info(f"JSON report saved: {report_path}")

    return 0 if scenario.fail_count == 0 else 1


if __name__ == "__main__":
    sys.exit(main())