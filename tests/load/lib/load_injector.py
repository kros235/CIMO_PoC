 # tests/load/lib/load_injector.py
"""
Day 7 성능 테스트 - 동시성 부하 주입기 (Concurrent Load Injector)

목적:
    NiFi ListenHTTP 엔드포인트(http://localhost:8090/am/send)에 대해
    목표 TPS 또는 최대 속도로 동시 HTTP 요청을 발생시킨다.

왜 이 모듈이 필요한가:
    - 기존 tests/validation/lib/nifi_client.py 의 send_bulk() 는 순차(sequential)
      전송이라, 요청 1건의 왕복 지연(~40ms)에 묶여 초당 ~25건이 한계다.
    - 2,000 TPS 부하를 만들려면 여러 요청을 "동시에" 보내야 한다.
    - ThreadPoolExecutor 로 동시성을 부여하면 I/O 대기 시간을 겹쳐
      목표 처리량을 만들 수 있다 (HTTP 요청은 I/O-bound 라 GIL 영향이 작다).

설계 원칙:
    - 기존 NiFiClient / tx_generator 를 수정 없이 import 재사용
    - 절대경로 사용 금지 (Path 기반 상대 경로 + sys.path 주입)
    - 두 가지 주입 모드 제공:
        run_at_target_tps() : 초당 일정 속도 유지 (페이싱) → 시나리오 A 지속 부하
        run_burst()         : 페이싱 없이 최대 속도 전량 투입 → 피크/대량 일괄

불변 규칙 (사용자 확정):
    - scheduledAt 에 값이 있으면 반드시 sendMethodCode in (01, 02)
    - 실시간(03~05)은 scheduledAt = None
    → 본 주입기의 메시지 생성 헬퍼(build_realtime_messages / build_batch_messages)가
      이 규칙을 강제한다.

사용 예:
    from lib.load_injector import LoadInjector, build_realtime_messages

    msgs = build_realtime_messages(count=10000, channels=["SMS"])
    injector = LoadInjector(max_workers=64)
    stats = injector.run_at_target_tps(msgs, target_tps=2000, duration_sec=10)
    print(stats["achieved_tps"], stats["latency_p95_ms"])
"""

import os
import sys
import time
import threading
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone, timedelta

# ─────────────────────────────────────────────────────────────
# 경로 설정 (절대경로 금지 — 이 파일 위치 기준 상대 경로로 lib 들을 잇는다)
#   tests/load/lib/load_injector.py  →  tests/validation/lib/ 를 sys.path 에 추가
# ─────────────────────────────────────────────────────────────
LOAD_LIB_DIR       = Path(__file__).resolve().parent          # tests/load/lib
TESTS_DIR          = LOAD_LIB_DIR.parent.parent                # tests
VALIDATION_LIB_DIR = TESTS_DIR / "validation" / "lib"          # tests/validation/lib

for _p in (str(VALIDATION_LIB_DIR),):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# 기존 검증 라이브러리 재사용 (수정하지 않음)
from nifi_client import NiFiClient            # noqa: E402
from tx_generator import build_tx_id          # noqa: E402


KST = timezone(timedelta(hours=9))

# 채널별 기본 수신번호/수신처 생성용
_CHANNEL_RECEIVERS = {
    "SMS":   lambda i: f"0101000{i % 10000:04d}",
    "MMS":   lambda i: f"0102000{i % 10000:04d}",
    "RCS":   lambda i: f"0103000{i % 10000:04d}",
    "FAX":   lambda i: f"0204000{i % 10000:04d}",
    "EMAIL": lambda i: f"loadtest{i % 10000:04d}@example.com",
}


