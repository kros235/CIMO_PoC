# tests/validation/conftest.py
"""
Common configuration and utilities for Day 6 validation tests.

This file serves two roles:
    1. pytest fixtures (for `pytest tests/validation`)
    2. Direct-import utilities (for `python tests/validation/ts0001_*.py`)

Path layout reference (relative to repo root):
    tests/validation/
        conftest.py                         <- this file
        lib/
            tx_generator.py
            nifi_client.py
            db_checker.py
            adapter_controller.py
        ts0001_pipeline_consistency.py
        ts0002_adapter_isolation.py
        ...
        reports/                            <- JSON / HTML reports land here
"""

import os
import sys
import json
import logging
from datetime import datetime, timezone, timedelta
from pathlib import Path


# ─────────────────────────────────────────────────────────────
# Path constants (all relative - no absolute paths anywhere)
# ─────────────────────────────────────────────────────────────
# conftest.py lives at tests/validation/conftest.py
VALIDATION_DIR = Path(__file__).resolve().parent
LIB_DIR        = VALIDATION_DIR / "lib"
REPORTS_DIR    = VALIDATION_DIR / "reports"
TESTS_DIR      = VALIDATION_DIR.parent              # tests/
REPO_ROOT      = TESTS_DIR.parent                   # project root

# Ensure lib/ is importable when running standalone scripts
if str(LIB_DIR) not in sys.path:
    sys.path.insert(0, str(LIB_DIR))

# Create reports dir if missing
REPORTS_DIR.mkdir(parents=True, exist_ok=True)


# ─────────────────────────────────────────────────────────────
# Time helpers
# ─────────────────────────────────────────────────────────────
KST = timezone(timedelta(hours=9))


def now_kst() -> datetime:
    """Current time in KST (Asia/Seoul)."""
    return datetime.now(KST)


def timestamp_slug() -> str:
    """Compact timestamp suitable for filenames (e.g. 20260422_143015)."""
    return now_kst().strftime("%Y%m%d_%H%M%S")


# ─────────────────────────────────────────────────────────────
# Colored console output
# (colorama provides Windows-safe ANSI; degrades gracefully on Linux)
# ─────────────────────────────────────────────────────────────
try:
    from colorama import Fore, Style, init as colorama_init
    colorama_init(autoreset=True)
    COLOR_ENABLED = True
except ImportError:
    # Degrade to no-op color codes if colorama missing
    COLOR_ENABLED = False

    class _Dummy:
        def __getattr__(self, name): return ""
    Fore = _Dummy()
    Style = _Dummy()


def banner(text: str, char: str = "=") -> None:
    """Print a prominent banner to stdout."""
    line = char * 72
    print(f"\n{Fore.CYAN}{line}")
    print(f"  {text}")
    print(f"{line}{Style.RESET_ALL}")


def print_pass(text: str) -> None:
    print(f"  {Fore.GREEN}[PASS]{Style.RESET_ALL} {text}")


def print_fail(text: str) -> None:
    print(f"  {Fore.RED}[FAIL]{Style.RESET_ALL} {text}")


def print_info(text: str) -> None:
    print(f"  {Fore.WHITE}[INFO]{Style.RESET_ALL} {text}")


def print_warn(text: str) -> None:
    print(f"  {Fore.YELLOW}[WARN]{Style.RESET_ALL} {text}")


# ─────────────────────────────────────────────────────────────
# Logging setup (scenarios opt-in by calling configure_logging())
# ─────────────────────────────────────────────────────────────
def configure_logging(verbose: bool = False) -> None:
    """Configure root logger with consistent format across all scenarios."""
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
        datefmt="%H:%M:%S",
    )


# ─────────────────────────────────────────────────────────────
# JSON report serializer
# (HTML generator in Phase 3; JSON comes first for Day 7 reuse)
# ─────────────────────────────────────────────────────────────
def save_json_report(scenario_code: str, payload: dict) -> Path:
    """
    Save a scenario result JSON report to tests/validation/reports/.

    Filename pattern: <scenario_code>_<timestamp>.json
    Example:          TS-0001_20260422_143015.json

    Returns the saved file path.
    """
    ts = timestamp_slug()
    filename = f"{scenario_code}_{ts}.json"
    output_path = REPORTS_DIR / filename

    # Inject metadata at top level if not already present
    if "meta" not in payload:
        payload["meta"] = {}
    payload["meta"]["scenario_code"] = scenario_code
    payload["meta"]["timestamp_kst"] = now_kst().isoformat(timespec="seconds")
    payload["meta"]["reports_dir"]   = str(REPORTS_DIR.relative_to(REPO_ROOT))

    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2, default=_json_default)

    return output_path


