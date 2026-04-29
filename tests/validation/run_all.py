#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
run_all.py - CIMO_PoC 통합 검증 실행기

목적:
    TS-0001 ~ TS-0006 6개 검증 시나리오를 순차 실행하고,
    각 시나리오의 JSON 리포트를 합쳐서 HTML 종합 리포트를 생성한다.

실행 흐름:
    1. 각 ts*.py 를 subprocess 로 실행 (독립 프로세스 격리)
    2. 각 실행의 종료 코드 + 콘솔 출력 캡처
    3. 각 시나리오가 생성한 JSON 리포트 파일 읽기
    4. 콘솔에 종합 결과 표 출력
    5. HTML 종합 리포트 생성 (reports/RUN-ALL_<timestamp>.html)

사용법:
    python tests/validation/run_all.py
    python tests/validation/run_all.py --skip TS-0003 TS-0004    # 일부 스킵
    python tests/validation/run_all.py --only TS-0006             # 한개만 실행

출력:
    - 콘솔: 종합 결과 표
    - HTML: tests/validation/reports/RUN-ALL_<timestamp>.html
    - 종료 코드: 모두 PASS 면 0, 한 시나리오라도 FAIL 이면 1
"""

import os
import sys
import json
import time
import argparse
import subprocess
import platform
from datetime import datetime
from pathlib import Path

# 색상
RED    = "\033[0;31m"
GREEN  = "\033[0;32m"
YELLOW = "\033[1;33m"
BLUE   = "\033[0;34m"
GRAY   = "\033[0;90m"
NC     = "\033[0m"


# 시나리오 메타데이터
SCENARIOS = [
    {
        "code":      "TS-0001",
        "title_ko":  "파이프라인 정합성 검증",
        "title_en":  "Pipeline Consistency Verification",
        "script":    "ts0001_pipeline_consistency.py",
    },
    {
        "code":      "TS-0002",
        "title_ko":  "어댑터 장애 격리",
        "title_en":  "Adapter Failure Isolation",
        "script":    "ts0002_adapter_isolation.py",
    },
    {
        "code":      "TS-0003",
        "title_ko":  "재처리 메커니즘 검증",
        "title_en":  "Retry Mechanism Verification",
        "script":    "ts0003_retry_mechanism.py",
    },
    {
        "code":      "TS-0004",
        "title_ko":  "RCS-SMS Fallback 검증",
        "title_en":  "RCS to SMS Fallback Verification",
        "script":    "ts0004_rcs_fallback.py",
    },
    {
        "code":      "TS-0005",
        "title_ko":  "DLQ 동작 검증",
        "title_en":  "DLQ Behavior Verification",
        "script":    "ts0005_dlq_behavior.py",
    },
    {
        "code":      "TS-0006",
        "title_ko":  "VOC History API 검증",
        "title_en":  "VOC History API Verification",
        "script":    "ts0006_voc_api.py",
    },
]


# 경로
SCRIPT_DIR    = Path(__file__).resolve().parent
REPORTS_DIR   = SCRIPT_DIR / "reports"
PROJECT_ROOT  = SCRIPT_DIR.parent.parent     # /c/Projects/CIMO_PoC


def banner(title, char="="):
    line = char * 72
    print()
    print(line)
    print("  {}".format(title))
    print(line)


def log_info(msg):
    print("  {}[INFO]{}  {}".format(BLUE, NC, msg))


def log_pass(msg):
    print("  {}[PASS]{}  {}".format(GREEN, NC, msg))


def log_fail(msg):
    print("  {}[FAIL]{}  {}".format(RED, NC, msg))


def log_warn(msg):
    print("  {}[WARN]{}  {}".format(YELLOW, NC, msg))


def get_latest_json_report(scenario_code):
    """reports/ 에서 가장 최근 <scenario_code>_*.json 찾기."""
    pattern = "{}_*.json".format(scenario_code)
    matches = sorted(REPORTS_DIR.glob(pattern), key=lambda p: p.stat().st_mtime)
    return matches[-1] if matches else None


def run_scenario(scenario):
    """
    Run a single scenario as subprocess.
    Returns dict with execution metadata.
    """
    code     = scenario["code"]
    script   = scenario["script"]
    script_path = SCRIPT_DIR / script

    if not script_path.exists():
        return {
            "scenario_code": code,
            "title_ko":      scenario["title_ko"],
            "title_en":      scenario["title_en"],
            "executed":      False,
            "skipped":       False,
            "error":         "Script not found: {}".format(script),
            "elapsed_sec":   0.0,
        }

    log_info("실행 중: {} ({})".format(code, scenario["title_ko"]))

    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"

    start = time.time()
    try:
        result = subprocess.run(
            [sys.executable, str(script_path)],
            capture_output=False,    # 콘솔에 그대로 출력 (실시간 진행 보여주기)
            timeout=600,             # 10분 timeout
            env=env,
            cwd=str(PROJECT_ROOT),
        )
        elapsed = time.time() - start

        return {
            "scenario_code": code,
            "title_ko":      scenario["title_ko"],
            "title_en":      scenario["title_en"],
            "executed":      True,
            "skipped":       False,
            "exit_code":     result.returncode,
            "elapsed_sec":   round(elapsed, 1),
        }
    except subprocess.TimeoutExpired:
        elapsed = time.time() - start
        log_fail("{} timeout 후 강제 종료 (10분)".format(code))
        return {
            "scenario_code": code,
            "title_ko":      scenario["title_ko"],
            "title_en":      scenario["title_en"],
            "executed":      True,
            "skipped":       False,
            "exit_code":     -1,
            "error":         "Timeout (10min)",
            "elapsed_sec":   round(elapsed, 1),
        }


def load_json_report(scenario_code):
    if report_path.stat().st_mtime < RUN_START_TIME:
        return None    # 이번 실행 전에 만들어진 JSON 은 무시

    """Load latest JSON report for a scenario."""
    report_path = get_latest_json_report(scenario_code)
    if not report_path:
        return None
    try:
        with open(report_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        log_warn("{} JSON 리포트 로드 실패: {}".format(scenario_code, e))
        return None


def print_summary_table(scenario_results):
    """Print final summary table to console."""
    banner("종합 검증 리포트 - CIMO_PoC Validation Suite")

    total_tcs = 0
    pass_tcs  = 0
    fail_tcs  = 0
    total_elapsed = 0.0

    for sr in scenario_results:
        code      = sr["scenario_code"]
        title_ko  = sr["title_ko"]
        elapsed   = sr["elapsed_sec"]
        report    = sr.get("json_report")

        total_elapsed += elapsed

        if sr.get("skipped"):
            status_str = "{}SKIP{}".format(GRAY, NC)
            tc_str     = "  - "
        elif not sr.get("executed"):
            status_str = "{}NORUN{}".format(GRAY, NC)
            tc_str     = "  - "
        elif report:
            summary = report.get("summary", {})
            p       = summary.get("pass_count", 0)
            t       = summary.get("total_count", 0)
            f       = summary.get("fail_count", 0)
            total_tcs += t
            pass_tcs  += p
            fail_tcs  += f

            if f == 0 and t > 0:
                status_str = "{}PASS{}".format(GREEN, NC)
            else:
                status_str = "{}FAIL{}".format(RED, NC)
            tc_str = "{}/{}".format(p, t)
        else:
            # 실행은 됐으나 JSON 못 읽음
            status_str = "{}???{}".format(YELLOW, NC)
            tc_str     = "  - "

        # 한국어 폭 보정 (한글 1글자 = 2 width)
        title_padded = title_ko + " " * max(0, 32 - len(title_ko) * 2)

        print("  {}  {}  {}   {:>5}   ({:>5.1f}s)".format(
            code,
            title_padded,
            status_str,
            tc_str,
            elapsed,
        ))

    print()
    print("  " + "-" * 70)

    # 종합
    overall = "PASSED" if fail_tcs == 0 and total_tcs > 0 else "FAILED"
    overall_color = GREEN if overall == "PASSED" else RED

    executed_count = sum(1 for sr in scenario_results if sr.get("executed"))
    print("  종합:  {} 시나리오 / {} TC / {} PASS / {} FAIL  ->  {}{}{}".format(
        executed_count, total_tcs, pass_tcs, fail_tcs,
        overall_color, overall, NC,
    ))
    print("  소요:  {:.1f} 초".format(total_elapsed))
    print("  " + "-" * 70)

    return {
        "total_scenarios": executed_count,
        "total_tcs":       total_tcs,
        "pass_tcs":        pass_tcs,
        "fail_tcs":        fail_tcs,
        "total_elapsed":   round(total_elapsed, 1),
        "overall_pass":    fail_tcs == 0 and total_tcs > 0,
    }


def generate_html_report(scenario_results, summary, output_path):
    """Generate consolidated HTML report."""
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    overall_str = "PASSED" if summary["overall_pass"] else "FAILED"
    overall_color = "#2E7D32" if summary["overall_pass"] else "#C62828"
    pass_rate = (summary["pass_tcs"] / summary["total_tcs"] * 100) if summary["total_tcs"] > 0 else 0

    # 시나리오 카드 HTML 생성
    scenario_cards = []
    for sr in scenario_results:
        code      = sr["scenario_code"]
        title_ko  = sr["title_ko"]
        title_en  = sr["title_en"]
        elapsed   = sr["elapsed_sec"]
        report    = sr.get("json_report")

        if sr.get("skipped"):
            badge = '<span class="badge skip">SKIP</span>'
            tc_summary = "(스킵됨)"
            tc_rows = ""
        elif not sr.get("executed"):
            badge = '<span class="badge norun">NOT RUN</span>'
            tc_summary = "(실행 안 됨)"
            tc_rows = ""
        elif report:
            summary_data = report.get("summary", {})
            p = summary_data.get("pass_count", 0)
            t = summary_data.get("total_count", 0)
            f = summary_data.get("fail_count", 0)

            if f == 0 and t > 0:
                badge = '<span class="badge pass">PASS</span>'
            else:
                badge = '<span class="badge fail">FAIL</span>'

            tc_summary = "{}/{} TC ({:.1f}s)".format(p, t, elapsed)

            # TC 상세 테이블
            tc_rows_list = []
            for tc in summary_data.get("test_cases", []):
                tc_passed = tc.get("passed", False)
                tc_class = "tc-pass" if tc_passed else "tc-fail"
                tc_status = "PASS" if tc_passed else "FAIL"
                tc_elapsed = tc.get("elapsed_sec", 0)
                tc_title = (tc.get("title", "") or "").replace("<", "&lt;").replace(">", "&gt;")
                tc_error = tc.get("error", "")

                error_html = ""
                if tc_error:
                    err_safe = tc_error.replace("<", "&lt;").replace(">", "&gt;")
                    error_html = '<div class="tc-error">{}</div>'.format(err_safe)

                tc_rows_list.append("""
                <tr class="{cls}">
                    <td class="tc-code">{code}</td>
                    <td class="tc-title">{title}{err}</td>
                    <td class="tc-status">{status}</td>
                    <td class="tc-elapsed">{elapsed:.1f}s</td>
                </tr>""".format(
                    cls=tc_class,
                    code=tc.get("tc_code", ""),
                    title=tc_title,
                    err=error_html,
                    status=tc_status,
                    elapsed=tc_elapsed,
                ))
            tc_rows = "".join(tc_rows_list)
        else:
            badge = '<span class="badge unknown">???</span>'
            tc_summary = "(JSON 리포트 없음)"
            tc_rows = ""

        card_html = """
        <div class="scenario-card">
            <div class="scenario-header">
                <span class="scenario-code">{code}</span>
                <span class="scenario-title">{title_ko}</span>
                {badge}
                <span class="scenario-summary">{tc_summary}</span>
            </div>
            <div class="scenario-subtitle">{title_en}</div>
            {table_html}
        </div>""".format(
            code=code,
            title_ko=title_ko,
            badge=badge,
            tc_summary=tc_summary,
            title_en=title_en,
            table_html=("""
            <table class="tc-table">
                <thead>
                    <tr><th>TC</th><th>제목</th><th>상태</th><th>시간</th></tr>
                </thead>
                <tbody>{}</tbody>
            </table>""".format(tc_rows)) if tc_rows else "",
        )
        scenario_cards.append(card_html)

    # 전체 HTML
    html = """<!DOCTYPE html>