# ─────────────────────────────────────────────────────────────
# 메시지 생성 헬퍼
# ─────────────────────────────────────────────────────────────
def build_realtime_messages(count: int, channels=None, sender_code: str = "007") -> list:
    """
    실시간 발송 메시지 N건 생성 (sendMethodCode=03, scheduledAt=None).

    불변 규칙 준수: 실시간은 scheduledAt 을 절대 세팅하지 않는다.

    Args:
        count:       생성할 메시지 수
        channels:    분배할 채널 리스트 (기본 ["SMS"])
        sender_code: txId 발송처 코드 3자리

    Returns:
        [{"tx_id", "channel", "receiver", "send_method_code", "scheduled_at"}, ...]
        (NiFiClient.send_one 의 **payload_overrides 로 펼쳐 넣기 좋은 형태)
    """
    if channels is None:
        channels = ["SMS"]

    messages = []
    for i in range(count):
        channel = channels[i % len(channels)]
        recv_fn = _CHANNEL_RECEIVERS.get(channel, _CHANNEL_RECEIVERS["SMS"])
        tx_id = build_tx_id("03", sender_code)        # 03 = 실시간
        messages.append({
            "tx_id":            tx_id,
            "channel":          channel,
            "receiver":         recv_fn(i),
            "message_body":     f"Day7 realtime load #{i}",
            "source":           "LOADTEST_REALTIME",
            "scheduled_at":     None,                 # 실시간 → 반드시 None
        })
    return messages


def build_batch_messages(
    count: int,
    scheduled_at_iso: str,
    channels=None,
    send_method_code: str = "01",
    sender_code: str = "001",
) -> list:
    """
    배치(예약) 발송 메시지 N건 생성 (sendMethodCode=01/02, scheduledAt=지정 시각).

    불변 규칙 준수: scheduledAt 에 값이 있으므로 sendMethodCode 는 반드시 01 또는 02.

    Args:
        count:            생성할 메시지 수
        scheduled_at_iso: 예약 발송 시각 (ISO-8601, 예: '2026-05-28T14:30:00+09:00')
        channels:         분배할 채널 리스트 (기본 ["SMS"])
        send_method_code: "01" 또는 "02" 만 허용
        sender_code:      txId 발송처 코드 3자리

    Returns:
        build_realtime_messages 와 동일 구조 (단, scheduled_at 에 값 존재)

    Raises:
        ValueError: send_method_code 가 01/02 가 아니면 (불변 규칙 위반)
    """
    if send_method_code not in ("01", "02"):
        raise ValueError(
            f"배치 발송의 sendMethodCode 는 01 또는 02 여야 한다 (받은 값: '{send_method_code}'). "
            f"불변 규칙: scheduledAt 값이 있으면 반드시 01/02."
        )
    if channels is None:
        channels = ["SMS"]

    messages = []
    for i in range(count):
        channel = channels[i % len(channels)]
        recv_fn = _CHANNEL_RECEIVERS.get(channel, _CHANNEL_RECEIVERS["SMS"])
        tx_id = build_tx_id(send_method_code, sender_code)
        messages.append({
            "tx_id":            tx_id,
            "channel":          channel,
            "receiver":         recv_fn(i),
            "message_body":     f"Day7 batch load #{i}",
            "source":           "LOADTEST_BATCH",
            "scheduled_at":     scheduled_at_iso,     # 배치 → 예약 시각 세팅
        })
    return messages


# ─────────────────────────────────────────────────────────────
# 지연(latency) 백분위 계산 헬퍼
# ─────────────────────────────────────────────────────────────
def _percentile(sorted_values: list, pct: float) -> float:
    """정렬된 리스트에서 pct(0~100) 백분위 값을 선형 보간 없이 근사 반환."""
    if not sorted_values:
        return 0.0
    if pct <= 0:
        return sorted_values[0]
    if pct >= 100:
        return sorted_values[-1]
    # nearest-rank 방식
    k = max(0, min(len(sorted_values) - 1,
                   int(round((pct / 100.0) * len(sorted_values) + 0.5)) - 1))
    return sorted_values[k]