def _json_default(obj):
    """Fallback serializer for datetime, Path, set, etc."""
    if isinstance(obj, datetime):
        return obj.isoformat()
    if isinstance(obj, Path):
        return str(obj)
    if isinstance(obj, set):
        return list(obj)
    return str(obj)


# ─────────────────────────────────────────────────────────────
# TestCase result container (used by all TS scenarios)
# ─────────────────────────────────────────────────────────────
class TestCaseResult:
    """Lightweight container for a single TC's pass/fail outcome."""

    def __init__(self, tc_code: str, title: str):
        self.tc_code = tc_code
        self.title   = title
        self.passed  = False
        self.started_at = now_kst()
        self.finished_at = None
        self.elapsed_sec = 0.0
        self.details: dict = {}
        self.error: str | None = None

    def finish_pass(self, details: dict | None = None):
        self.passed = True
        self.finished_at = now_kst()
        self.elapsed_sec = (self.finished_at - self.started_at).total_seconds()
        if details:
            self.details.update(details)
        print_pass(f"{self.tc_code} {self.title} ({self.elapsed_sec:.1f}s)")

    def finish_fail(self, error: str, details: dict | None = None):
        self.passed = False
        self.finished_at = now_kst()
        self.elapsed_sec = (self.finished_at - self.started_at).total_seconds()
        self.error = error
        if details:
            self.details.update(details)
        print_fail(f"{self.tc_code} {self.title} ({self.elapsed_sec:.1f}s)")
        print(f"         {Fore.RED}{error}{Style.RESET_ALL}")

    def to_dict(self) -> dict:
        return {
            "tc_code":     self.tc_code,
            "title":       self.title,
            "passed":      self.passed,
            "started_at":  self.started_at.isoformat(),
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "elapsed_sec": round(self.elapsed_sec, 2),
            "details":     self.details,
            "error":       self.error,
        }


class TestScenarioResult:
    """Collects all TC results for a single TS and summarizes."""

    def __init__(self, scenario_code: str, title: str):
        self.scenario_code = scenario_code
        self.title = title
        self.started_at = now_kst()
        self.finished_at = None
        self.tc_results: list[TestCaseResult] = []

    def add_tc(self, tc_result: TestCaseResult) -> None:
        self.tc_results.append(tc_result)

    def finish(self) -> None:
        self.finished_at = now_kst()

    @property
    def pass_count(self) -> int:
        return sum(1 for tc in self.tc_results if tc.passed)

    @property
    def fail_count(self) -> int:
        return sum(1 for tc in self.tc_results if not tc.passed)

    @property
    def total_count(self) -> int:
        return len(self.tc_results)

    def to_dict(self) -> dict:
        return {
            "scenario_code": self.scenario_code,
            "title":         self.title,
            "started_at":    self.started_at.isoformat(),
            "finished_at":   self.finished_at.isoformat() if self.finished_at else None,
            "total_count":   self.total_count,
            "pass_count":    self.pass_count,
            "fail_count":    self.fail_count,
            "overall_pass":  self.fail_count == 0,
            "test_cases":    [tc.to_dict() for tc in self.tc_results],
        }

    def print_summary(self) -> None:
        banner(f"{self.scenario_code} Summary")
        for tc in self.tc_results:
            marker = f"{Fore.GREEN}PASS" if tc.passed else f"{Fore.RED}FAIL"
            print(f"  {marker}{Style.RESET_ALL}  {tc.tc_code}  {tc.title}  ({tc.elapsed_sec:.1f}s)")
        print()
        status = f"{Fore.GREEN}PASSED" if self.fail_count == 0 else f"{Fore.RED}FAILED"
        print(f"  Total: {self.total_count}  Pass: {self.pass_count}  Fail: {self.fail_count}  -> {status}{Style.RESET_ALL}")
        print()


if __name__ == "__main__":
    # Self-check: print resolved paths
    banner("conftest.py self-check")
    print_info(f"VALIDATION_DIR: {VALIDATION_DIR}")
    print_info(f"LIB_DIR:        {LIB_DIR}")
    print_info(f"REPORTS_DIR:    {REPORTS_DIR}")
    print_info(f"REPO_ROOT:      {REPO_ROOT}")
    print_info(f"Current KST:    {now_kst().isoformat()}")
    print_info(f"Color enabled:  {COLOR_ENABLED}")
    print_pass("conftest.py loaded successfully")