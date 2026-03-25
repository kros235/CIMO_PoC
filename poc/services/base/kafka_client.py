# poc/services/base/kafka_client.py
"""
AM 플랫폼 공통 Kafka 클라이언트 모듈
- 모든 Mock Adapter가 공통으로 사용하는 Kafka consumer/producer 래퍼
- 환경변수 기반 설정 (절대경로 사용 금지)
"""

import json
import logging
import os
from typing import Callable, Optional

from confluent_kafka import Consumer, Producer, KafkaException, KafkaError

logger = logging.getLogger(__name__)


def get_kafka_bootstrap_servers() -> str:
    """환경변수에서 Kafka 브로커 주소를 읽어온다. 기본값: kafka:9092"""
    return os.getenv("KAFKA_BOOTSTRAP_SERVERS", "kafka:9092")


def build_producer(extra_config: Optional[dict] = None) -> Producer:
    """
    Kafka Producer 인스턴스를 생성한다.
    :param extra_config: 추가 Kafka 설정 (기본 설정에 병합됨)
    :return: confluent_kafka.Producer
    """
    config = {
        "bootstrap.servers": get_kafka_bootstrap_servers(),
        "acks": "all",                      # 모든 레플리카 확인 후 ack
        "retries": 3,
        "retry.backoff.ms": 300,
        "linger.ms": 5,                     # 배치 효율을 위한 최소 대기
        "batch.size": 16384,
        "compression.type": "snappy",
        "enable.idempotence": True,         # 중복 전송 방지
    }
    if extra_config:
        config.update(extra_config)
    return Producer(config)


def build_consumer(group_id: str, topics: list[str], extra_config: Optional[dict] = None) -> Consumer:
    """
    Kafka Consumer 인스턴스를 생성하고 토픽을 구독한다.
    :param group_id: Consumer Group ID
    :param topics: 구독할 토픽 목록
    :param extra_config: 추가 Kafka 설정
    :return: confluent_kafka.Consumer (구독 상태)
    """
    config = {
        "bootstrap.servers": get_kafka_bootstrap_servers(),
        "group.id": group_id,
        "auto.offset.reset": "earliest",    # 최초 기동 시 처음부터 읽음
        "enable.auto.commit": False,        # 수동 커밋으로 정확한 1회 처리 보장
        "max.poll.interval.ms": 300000,
        "session.timeout.ms": 30000,
        "heartbeat.interval.ms": 10000,
    }
    if extra_config:
        config.update(extra_config)
    consumer = Consumer(config)
    consumer.subscribe(topics)
    logger.info(f"[KafkaClient] Consumer 구독 완료 | group={group_id} | topics={topics}")
    return consumer


def delivery_report(err, msg):
    """Producer 전송 결과 콜백 (로그 기록용)"""
    if err is not None:
        logger.error(f"[KafkaClient] 메시지 전송 실패 | topic={msg.topic()} | error={err}")
    else:
        logger.debug(
            f"[KafkaClient] 메시지 전송 성공 | topic={msg.topic()} "
            f"| partition={msg.partition()} | offset={msg.offset()}"
        )


def produce_message(producer: Producer, topic: str, key: str, value: dict) -> None:
    """
    Kafka 토픽에 메시지를 발행한다.
    :param producer: confluent_kafka.Producer 인스턴스
    :param topic: 발행 대상 토픽명
    :param key: 메시지 키 (txId 사용 권장 — 파티셔닝 기준)
    :param value: 발행할 딕셔너리 (JSON 직렬화됨)
    """
    try:
        producer.produce(
            topic=topic,
            key=key.encode("utf-8") if key else None,
            value=json.dumps(value, ensure_ascii=False).encode("utf-8"),
            callback=delivery_report,
        )
        producer.poll(0)  # 비동기 전송 트리거
    except KafkaException as e:
        logger.error(f"[KafkaClient] produce 실패 | topic={topic} | key={key} | error={e}")
        raise


def flush_producer(producer: Producer, timeout: float = 10.0) -> None:
    """Producer 버퍼에 남은 메시지를 모두 전송하고 완료를 기다린다."""
    remaining = producer.flush(timeout)
    if remaining > 0:
        logger.warning(f"[KafkaClient] flush 후 미전송 메시지 {remaining}건 남음")