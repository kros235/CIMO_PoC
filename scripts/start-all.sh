#!/usr/bin/env bash
# scripts/start-all.sh
#
# CIMO_PoC 전체 환경 자동 기동 스크립트.
#
# 가정:
#   - Docker Desktop 가 실행 중
#   - 프로젝트 루트에서 실행 (또는 어디서든)
#   - poc/docker/.env 파일이 존재 (없으면 .env.example 에서 자동 복사)
#   - poc/flink/am-flink-fat.jar 가 빌드되어 있음
#
# 실행:
#   bash scripts/start-all.sh
#
# 단계:
#   0. 사전 체크 (.env, fat-jar)
#   1. core 인프라 + 모니터링 기동 (postgres/mongo/zookeeper/kafka/nifi/flink + prometheus/grafana/history-api/kafka-ui)
#   2. ZooKeeper, Kafka 정상 응답 대기
#   3. Kafka 토픽 11개 생성 (idempotent)
#   4. Adapter 5개 기동
#   5. Flink Job 3개 제출 (좀비 Job 자동 정리 포함)
#   6. 헬스체크 종합 리포트

set -e  # 에러 발생 시 즉시 중단

# ───────────── 색상 출력 ─────────────
RED=$'\033[0;31m'
GREEN=$'\033[0;32m'
YELLOW=$'\033[1;33m'
BLUE=$'\033[0;34m'
NC=$'\033[0m'

log_info()  { echo "${BLUE}[INFO]${NC}  $1"; }
log_pass()  { echo "${GREEN}[PASS]${NC}  $1"; }
log_warn()  { echo "${YELLOW}[WARN]${NC}  $1"; }
log_fail()  { echo "${RED}[FAIL]${NC}  $1"; }
log_step()  { echo ""; echo "${BLUE}════════════════════════════════════════${NC}"; echo "${BLUE}  $1${NC}"; echo "${BLUE}════════════════════════════════════════${NC}"; }

# ───────────── 경로 정의 (스크립트 위치 기준 상대) ─────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
DOCKER_DIR="$PROJECT_ROOT/poc/docker"
FLINK_DIR="$PROJECT_ROOT/poc/flink"
ENV_FILE="$DOCKER_DIR/.env"
ENV_EXAMPLE="$DOCKER_DIR/.env.example"
FAT_JAR="$FLINK_DIR/am-flink-fat.jar"

cd "$PROJECT_ROOT"

# Windows Git Bash 에서 docker exec 의 path 변환 방지
export MSYS_NO_PATHCONV=1

# ═════════════════════════════════════════════════════════
log_step "0단계: 사전 체크"
# ═════════════════════════════════════════════════════════

# Docker 살아있는지
if ! docker info > /dev/null 2>&1; then
    log_fail "Docker 가 실행 중이 아닙니다. Docker Desktop 을 먼저 실행하세요."
    exit 1
fi
log_pass "Docker daemon 응답"

# .env 파일 자동 복사
if [ ! -f "$ENV_FILE" ]; then
    if [ -f "$ENV_EXAMPLE" ]; then
        cp "$ENV_EXAMPLE" "$ENV_FILE"
        log_warn ".env 파일이 없어서 .env.example 에서 자동 복사함"
    else
        log_fail ".env 도 .env.example 도 없음. 환경변수 설정 필요."
        exit 1
    fi
fi
log_pass ".env 파일 존재: $ENV_FILE"

# Flink fat-jar 존재 확인
if [ ! -f "$FAT_JAR" ]; then
    log_fail "Flink fat-jar 가 없음: $FAT_JAR"
    log_info "Maven 빌드를 먼저 수행하세요: cd poc/flink && mvn clean package"
    exit 1
fi
log_pass "Flink fat-jar 존재: $(du -h "$FAT_JAR" | cut -f1)"

# ═════════════════════════════════════════════════════════
log_step "1단계: Core 인프라 + 모니터링 기동"
# ═════════════════════════════════════════════════════════

cd "$DOCKER_DIR"

log_info "docker-compose 기동 (core + monitoring)..."
docker compose -f docker-compose.yml -f docker-compose.monitoring.yml up -d 2>&1 \
    | grep -v "the attribute .version. is obsolete" \
    | grep -v "Found orphan containers" || true

