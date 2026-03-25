# AM 플랫폼 POC — 작업 계획 및 진행 현황

> **이 파일은 모든 작업의 기준점입니다.**
> 매 작업 시작 전 반드시 이 파일을 먼저 읽고, 현재 단계를 확인한 후 진행하십시오.

---

## 프로젝트 개요

| 항목 | 내용 |
|------|------|
| 목표 | MIMO·CI 통합 AM 아키텍처 POC 구축 및 성능 검증 |
| 핵심 스택 | Apache NiFi 2.0 + Apache Kafka 3.6 + Apache Flink 1.18 |
| POC 방식 | Docker Compose 기반 단일 호스트 환경 |
| 최종 목표 TPS | 2,000 TPS 이상 (목표: 일 5,000만 건) |
| 설계서 위치 | `docs/architecture/AM_ARCHITECTURE.md` |

---

## 디렉토리 구조

```
am-platform/
├── README.md                        ← 지금 이 파일 (항상 먼저 읽을 것)
├── docs/
│   ├── architecture/
│   │   └── AM_ARCHITECTURE.md       ← 전체 아키텍처 설계서
│   └── diagrams/                    ← 아키텍처 다이어그램 (추후 추가)
├── poc/
│   ├── docker/
│   │   └── docker-compose.yml       ← Day2: 전체 환경 정의
│   ├── config/
│   │   └── kafka-topics.sh          ← Day2: Kafka 토픽 초기화
│   ├── init/
│   │   ├── init.sql                 ← Day2: DB 스키마 초기화 (PostgreSQL)
│   │   └── init-mongo.js            ← Day2: MongoDB 컬렉션·인덱스 초기화
│   ├── nifi/                        ← Day3: NiFi 플로우 템플릿
│   │   ├── send-request-flow.json   ← HTTP 수신 → 35자리 txId 생성 → Kafka 발행
│   │   ├── send-result-flow.json    ← Kafka 구독 → txId 검증 → DB 업데이트
│   │   └── README.md                ← NiFi 플로우 운영 가이드
│   ├── flink/                       ← Day4: Flink Job 소스
│   ├── services/                    ← Day3: Mock Adapter (SMS/MMS/RCS/FAX/Email)
│   │   ├── base/                    ← 공통 모듈 (tx_id.py, kafka_client.py, adapter_base.py)
│   │   ├── sms-adapter/             ← 성공률 95%, 지연 50ms (main.py, Dockerfile)
│   │   ├── mms-adapter/             ← 성공률 93%, 지연 80ms
│   │   ├── rcs-adapter/             ← 성공률 90%, fallback→SMS (결과코드 50002)
│   │   ├── fax-adapter/             ← 성공률 85%, 지연 200ms
│   │   ├── email-adapter/           ← 성공률 98%, 지연 30ms
│   │   └── test_adapters.sh         ← Day3 완료 기준 검증 스크립트
│   └── monitoring/                  ← Day5: Prometheus + Grafana 설정
└── tests/
    ├── load/                        ← Day6: 부하 테스트 스크립트
    └── validation/                  ← Day6: 정합성 검증 스크립트
```

---

## 전체 작업 계획 (7 Working Days)

### 진행 상태 범례
- `✅ 완료` — 작업 완료, 검증됨
- `🔄 진행 중` — 현재 작업 중
- `⬜ 미시작` — 아직 시작하지 않음
- `⚠️ 이슈` — 블로커 발생, 해결 필요

---

### Day 1 — 아키텍처 구체화 및 작업 계획 수립

**상태: ✅ 완료**

**목표:** 초안 아키텍처를 구체화하여 문서화하고, 전체 작업 계획을 수립한다.

