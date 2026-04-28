#!/usr/bin/env bash
# scripts/stop-all.sh
#
# CIMO_PoC 전체 환경 정상 종료 스크립트.
#
# 옵션:
#   (옵션 없음)        - 모든 컨테이너 정지·제거 (DB / Kafka 데이터 보존)
#   --clean-kafka     - Kafka stale 데이터까지 정리 (다음 기동 시 토픽 새로 생성)
#   --full-reset      - DB / Kafka / NiFi 모든 영속 데이터 삭제 (주의)
#
# 실행:
#   bash scripts/stop-all.sh
#   bash scripts/stop-all.sh --clean-kafka
#   bash scripts/stop-all.sh --full-reset

set -e

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

# ───────────── 경로 정의 ─────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
DOCKER_DIR="$PROJECT_ROOT/poc/docker"

cd "$PROJECT_ROOT"
export MSYS_NO_PATHCONV=1

# ───────────── 옵션 파싱 ─────────────
CLEAN_KAFKA="false"
FULL_RESET="false"

for arg in "$@"; do
    case $arg in
        --clean-kafka) CLEAN_KAFKA="true" ;;
        --full-reset)  FULL_RESET="true"; CLEAN_KAFKA="true" ;;
        --help|-h)
            echo "Usage: bash scripts/stop-all.sh [OPTION]"
            echo ""
            echo "Options:"
            echo "  (없음)             모든 컨테이너 정지·제거 (DB / Kafka 데이터 보존)"
            echo "  --clean-kafka     Kafka stale 데이터까지 정리"
            echo "  --full-reset      DB / Kafka / NiFi 모든 영속 데이터 삭제 (주의)"
            echo "  --help, -h        이 도움말 표시"
            exit 0
            ;;
        *)
            log_fail "알 수 없는 옵션: $arg (--help 로 사용법 확인)"
            exit 1
            ;;
    esac
done

# ═════════════════════════════════════════════════════════
log_step "1단계: Flink Job 전부 cancel (좀비 방지)"
# ═════════════════════════════════════════════════════════

if docker ps -q --filter "name=am-flink-jobmanager" | grep -q .; then
    EXISTING_JOBS=$(docker exec am-flink-jobmanager flink list 2>&1 \
        | grep -v WARNING \
        | grep -E "(RUNNING|RESTARTING)" \
        | awk '{print $4}' || true)

    if [ -n "$EXISTING_JOBS" ]; then
        for jid in $EXISTING_JOBS; do
            log_info "  Cancelling: $jid"
            docker exec am-flink-jobmanager flink cancel "$jid" 2>&1 \
                | grep -v WARNING | tail -1 || true
        done
        log_info "Flink Job 종료 대기 (5초)..."
        sleep 5
    else
        log_info "실행 중인 Flink Job 없음"
    fi
else
    log_info "Flink JobManager 가 이미 정지됨 - skip"
fi

# ═════════════════════════════════════════════════════════
log_step "2단계: Adapter 5개 정지"
# ═════════════════════════════════════════════════════════

cd "$DOCKER_DIR"

if [ "$FULL_RESET" = "true" ]; then
    log_warn "FULL RESET 모드 - Adapter 컨테이너 + 볼륨 삭제"
    docker compose -f docker-compose.adapters.yml down -v 2>&1 \
        | grep -v "obsolete" | grep -v "Found orphan" | tail -5 || true
else
    log_info "Adapter 컨테이너 정지·제거 (이미지·볼륨 유지)"
    docker compose -f docker-compose.adapters.yml down 2>&1 \
        | grep -v "obsolete" | grep -v "Found orphan" | tail -5 || true
fi
log_pass "Adapter 정지 완료"

# ═════════════════════════════════════════════════════════
log_step "3단계: Core 인프라 + 모니터링 정지"
# ═════════════════════════════════════════════════════════