log_info "컨테이너 안정화 대기 (10초)..."
sleep 10

# ═════════════════════════════════════════════════════════
log_step "2단계: ZooKeeper / Kafka 정상 응답 대기"
# ═════════════════════════════════════════════════════════

# ZooKeeper 가 응답하는지 30초 동안 대기
log_info "ZooKeeper 응답 대기 (최대 30초)..."
for i in {1..30}; do
    if docker exec am-zookeeper bash -c "echo srvr | nc localhost 2181" 2>/dev/null | grep -q "Zookeeper"; then
        log_pass "ZooKeeper 응답 OK ($i 초 후)"
        break
    fi
    sleep 1
    if [ $i -eq 30 ]; then
        log_fail "ZooKeeper 30초 안에 응답 안 함"
        exit 1
    fi
done

# Kafka 가 응답하는지 60초 동안 대기 (Kafka 는 ZK 의존성 때문에 더 오래)
log_info "Kafka broker 응답 대기 (최대 60초)..."
for i in {1..60}; do
    if docker exec am-kafka kafka-broker-api-versions \
        --bootstrap-server localhost:9092 > /dev/null 2>&1; then
        log_pass "Kafka broker 응답 OK ($i 초 후)"
        break
    fi
    sleep 1
    if [ $i -eq 60 ]; then
        log_fail "Kafka 60초 안에 응답 안 함. Kafka 로그 확인:"
        docker logs am-kafka --tail 20
        exit 1
    fi
done

# ═════════════════════════════════════════════════════════
log_step "3단계: Kafka 토픽 11개 생성 (idempotent)"
# ═════════════════════════════════════════════════════════

TOPICS=(
    "topic.send.request"
    "topic.send.dispatch.sms"
    "topic.send.dispatch.mms"
    "topic.send.dispatch.rcs"
    "topic.send.dispatch.fax"
    "topic.send.dispatch.email"
    "topic.send.result"
    "topic.send.retry"
    "topic.send.dlq"
    "topic.send.batch"
    "topic.monitor.metrics"
)

for topic in "${TOPICS[@]}"; do
    docker exec am-kafka kafka-topics \
        --bootstrap-server localhost:9092 \
        --create --if-not-exists \
        --topic "$topic" \
        --partitions 3 --replication-factor 1 2>&1 \
        | grep -E "Created|exists" || true
done
log_pass "11개 토픽 확보"

# ═════════════════════════════════════════════════════════
log_step "4단계: Adapter 5개 기동"
# ═════════════════════════════════════════════════════════

cd "$DOCKER_DIR"
docker compose -f docker-compose.adapters.yml up -d 2>&1 \
    | grep -v "the attribute .version. is obsolete" \
    | grep -v "Found orphan containers" || true

log_info "Adapter 안정화 대기 (8초)..."
sleep 8

# 5개 Adapter health check
ADAPTER_PORTS=(8101 8102 8103 8104 8105)
ADAPTER_NAMES=("SMS" "MMS" "RCS" "FAX" "EMAIL")
for i in "${!ADAPTER_PORTS[@]}"; do
    port="${ADAPTER_PORTS[$i]}"
    name="${ADAPTER_NAMES[$i]}"
    code=$(curl -s -o /dev/null -w '%{http_code}' "http://localhost:$port/health")
    if [ "$code" = "200" ]; then
        log_pass "$name Adapter (port $port) 응답 OK"
    else
        log_warn "$name Adapter (port $port) 응답 코드 $code (잠시 후 재기동될 수 있음)"
    fi
done

# ═════════════════════════════════════════════════════════
log_step "5단계: Flink Job 제출 (좀비 Job 자동 cancel)"
# ═════════════════════════════════════════════════════════

cd "$FLINK_DIR"

# 5-1. 기존 Job 전부 cancel (좀비 정리)
log_info "기존 Flink Job 전수조사 및 cancel..."
EXISTING_JOBS=$(docker exec am-flink-jobmanager flink list 2>&1 \
    | grep -v WARNING \
    | grep -E "(RUNNING|RESTARTING)" \
    | awk '{print $4}' || true)

