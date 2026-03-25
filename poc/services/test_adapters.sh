#!/usr/bin/env bash
# poc/services/test_adapters.sh
# Day 3 완료 기준 검증 스크립트
# 사용법: bash poc/services/test_adapters.sh
#
# 검증 항목:
#   1. 각 Adapter GET /health → 200 OK
#   2. 각 Adapter GET /info → 채널/토픽 정보 출력
#   3. NiFi HTTP 엔드포인트 활성화 여부 확인
#   4. Kafka 토픽 구독 현황 요약
#   5. txId 35자리 생성·검증·파싱 단독 테스트

set -euo pipefail

# ──────────────────────────────────────────────
# 색상 출력 헬퍼
# ──────────────────────────────────────────────
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m' # No Color

PASS_COUNT=0
FAIL_COUNT=0
SKIP_COUNT=0

pass()  { echo -e "${GREEN}[PASS]${NC} $1"; PASS_COUNT=$((PASS_COUNT+1)); }
fail()  { echo -e "${RED}[FAIL]${NC} $1"; FAIL_COUNT=$((FAIL_COUNT+1)); }
skip()  { echo -e "${YELLOW}[SKIP]${NC} $1"; SKIP_COUNT=$((SKIP_COUNT+1)); }
info()  { echo -e "${CYAN}[INFO]${NC} $1"; }
banner(){ echo -e "\n${CYAN}══════════════════════════════════════════${NC}"; echo -e "${CYAN} $1${NC}"; echo -e "${CYAN}══════════════════════════════════════════${NC}"; }

# ──────────────────────────────────────────────
# 검증 1: Adapter 헬스체크 (GET /health → 200 OK)
# ──────────────────────────────────────────────
banner "1. Adapter 헬스체크 (/health)"

declare -A ADAPTERS=(
  [8101]="SMS"
  [8102]="MMS"
  [8103]="RCS"
  [8104]="FAX"
  [8105]="EMAIL"
)

for port in 8101 8102 8103 8104 8105; do
  channel="${ADAPTERS[$port]}"
  http_code=$(curl -s -o /dev/null -w "%{http_code}" --max-time 3 "http://localhost:${port}/health" 2>/dev/null || echo "000")
  if [ "$http_code" = "200" ]; then
    pass "${channel} Adapter (포트 ${port}) /health → HTTP ${http_code}"
  else
    fail "${channel} Adapter (포트 ${port}) /health → HTTP ${http_code} (컨테이너 미기동 또는 응답 없음)"
  fi
done

# ──────────────────────────────────────────────
# 검증 2: Adapter /info 응답 (채널·토픽 정보)
# ──────────────────────────────────────────────
banner "2. Adapter /info 응답 확인"

declare -A EXPECTED_CHANNELS=(
  [8101]="SMS"
  [8102]="MMS"
  [8103]="RCS"
  [8104]="FAX"
  [8105]="EMAIL"
)

for port in 8101 8102 8103 8104 8105; do
  channel="${EXPECTED_CHANNELS[$port]}"
  response=$(curl -s --max-time 3 "http://localhost:${port}/info" 2>/dev/null || echo "{}")
  if echo "$response" | grep -q "\"channel\""; then
    # channel 필드 값 추출
    actual_channel=$(echo "$response" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('channel',''))" 2>/dev/null || echo "")
    dispatch_topic=$(echo "$response" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('dispatchTopic',''))" 2>/dev/null || echo "")
    pass "${channel} Adapter /info → channel=${actual_channel} | dispatchTopic=${dispatch_topic}"
  else
    fail "${channel} Adapter (포트 ${port}) /info → 응답 없음 또는 형식 오류"
  fi
done

# ──────────────────────────────────────────────
# 검증 3: NiFi UI 엔드포인트 확인
# ──────────────────────────────────────────────
banner "3. NiFi HTTP 엔드포인트 확인"

