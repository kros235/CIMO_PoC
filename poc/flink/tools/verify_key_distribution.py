#!/usr/bin/env python3
"""
Flink KeyGroup 배정 검증 도구 (Day 8 작업3에서 신규 작성)

배경:
    RateLimitOperator는 채널 이름으로 keyBy되는데, Flink의 배정 계산
    (MurmurHash 기반)이 어떤 채널 이름 조합·maxParallelism 값에서는
    서로 다른 채널을 같은 일꾼(subtask)에 겹치게 배정할 수 있다.
    이 겹침은 무작위가 아니라 고정된 공식에 따른 결정적 결과라서,
    재배포·재시도로는 해결되지 않는다 (RequestPipelineBuilder.java의
    주석 참고).

    이 스크립트는 Flink가 내부적으로 쓰는 계산 공식
    (org.apache.flink.util.MathUtils.murmurHash,
     org.apache.flink.runtime.state.KeyGroupRangeAssignment)을 그대로
    파이썬으로 재현하여, 실제 Flink Job을 띄우지 않고도 채널 이름들이
    서로 겹치지 않는 maxParallelism 값을 미리 찾을 수 있게 해준다.

사용 시점:
    - 채널이 추가/제거/이름 변경될 때
    - RateLimitOperator의 parallelism을 변경할 때
    - Flink UI에서 채널별 일꾼 배정이 불균등하다고 의심될 때

How to run:
    python poc/flink/tools/verify_key_distribution.py
"""

CHANNELS = ["SMS", "MMS", "RCS", "FAX", "EMAIL"]
PARALLELISM = 5
CURRENT_MAX_PARALLELISM = 15  # RequestPipelineBuilder.java와 반드시 일치시킬 것


def java_string_hashcode(s: str) -> int:
    """Java의 String.hashCode()를 그대로 재현 (32비트 부호있는 정수, 오버플로 포함)."""
    h = 0
    for c in s:
        h = (31 * h + ord(c)) & 0xFFFFFFFF
    if h >= 0x80000000:
        h -= 0x100000000
    return h


def _rotl32(x: int, n: int) -> int:
    x &= 0xFFFFFFFF
    return ((x << n) | (x >> (32 - n))) & 0xFFFFFFFF


def _bitmix(code: int) -> int:
    code &= 0xFFFFFFFF
    code ^= code >> 16
    code = (code * 0x85EBCA6B) & 0xFFFFFFFF
    code ^= code >> 13
    code = (code * 0xC2B2AE35) & 0xFFFFFFFF
    code ^= code >> 16
    return code


def murmur_hash(code: int) -> int:
    """org.apache.flink.util.MathUtils.murmurHash(int) 재현."""
    code &= 0xFFFFFFFF
    code = (code * 0xCC9E2D51) & 0xFFFFFFFF
    code = _rotl32(code, 15)
    code = (code * 0x1B873593) & 0xFFFFFFFF
    code = _rotl32(code, 13)
    code = (code * 5 + 0xE6546B64) & 0xFFFFFFFF
    code ^= 4
    code = _bitmix(code)
    signed = code - 0x100000000 if code >= 0x80000000 else code
    return -signed if signed < 0 else signed


def key_group(key_str: str, max_parallelism: int) -> int:
    """org.apache.flink.runtime.state.KeyGroupRangeAssignment.assignToKeyGroup() 재현."""
    return murmur_hash(java_string_hashcode(key_str)) % max_parallelism


def subtask_index(key_str: str, max_parallelism: int, parallelism: int) -> int:
    """KeyGroupRangeAssignment.computeOperatorIndexForKeyGroup() 재현."""
    kg = key_group(key_str, max_parallelism)
    return (kg * parallelism) // max_parallelism


def check_distribution(channels, max_parallelism, parallelism) -> bool:
    subtasks = {ch: subtask_index(ch, max_parallelism, parallelism) for ch in channels}
    is_even = len(set(subtasks.values())) == len(channels)
    print(f"maxParallelism={max_parallelism}, parallelism={parallelism}")
    for ch, st in subtasks.items():
        print(f"  {ch:6s} -> subtask {st}")
    print("  " + ("✅ 전부 다른 일꾼으로 배정됨" if is_even else "⚠️  겹치는 채널이 있음"))
    return is_even


def find_collision_free_max_parallelism(channels, parallelism, search_upto=500):
    """channels가 서로 겹치지 않는 maxParallelism 값을 오름차순으로 찾는다."""
    found = []
    for mp in range(parallelism, search_upto):
        subtasks = [subtask_index(ch, mp, parallelism) for ch in channels]
        if len(set(subtasks)) == len(channels):
            found.append(mp)
    return found


if __name__ == "__main__":
    print("=== 현재 설정값 검증 ===")
    check_distribution(CHANNELS, CURRENT_MAX_PARALLELISM, PARALLELISM)

    print()
    print(f"=== 겹치지 않는 maxParallelism 후보 (최대 {500}까지 탐색) ===")
    candidates = find_collision_free_max_parallelism(CHANNELS, PARALLELISM)
    print(candidates[:20], "..." if len(candidates) > 20 else "")
