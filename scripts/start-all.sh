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

# CHANGE: Kafka stale 데이터 사전 점검
# 컨테이너 down 상태에서 kafka/data 가 옛 clusterId 를 보유하면
# ZK 새 ID 와 불일치로 Kafka 가 InconsistentClusterIdException 으로 자살함.
# 자동 복구는 2단계에서 진행하지만 사전에 경고를 표시한다.
KAFKA_META="$DOCKER_DIR/kafka/data/meta.properties"
if [ -f "$KAFKA_META" ] && [ -z "$(docker ps -q --filter 'name=am-kafka')" ]; then
    OLD_CID=$(grep "^cluster.id=" "$KAFKA_META" 2>/dev/null | cut -d= -f2 | tr -d '\r\n' || echo "?")
    log_warn "Kafka 데이터에 기존 clusterId 있음: $OLD_CID"
    log_warn "ZK 가 새 ID 로 시작 시 충돌 가능. 충돌 발생 시 2단계에서 자동 복구."
fi


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

# NiFi lock 파일 자동 정리
# 이전 비정상 종료 시 nifi.pid/nifi.lock 이 남아 NiFi 가
# "Apache NiFi is already running" 으로 자살하는 케이스 대응
log_info "NiFi lock 파일 정리..."
docker exec am-nifi rm -f /opt/nifi/nifi-current/run/nifi.pid 2>/dev/null || true
docker exec am-nifi rm -f /opt/nifi/nifi-current/run/nifi.lock 2>/dev/null || true

if [ "$(docker inspect -f '{{.State.Running}}' am-nifi 2>/dev/null)" != "true" ]; then
    log_warn "NiFi 가 실행 중이 아님 - 재시작 시도"
    docker start am-nifi 2>/dev/null || true
    log_info "NiFi 재시작 후 안정화 대기 (15초)..."
    sleep 15
fi

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
KAFKA_OK="false"
for i in $(seq 1 60); do
    if docker exec am-kafka kafka-broker-api-versions \
        --bootstrap-server localhost:9092 > /dev/null 2>&1; then
        log_pass "Kafka broker 응답 OK ($i 초 후)"
        KAFKA_OK="true"
        break
    fi
    sleep 1
done

# CHANGE: Kafka 응답 실패 시 InconsistentClusterId 자동 복구
if [ "$KAFKA_OK" != "true" ]; then
    log_warn "Kafka 60초 안에 응답 안 함. 로그 분석 중..."
    KAFKA_LOG=$(docker logs am-kafka 2>&1 | tail -50)

    if echo "$KAFKA_LOG" | grep -q "InconsistentClusterIdException"; then
        log_warn "==================================================="
        log_warn "Kafka clusterId 불일치 자동 감지 - 복구 시도"
        log_warn "==================================================="

        # 1) Kafka 컨테이너 정지 (다른 컨테이너는 그대로 유지)
        log_info "1) Kafka 컨테이너 정지·제거..."
        docker stop am-kafka > /dev/null 2>&1 || true
        docker rm am-kafka > /dev/null 2>&1 || true

        # 2) stale 데이터 백업 (rm 대신 mv 로 안전하게)
        BACKUP_DIR="$DOCKER_DIR/kafka/data.backup.$(date +%Y%m%d_%H%M%S)"
        log_info "2) stale 데이터 백업: $(basename $BACKUP_DIR)"
        if [ -d "$DOCKER_DIR/kafka/data" ]; then
            mv "$DOCKER_DIR/kafka/data" "$BACKUP_DIR" 2>/dev/null || true
        fi
        mkdir -p "$DOCKER_DIR/kafka/data"

        # 3) ZK 의 /kafka 노드 정리 (ZK 에 옛 ID 가 캐시되어 있을 수 있음)
        log_info "3) ZooKeeper /brokers, /cluster 노드 정리..."
        docker exec am-zookeeper zookeeper-shell localhost:2181 \
            deleteall /brokers > /dev/null 2>&1 || true
        docker exec am-zookeeper zookeeper-shell localhost:2181 \
            deleteall /cluster > /dev/null 2>&1 || true

        # 4) Kafka 컨테이너만 재기동
        log_info "4) Kafka 컨테이너 재기동..."
        cd "$DOCKER_DIR"
        docker compose -f docker-compose.yml -f docker-compose.monitoring.yml up -d kafka 2>&1 \
            | grep -v "obsolete" | tail -3 || true
        cd "$PROJECT_ROOT"

        # 5) 재기동 후 다시 60초 대기
        log_info "5) Kafka 재응답 대기 (최대 60초)..."
        for i in $(seq 1 60); do
            if docker exec am-kafka kafka-broker-api-versions \
                --bootstrap-server localhost:9092 > /dev/null 2>&1; then
                log_pass "Kafka 자동 복구 성공 ($i 초 후 응답 OK)"
                log_info "백업 위치: $(basename $BACKUP_DIR)"
                log_info "  → 분석 후 불필요시 수동 삭제 가능"
                KAFKA_OK="true"
                break
            fi
            sleep 1
        done
    fi

    if [ "$KAFKA_OK" != "true" ]; then
        log_fail "Kafka 응답 실패 (자동 복구 후에도). 수동 점검 필요:"
        log_fail "  1. docker logs am-kafka --tail 50"
        log_fail "  2. ls -la $DOCKER_DIR/kafka/data"
        log_fail "  3. 필요시 전체 reset:"
        log_fail "     cd $DOCKER_DIR"
        log_fail "     docker compose down -v"
        log_fail "     rm -rf kafka/data"
        log_fail "     bash scripts/start-all.sh"
        docker logs am-kafka --tail 30
        exit 1
    fi
