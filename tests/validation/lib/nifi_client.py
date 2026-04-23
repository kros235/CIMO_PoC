# tests/validation/lib/nifi_client.py
"""
Day 6 integration tests - NiFi HTTP injection client.

Purpose:
    Send POST requests to NiFi ListenHTTP endpoint (http://localhost:8090/am/send)
    with properly formatted AM platform message bodies.

Why this module exists:
    - All TS scenarios inject messages via NiFi per Day 6 decision 1-A
    - NiFi's txId 35-digit validation is part of the E2E flow being verified
    - Centralizes retry/timeout handling so scenarios stay concise

Typical usage:
    from lib.nifi_client import NiFiClient
    from lib.tx_generator import realtime_tx_id

    client = NiFiClient()
    tx = realtime_tx_id()
    result = client.send_one(tx_id=tx, channel="SMS", receiver="01012345678")
    # result -> {"tx_id": "...", "http_status": 200, "elapsed_ms": 42.1}
"""

import os
import time
import logging
from typing import Optional
from datetime import datetime, timezone, timedelta

import requests


KST = timezone(timedelta(hours=9))

# NiFi endpoint (env override supported for future cross-env use)
NIFI_BASE_URL = os.getenv("NIFI_INJECT_URL", "http://localhost:8090")
NIFI_SEND_PATH = os.getenv("NIFI_INJECT_PATH", "/am/send")

# Default HTTP timeouts (connect, read) in seconds
DEFAULT_TIMEOUT = (3, 10)

# NiFi ListenHTTP returns 200 or 204 on successful receipt
NIFI_SUCCESS_CODES = {200, 204}


log = logging.getLogger("nifi_client")


