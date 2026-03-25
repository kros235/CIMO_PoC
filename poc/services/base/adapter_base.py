# poc/services/base/adapter_base.py
"""
AM 플랫폼 Mock Adapter 공통 베이스 클래스
- 모든 채널 Adapter(SMS/MMS/RCS/FAX/Email)는 이 클래스를 상속받아 구현한다.
- Kafka Consumer/Producer 라이프사이클 관리
- 발송 시뮬레이션 (성공률, 응답 지연 설정 가능)
- 공통 결과 포맷(JSON) 생성 및 result 토픽 발행
- 35자리 txId 기반 추적 로그 기록
"""

import asyncio
import json
import logging
import os
import random
import time
from abc import ABC, abstractmethod
from datetime import datetime, timezone, timedelta
from typing import Optional

from confluent_kafka import KafkaError

from .kafka_client import build_consumer, build_producer, produce_message, flush_producer
from .tx_id import validate_tx_id

logger = logging.getLogger(__name__)

# 한국 표준시 (KST = UTC+9)
KST = timezone(timedelta(hours=9))

# 공통 Kafka 토픽명 (환경변수 오버라이드 가능)
TOPIC_RESULT = os.getenv("KAFKA_TOPIC_RESULT", "topic.send.result")
TOPIC_RETRY  = os.getenv("KAFKA_TOPIC_RETRY",  "topic.send.retry")

# 결과 코드 정의 (AM 플랫폼 표준)
RESULT_CODE_SUCCESS        = "10000"   # 정상 전송 완료
RESULT_CODE_FAIL_INVALID   = "40001"   # 수신번호 오류
RESULT_CODE_FAIL_TIMEOUT   = "40002"   # 타임아웃
RESULT_CODE_FAIL_NETWORK   = "40003"   # 네트워크 오류
RESULT_CODE_RETRY_NEEDED   = "50001"   # 재처리 필요 (일시적 오류)