fi

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
    code=$(curl -s -m 5 --connect-timeout 3 -o /dev/null -w '%{http_code}' "http://localhost:$port/health" 2>/dev/null || true)
    if [ -z "$code" ]; then code="000"; fi
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
log_step "5.5단계: NiFi 플로우 자동 배포 (멱등)"
# ═════════════════════════════════════════════════════════

# NiFi 플로우 (ListenHTTP → EvaluateJsonPath → RouteOnAttribute →
# UpdateAttribute → PublishKafka → LogMessage 6~7개 프로세서) 자동 배포.
#
# 정책:
#   - 기존 배포 감지 시: skip (운영 중 흐름 보존, 수동 수정 보존)
#   - 기존 배포 없음: poc/nifi/deploy_flow.py 호출하여 신규 배포
#   - 강제 재배포 원할 시: bash scripts/start-all.sh --force-nifi-deploy
#
# 안전장치:
#   - 5.5단계 실패해도 인프라 자체는 정상 (best-effort)
#   - 수동 명령 안내 후 6단계 진행

NIFI_DEPLOY_SCRIPT="$PROJECT_ROOT/poc/nifi/deploy_flow.py"
NIFI_REQ_FILE="$PROJECT_ROOT/poc/nifi/requirements.txt"

# CLI 옵션 처리 (--force-nifi-deploy 시 강제 재배포)
FORCE_NIFI_DEPLOY="false"
for arg in "$@"; do
    if [ "$arg" = "--force-nifi-deploy" ]; then
        FORCE_NIFI_DEPLOY="true"
        log_warn "--force-nifi-deploy 플래그 감지: 기존 플로우 강제 재배포"
    fi
done

if [ ! -f "$NIFI_DEPLOY_SCRIPT" ]; then
    log_warn "NiFi 자동 배포 스크립트 없음: poc/nifi/deploy_flow.py"
    log_warn "→ NiFi UI 에서 수동 배포 필요 (http://localhost:8080/nifi)"
else
    # 1) 기존 배포 감지 - NiFi 루트 캔버스의 프로세서 개수 확인
    log_info "기존 NiFi 플로우 배포 상태 점검..."
    EXISTING_PROCESSORS="0"
    NIFI_API_RESP=$(curl -s -m 10 --connect-timeout 3 \
        "http://localhost:8080/nifi-api/process-groups/root" 2>/dev/null || true)
    if [ -n "$NIFI_API_RESP" ]; then
        # JSON 응답에서 processor 개수 추출 (간단한 grep)
        EXISTING_PROCESSORS=$(echo "$NIFI_API_RESP" \
            | grep -oE '"processorCount":[0-9]+' \
            | grep -oE '[0-9]+' \
            | head -1 || echo "0")
        if [ -z "$EXISTING_PROCESSORS" ]; then EXISTING_PROCESSORS="0"; fi
    fi

    log_info "현재 캔버스 프로세서: $EXISTING_PROCESSORS 개"

    # 2) 배포 정책 판단
    if [ "$EXISTING_PROCESSORS" -gt "0" ] && [ "$FORCE_NIFI_DEPLOY" != "true" ]; then
        log_pass "기존 NiFi 플로우 발견 ($EXISTING_PROCESSORS 개 프로세서) - 배포 skip"
        log_info "  → 강제 재배포: bash scripts/start-all.sh --force-nifi-deploy"
        log_info "  → 수동 점검:   http://localhost:8080/nifi"
    else
        # 신규 배포 또는 강제 재배포
        if [ "$EXISTING_PROCESSORS" -gt "0" ]; then
            log_warn "강제 재배포 모드 - 기존 $EXISTING_PROCESSORS 개 프로세서 삭제 후 재배포"
        else
            log_info "기존 플로우 없음 - 신규 배포 시작"
        fi

        # requests 라이브러리 설치 확인
        if ! python -c "import requests" 2>/dev/null; then
            log_info "requests 라이브러리 설치 중..."
            if [ -f "$NIFI_REQ_FILE" ]; then
                python -m pip install -q -r "$NIFI_REQ_FILE" 2>&1 | tail -3 || true
            else
                python -m pip install -q "requests>=2.31.0" 2>&1 | tail -3 || true
            fi
        fi

        # deploy_flow.py 실행
        log_info "NiFi 플로우 배포 중 (deploy_flow.py 호출)..."
        cd "$PROJECT_ROOT/poc/nifi"
        DEPLOY_OK="false"
        if PYTHONIOENCODING=utf-8 python deploy_flow.py 2>&1 | tail -15; then
            DEPLOY_OK="true"
        fi
        cd "$PROJECT_ROOT"

        if [ "$DEPLOY_OK" = "true" ]; then
            log_pass "NiFi 플로우 자동 배포 완료"
        else
            log_warn "NiFi 플로우 배포 실패 - 수동 점검 필요"
            log_warn "  → http://localhost:8080/nifi 접속 확인"
            log_warn "  → 수동 재실행: cd poc/nifi && python deploy_flow.py"
        fi
    fi
fi

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
    code=$(curl -s -m 5 --connect-timeout 3 -o /dev/null -w '%{http_code}' "${ENDPOINTS[$name]}" 2>/dev/null || true)
    if [ -z "$code" ]; then code="000"; fi
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