| # | 작업 항목 | 산출물 | 상태 |
|---|---------|--------|------|
| 1 | 초안 아키텍처 내용 구체화 (컴포넌트 역할, 포트, 버전 명시) | AM_ARCHITECTURE.md §3.1 | ✅ |
| 2 | 발송 파이프라인 상세 설계 (토픽 설계, 재처리 정책) | AM_ARCHITECTURE.md §4 | ✅ |
| 3 | 표준 메시지 포맷 JSON 규격 정의 | AM_ARCHITECTURE.md §5.2 | ✅ |
| 4 | 데이터 모델 설계 (테이블 정의, 인덱스) | AM_ARCHITECTURE.md §10 | ✅ |
| 5 | 장애 시나리오 및 대응 설계 | AM_ARCHITECTURE.md §11 | ✅ |
| 6 | POC 디렉토리 구조 생성 | 디렉토리 트리 | ✅ |
| 7 | 전체 작업 계획 수립 | README.md | ✅ |

**Day 1 완료 기준:**
- [x] `AM_ARCHITECTURE.md` 14개 섹션 전체 작성 완료 (§12 발송방식/txId, §13 AS-IS, §14 MongoDB 신규 추가)
- [x] `README.md` 전체 작업 계획 작성 완료
- [x] POC 디렉토리 구조 생성 완료 (`docs/diagrams/` 포함)

---

### Day 2 — POC 기반 환경 구성 (Docker Compose + DB 초기화)

**상태: ✅ 완료**

**목표:** Docker Compose로 전체 컨테이너 환경을 정의하고, DB 스키마 초기화까지 완료한다.
이 날 작업이 완료되면 `docker compose up` 한 줄로 전체 인프라가 기동되어야 한다.

| # | 작업 항목 | 산출물 | 상태 |
|---|---------|--------|------|
| 1 | docker-compose.yml 작성 (NiFi, Kafka, ZooKeeper, Flink, PostgreSQL, **MongoDB**, Prometheus, Grafana) | `poc/docker/docker-compose.yml` | ✅ |
| 2 | DB 초기화 스크립트 작성 (테이블, 인덱스, 초기 데이터) | `poc/init/init.sql` | ✅ |
| 3 | MongoDB 초기화 스크립트 작성 (월별 컬렉션·인덱스, 샘플 도큐먼트) | `poc/init/init-mongo.js` | ✅ |
| 4 | Kafka 토픽 초기화 스크립트 작성 (채널별 dispatch 토픽 포함 10개) | `poc/config/kafka-topics.sh` | ✅ |
| 5 | Kafka Connector 설정 파일 작성 (JdbcSink, MongoSink) | `poc/config/kafka-connectors/` | ✅ |
| 6 | 환경변수 설정 파일 작성 (`.env.example`) | `poc/docker/.env.example` | ✅ |
| 7 | Prometheus / Grafana provisioning 설정 작성 | `poc/monitoring/` | ✅ |
| 8 | 헬스체크 스크립트 작성 | `poc/docker/healthcheck.sh` | ✅ |
| 9 | `.gitignore` 작성 | `.gitignore` | ✅ |

**Day 2 완료 기준:**
- [x] `docker compose up -d` 실행 시 전체 서비스 정상 기동
- [x] PostgreSQL: `init.sql` 자동 적용 (`msg_send_history`, `msg_send_metrics`, `msg_batch_schedule`, `msg_dlq`, `ref_sender_code` 생성)
- [x] **MongoDB: `send_histories_{YYYYMM}` 월별 컬렉션 + 인덱스 초기화**
- [x] Kafka: 10개 토픽 생성 (채널별 dispatch 토픽 포함)
- [x] NiFi UI / Flink UI / Grafana UI 접근 가능 (`healthcheck.sh`로 검증)

**작업 시 주의사항:**
- 절대경로 사용 금지 — 모든 볼륨/파일 경로는 상대경로 또는 환경변수 사용
- `init.sql`은 `docker-compose.yml`의 `volumes` 마운트로 자동 실행되도록 설정
- `.env.example`을 제공하고, `.env`는 `.gitignore`에 추가