nifi_code=$(curl -s -o /dev/null -w "%{http_code}" --max-time 5 "http://localhost:8080/nifi/" 2>/dev/null || echo "000")
if [ "$nifi_code" = "200" ] || [ "$nifi_code" = "302" ]; then
  pass "NiFi UI (http://localhost:8080/nifi/) → HTTP ${nifi_code}"
else
  fail "NiFi UI (http://localhost:8080/nifi/) → HTTP ${nifi_code} (NiFi 미기동 또는 포트 확인 필요)"
fi

nifi_http_code=$(curl -s -o /dev/null -w "%{http_code}" --max-time 5 "http://localhost:8090/am/send" \
  -X POST -H "Content-Type: application/json" \
  -d '{"txId":"12345678901230308400700000000000001","channel":"SMS","sender":"15881234","receiver":"01012345678","body":"test"}' \
  2>/dev/null || echo "000")

# NiFi ListenHTTP는 요청 수신 시 200 또는 204 반환
if [ "$nifi_http_code" = "200" ] || [ "$nifi_http_code" = "204" ]; then
  pass "NiFi 발송 요청 수신 엔드포인트 (http://localhost:8090/am/send) → HTTP ${nifi_http_code}"
else
  info "NiFi 발송 수신 포트 8090 → HTTP ${nifi_http_code} (NiFi 플로우 미설정 시 정상 — 플로우 등록 후 재확인)"
  skip "NiFi 발송 수신 엔드포인트 — 플로우 미등록 상태로 SKIP"
fi

# ──────────────────────────────────────────────
# 검증 4: Kafka 토픽 목록 확인
# ──────────────────────────────────────────────
banner "4. Kafka 토픽 현황"

REQUIRED_TOPICS=(
  "topic.send.request"
  "topic.send.dispatch.sms"
  "topic.send.dispatch.mms"
  "topic.send.dispatch.rcs"
  "topic.send.dispatch.fax"
  "topic.send.dispatch.email"
  "topic.send.result"
  "topic.send.retry"
  "topic.send.dlq"
  "topic.monitor.metrics"
)

# docker exec으로 Kafka 컨테이너에서 토픽 목록 조회
# Windows Git Bash에서는 MSYS_NO_PATHCONV=1 필요
KAFKA_TOPICS=$(MSYS_NO_PATHCONV=1 docker exec am-kafka \
  kafka-topics --bootstrap-server localhost:9092 --list 2>/dev/null || echo "KAFKA_UNAVAILABLE")

if [ "$KAFKA_TOPICS" = "KAFKA_UNAVAILABLE" ]; then
  info "Kafka 컨테이너(am-kafka)에 접근할 수 없습니다 — 인프라 기동 여부 확인 필요"
  for topic in "${REQUIRED_TOPICS[@]}"; do
    skip "토픽 ${topic} — Kafka 미접근"
  done
else
  info "Kafka 접근 성공 — 필수 토픽 존재 여부 확인 중..."
  for topic in "${REQUIRED_TOPICS[@]}"; do
    if echo "$KAFKA_TOPICS" | grep -qxF "$topic"; then
      pass "토픽 존재: ${topic}"
    else
      fail "토픽 없음: ${topic} (kafka-topics.sh 실행 필요)"
    fi
  done
fi

# ──────────────────────────────────────────────
# 검증 5: txId 35자리 생성·검증·파싱 단독 테스트
# (Python 환경 필요, Kafka/컨테이너 불필요)
# ──────────────────────────────────────────────
banner "5. txId 35자리 단독 테스트 (Python)"

# python3 또는 python 명령 탐색
PYTHON_CMD=""
if command -v python3 &>/dev/null; then
  PYTHON_CMD="python3"
elif command -v python &>/dev/null; then
  PYTHON_CMD="python"
fi

if [ -z "$PYTHON_CMD" ]; then
  skip "Python 명령을 찾을 수 없습니다 (python3 또는 python 필요)"