<html lang="ko">
<head>
    <meta charset="UTF-8">
    <title>CIMO_PoC Validation Report - {ts}</title>
    <style>
        * {{ box-sizing: border-box; }}
        body {{
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "Apple SD Gothic Neo", sans-serif;
            margin: 0; padding: 24px;
            background: #f5f5f5; color: #222;
        }}
        .container {{ max-width: 1100px; margin: 0 auto; }}
        h1 {{ color: #1565C0; margin-bottom: 8px; }}
        .subtitle {{ color: #666; margin-bottom: 24px; font-size: 14px; }}

        .summary-box {{
            background: white;
            border-left: 6px solid {overall_color};
            padding: 20px 24px;
            margin-bottom: 24px;
            border-radius: 4px;
            box-shadow: 0 1px 3px rgba(0,0,0,0.1);
        }}
        .summary-row {{
            display: flex; gap: 32px; flex-wrap: wrap;
            align-items: center;
        }}
        .summary-stat {{
            display: flex; flex-direction: column;
        }}
        .summary-stat .label {{
            font-size: 12px; color: #888; text-transform: uppercase;
        }}
        .summary-stat .value {{
            font-size: 28px; font-weight: bold;
        }}
        .summary-stat.overall .value {{ color: {overall_color}; }}

        .scenario-card {{
            background: white;
            margin-bottom: 16px;
            border-radius: 4px;
            box-shadow: 0 1px 3px rgba(0,0,0,0.1);
            overflow: hidden;
        }}
        .scenario-header {{
            padding: 14px 20px;
            display: flex; align-items: center; gap: 12px;
            border-bottom: 1px solid #eee;
            flex-wrap: wrap;
        }}
        .scenario-code {{
            font-weight: bold; color: #1565C0;
            font-family: "Consolas", monospace;
        }}
        .scenario-title {{
            font-weight: 600; flex-grow: 1;
        }}
        .scenario-summary {{
            color: #666; font-size: 13px;
        }}
        .scenario-subtitle {{
            padding: 6px 20px;
            color: #888; font-size: 12px; font-style: italic;
            border-bottom: 1px solid #f0f0f0;
        }}

        .badge {{
            padding: 3px 10px; border-radius: 12px;
            font-size: 11px; font-weight: bold;
            color: white;
        }}
        .badge.pass {{ background: #2E7D32; }}
        .badge.fail {{ background: #C62828; }}
        .badge.skip {{ background: #757575; }}
        .badge.norun {{ background: #BDBDBD; }}
        .badge.unknown {{ background: #F9A825; }}

        .tc-table {{
            width: 100%;
            border-collapse: collapse;
        }}
        .tc-table th, .tc-table td {{
            padding: 8px 16px;
            text-align: left;
            border-bottom: 1px solid #f0f0f0;
            font-size: 13px;
        }}
        .tc-table th {{
            background: #fafafa;
            color: #555;
            font-weight: 600;
            font-size: 12px;
        }}
        .tc-table tr.tc-pass .tc-status {{ color: #2E7D32; font-weight: bold; }}
        .tc-table tr.tc-fail .tc-status {{ color: #C62828; font-weight: bold; }}
        .tc-table tr.tc-fail {{ background: #FFEBEE; }}
        .tc-code {{ font-family: "Consolas", monospace; color: #1565C0; }}
        .tc-elapsed {{ color: #888; font-family: "Consolas", monospace; }}
        .tc-error {{
            margin-top: 4px;
            padding: 6px 8px;
            background: #FFF8E1; color: #C62828;
            font-family: "Consolas", monospace;
            font-size: 12px;
            border-radius: 3px;
        }}

        .footer {{
            margin-top: 32px; padding-top: 16px;
            border-top: 1px solid #ddd;
            color: #888; font-size: 12px; text-align: center;
        }}
    </style>
</head>
<body>
    <div class="container">
        <h1>CIMO_PoC Validation Report</h1>
        <div class="subtitle">생성 일시: {ts}</div>

        <div class="summary-box">
            <div class="summary-row">
                <div class="summary-stat overall">
                    <span class="label">종합 결과</span>
                    <span class="value">{overall_str}</span>
                </div>
                <div class="summary-stat">
                    <span class="label">시나리오</span>
                    <span class="value">{n_scenarios}</span>
                </div>
                <div class="summary-stat">
                    <span class="label">전체 TC</span>
                    <span class="value">{n_total}</span>
                </div>
                <div class="summary-stat">
                    <span class="label">PASS</span>
                    <span class="value" style="color: #2E7D32;">{n_pass}</span>
                </div>
                <div class="summary-stat">
                    <span class="label">FAIL</span>
                    <span class="value" style="color: {fail_color};">{n_fail}</span>
                </div>
                <div class="summary-stat">
                    <span class="label">통과율</span>
                    <span class="value">{pass_rate:.1f}%</span>
                </div>
                <div class="summary-stat">
                    <span class="label">소요 시간</span>
                    <span class="value" style="font-size: 20px;">{elapsed:.1f}s</span>
                </div>
            </div>
        </div>

        {scenario_cards}

        <div class="footer">
            CIMO_PoC Validation Suite - generated by run_all.py
        </div>
    </div>
</body>
</html>
""".format(
        ts=timestamp,
        overall_str=overall_str,
        overall_color=overall_color,
        n_scenarios=summary["total_scenarios"],
        n_total=summary["total_tcs"],
        n_pass=summary["pass_tcs"],
        n_fail=summary["fail_tcs"],
        fail_color="#C62828" if summary["fail_tcs"] > 0 else "#888",
        pass_rate=pass_rate,
        elapsed=summary["total_elapsed"],
        scenario_cards="\n".join(scenario_cards),
    )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(html, encoding="utf-8")


def main():
    RUN_START_TIME = time.time()
    parser = argparse.ArgumentParser(description="CIMO_PoC Validation Suite Runner")
    parser.add_argument("--skip", nargs="*", default=[],
                        help="시나리오 코드 (예: --skip TS-0003 TS-0004)")
    parser.add_argument("--only", nargs="*", default=[],
                        help="특정 시나리오만 실행 (예: --only TS-0006)")
    parser.add_argument("--no-html", action="store_true",
                        help="HTML 리포트 생성 안 함")
    args = parser.parse_args()

    # 시나리오 필터링
    scenarios_to_run = []
    for sc in SCENARIOS:
        if args.only and sc["code"] not in args.only:
            continue
        if sc["code"] in args.skip:
            scenarios_to_run.append({**sc, "_skip": True})
            continue
        scenarios_to_run.append({**sc, "_skip": False})

    # 헤더
    banner("CIMO_PoC Validation Suite Runner")
    log_info("실행 시나리오: {}".format(", ".join(sc["code"] for sc in scenarios_to_run if not sc.get("_skip"))))
    if args.skip:
        log_info("스킵 시나리오: {}".format(", ".join(args.skip)))

    # 실행
    scenario_results = []
    for sc in scenarios_to_run:
        if sc.get("_skip"):
            log_warn("스킵: {} ({})".format(sc["code"], sc["title_ko"]))
            scenario_results.append({
                "scenario_code": sc["code"],
                "title_ko":      sc["title_ko"],
                "title_en":      sc["title_en"],
                "executed":      False,
                "skipped":       True,
                "elapsed_sec":   0.0,
            })
            continue

        result = run_scenario(sc)
        # JSON 리포트 로드
        result["json_report"] = load_json_report(sc["code"])
        scenario_results.append(result)

    # 종합 출력
    summary = print_summary_table(scenario_results)

    # HTML 리포트 생성
    if not args.no_html:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        html_path = REPORTS_DIR / "RUN-ALL_{}.html".format(timestamp)
        try:
            generate_html_report(scenario_results, summary, html_path)
            print()
            log_info("📄 HTML 리포트: {}".format(html_path))

            if platform.system() == "Windows":
                # Windows path 변환
                win_path = str(html_path).replace("/", "\\")
                log_info("브라우저로 열어보기: start {}".format(win_path))
            else:
                log_info("브라우저로 열어보기: open {}".format(html_path))
        except Exception as e:
            log_warn("HTML 리포트 생성 실패: {}".format(e))

    print()

    return 0 if summary["overall_pass"] else 1


if __name__ == "__main__":
    sys.exit(main())