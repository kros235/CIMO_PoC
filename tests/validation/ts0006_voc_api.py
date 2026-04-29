#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
TS-0006 - VOC History API 검증 (VOC History API Verification)

검증 대상:
    History API (8200) 의 VOC 처리 기능 — 발송 이력 조회, 수신번호 검색,
    실시간 통계 조회 등 VOC 상담원이 사용하는 핵심 기능 검증.

테스트 케이스:
    TC-0014 단건 조회 응답 시간
        - DELIVERED 인 txId 로 /api/v1/history/tx/{tx_id} 호출
        - 응답 시간 < 1000ms (VOC SLA: 5분 → PoC 환경 충분히 빠름)
    TC-0015 단건 조회 응답 정합성
        - 필수 필드 (tx_id, channel, status, pipeline_stages 등) 존재
        - pipeline_stages 6개 단계 (nifi/kafka/flink/adapter/result/db) 모두 포함
    TC-0016 수신번호 검색
        - /api/v1/history/receiver/{phone} 호출
        - items >= 1 + 페이지네이션 (limit/offset) 정상 동작
    TC-0017 통계 endpoint
        - /api/v1/metrics/success-rate (200 OK + channels 배열)
        - /api/v1/metrics/tps (200 OK + window_seconds)

설계 가정:
    - DB 에 DELIVERED 상태 메시지가 최소 1건 이상 존재 (사전 시나리오들이 적재)
    - PoC 환경에서 API 응답 1초 이내

PoC 알려진 한계:
    - L02: DISPATCHING 상태 고착 → 일부 status 가 DISPATCHING 일 수 있음
            그러나 VOC 조회 자체에는 영향 없음

실행:
    python tests/validation/ts0006_voc_api.py
