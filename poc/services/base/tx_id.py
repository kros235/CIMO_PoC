# poc/services/base/tx_id.py
"""
AM 플랫폼 35자리 트랜잭션 ID 생성 및 검증 모듈

트랜잭션 ID 구조 (총 35자리):
  [메시지ID 13자리] [발송방법코드 2자리] [일자카운트 3자리] [발송처코드 3자리] [시퀀스 14자리]

예시:
  1234567890123  03  084  007  00000000000001
  └─ 13자리 ──┘ └2┘ └3┘ └3┘ └──── 14자리 ────┘  = 총 35자리

발송 방법 코드:
  01: 배치성 (EAI Queue 인입형)
  02: 배치성 (I/F 테이블 polling형)
  03: 온라인(실시간) 발송
  04: 준실시간 (Rater)
  05: 준실시간 (KOS-Online)
"""

import re
import time
import threading
from datetime import datetime, timezone, timedelta

# 한국 표준시 (KST = UTC+9)
KST = timezone(timedelta(hours=9))

# 유효한 발송 방법 코드
VALID_SEND_METHOD_CODES = {"01", "02", "03", "04", "05"}

# 시퀀스 자릿수
_SEQ_DIGITS = 14
_SEQ_MAX = 10 ** _SEQ_DIGITS  # 최대 시퀀스 값 (초과 시 wrap-around)

# 스레드 안전한 시퀀스 카운터
_lock = threading.Lock()
_sequence_counter: int = 0


def _next_sequence() -> int:
    """스레드 안전한 시퀀스 번호를 반환한다."""
    global _sequence_counter
    with _lock:
        _sequence_counter = (_sequence_counter + 1) % _SEQ_MAX
        return _sequence_counter


def day_of_year(dt: datetime = None) -> int:
    """
    주어진 날짜가 365일(366일) 중 몇 번째 날인지 반환한다.
    :param dt: 기준 datetime (None이면 KST 현재 시각)
    :return: 1~366 사이의 정수
    """
    if dt is None:
        dt = datetime.now(KST)
    return dt.timetuple().tm_yday


def generate_message_id() -> str:
    """
    13자리 메시지 ID를 생성한다.
    epoch 밀리초(13자리) 기반으로 생성하여 유니크성을 확보한다.
    :return: 13자리 숫자 문자열
    """
    ms = int(time.time() * 1000)          # epoch milliseconds = 13자리
    return str(ms)[-13:].zfill(13)       # 13자리 고정


def build_tx_id(
    send_method_code: str,
    sender_code: str,
    message_id: str = None,
    sequence: int = None,
    dt: datetime = None,
) -> str:
    """
    35자리 트랜잭션 ID를 생성한다.

    :param send_method_code: 발송 방법 코드 2자리 (01~05)
    :param sender_code: 발송처 코드 3자리 숫자 문자열 (예: '007')
    :param message_id: 메시지 ID 13자리 (None이면 자동 생성)
    :param sequence: 시퀀스 번호 (None이면 자동 증가)
    :param dt: 기준 날짜 datetime (None이면 KST 현재)
    :return: 35자리 트랜잭션 ID 문자열
    :raises ValueError: 입력값 형식 오류 시
    """
    # 발송 방법 코드 검증
    if send_method_code not in VALID_SEND_METHOD_CODES:
        raise ValueError(
            f"유효하지 않은 발송방법코드: '{send_method_code}'. "
            f"허용값: {sorted(VALID_SEND_METHOD_CODES)}"
        )

    # 발송처 코드 검증 및 패딩
    sender_code_str = str(sender_code).zfill(3)
    if not sender_code_str.isdigit() or len(sender_code_str) != 3:
        raise ValueError(f"발송처코드는 3자리 숫자여야 합니다. 입력값: '{sender_code}'")

    # 메시지 ID (13자리)
    if message_id is None:
        message_id = generate_message_id()
    msg_id_str = str(message_id).zfill(13)
    if len(msg_id_str) != 13 or not msg_id_str.isdigit():
        raise ValueError(f"메시지ID는 13자리 숫자여야 합니다. 입력값: '{message_id}'")

    # 일자 카운트 (3자리, 1~366)
    day_count = day_of_year(dt)
    day_count_str = str(day_count).zfill(3)

    # 시퀀스 (14자리)
    if sequence is None:
        sequence = _next_sequence()
    seq_str = str(sequence).zfill(_SEQ_DIGITS)
    if len(seq_str) > _SEQ_DIGITS:
        raise ValueError(f"시퀀스가 {_SEQ_DIGITS}자리를 초과했습니다. 입력값: {sequence}")

    tx_id = f"{msg_id_str}{send_method_code}{day_count_str}{sender_code_str}{seq_str}"

    # 최종 길이 검증
    assert len(tx_id) == 35, (
        f"트랜잭션 ID 길이 오류: 생성된 ID는 {len(tx_id)}자리. "
        f"구성: msgId({len(msg_id_str)}) + code({len(send_method_code)}) "
        f"+ day({len(day_count_str)}) + sender({len(sender_code_str)}) + seq({len(seq_str)})"
    )

    return tx_id


def validate_tx_id(tx_id: str) -> bool:
    """
    트랜잭션 ID가 35자리 숫자 형식인지 검증한다.
    :param tx_id: 검증할 트랜잭션 ID
    :return: 유효하면 True, 아니면 False
    """
    if not tx_id or not isinstance(tx_id, str):
        return False
    if len(tx_id) != 35:
        return False
    if not tx_id.isdigit():
        return False
    # 발송 방법 코드 (13~14번째 자리, 0-indexed: [13:15])
    method_code = tx_id[13:15]
    if method_code not in VALID_SEND_METHOD_CODES:
        return False
    return True


def parse_tx_id(tx_id: str) -> dict:
    """
    35자리 트랜잭션 ID를 구성 요소별로 파싱한다.
    :param tx_id: 35자리 트랜잭션 ID
    :return: 구성 요소 딕셔너리
    :raises ValueError: 유효하지 않은 트랜잭션 ID
    """
    if not validate_tx_id(tx_id):
        raise ValueError(f"유효하지 않은 트랜잭션 ID: '{tx_id}' (35자리 숫자, 방법코드 01~05 필요)")

    return {
        "tx_id":            tx_id,
        "message_id":       tx_id[0:13],    # 13자리
        "send_method_code": tx_id[13:15],   # 2자리 (01~05)
        "day_of_year":      int(tx_id[15:18]),  # 3자리 (001~366)
        "sender_code":      tx_id[18:21],   # 3자리
        "sequence":         int(tx_id[21:35]),  # 14자리
    }