# ─────────────────────────────────────────────────────────────
# 동시성 주입기 본체
# ─────────────────────────────────────────────────────────────
class LoadInjector:
    """
    ThreadPoolExecutor 기반 동시 HTTP 주입기.

    - NiFiClient 를 워커마다 1개씩 두어 requests.Session 을 재사용한다
      (스레드별 Session 이 안전하고 빠르다).
    - 각 요청의 (success, http_status, elapsed_ms) 를 수집한다.
    """

    def __init__(self, max_workers: int = 64, base_url: str = None):
        self.max_workers = max_workers
        self.base_url = base_url
        # 스레드 로컬에 NiFiClient 보관 (Session 스레드 안전성 확보)
        self._local = threading.local()
        # 결과 수집용 락 + 리스트
        self._lock = threading.Lock()
        self._results = []

    def _client(self) -> NiFiClient:
        """현재 스레드 전용 NiFiClient 반환 (없으면 생성)."""
        if not hasattr(self._local, "client"):
            if self.base_url:
                self._local.client = NiFiClient(base_url=self.base_url)
            else:
                self._local.client = NiFiClient()
        return self._local.client

    def _send_one(self, msg: dict) -> dict:
        """단일 메시지 전송 (스레드에서 실행)."""
        m = dict(msg)  # 원본 보호 (pop 부작용 방지)
        tx_id    = m.pop("tx_id")
        channel  = m.pop("channel")
        receiver = m.pop("receiver")
        client = self._client()
        result = client.send_one(tx_id=tx_id, channel=channel, receiver=receiver, **m)
        return result

    def _reset(self):
        with self._lock:
            self._results = []

    def _collect(self, result: dict):
        with self._lock:
            self._results.append(result)

    # ─────────────────────────────────────────────────────────
    # 모드 1: 목표 TPS 유지 (페이싱)
    # ─────────────────────────────────────────────────────────
    def run_at_target_tps(
        self,
        messages: list,
        target_tps: int,
        duration_sec: int = None,
        progress_callback=None,
    ) -> dict:
        """
        초당 target_tps 건 속도로 messages 를 투입한다.

        동작:
            - 1초 단위 윈도우로 나눠 매 초 target_tps 건씩 풀에 제출
            - 해당 초의 제출이 끝나면 다음 초 경계까지 sleep (페이싱)
            - duration_sec 지정 시 그 시간만큼만 투입 (messages 가 모자라면 순환 재사용)
            - duration_sec=None 이면 messages 전량 소진까지

        Args:
            messages:          투입할 메시지 리스트
            target_tps:        목표 초당 요청 수
            duration_sec:      투입 지속 시간(초). None 이면 전량 투입
            progress_callback: fn(sec_index, submitted, success, fail) 매 초 호출

        Returns:
            통계 dict (아래 _summarize 참조)
        """
        self._reset()

        if duration_sec is not None:
            total_to_send = target_tps * duration_sec
        else:
            total_to_send = len(messages)
            duration_sec = (total_to_send + target_tps - 1) // target_tps  # 올림

        t_start = time.monotonic()
        submitted = 0
        msg_len = len(messages)

        with ThreadPoolExecutor(max_workers=self.max_workers) as pool:
            futures = []
            for sec in range(duration_sec):
                sec_window_start = t_start + sec  # 이 초의 시작 기준 시각

                # 이번 초에 보낼 건수 (마지막 초는 남은 만큼만)
                remaining = total_to_send - submitted
                this_sec_count = min(target_tps, remaining)
                if this_sec_count <= 0:
                    break

                for _ in range(this_sec_count):
                    msg = messages[submitted % msg_len]
                    futures.append(pool.submit(self._send_one, msg))
                    submitted += 1

                # 매 초 진행 상황 콜백 (현재까지 완료분 기준)
                if progress_callback:
                    done = [f for f in futures if f.done()]
                    succ = sum(1 for f in done if f.result().get("success"))
                    progress_callback(
                        sec_index=sec + 1,
                        submitted=submitted,
                        success=succ,
                        fail=len(done) - succ,
                    )

                # 다음 초 경계까지 페이싱 (이미 지났으면 대기 없음)
                next_boundary = t_start + (sec + 1)
                sleep_for = next_boundary - time.monotonic()
                if sleep_for > 0:
                    time.sleep(sleep_for)

            # 남은 future 수집
            for f in as_completed(futures):
                self._collect(f.result())

        wall_elapsed = time.monotonic() - t_start
        return self._summarize(wall_elapsed_sec=wall_elapsed, target_tps=target_tps)

    # ─────────────────────────────────────────────────────────
    # 모드 2: 최대 속도 전량 투입 (페이싱 없음)
    # ─────────────────────────────────────────────────────────
    def run_burst(
        self,
        messages: list,
        progress_every: int = 1000,
        progress_callback=None,
    ) -> dict:
        """
        페이싱 없이 messages 전량을 가능한 한 빨리 투입한다.
        피크 트래픽(순간 폭주) / 대량 일괄 투입 측정에 사용.

        Args:
            messages:          투입할 메시지 리스트
            progress_every:    N건 완료마다 진행 콜백 호출
            progress_callback: fn(done, success, fail) 호출

        Returns:
            통계 dict
        """
        self._reset()
        t_start = time.monotonic()
        done = 0
        success = 0

        with ThreadPoolExecutor(max_workers=self.max_workers) as pool:
            futures = [pool.submit(self._send_one, m) for m in messages]
            for f in as_completed(futures):
                res = f.result()
                self._collect(res)
                done += 1
                if res.get("success"):
                    success += 1
                if progress_callback and (done % progress_every == 0):
                    progress_callback(done=done, success=success, fail=done - success)

        wall_elapsed = time.monotonic() - t_start
        return self._summarize(wall_elapsed_sec=wall_elapsed, target_tps=None)

    # ─────────────────────────────────────────────────────────
    # 통계 집계
    # ─────────────────────────────────────────────────────────
    def _summarize(self, wall_elapsed_sec: float, target_tps: int = None) -> dict:
        with self._lock:
            results = list(self._results)

        total = len(results)
        success = sum(1 for r in results if r.get("success"))
        fail = total - success

        latencies = sorted(r.get("elapsed_ms", 0.0) for r in results)
        avg_latency = (sum(latencies) / total) if total else 0.0

        achieved_tps = (total / wall_elapsed_sec) if wall_elapsed_sec > 0 else 0.0

        # 실패 사유 상위 집계 (HTTP status / error 메시지)
        fail_reasons = {}
        for r in results:
            if not r.get("success"):
                key = r.get("error") or f"HTTP {r.get('http_status')}"
                fail_reasons[key] = fail_reasons.get(key, 0) + 1

        summary = {
            "total":              total,
            "success":            success,
            "fail":               fail,
            "success_rate":       round(success / total, 4) if total else 0.0,
            "wall_elapsed_sec":   round(wall_elapsed_sec, 2),
            "achieved_tps":       round(achieved_tps, 1),
            "latency_avg_ms":     round(avg_latency, 2),
            "latency_p50_ms":     round(_percentile(latencies, 50), 2),
            "latency_p95_ms":     round(_percentile(latencies, 95), 2),
            "latency_p99_ms":     round(_percentile(latencies, 99), 2),
            "latency_max_ms":     round(latencies[-1], 2) if latencies else 0.0,
            "fail_reasons":       dict(sorted(fail_reasons.items(),
                                              key=lambda kv: kv[1], reverse=True)[:10]),
        }
        if target_tps is not None:
            summary["target_tps"] = target_tps
            summary["tps_achievement_ratio"] = (
                round(achieved_tps / target_tps, 4) if target_tps else 0.0
            )
        return summary

    def close(self):
        """스레드 로컬 클라이언트 정리는 풀 종료 시 자동되므로 no-op (호환용)."""
        pass


if __name__ == "__main__":
    # 자가 점검: NiFi 헬스체크 + 소량 버스트 + 목표 TPS 짧은 투입
    print("=== load_injector self-check ===")
    print(f"validation lib path: {VALIDATION_LIB_DIR}")

    probe = NiFiClient()
    healthy = probe.health_check()
    print(f"NiFi health: {'OK' if healthy else 'FAIL (NiFi 8090 미기동?)'}")

    if not healthy:
        print("NiFi 가 기동되어 있지 않아 실제 투입 테스트는 건너뜁니다.")
        sys.exit(0)

    # 1) 버스트 모드 100건
    msgs = build_realtime_messages(count=100, channels=["SMS"])
    inj = LoadInjector(max_workers=32)
    burst_stats = inj.run_burst(msgs)
    print(f"[burst]  {burst_stats}")

    # 2) 목표 TPS 모드: 200 TPS x 2초
    msgs2 = build_realtime_messages(count=400, channels=["SMS"])
    inj2 = LoadInjector(max_workers=32)
    tps_stats = inj2.run_at_target_tps(msgs2, target_tps=200, duration_sec=2)
    print(f"[tps]    {tps_stats}")

    print("=== self-check done ===")