"""

import os
import sys
import time
import json
from pathlib import Path

import requests

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from conftest import (
    banner, print_pass, print_fail, print_info, print_warn,
    configure_logging, save_json_report,
    TestCaseResult, TestScenarioResult,
)
from lib.db_checker import DBChecker


# Config
SCENARIO_CODE     = "TS-0006"
SCENARIO_TITLE    = "VOC History API Verification"
SCENARIO_TITLE_KO = "VOC History API 검증"
TC_TITLES_KO = {
    "TC-0014": "단건 조회 응답 시간 - VOC SLA 충족 확인 (< 1초)",
    "TC-0015": "단건 조회 응답 정합성 - 필수 필드 + pipeline_stages 6단계",
    "TC-0016": "수신번호 검색 - phone 기반 발송 이력 목록 조회",
    "TC-0017": "통계 endpoint - 성공률 / TPS 응답 정상",
}

API_BASE_URL              = "http://localhost:8200"
RESPONSE_TIME_THRESHOLD_MS = 1000           # VOC 응답 시간 1초 이내
MIN_RECEIVER_RESULTS       = 1              # 수신번호 검색 최소 1건
EXPECTED_PIPELINE_STAGES   = ["nifi", "kafka", "flink", "adapter", "result", "db"]
HTTP_TIMEOUT_SEC           = 5


# Helpers
def http_get(url, timeout=HTTP_TIMEOUT_SEC):
    """Wrapper around requests.get with timeout + elapsed measurement."""
    start = time.monotonic()
    try:
        resp = requests.get(url, timeout=timeout)
        elapsed_ms = (time.monotonic() - start) * 1000
        return resp, elapsed_ms, None
    except Exception as e:
        elapsed_ms = (time.monotonic() - start) * 1000
        return None, elapsed_ms, str(e)


def fetch_delivered_tx_id_from_db(db):
    """DB에서 DELIVERED 상태 txId 1건 가져오기 (TC-0014, TC-0015 용)."""
    sql = """
        SELECT tx_id, receiver
        FROM msg_send_history
        WHERE status = 'DELIVERED'
        ORDER BY created_at DESC
        LIMIT 1
    """
    with db._cursor() as cur:
        cur.execute(sql)
        row = cur.fetchone()
        if row:
            return row["tx_id"], row["receiver"]     #← dict 키 접근으로 변경
        return None, None


# Test cases
def run_tc_0014(tx_id):
    """TC-0014: 단건 조회 응답 시간 검증."""
    tc = TestCaseResult(
        "TC-0014",
        "Single tx history query response time (< {}ms)".format(RESPONSE_TIME_THRESHOLD_MS),
    )

    try:
        url = "{}/api/v1/history/tx/{}".format(API_BASE_URL, tx_id)
        print_info("GET {}".format(url))

        resp, elapsed_ms, error = http_get(url)

        if error:
            tc.finish_fail("HTTP request failed: {}".format(error))
            return tc

        if resp.status_code != 200:
            tc.finish_fail(
                "Expected 200, got {}".format(resp.status_code),
                details={"http_status": resp.status_code, "body": resp.text[:200]},
            )
            return tc

        # API 자체 측정값
        try:
            data = resp.json()
            api_elapsed = data.get("query_elapsed_ms", -1)
        except Exception:
            api_elapsed = -1

        print_info("Client-side elapsed: {:.1f}ms  /  API self-measured: {}ms".format(
            elapsed_ms, api_elapsed))

        if elapsed_ms <= RESPONSE_TIME_THRESHOLD_MS:
            tc.finish_pass(details={
                "tx_id":                  tx_id,
                "client_elapsed_ms":      round(elapsed_ms, 1),
                "api_self_elapsed_ms":    api_elapsed,
                "threshold_ms":           RESPONSE_TIME_THRESHOLD_MS,
                "http_status":            resp.status_code,
                "verification":           "GET /api/v1/history/tx/{tx_id} response time check",
            })
        else:
            tc.finish_fail(
                "Response time {:.1f}ms exceeded threshold {}ms".format(
                    elapsed_ms, RESPONSE_TIME_THRESHOLD_MS),
                details={
                    "client_elapsed_ms": round(elapsed_ms, 1),
                    "threshold_ms":      RESPONSE_TIME_THRESHOLD_MS,
                },
            )
    except Exception as e:
        tc.finish_fail("Exception: {}".format(e))

    return tc


def run_tc_0015(tx_id):
    """TC-0015: 단건 조회 응답 정합성 검증."""
    tc = TestCaseResult(
        "TC-0015",
        "Single tx history response schema (required fields + pipeline_stages)",
    )

    try:
        url = "{}/api/v1/history/tx/{}".format(API_BASE_URL, tx_id)
        resp, elapsed_ms, error = http_get(url)

        if error or resp.status_code != 200:
            tc.finish_fail("API call failed: error={}, status={}".format(
                error, resp.status_code if resp else "n/a"))
            return tc

        data = resp.json()

        # 필수 필드 검증
        required_fields = ["tx_id", "channel", "status", "pipeline_stages"]
        missing = [f for f in required_fields if f not in data]

        if missing:
            tc.finish_fail(
                "Missing required fields: {}".format(missing),
                details={
                    "missing_fields": missing,
                    "fields_present": list(data.keys()),
                },
            )
            return tc

        # pipeline_stages 검증
        stages = data.get("pipeline_stages", [])
        stage_ids = [s.get("id") for s in stages if isinstance(s, dict)]
        missing_stages = [s for s in EXPECTED_PIPELINE_STAGES if s not in stage_ids]

        if missing_stages:
            tc.finish_fail(
                "Missing pipeline stages: {}".format(missing_stages),
                details={
                    "expected_stages": EXPECTED_PIPELINE_STAGES,
                    "actual_stages":   stage_ids,
                    "missing_stages":  missing_stages,
                },
            )
            return tc

        # tx_id 매칭 확인
        if data.get("tx_id") != tx_id:
            tc.finish_fail(
                "tx_id mismatch: requested={}, returned={}".format(tx_id, data.get("tx_id")),
            )
            return tc

        # PASS
        print_info("Fields verified: {} fields present".format(len(data.keys())))
        print_info("Pipeline stages: {} (all 6 expected)".format(stage_ids))
        tc.finish_pass(details={
            "tx_id":             tx_id,
            "channel":           data.get("channel"),
            "status":            data.get("status"),
            "fields_count":      len(data.keys()),
            "pipeline_stages":   stage_ids,
            "verification":      "Required fields + pipeline_stages 6-step verification",
        })
    except Exception as e:
        tc.finish_fail("Exception: {}".format(e))

    return tc


def run_tc_0016(receiver_phone):
    """TC-0016: 수신번호 검색."""
    tc = TestCaseResult(
        "TC-0016",
        "Receiver phone search (phone={})".format(receiver_phone),
    )

    try:
        # limit=3 으로 호출 (페이지네이션 검증 포함)
        url = "{}/api/v1/history/receiver/{}?limit=3".format(API_BASE_URL, receiver_phone)
        print_info("GET {}".format(url))

        resp, elapsed_ms, error = http_get(url)

        if error or resp.status_code != 200:
            tc.finish_fail("API call failed: error={}, status={}".format(
                error, resp.status_code if resp else "n/a"))
            return tc

        data = resp.json()

        # 응답 구조 검증
        required_fields = ["phone", "total", "items", "limit", "offset"]
        missing = [f for f in required_fields if f not in data]

        if missing:
            tc.finish_fail(
                "Missing required fields: {}".format(missing),
                details={
                    "missing_fields": missing,
                    "fields_present": list(data.keys()),
                },
            )
            return tc

        items = data.get("items", [])
        total = data.get("total", 0)

        print_info("Search result: total={}, items returned={}".format(total, len(items)))

        if total < MIN_RECEIVER_RESULTS:
            tc.finish_fail(
                "Expected total >= {}, got {}".format(MIN_RECEIVER_RESULTS, total),
                details={"total": total, "min_threshold": MIN_RECEIVER_RESULTS},
            )
            return tc

        # PASS
        tc.finish_pass(details={
            "receiver_phone":  receiver_phone,
            "total":           total,
            "items_returned":  len(items),
            "client_elapsed_ms": round(elapsed_ms, 1),
            "verification":    "GET /api/v1/history/receiver/{phone} - structure + pagination",
        })
    except Exception as e:
        tc.finish_fail("Exception: {}".format(e))

    return tc


def run_tc_0017():
    """TC-0017: 통계 endpoint (success-rate + tps)."""
    tc = TestCaseResult(
        "TC-0017",
        "Metrics endpoints (success-rate + tps)",
    )

    try:
        results = {}

        # /api/v1/metrics/success-rate
        url1 = "{}/api/v1/metrics/success-rate".format(API_BASE_URL)
        resp1, elapsed1, error1 = http_get(url1)

        if error1 or resp1.status_code != 200:
            tc.finish_fail("success-rate endpoint failed: error={}, status={}".format(
                error1, resp1.status_code if resp1 else "n/a"))
            return tc

        data1 = resp1.json()
        if "channels" not in data1 or "window" not in data1:
            tc.finish_fail(
                "success-rate response missing 'channels' or 'window'",
                details={"fields": list(data1.keys())},
            )
            return tc

        results["success_rate"] = {
            "http_status":      resp1.status_code,
            "elapsed_ms":       round(elapsed1, 1),
            "window":           data1.get("window"),
            "channels_count":   len(data1.get("channels", [])),
        }
        print_info("success-rate: window={}, channels_count={}".format(
            data1.get("window"), len(data1.get("channels", []))))

        # /api/v1/metrics/tps
        url2 = "{}/api/v1/metrics/tps".format(API_BASE_URL)
        resp2, elapsed2, error2 = http_get(url2)

        if error2 or resp2.status_code != 200:
            tc.finish_fail("tps endpoint failed: error={}, status={}".format(
                error2, resp2.status_code if resp2 else "n/a"))
            return tc

        data2 = resp2.json()
        if "window_seconds" not in data2:
            tc.finish_fail(
                "tps response missing 'window_seconds'",
                details={"fields": list(data2.keys())},
            )
            return tc

        results["tps"] = {
            "http_status":     resp2.status_code,
            "elapsed_ms":      round(elapsed2, 1),
            "window_seconds":  data2.get("window_seconds"),
            "channels_count":  len(data2.get("channels", [])),
        }
        print_info("tps: window_seconds={}, channels_count={}".format(
            data2.get("window_seconds"), len(data2.get("channels", []))))

        # PASS
        tc.finish_pass(details={
            "success_rate": results["success_rate"],
            "tps":          results["tps"],
            "verification": "Both metrics endpoints return 200 + valid schema",
        })
    except Exception as e:
        tc.finish_fail("Exception: {}".format(e))

    return tc


def main():
    configure_logging(verbose=False)

    banner("{} - {} ({})".format(SCENARIO_CODE, SCENARIO_TITLE_KO, SCENARIO_TITLE))
    print_info("Config: API_BASE_URL={}".format(API_BASE_URL))
    print_info("Response time threshold: {}ms".format(RESPONSE_TIME_THRESHOLD_MS))
    print_info("Min receiver results: {}".format(MIN_RECEIVER_RESULTS))

    scenario = TestScenarioResult(SCENARIO_CODE, SCENARIO_TITLE)

    db = DBChecker()

    try:
        # Pre-flight checks
        # 1) History API health
        resp, elapsed, error = http_get("{}/health".format(API_BASE_URL))
        if error or resp.status_code != 200:
            print_fail("History API /health unreachable: {}".format(error or resp.status_code))
            return 1
        print_pass("History API /health reachable ({:.1f}ms)".format(elapsed))

        # 2) DB connection
        try:
            db.connect()
            print_pass("PostgreSQL connection OK")
        except Exception as e:
            print_fail("PostgreSQL connect failed: {}".format(e))
            return 1

        # 3) DB 에서 DELIVERED txId 가져오기
        tx_id, receiver_phone = fetch_delivered_tx_id_from_db(db)
        if not tx_id:
            print_fail("No DELIVERED message in DB - run TS-0001 first to populate")
            return 1
        print_pass("Found DELIVERED tx_id from DB: {} (receiver={})".format(tx_id, receiver_phone))

        # TC-0014
        print_info("")
        banner("TC-0014 execution - {}".format(TC_TITLES_KO["TC-0014"]), char="-")
        tc14 = run_tc_0014(tx_id)
        scenario.add_tc(tc14)

        # TC-0015
        print_info("")
        banner("TC-0015 execution - {}".format(TC_TITLES_KO["TC-0015"]), char="-")
        tc15 = run_tc_0015(tx_id)
        scenario.add_tc(tc15)

        # TC-0016
        print_info("")
        banner("TC-0016 execution - {}".format(TC_TITLES_KO["TC-0016"]), char="-")
        tc16 = run_tc_0016(receiver_phone)
        scenario.add_tc(tc16)

        # TC-0017
        print_info("")
        banner("TC-0017 execution - {}".format(TC_TITLES_KO["TC-0017"]), char="-")
        tc17 = run_tc_0017()
        scenario.add_tc(tc17)

    finally:
        scenario.finish()
        db.close()

    # Summary
    scenario.print_summary(title_ko=SCENARIO_TITLE_KO)

    # JSON report
    report_path = save_json_report(SCENARIO_CODE, {
        "summary": scenario.to_dict(),
    })
    print_info("")
    print_info("  [INFO] JSON report saved: {}".format(report_path))

    return 0 if scenario.fail_count == 0 else 1


if __name__ == "__main__":
    sys.exit(main())