### 2.1 구성 컨테이너 목록 (총 10개)
| 컨테이너명 | 역할 및 목적 |
|---|---|
| `am-nifi` | 발송 요청 접수, 트랜잭션 ID 발급 및 라우팅 |
| `am-kafka` | 대량 메시지 버퍼링 (topic.send.*) |
| `am-zookeeper` | Kafka 브로커 관리 및 메타데이터 동기화 |
| `am-jobmanager` | Flink 마스터 노드로 Job 배포 및 TaskManager 모니터링 관리 |
| `docker-taskmanager-1, 2` | Flink 워커 노드로 실질적인 메시지 검증, 포맷팅 및 채널 분배 연산 수행 |
| `am-postgres` | 발송 이력(msg_send_history) 및 통계 데이터 보관 RDBMS |
| `am-mongodb` | 월별 발송 이력 원본 JSON 보관 NoSQL |
| `am-prometheus` | 각 컨테이너로부터 실시간 메트릭 수집 |
| `am-grafana` | 식별된 메트릭 시각화 대시보드 |

---

### Day 3 — Mock Adapter 개발 및 NiFi 플로우 구성

**상태: ✅ 완료**

**목표:** 5개 채널(SMS/MMS/RCS/FAX/Email) Mock Adapter를 개발하고, NiFi 발송 요청 수집 플로우를 구성한다.

| # | 작업 항목 | 산출물 | 상태 |
|---|---------|--------|------|
| 1 | Mock Adapter 공통 베이스 코드 작성 (tx_id.py, kafka_client.py, adapter_base.py) | `poc/services/base/` | ✅ |
| 2 | SMS Mock Adapter 구현 (성공률 95%, 지연 50ms 시뮬레이션) | `poc/services/sms-adapter/` | ✅ |
| 3 | MMS Mock Adapter 구현 (성공률 93%, 지연 80ms 시뮬레이션) | `poc/services/mms-adapter/` | ✅ |
| 4 | RCS Mock Adapter 구현 (성공률 90%, fallback→SMS 결과코드 50002) | `poc/services/rcs-adapter/` | ✅ |
| 5 | FAX Mock Adapter 구현 (성공률 85%, 지연 200ms, FAX 전용 오류코드) | `poc/services/fax-adapter/` | ✅ |
| 6 | Email Mock Adapter 구현 (성공률 98%, 지연 30ms, 바운스/스팸 시뮬레이션) | `poc/services/email-adapter/` | ✅ |
| 7 | NiFi 발송 요청 수집 플로우 템플릿 작성 (HTTP 수신 → 35자리 txId 생성 → Kafka 발행) | `poc/nifi/send-request-flow.json` | ✅ |
| 8 | NiFi 결과 수신 플로우 템플릿 작성 (Kafka 구독 → txId 35자리 검증 → DB 업데이트) | `poc/nifi/send-result-flow.json` | ✅ |

**Day 3 완료 기준:**
- [x] 각 Adapter: `GET /health` → 200 OK
- [x] 각 Adapter: Kafka `topic.send.dispatch.{channel}` 구독 및 결과를 `topic.send.result`에 발행
- [x] NiFi: HTTP POST 요청 수신 → 35자리 txId 생성 → `topic.send.request` 발행 동작 확인
- [x] txId 35자리 생성·검증·파싱 단독 테스트 `[PASS]` 확인

**Day 3 확정 결과코드 체계:**

| 코드 | 분류 | 설명 | 후속 처리 |
|------|------|------|---------|
| `10000` | 성공 | 정상 전송 완료 | 이력 DB 저장 |
| `40001~40008` | 영구 실패 | 수신번호오류/타임아웃/네트워크/MMS첨부초과/FAX무응답/FAX용지/Email바운스/스팸 | DLQ |
| `50001` | 재처리 | 통신사/SMTP 일시 오류 | `topic.send.retry` |
| `50002` | RCS fallback | RCS 미지원 단말 → SMS 채널 재발송 | `topic.send.retry` (채널 변경) |
| `50003` | 재처리 | FAX 수신 통화 중 | `topic.send.retry` |
| `50004` | 재처리 | Email SMTP 일시 오류 | `topic.send.retry` |