if [ "$FULL_RESET" = "true" ]; then
    log_warn "FULL RESET 모드 - 모든 컨테이너 + 볼륨 삭제"
    docker compose -f docker-compose.yml -f docker-compose.monitoring.yml down -v 2>&1 \
        | grep -v "obsolete" | grep -v "Found orphan" | tail -5 || true
else
    log_info "컨테이너 정지·제거 (영속 데이터 유지)"
    docker compose -f docker-compose.yml -f docker-compose.monitoring.yml down 2>&1 \
        | grep -v "obsolete" | grep -v "Found orphan" | tail -5 || true
fi
log_pass "Core + 모니터링 정지 완료"

cd "$PROJECT_ROOT"

# ═════════════════════════════════════════════════════════
log_step "4단계: 데이터 정리 (옵션에 따라)"
# ═════════════════════════════════════════════════════════

if [ "$FULL_RESET" = "true" ]; then
    log_warn "FULL RESET - 모든 영속 데이터 삭제"
    
    log_info "  PostgreSQL 데이터 삭제..."
    rm -rf "$DOCKER_DIR/postgres/data" 2>/dev/null || true
    mkdir -p "$DOCKER_DIR/postgres/data"
    
    log_info "  MongoDB 데이터 삭제..."
    rm -rf "$DOCKER_DIR/mongodb/data" 2>/dev/null || true
    mkdir -p "$DOCKER_DIR/mongodb/data"
    
    log_info "  Kafka 데이터 삭제..."
    rm -rf "$DOCKER_DIR/kafka/data" 2>/dev/null || true
    mkdir -p "$DOCKER_DIR/kafka/data"
    
    log_info "  NiFi state 삭제..."
    rm -rf "$PROJECT_ROOT/poc/nifi/state" 2>/dev/null || true
    
    log_pass "FULL RESET 완료 - 다음 기동 시 모든 데이터 새로 생성"
    
elif [ "$CLEAN_KAFKA" = "true" ]; then
    log_warn "Kafka stale 데이터 정리 (DB / NiFi 는 보존)"
    
    log_info "  Kafka 데이터 삭제..."
    rm -rf "$DOCKER_DIR/kafka/data" 2>/dev/null || true
    mkdir -p "$DOCKER_DIR/kafka/data"
    
    log_pass "Kafka 정리 완료 - 다음 기동 시 토픽 새로 생성"
    
else
    log_info "데이터 보존 모드 (default)"
    log_info "  - PostgreSQL / MongoDB / Kafka / NiFi 데이터 모두 유지"
    log_info "  - 다음 기동 시 이전 상태 그대로 복원"
fi

# ═════════════════════════════════════════════════════════
log_step "5단계: 종료 확인"
# ═════════════════════════════════════════════════════════

REMAINING=$(docker ps -a --filter "name=am-" --format "{{.Names}}" | wc -l)
TASKMGR=$(docker ps -a --filter "name=docker-taskmanager" --format "{{.Names}}" | wc -l)

if [ "$REMAINING" -eq 0 ] && [ "$TASKMGR" -eq 0 ]; then
    log_pass "모든 컨테이너 정상 정리됨"
else
    log_warn "잔여 컨테이너:"
    docker ps -a --filter "name=am-" --format "table {{.Names}}\t{{.Status}}" | tail -n +2
    docker ps -a --filter "name=docker-taskmanager" --format "table {{.Names}}\t{{.Status}}" | tail -n +2
fi

# ═════════════════════════════════════════════════════════
log_step "종료 완료!"
# ═════════════════════════════════════════════════════════
echo ""

if [ "$FULL_RESET" = "true" ]; then
    echo "  💀 FULL RESET 완료"
    echo "      모든 영속 데이터 삭제됨"
    echo ""
elif [ "$CLEAN_KAFKA" = "true" ]; then
    echo "  🧹 Kafka 정리 완료"
    echo "      DB / NiFi 데이터 보존"
    echo ""
else
    echo "  💾 데이터 보존 모드 (default)"
    echo "      모든 영속 데이터 유지"
    echo ""
fi

echo "  🚀 다시 기동하려면:"
echo "      bash scripts/start-all.sh"
echo ""