class AdapterBase(ABC):
    """
    채널별 Mock Adapter 공통 베이스 클래스.
    서브클래스는 channel_name, dispatch_topic, success_rate, base_delay_ms를 설정하고,
    _simulate_send() 메서드를 오버라이드하여 채널별 발송 로직을 구현한다.
    """

    # 서브클래스에서 반드시 설정해야 하는 속성
    channel_name: str = "UNKNOWN"          # 채널 이름 (SMS, MMS, RCS, FAX, EMAIL)
    dispatch_topic: str = ""               # 구독할 Kafka dispatch 토픽
    success_rate: float = 0.95             # 발송 성공률 (0.0 ~ 1.0)
    base_delay_ms: int = 50               # 기본 응답 지연 (ms)
    group_id: str = ""                     # Kafka Consumer Group ID

    def __init__(self):
        self._producer = None
        self._consumer = None
        self._running = False

        # 환경변수로 성공률/지연 오버라이드 지원
        env_rate = os.getenv(f"{self.channel_name}_SUCCESS_RATE")
        if env_rate is not None:
            self.success_rate = float(env_rate)

        env_delay = os.getenv(f"{self.channel_name}_DELAY_MS")
        if env_delay is not None:
            self.base_delay_ms = int(env_delay)

        logger.info(
            f"[{self.channel_name}Adapter] 초기화 | "
            f"성공률={self.success_rate:.0%} | 기본지연={self.base_delay_ms}ms"
        )

    def start(self):
        """Kafka Consumer/Producer 초기화 및 메시지 처리 루프 시작"""
        self._producer = build_producer()
        self._consumer = build_consumer(
            group_id=self.group_id or f"am-{self.channel_name.lower()}-adapter",
            topics=[self.dispatch_topic],
        )
        self._running = True
        logger.info(f"[{self.channel_name}Adapter] 기동 완료 — dispatch 토픽 구독 중: {self.dispatch_topic}")
        self._poll_loop()

    def stop(self):
        """Adapter 종료 — consumer/producer 정리"""
        self._running = False
        if self._consumer:
            self._consumer.close()
        if self._producer:
            flush_producer(self._producer)
        logger.info(f"[{self.channel_name}Adapter] 종료 완료")

    def _poll_loop(self):
        """Kafka dispatch 토픽을 폴링하며 메시지를 처리한다."""
        logger.info(f"[{self.channel_name}Adapter] 폴링 루프 시작")
        while self._running:
            msg = self._consumer.poll(timeout=1.0)
            if msg is None:
                continue
            if msg.error():
                if msg.error().code() == KafkaError._PARTITION_EOF:
                    continue
                logger.error(f"[{self.channel_name}Adapter] Kafka 오류: {msg.error()}")
                continue

            try:
                payload = json.loads(msg.value().decode("utf-8"))
                self._process_message(payload)
                self._consumer.commit(asynchronous=False)  # 수동 커밋 (정확한 1회 처리)
            except Exception as e:
                logger.exception(
                    f"[{self.channel_name}Adapter] 메시지 처리 오류 | "
                    f"offset={msg.offset()} | error={e}"
                )
                # 예외 발생 시에도 커밋 (무한 재시도 방지 — retry는 Flink RetryJob이 담당)
                self._consumer.commit(asynchronous=False)

    def _process_message(self, payload: dict):
        """
        dispatch 토픽에서 수신한 메시지를 처리한다.
        1. txId 검증 (35자리)
        2. 발송 시뮬레이션 (성공률/지연)
        3. 결과 포맷 생성
        4. result 토픽 발행
        """
        tx_id = payload.get("txId", "")
        receiver = payload.get("receiver", "")
        sender = payload.get("sender", "")

        # txId 검증 — 35자리 구조 확인
        if not validate_tx_id(tx_id):
            logger.error(
                f"[{self.channel_name}Adapter] 유효하지 않은 txId 수신 | "
                f"txId='{tx_id}' | receiver={receiver}"
            )
            # 유효하지 않은 txId는 DLQ로 직접 이동 (재처리 불가)
            self._publish_result(
                tx_id=tx_id or "INVALID_TX_ID",
                payload=payload,
                success=False,
                result_code=RESULT_CODE_FAIL_INVALID,
                error_detail="txId 형식 오류 (35자리 숫자 아님)",
                elapsed_ms=0,
            )
            return

        logger.debug(
            f"[{self.channel_name}Adapter] 메시지 수신 | txId={tx_id} | receiver={receiver}"
        )

        # 채널별 발송 시뮬레이션 수행
        start_time = time.monotonic()
        success, result_code, error_detail = self._simulate_send(payload)
        elapsed_ms = int((time.monotonic() - start_time) * 1000)

        # 결과 발행
        self._publish_result(
            tx_id=tx_id,
            payload=payload,
            success=success,
            result_code=result_code,
            error_detail=error_detail,
            elapsed_ms=elapsed_ms,
        )

    def _simulate_send(self, payload: dict) -> tuple[bool, str, Optional[str]]:
        """
        발송 시뮬레이션을 수행한다 (서브클래스에서 오버라이드 가능).
        :return: (성공여부, 결과코드, 오류상세)
        """
        # 기본 응답 지연 시뮬레이션 (base_delay_ms ± 20% 랜덤)
        jitter = random.uniform(0.8, 1.2)
        delay_s = (self.base_delay_ms * jitter) / 1000.0
        time.sleep(delay_s)

        # 성공률 기반 결과 결정
        rand = random.random()
        if rand < self.success_rate:
            return True, RESULT_CODE_SUCCESS, None
        elif rand < self.success_rate + 0.03:
            # 3% 확률로 일시적 오류 (재처리 대상)
            return False, RESULT_CODE_RETRY_NEEDED, "일시적 네트워크 오류"
        else:
            # 나머지는 영구 실패
            return False, RESULT_CODE_FAIL_NETWORK, "수신 단말 오류"

    def _publish_result(
        self,
        tx_id: str,
        payload: dict,
        success: bool,
        result_code: str,
        error_detail: Optional[str],
        elapsed_ms: int,
    ):
        """
        발송 결과를 topic.send.result 토픽에 발행한다.
        결과 코드가 5xxxx이면 retry 토픽에도 발행한다.
        """
        now_kst = datetime.now(KST).isoformat()
        status = "DELIVERED" if success else (
            "RETRYING" if result_code.startswith("5") else "FAILED"
        )

        result_payload = {
            "txId":          tx_id,
            "channel":       self.channel_name,
            "receiver":      payload.get("receiver", ""),
            "sender":        payload.get("sender", ""),
            "status":        status,
            "resultCode":    result_code,
            "errorDetail":   error_detail,
            "retryCount":    payload.get("meta", {}).get("retryCount", 0),
            "source":        payload.get("source", ""),
            "dispatchedAt":  payload.get("dispatchedAt", now_kst),
            "deliveredAt":   now_kst,
            "elapsedMs":     elapsed_ms,
            "adapterVersion": "1.0.0",
        }

        # result 토픽 발행 (항상)
        produce_message(self._producer, TOPIC_RESULT, tx_id, result_payload)
        logger.info(
            f"[{self.channel_name}Adapter] 발송 결과 발행 | "
            f"txId={tx_id} | status={status} | code={result_code} | elapsed={elapsed_ms}ms"
        )

        # 재처리 대상(5xxxx)이면 retry 토픽에도 발행
        if result_code.startswith("5"):
            retry_payload = {**payload, "retryResultCode": result_code}
            produce_message(self._producer, TOPIC_RETRY, tx_id, retry_payload)
            logger.info(
                f"[{self.channel_name}Adapter] retry 토픽 발행 | txId={tx_id}"
            )

    def get_health(self) -> dict:
        """헬스체크 응답용 상태 딕셔너리를 반환한다."""
        return {
            "status": "ok" if self._running else "stopped",
            "channel": self.channel_name,
            "dispatchTopic": self.dispatch_topic,
            "successRate": self.success_rate,
            "baseDelayMs": self.base_delay_ms,
        }