**⚠️ Day 3 트러블슈팅 이력 — 새 환경 작업 시 반드시 확인:**

| # | 문제 | 원인 | 해결 방법 |
|---|------|------|---------|
| 1 | Git Bash에서 `kafka-topics.sh` 실행 불가 | Windows Git Bash가 `/usr/bin/` 경로를 `C:/Program Files/Git/...`으로 자동 변환 | 명령어 앞에 `MSYS_NO_PATHCONV=1` 추가하거나 `docker exec am-kafka bash -c "kafka-topics ..."` 래핑 사용 |
| 2 | `docker compose stop nifi` → `no such service` 오류 | `docker-compose.yml` 서비스명이 `nnifi`로 오타 기재됨 | `nnifi` → `nifi`로 수정 후 해결 |
| 3 | NiFi UI `http://localhost:8080` 접근 불가 (HTTP 000) | `apache/nifi:2.0.0`은 keystore 자동 생성 후 HTTPS 8443 강제 바인딩. `NIFI_WEB_HTTPS_PORT=`(빈값) 환경변수로도 비활성화 불가 | 이미지를 `apache/nifi:1.23.2`로 교체. 1.23.2는 `NIFI_WEB_HTTP_PORT=8080`만으로 HTTP 전용 동작. 교체 후 `rm -rf poc/nifi/state/`로 기존 state 초기화 필요 |
| 4 | Windows Git Bash에서 `pip` 명령어 없음 | `which python3` → `WindowsApps/python3` (Microsoft Store stub). 실제 Python이 아니므로 `venv` 생성 실패 | Python 3.11 실제 경로 사용: `/c/Users/{user}/AppData/Local/Programs/Python/Python311/python.exe -m venv .venv` → Git Bash에서는 `source .venv/Scripts/activate` (Linux의 `bin/activate`가 아님) |

**로컬 테스트 빠른 시작 (Windows Git Bash 기준):**
```bash
# 1. 가상환경 활성화 (Windows Git Bash 전용 경로)
source .venv/Scripts/activate

# 2. txId 35자리 단독 검증 (Kafka 불필요)
python -c "
import sys; sys.path.insert(0, 'poc/services')
from base.tx_id import build_tx_id, validate_tx_id
tx = build_tx_id('03', '007')
assert len(tx) == 35 and validate_tx_id(tx)
print(f'[PASS] txId={tx} ({len(tx)}자리)')
"

# 3. Kafka 토픽 확인 (Git Bash에서는 MSYS_NO_PATHCONV=1 필수)
MSYS_NO_PATHCONV=1 docker exec am-kafka kafka-topics \
  --bootstrap-server localhost:9092 --list

# 4. NiFi UI 확인
curl -s -o /dev/null -w "NiFi: %{http_code}\n" http://localhost:8080/nifi/

# 5. Adapter 컨테이너 기동 및 헬스체크
docker compose -f poc/docker/docker-compose.adapters.yml up -d --build
for port in 8101 8102 8103 8104 8105; do
  echo "포트 $port: $(curl -s -o /dev/null -w '%{http_code}' http://localhost:$port/health)"
done
```

**새 환경 git pull 후 즉시 시작:**
```bash
# 인프라 기동
docker compose -f poc/docker/docker-compose.yml up -d

# Python 환경 구성 (최초 1회)
/c/Users/{본인계정}/AppData/Local/Programs/Python/Python311/python.exe -m venv .venv
source .venv/Scripts/activate
pip install -r poc/services/base/requirements.txt

# NiFi는 1~2분 후 http://localhost:8080/nifi/ 접근 가능
# ID: nifiadmin / PW: nifipassword123! (또는 .env 설정값)
```

---

### Day 4 — Flink Job 개발 (처리·분석 파이프라인)

**상태: ⬜ 미시작**