class NiFiClient:
    """
    Thin wrapper around NiFi's ListenHTTP endpoint for test scenarios.
    Uses a requests.Session for connection reuse (measurably faster for 1000+ requests).
    """

    def __init__(self, base_url: str = NIFI_BASE_URL, timeout: tuple = DEFAULT_TIMEOUT):
        self.base_url = base_url.rstrip("/")
        self.send_url = f"{self.base_url}{NIFI_SEND_PATH}"
        self.timeout = timeout
        self.session = requests.Session()
        # Default headers shared across all requests
        self.session.headers.update({"Content-Type": "application/json"})

    def close(self):
        self.session.close()

    # ─────────────────────────────────────────────────────────
    # Message body builder
    # ─────────────────────────────────────────────────────────
    @staticmethod
    def build_payload(
        tx_id: str,
        channel: str,
        receiver: str,
        sender: str = "15881234",
        message_body: str = "Day 6 integration test message",
        source: str = "TEST_DAY6",
        scheduled_at: Optional[str] = None,
        template_id: Optional[str] = None,
        customer_id: Optional[str] = None,
        campaign_id: Optional[str] = None,
    ) -> dict:
        """
        Builds a JSON payload matching Flink SendMessage.java (15-field spec).
        ValidationOperator requires: txId, channel, receiver, messageBody.
        Optional fields follow @JsonProperty annotations in SendMessage.java.
        """
        now_iso = datetime.now(KST).isoformat(timespec="seconds")

        # Derive sendMethodCode/senderCode from txId (positions 14-15 and 19-21)
        send_method_code = tx_id[13:15] if len(tx_id) >= 15 else "03"
        sender_code      = tx_id[18:21] if len(tx_id) >= 21 else "007"

        return {
            "txId":            tx_id,
            "sendMethodCode":  send_method_code,
            "channel":         channel,
            "sender":          sender,
            "receiver":        receiver,
            "messageBody":     message_body,
            "customerId":      customer_id or f"CUST_{tx_id[-10:]}",
            "senderCode":      sender_code,
            "scheduledAt":     scheduled_at,
            "requestedAt":     now_iso,
            "source":          source,
            "campaignId":      campaign_id,
            "templateId":      template_id or "TPL_TEST_0001",
            "status":          "PENDING",
            "retryCount":      0,
        }

    # ─────────────────────────────────────────────────────────
    # Single message injection
    # ─────────────────────────────────────────────────────────
    def send_one(
        self,
        tx_id: str,
        channel: str,
        receiver: str,
        **payload_overrides,
    ) -> dict:
        """
        Send exactly one message to NiFi.

        Returns:
            {
              "tx_id": str,
              "http_status": int,       # HTTP status code
              "success": bool,          # True if NiFi accepted (200/204)
              "elapsed_ms": float,      # client-side elapsed time
              "error": str | None,      # error message if request failed
            }
        """
        payload = self.build_payload(tx_id=tx_id, channel=channel, receiver=receiver, **payload_overrides)

        start = time.monotonic()
        http_status = 0
        success = False
        error = None

        try:
            resp = self.session.post(self.send_url, json=payload, timeout=self.timeout)
            http_status = resp.status_code
            success = http_status in NIFI_SUCCESS_CODES
            if not success:
                error = f"unexpected HTTP {http_status}: {resp.text[:200]}"
        except requests.exceptions.Timeout:
            error = "request timeout"
        except requests.exceptions.ConnectionError as e:
            error = f"connection error: {e}"
        except Exception as e:
            error = f"{type(e).__name__}: {e}"

        elapsed_ms = (time.monotonic() - start) * 1000

        return {
            "tx_id":       tx_id,
            "http_status": http_status,
            "success":     success,
            "elapsed_ms":  round(elapsed_ms, 2),
            "error":       error,
        }

    # ─────────────────────────────────────────────────────────
    # Bulk injection (sequential, suitable for 100-1000 messages)
    # ─────────────────────────────────────────────────────────
    def send_bulk(
        self,
        messages: list,
        progress_every: int = 100,
    ) -> dict:
        """
        Inject many messages sequentially.

        Args:
            messages: list of dicts, each must have at least {"tx_id", "channel", "receiver"}
                      plus any optional overrides (sender, body, etc.)
            progress_every: log a progress line every N messages (0 to disable)

        Returns:
            {
              "total": int,
              "success_count": int,
              "fail_count": int,
              "elapsed_total_ms": float,
              "avg_elapsed_ms": float,
              "failures": [ {tx_id, http_status, error}, ... ]   # up to 20 shown
            }
        """
        total = len(messages)
        success_count = 0
        failures = []
        per_msg_elapsed = []

        t_start = time.monotonic()
        for i, msg in enumerate(messages, 1):
            tx_id    = msg.pop("tx_id")
            channel  = msg.pop("channel")
            receiver = msg.pop("receiver")
            result = self.send_one(tx_id=tx_id, channel=channel, receiver=receiver, **msg)
            per_msg_elapsed.append(result["elapsed_ms"])
            if result["success"]:
                success_count += 1
            else:
                if len(failures) < 20:
                    failures.append({
                        "tx_id":       tx_id,
                        "http_status": result["http_status"],
                        "error":       result["error"],
                    })
            if progress_every > 0 and i % progress_every == 0:
                log.info(f"    [{i}/{total}] injected (success={success_count}, fail={len(failures)})")

        total_elapsed_ms = (time.monotonic() - t_start) * 1000
        avg_elapsed_ms = sum(per_msg_elapsed) / total if total else 0

        return {
            "total":            total,
            "success_count":    success_count,
            "fail_count":       total - success_count,
            "elapsed_total_ms": round(total_elapsed_ms, 2),
            "avg_elapsed_ms":   round(avg_elapsed_ms, 2),
            "failures":         failures,
        }

    # ─────────────────────────────────────────────────────────
    # Health check (quick probe before running tests)
    # ─────────────────────────────────────────────────────────
    def health_check(self) -> bool:
        """
        Probe NiFi 8090 endpoint with an invalid-but-parseable payload.
        NiFi returns 200 even for malformed txId (RouteOnAttribute drops to log branch),
        so any 200/204 means the endpoint is reachable and ListenHTTP is RUNNING.
        """
        try:
            resp = self.session.post(
                self.send_url,
                json={"probe": "healthcheck"},
                timeout=(3, 5),
            )
            return resp.status_code in NIFI_SUCCESS_CODES
        except Exception:
            return False


if __name__ == "__main__":
    # Self-check: health check + 1 real message
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    from tx_generator import realtime_tx_id

    client = NiFiClient()
    print(f"[NiFiClient] URL: {client.send_url}")
    print(f"[NiFiClient] Health check: {'OK' if client.health_check() else 'FAIL'}")

    tx = realtime_tx_id()
    result = client.send_one(tx_id=tx, channel="SMS", receiver="01012345678")
    print(f"[NiFiClient] send_one result: {result}")
    client.close()