if [ -n "$EXISTING_JOBS" ]; then
    for jid in $EXISTING_JOBS; do
        log_info "  Cancelling old job: $jid"
        docker exec am-flink-jobmanager flink cancel "$jid" 2>&1 | grep -v WARNING | tail -1 || true
    done
    sleep 5
else
    log_info "기존 Job 없음 (clean start)"
fi

# 5-2. fat-jar 복사
log_info "Flink fat-jar 컨테이너 내부로 복사..."
docker cp am-flink-fat.jar am-flink-jobmanager:/tmp/am-flink-fat.jar > /dev/null

# 5-3. Job 3개 제출
JOB_CLASSES=("SendRequestJob" "SendResultJob" "RetryJob")
for cls in "${JOB_CLASSES[@]}"; do
    log_info "  제출 중: $cls"
    docker exec am-flink-jobmanager flink run -d \
        --class "com.am.platform.jobs.$cls" \
        /tmp/am-flink-fat.jar 2>&1 \
        | grep -v WARNING \
        | grep "Job has been submitted" || true
    sleep 3
done

log_info "Job 안정화 대기 (30초)..."
sleep 30

# 5-4. Job 상태 확인
log_info "최종 Job 상태:"
docker exec am-flink-jobmanager flink list 2>&1 \
    | grep -v WARNING \
    | grep -E "RUNNING|RESTARTING|FAILED" || true

# ═════════════════════════════════════════════════════════
log_step "6단계: 종합 헬스체크"
# ═════════════════════════════════════════════════════════

# 컨테이너 상태
echo ""
echo "── 컨테이너 상태 ──"
docker ps --format "table {{.Names}}\t{{.Status}}" | grep -E "am-|docker-"

# 엔드포인트 헬스체크
echo ""
echo "── 엔드포인트 응답 ──"
declare -A ENDPOINTS=(
    ["NiFi (8090)"]="http://localhost:8090/am/send"
    ["History API (8200)"]="http://localhost:8200/health"
    ["Prometheus (9090)"]="http://localhost:9090/-/healthy"
    ["Grafana (3000)"]="http://localhost:3000/api/health"
    ["Flink UI (8081)"]="http://localhost:8081/overview"
    ["Kafka UI (8989)"]="http://localhost:8989/actuator/health"
    ["SMS Adapter (8101)"]="http://localhost:8101/health"
    ["MMS Adapter (8102)"]="http://localhost:8102/health"
    ["RCS Adapter (8103)"]="http://localhost:8103/health"
    ["FAX Adapter (8104)"]="http://localhost:8104/health"
    ["EMAIL Adapter (8105)"]="http://localhost:8105/health"
)

for name in "${!ENDPOINTS[@]}"; do
    code=$(curl -s -o /dev/null -w '%{http_code}' "${ENDPOINTS[$name]}" 2>/dev/null || echo "000")
    if [ "$code" = "200" ] || [ "$code" = "401" ]; then
        log_pass "$name → $code"
    else
        log_warn "$name → $code"
    fi
done

# 토픽 개수
TOPIC_COUNT=$(docker exec am-kafka kafka-topics \
    --bootstrap-server localhost:9092 --list 2>&1 \
    | grep "^topic\." | wc -l)

# Job 개수
JOB_COUNT=$(docker exec am-flink-jobmanager flink list 2>&1 \
    | grep -v WARNING | grep -E "RUNNING" | wc -l)

# ═════════════════════════════════════════════════════════
log_step "기동 완료!"
# ═════════════════════════════════════════════════════════
echo ""
echo "  📊 인프라 요약"
echo "      Kafka 토픽:   $TOPIC_COUNT 개"
echo "      Flink Job:    $JOB_COUNT 개 (기대: 3개)"
echo ""
echo "  🌐 주요 UI"
echo "      Flink UI:     http://localhost:8081"
echo "      Grafana:      http://localhost:3000   (admin / admin)"
echo "      Prometheus:   http://localhost:9090"
echo "      Kafka UI:     http://localhost:8989"
echo "      NiFi UI:      http://localhost:8443/nifi"
echo "      History API:  http://localhost:8200/api/v1/messages/{txId}"
echo ""
echo "  🚀 다음 단계"
echo "      통합 테스트:  python tests/validation/ts0001_pipeline_consistency.py"
echo ""