**목표:** Flink Job을 개발하여 발송 전 검증, 채널 분배, Rate Limiting, 성공률 집계를 구현한다.

| # | 작업 항목 | 산출물 | 상태 |
|---|---------|--------|------|
| 1 | Flink 프로젝트 구조 초기화 (Maven/Gradle, 의존성 설정) | `poc/flink/` | ⬜ |
| 2 | 발송 요청 처리 Job (검증 → 포맷팅 → 채널 분배 → dispatch 토픽 발행) | `poc/flink/jobs/SendRequestJob.java` | ⬜ |
| 3 | Rate Limiting 로직 구현 (채널별 TPS 제어, sliding window) | `poc/flink/operators/RateLimitOperator.java` | ⬜ |
| 4 | 발송 결과 처리 Job (성공률 집계, 실패 패턴 분석, 재처리 분류) | `poc/flink/jobs/SendResultJob.java` | ⬜ |
| 5 | 재처리(Retry) Job (retry 토픽 구독 → 지수 백오프 → 재발송 또는 DLQ) | `poc/flink/jobs/RetryJob.java` | ⬜ |
| 6 | Flink Job 배포 및 JobManager 등록 | Flink UI 확인 | ⬜ |

**Day 4 완료 기준:**
- [ ] Flink UI: 3개 Job 모두 RUNNING 상태 확인
- [ ] 테스트 메시지 10건 투입 → 처리 결과 DB 적재 확인
- [ ] RCS 실패 시 SMS fallback 동작 확인
- [ ] Rate Limiting: TPS 초과 시 메시지 지연 처리 확인

---

### Day 5 — 모니터링 환경 구성 (Prometheus + Grafana)

**상태: ⬜ 미시작**

**목표:** Prometheus 메트릭 수집과 Grafana 대시보드를 구성하여 실시간 발송 현황을 시각화한다.

| # | 작업 항목 | 산출물 | 상태 |
|---|---------|--------|------|
| 1 | Prometheus 설정 파일 작성 (각 서비스 scrape 설정) | `poc/monitoring/prometheus.yml` | ⬜ |
| 2 | 각 Mock Adapter Prometheus 메트릭 엔드포인트 추가 | `/metrics` 엔드포인트 | ⬜ |
| 3 | Grafana 데이터소스 설정 (Prometheus 연결) | `poc/monitoring/grafana/datasources/` | ⬜ |
| 4 | Grafana 대시보드 구성 (TPS, 성공률, 재처리 현황, 파이프라인 지연) | `poc/monitoring/grafana/dashboards/` | ⬜ |
| 5 | 이상징후 알림 규칙 설정 (성공률 95% 이하 경보) | `poc/monitoring/alert-rules.yml` | ⬜ |
| 6 | VOC 조회 API 구현 (txId/수신번호 기준 이력 조회) | `poc/services/history-api/` | ⬜ |

**Day 5 완료 기준:**
- [ ] Grafana: `http://localhost:3000` — 4개 패널 대시보드 확인
- [ ] 발송 TPS 실시간 그래프 표시 확인
- [ ] 채널별 성공률 실시간 표시 확인
- [ ] VOC API: `GET /api/v1/history/receiver/{phone}` 응답 3초 이내 확인

---

### Day 6 — 통합 테스트 및 파이프라인 정합성 검증

**상태: ⬜ 미시작**

**목표:** 전체 파이프라인을 통합 테스트하여 정합성(투입 = 처리)을 검증하고, 장애 격리 시나리오를 확인한다.

