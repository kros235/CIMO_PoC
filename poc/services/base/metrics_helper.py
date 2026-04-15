# poc/services/base/metrics_helper.py
"""
Prometheus 메트릭 중복 등록 방지 헬퍼.
uvicorn 재시작 시 CollectorRegistry에 이미 등록된 메트릭을 재사용합니다.
"""
from prometheus_client import Counter, Histogram, REGISTRY


def get_or_create_counter(name: str, documentation: str, labelnames=()) -> Counter:
    if name in REGISTRY._names_to_collectors:
        return REGISTRY._names_to_collectors[name]
    return Counter(name, documentation, labelnames)


def get_or_create_histogram(name: str, documentation: str, buckets=None) -> Histogram:
    if name in REGISTRY._names_to_collectors:
        return REGISTRY._names_to_collectors[name]
    if buckets:
        return Histogram(name, documentation, buckets=buckets)
    return Histogram(name, documentation)