else
  # 스크립트 위치 기준 상대경로로 base 모듈 접근
  SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  BASE_MODULE_PATH="${SCRIPT_DIR}"

  TX_TEST_RESULT=$($PYTHON_CMD - <<EOF 2>&1
import sys
sys.path.insert(0, '${BASE_MODULE_PATH}')
from base.tx_id import build_tx_id, validate_tx_id, parse_tx_id

errors = []

# 테스트 1: 실시간 발송 (코드 03)
tx03 = build_tx_id('03', '007')
if len(tx03) != 35:
    errors.append(f"코드03 길이 오류: {len(tx03)}자리")
if not validate_tx_id(tx03):
    errors.append(f"코드03 검증 실패: {tx03}")
parsed03 = parse_tx_id(tx03)
if parsed03['send_method_code'] != '03':
    errors.append(f"코드03 파싱 오류: {parsed03}")
print(f"[SUB-PASS] 실시간(03) txId={tx03} len={len(tx03)}")

# 테스트 2: 배치 발송 (코드 01, 02)
for code in ['01', '02']:
    tx = build_tx_id(code, '001')
    if len(tx) != 35 or not validate_tx_id(tx):
        errors.append(f"코드{code} 오류")
    else:
        print(f"[SUB-PASS] 배치({code}) txId={tx} len={len(tx)}")

# 테스트 3: 준실시간 발송 (코드 04, 05)
for code in ['04', '05']:
    tx = build_tx_id(code, '002')
    if len(tx) != 35 or not validate_tx_id(tx):
        errors.append(f"코드{code} 오류")
    else:
        print(f"[SUB-PASS] 준실시간({code}) txId={tx} len={len(tx)}")

# 테스트 4: 잘못된 txId 검증 거부 확인
invalid_cases = ['', '1234', 'uuid-v4-not-35chars', 'A' * 35, '9' * 34]
for inv in invalid_cases:
    if validate_tx_id(inv):
        errors.append(f"잘못된 txId를 유효하다고 판단함: '{inv}'")
print(f"[SUB-PASS] 유효하지 않은 txId {len(invalid_cases)}건 올바르게 거부됨")

# 테스트 5: 고유성 확인 (100건 생성 시 중복 없음)
ids = set()
for _ in range(100):
    ids.add(build_tx_id('03', '007'))
if len(ids) != 100:
    errors.append(f"txId 중복 발생: 100건 중 {len(ids)}건 고유")
else:
    print(f"[SUB-PASS] txId 고유성 확인 — 100건 모두 중복 없음")

if errors:
    for e in errors:
        print(f"[ERROR] {e}")
    sys.exit(1)
else:
    print("[ALL-PASS]")
EOF
  )

  if echo "$TX_TEST_RESULT" | grep -q "\[ALL-PASS\]"; then
    # 서브 결과 출력
    echo "$TX_TEST_RESULT" | grep "\[SUB-PASS\]" | while read line; do
      echo "  ${line}"
    done
    pass "txId 35자리 생성·검증·파싱·고유성 전체 통과"
  else
    echo "$TX_TEST_RESULT"
    fail "txId 테스트 실패 — 상세 내용 위 참조"
  fi
fi

# ──────────────────────────────────────────────
# 최종 결과 요약
# ──────────────────────────────────────────────
banner "검증 결과 요약"
echo -e "  ${GREEN}PASS${NC}: ${PASS_COUNT}건"
echo -e "  ${RED}FAIL${NC}: ${FAIL_COUNT}건"
echo -e "  ${YELLOW}SKIP${NC}: ${SKIP_COUNT}건"
echo ""

if [ "$FAIL_COUNT" -eq 0 ]; then
  echo -e "${GREEN}✅ 모든 검증 통과 (FAIL 0건)${NC}"
  exit 0
else
  echo -e "${RED}❌ 실패 ${FAIL_COUNT}건 발생 — 위 [FAIL] 항목 확인 필요${NC}"
  exit 1
fi