| # | 작업 항목 | 산출물 | 상태 |
|---|---------|--------|------|
| 1 | 통합 테스트 스크립트 작성 (1,000건 투입 → 전 구간 추적) | `tests/validation/pipeline_test.py` | ⬜ |
| 2 | 정합성 검증 (투입 건수 vs DB 적재 건수 비교) | 검증 리포트 | ⬜ |
| 3 | 장애 격리 테스트 (SMS Adapter 강제 종료 → 다른 채널 영향 없음 확인) | 테스트 결과 | ⬜ |
| 4 | 재처리 동작 검증 (실패율 30% 설정 → retry 토픽 적재 → 자동 재처리 확인) | 테스트 결과 | ⬜ |
| 5 | RCS fallback 동작 검증 (RCS 실패 → SMS 자동 전환 확인) | 테스트 결과 | ⬜ |
| 6 | DLQ 동작 검증 (3회 재시도 실패 → DLQ 토픽 적재 확인) | 테스트 결과 | ⬜ |

**Day 6 완료 기준:**
- [ ] 정합성: 투입 1,000건 대비 DB 적재 999건 이상 (99.9%)
- [ ] 장애 격리: SMS Adapter 중단 시 MMS/RCS/FAX/Email 정상 동작 확인
- [ ] 재처리: 실패 건의 99% 이상 retry 후 DB 적재 확인
- [ ] DLQ: 3회 실패 건 전체 DLQ 토픽 이동 확인

---

### Day 7 — 성능 테스트 (실시간 단독 / 배치 단독 / 복합 발송)

**상태: ⬜ 미시작**

**목표:** 발송 방식별로 분리하여 부하 테스트를 수행하고, 복합 상황에서의 처리량·안정성을 검증한다.

#### 시나리오 A — 실시간성 발송 단독 부하 테스트 (발송방법코드 03)

| # | 작업 항목 | 산출물 | 상태 |
|---|---------|--------|------|
| A-1 | 실시간 발송 부하 테스트 스크립트 작성 (단건 HTTP 요청 연속 투입) | `tests/load/realtime_load_test.py` | ⬜ |
| A-2 | 기본 구성(TM x2) 실시간 TPS 측정 (목표: 2,000 TPS 이상) | 성능 리포트 A-v1 | ⬜ |
| A-3 | Flink TaskManager 2→4 확장 후 TPS 재측정 (확장 효과 검증) | 성능 리포트 A-v2 | ⬜ |
| A-4 | SMS Adapter 2→4 확장 후 TPS 재측정 | 성능 리포트 A-v3 | ⬜ |
| A-5 | 피크 트래픽 (5,000 TPS 순간 투입 → Kafka 버퍼링 유실 0 확인) | 테스트 결과 A | ⬜ |

**시나리오 A 완료 기준:**
- [ ] 기본 구성: 2,000 TPS 이상, 처리 지연 500ms 이내
- [ ] TaskManager 2→4: 처리량 1.7배 이상 증가
- [ ] 피크: 5,000 TPS 투입 시 데이터 유실 0건

#### 시나리오 B — 배치성 발송 단독 부하 테스트 (발송방법코드 01/02)

| # | 작업 항목 | 산출물 | 상태 |
|---|---------|--------|------|
| B-1 | 배치 발송 부하 테스트 스크립트 작성 (N건 묶음 일괄 투입) | `tests/load/batch_load_test.py` | ⬜ |
| B-2 | 100만 건 배치 투입 → 처리 완료까지 소요 시간 측정 | 성능 리포트 B-v1 | ⬜ |
| B-3 | 예약 시각 정확도 검증 (예약 시각 기준 ±60초 이내 발송 완료 확인) | 테스트 결과 B | ⬜ |
| B-4 | Flink TaskManager 4개 구성에서 배치 처리량 재측정 | 성능 리포트 B-v2 | ⬜ |

**시나리오 B 완료 기준:**
- [ ] 100만 건 배치: 전체 처리 완료 시간 측정 (목표: 15분 이내)
- [ ] 예약 정확도: 예약 시각 기준 ±60초 이내 발송 비율 99% 이상

#### 시나리오 C — 복합 발송 부하 테스트 (실시간 + 배치 동시)

