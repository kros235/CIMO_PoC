# tests/validation/lib/tx_generator.py
"""
Day 6 integration tests - txId 35-digit generator.

Purpose:
    Generate unique 35-digit transaction IDs (txId) matching the project spec:
      messageId(13) + sendMethodCode(2) + dayOfYear(3) + senderCode(3) + sequence(14)

Why this module exists:
    - poc/services/base/tx_id.py is designed for Mock Adapter containers
    - Test scripts run on the host, not inside containers, so direct import is awkward
    - This module replicates the same 35-digit spec in a host-friendly form
    - All test scenarios (TS-0001 ~ TS-0006) use this generator

Send method codes (sendMethodCode):
    01, 02 -> batch send (scheduled)
    03     -> real-time send (immediate, single message)
    04, 05 -> near-real-time send
"""

import threading
import time
from datetime import datetime, timezone, timedelta
from typing import Optional


KST = timezone(timedelta(hours=9))

# sendMethodCode valid values per project spec
VALID_METHOD_CODES = {"01", "02", "03", "04", "05"}

# Thread-safe sequence counter (ensures uniqueness within the same millisecond)
_sequence_counter = 0
_sequence_lock = threading.Lock()


def _next_sequence() -> int:
    """Returns a monotonically increasing sequence number (thread-safe)."""
    global _sequence_counter
    with _sequence_lock:
        _sequence_counter = (_sequence_counter + 1) % (10 ** 14)
        return _sequence_counter


def build_tx_id(send_method_code: str, sender_code: str) -> str:
    """
    Build a 35-digit txId.

    Args:
        send_method_code: 2 digits, must be one of "01"~"05"
        sender_code:      3 digits (upstream sender identifier)

    Returns:
        35-digit numeric string.

    Raises:
        ValueError: if send_method_code or sender_code is malformed.
    """
    if send_method_code not in VALID_METHOD_CODES:
        raise ValueError(
            f"send_method_code must be one of {sorted(VALID_METHOD_CODES)}, got '{send_method_code}'"
        )

    if not (len(sender_code) == 3 and sender_code.isdigit()):
        raise ValueError(f"sender_code must be exactly 3 digits, got '{sender_code}'")

    # messageId: 13 digits based on unix milliseconds
    now = datetime.now(KST)
    unix_ms = int(time.time() * 1000)
    message_id = str(unix_ms)[-13:].rjust(13, "0")

    # dayOfYear: 3 digits, 001-366
    day_of_year = f"{now.timetuple().tm_yday:03d}"

    # sequence: 14 digits, monotonically increasing
    sequence = f"{_next_sequence():014d}"

    tx_id = message_id + send_method_code + day_of_year + sender_code + sequence

    if len(tx_id) != 35:
        # Defensive check — should never trigger if inputs valid
        raise RuntimeError(f"Generated txId is not 35 digits: '{tx_id}' (len={len(tx_id)})")

    return tx_id


def validate_tx_id(tx_id: str) -> bool:
    """
    Validate a txId string against the 35-digit spec.

    Returns:
        True if tx_id is 35 digits and the sendMethodCode (positions 14-15)
        is within the valid set; False otherwise.
    """
    if not isinstance(tx_id, str):
        return False
    if len(tx_id) != 35:
        return False
    if not tx_id.isdigit():
        return False
    send_method_code = tx_id[13:15]
    if send_method_code not in VALID_METHOD_CODES:
        return False
    return True


def parse_tx_id(tx_id: str) -> dict:
    """
    Decompose a 35-digit txId into its 5 components.

    Returns:
        {"message_id": str, "send_method_code": str, "day_of_year": str,
         "sender_code": str, "sequence": str}

    Raises:
        ValueError: if tx_id is not a valid 35-digit txId.
    """
    if not validate_tx_id(tx_id):
        raise ValueError(f"Invalid txId: '{tx_id}'")

    return {
        "message_id":       tx_id[0:13],
        "send_method_code": tx_id[13:15],
        "day_of_year":      tx_id[15:18],
        "sender_code":      tx_id[18:21],
        "sequence":         tx_id[21:35],
    }


# Convenience shortcuts for common scenarios
def realtime_tx_id(sender_code: str = "007") -> str:
    """Shortcut for real-time send (sendMethodCode=03)."""
    return build_tx_id("03", sender_code)


def batch_tx_id(sender_code: str = "001") -> str:
    """Shortcut for batch send (sendMethodCode=01)."""
    return build_tx_id("01", sender_code)


if __name__ == "__main__":
    # Self-check: generate 5 realtime + 5 batch txIds and validate each
    print("=== tx_generator self-check ===")
    for _ in range(5):
        tx = realtime_tx_id()
        assert validate_tx_id(tx), f"invalid: {tx}"
        print(f"[realtime 03] {tx}  parsed={parse_tx_id(tx)}")
    for _ in range(5):
        tx = batch_tx_id()
        assert validate_tx_id(tx), f"invalid: {tx}"
        print(f"[batch    01] {tx}  parsed={parse_tx_id(tx)}")
    print("=== all passed ===")