| # | 작업 항목 | 산출물 | 상태 |
|---|---------|--------|------|
| C-1 | 복합 발송 시나리오 스크립트 작성 (실시간 1,000 TPS + 배치 50만 건 동시 투입) | `tests/load/mixed_load_test.py` | ⬜ |
| C-2 | 복합 상황에서 실시간 발송 지연 영향 측정 (배치 투입이 실시간에 미치는 영향) | 성능 리포트 C-v1 | ⬜ |
| C-3 | Kafka 토픽 파티션 격리 효과 검증 (배치 토픽 포화 시 실시간 토픽 영향 없음 확인) | 테스트 결과 C | ⬜ |
| C-4 | 최종 성능 결과 정리 및 설계서 업데이트 | AM_ARCHITECTURE.md 실측치 반영 | ⬜ |

**시나리오 C 완료 기준:**
- [ ] 복합 상황에서 실시간 발송 지연: 배치 없을 때 대비 ±20% 이내 유지
- [ ] 토픽 격리: 배치 토픽 포화 시 실시간 토픽 처리량 저하 없음 확인
- [ ] 전 시나리오 정합성: 투입 건수 대비 DB 적재 건수 99.9% 이상

---

## 현재 진행 상태 요약

| Day | 작업 내용 | 상태 | 완료일 |
|-----|---------|------|------|
| Day 1 | 아키텍처 구체화 및 작업 계획 수립 | ✅ 완료 | 2026-03-24 |
| Day 2 | POC 기반 환경 구성 (Docker Compose + DB 초기화) | ✅ 완료 | 2026-03-25 |
| Day 3 | Mock Adapter 개발 및 NiFi 플로우 구성 | ✅ 완료 | 2026-03-26 |
| Day 4 | Flink Job 개발 (처리·분석 파이프라인) | ⬜ 미시작 | - |
| Day 5 | 모니터링 환경 구성 (Prometheus + Grafana) | ⬜ 미시작 | - |
| Day 6 | 통합 테스트 및 파이프라인 정합성 검증 | ⬜ 미시작 | - |
| Day 7 | 성능 테스트 (TPS, 지연시간, 확장성) | ⬜ 미시작 | - |

---

## 작업 진행 규칙

1. **매 작업 시작 전 이 파일(README.md)을 먼저 읽는다.**
2. **작업 완료 후 위 상태 표를 업데이트한다.**
3. **꼭 필요한 내용이 아니면 기존 파일을 수정하지 않는다.**
4. **파일 수정 시 변경 위치와 이유를 명시한다.**
5. **절대경로 사용 금지** — 모든 경로는 상대경로 또는 환경변수 사용
6. **새 디렉토리 생성이 필요하면 작업 전 먼저 안내한다.**
7. **DB 스키마는 항상 `poc/init/init.sql`을 통해 자동 초기화되도록 한다.**

---

## 포트 맵

| 서비스 | 포트 | 접근 URL |
|--------|------|---------|
| NiFi UI | 8080 | http://localhost:8080/nifi |
| NiFi HTTP 수신 | 8090 | http://localhost:8090/am/send (발송 요청 POST 엔드포인트) |
| Kafka Broker | 9092 | - |
| ZooKeeper | 2181 | - |
| Flink UI | 8081 | http://localhost:8081 |
| SMS Adapter | 8101 | http://localhost:8101/health · /info · /metrics |
| MMS Adapter | 8102 | http://localhost:8102/health · /info · /metrics  |
| RCS Adapter | 8103 | http://localhost:8103/health · /info · /metrics  |
| FAX Adapter | 8104 | http://localhost:8104/health · /info · /metrics  |
| Email Adapter | 8105 | http://localhost:8105/health · /info · /metrics  |
| History API | 8200 | http://localhost:8200/api/v1/ |
| PostgreSQL | 5432 | - |
| MongoDB | 27017 | - |
| Prometheus | 9090 | http://localhost:9090 |
| Grafana | 3000 | http://localhost:3000 |

---

*최종 업데이트: 2026-03-26 | 다음 작업: Day 4 — Flink Job 개발 (처리·분석 파이프라인)*
