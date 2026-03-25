# AM 아키텍처 설계서

### MIMO·CI 통합 AM을 통한 초대용량 발송 플랫폼 고도화

> **문서 버전:** v0.2 (초안 → 구체화)
> **최초 작성일:** 2026-03-24
> **상태:** POC 진행 중

---

## 목차

1. [설계 배경 및 목적](#1-설계-배경-및-목적)
2. [코어 영역 / 비즈니스 영역 분리 설계](#2-코어-영역--비즈니스-영역-분리-설계)
3. [오픈소스 3종 역할 정의 및 파이프라인 설계](#3-오픈소스-3종-역할-정의-및-파이프라인-설계)
4. [발송 파이프라인 설계](#4-발송-파이프라인-설계)
5. [멀티채널 발송 인터페이스 표준화 설계](#5-멀티채널-발송-인터페이스-표준화-설계)
6. [기능별 독립 서비스 기반 가용성·확장성 설계](#6-기능별-독립-서비스-기반-가용성확장성-설계)
7. [통합 모니터링 및 VOC 대응 체계](#7-통합-모니터링-및-voc-대응-체계)
8. [현행 대비 개선 효과 요약](#8-현행-대비-개선-효과-요약)
9. [POC 환경 구성 계획](#9-poc-환경-구성-계획)
10. [데이터 모델 설계](#10-데이터-모델-설계)
11. [장애 시나리오 및 대응 설계](#11-장애-시나리오-및-대응-설계)
12. ⭐ [발송 방식 및 트랜잭션 ID 구조](#12-발송-방식-및-트랜잭션-id-구조) ← **신규 추가**
13. ⭐ [AS-IS 연동 구조 상세](#13-as-is-연동-구조-상세) ← **신규 추가**
14. ⭐ [MongoDB 고객 발송 이력 적재 구조](#14-mongodb-고객-발송-이력-적재-구조) ← **신규 추가**
15. ⭐ [RDBMS 선택 분석 — PostgreSQL vs TiberoDB](#15-rdbms-선택-분석--postgresql-vs-tiberodb) ← **신규 추가**

---

## 1. 설계 배경 및 목적

### 1.1 현행 아키텍처 한계

현행 MIMO와 CI가 분리 운영되는 구조는 다음과 같은 한계를 가지고 있다.

**MIMO 영역:**
- 실시간 메시지 추적 과정에서 VOC 처리 시간 과다 소요 (건당 약 30분)
- 시스템 전체가 하나로 묶여 있어, 특정 기능만 수정해도 전체를 다시 배포해야 함
- Scale In/Out 불가로 트래픽 증감 대응 어려움
- 신규 연동 포인트 확장 시 기존 구조에 종속적 수정 필요
- 발송 실패 건에 대한 자동 재처리 불가

**고객접점이력통합관리(CI) 영역:**
- nMIMO와 CI SMS 간 공통 로직(템플릿 관리, 발송 퍼미션 관리 등)이 분산 구현되어 관리 포인트 증가
- 정책 변경·기능 개선 시 양 시스템 동시 수정 필요
- 실발송 결과 상세 파악이 어려워 이력 역추적 불가

**KOS-CRM 공통 영역:**
- 다수의 연동 구간(CI→InfiniGW→TCP→nMIMO)으로 발송 처리 시 구조적 지연 발생
- 실 예약시간보다 내부 처리·연동으로 인해 발송이 뒤늦게 이루어지는 사례 발생
- MIMO와 CI 간 조회 범위 상이로 통합 발송 이력 관리 불가

### 1.2 설계 목적

상기 한계를 해소하기 위해, MIMO·CI 공통 기능을 통합한 신규 AM 아키텍처를 설계한다.
오픈소스 3종(NiFi·Kafka·Flink) 조합 기반의 발송 파이프라인을 구축하고,
코어 영역과 비즈니스 영역을 분리하여 멀티채널 확장이 가능한 구조를 확보한다.

---

## 2. 코어 영역 / 비즈니스 영역 분리 설계

### 2.1 분리 기준

AM 아키텍처는 **코어 영역**과 **비즈니스 영역**을 명확히 분리하여,
코어 영역은 채널·서비스와 무관한 독립적 솔루션으로서의 가치를 확보한다.

| 구분 | 코어 영역 | 비즈니스 영역 |
|------|----------|--------------|
| 정의 | 채널·서비스에 독립적인 발송 처리 공통 기능 | 특정 채널·서비스에 종속적인 업무 로직 |
| 변경 빈도 | 낮음 (안정적) | 높음 (사업 요구에 따라 수시 변경) |
| 소유 | 플랫폼 팀 (솔루션 자산) | 각 서비스 담당 |

### 2.2 코어 영역 구성요소

| 기능 | 설명 | 담당 컴포넌트 |
|------|------|------------|
| 발송 분배 | 요청 수신 후 채널별 분배 처리 (라운드로빈, 우선순위 등) | Flink |
| 메시지 포맷팅 | 채널별 메시지 규격 변환 (SMS 80byte, MMS 멀티미디어, RCS 리치카드 등) | Flink |
| 발송 전 검증 | 수신번호 유효성, 발신번호 사전등록 여부, 수신거부 조회 등 공통 검증 | Flink |
| 채널 분배 | 메시지 유형·정책에 따른 최적 채널 자동 선택 (예: RCS 실패 시 SMS fallback) | Flink |
| 재처리(Retry) | 발송 실패 건 자동 재처리, 재시도 정책(횟수, 간격, 조건) 관리 | Flink + Kafka |
| 이력 관리 | 트랜잭션 ID 기반 발송 요청~결과 수신 전 구간 이력 추적 | NiFi + PostgreSQL |
| 트래픽 제어 | Rate Limiting, 발송 TPS 자동 조절 | Flink |

### 2.3 비즈니스 영역 구성요소

| 기능 | 설명 | 담당 컴포넌트 |
|------|------|------------|
| 캠페인 관리 | AB캠페인, 타겟팅, 발송 스케줄링 등 사업 부서 업무 로직 | Business API |
| 템플릿 관리 | 사업 부서별 메시지 템플릿 등록·관리 | Business API + PostgreSQL |
| 발송 퍼미션 | 수신동의, 야간발송 제한, 부서별 발송 권한 관리 | Business API |
| 통계·리포팅 | 사업 부서 대상 발송 현황, 캠페인 성과 리포트 | Grafana + PostgreSQL |
| 채널별 Adapter | 채널(SKT, KT, LGU+, 카카오 등) 연동 인터페이스 | Adapter 서비스 (MSA) |

### 2.4 분리 효과

- 코어 영역은 비즈니스와 무관한 **솔루션 자산**으로 독립적 운영·확장 가능
- 비즈니스 요구사항 변경 시 코어 영역 수정 없이 비즈니스 영역만 변경
- 신규 채널 추가 시 Adapter만 개발하여 코어 파이프라인에 연결하는 구조로 **확장 비용 최소화**

---

## 3. 오픈소스 3종 역할 정의 및 파이프라인 설계

### 3.1 오픈소스 3종 조합 개요

단일 오픈소스로는 AM이 요구하는 "이력 추적 + 대용량 처리 + 실시간 분석"을 동시에 충족할 수 없다.
각 도구가 잘하는 영역만 담당하고, 못하는 부분은 다른 도구가 보완하는 3종 조합 구조를 채택한다.

**NiFi (수집·추적) → Kafka (버퍼링) → Flink (처리·분석)**

| 구분 | Apache NiFi | Apache Kafka | Apache Flink |
|------|------------|-------------|-------------|
| 역할 | 데이터 수집 / 라우팅 / 추적 | 메시지 버퍼링 / 전달 | 데이터 처리 / 분석 |
| 핵심 기능 | 대부분의 시스템과 즉시 연결 가능, 데이터 흐름 경로 자동 추적 | 대량 메시지 순서 대기 처리, 시스템 장애 시에도 데이터 유실 방지 | 실시간·일괄 처리 동시 지원, 중복·누락 없는 정확한 1회 처리 보장 |
| AM 내 역할 | 발송 요청/결과 수집, 트랜잭션 ID 추적 시작 | 피크 트래픽 버퍼링, 속도 차이 흡수 | Rate Limiting, 성공률 집계, 실패 패턴 분석 |
| POC 버전 | 2.0.x | 3.6.x | 1.18.x |
| 포트 | 8080 (UI), 9090 (API) | 9092 (Broker), 2181 (ZooKeeper) | 8081 (UI), 6123 (RPC) |

### 3.2 3종 조합이 필요한 이유

| 단독 사용 시 한계 | 3종 조합 시 해결 |
|------------------|-----------------|
| NiFi 단독: 복잡한 연산(집계, 윈도우 분석) 불가 | Flink가 연산 담당 |
| Flink 단독: 다양한 프로토콜 수집 불가, 이력 추적 미지원 | NiFi가 수집·추적 담당 |
| Kafka 없이 NiFi→Flink 직접 연결: 처리 속도 차이로 병목 발생 | Kafka가 중간에서 속도 차이 흡수 |
| NiFi/Flink 단독: 피크 트래픽 시 데이터 유실 위험 | Kafka가 버퍼링·유실 방지 담당 |

### 3.3 요구사항별 오픈소스 처리 가능 여부

| 요구사항 | NiFi 단독 | Kafka 단독 | Flink 단독 | 3종 조합 |
|---------|----------|-----------|-----------|---------|
| 다양한 프로토콜 수집 (HTTP, TCP, REST) | ✓ | ✗ | ✗ | ✓ |
| 트랜잭션 ID 기반 이력 추적 | ✓ | ✗ | ✗ | ✓ |
| 일 5,000만 건 메시지 버퍼링 | △ | ✓ | ✗ | ✓ |
| 시스템 장애 시 데이터 유실 방지 | △ | ✓ | △ | ✓ |
| 실시간 + 일괄 통합 처리 | ✗ | ✗ | ✓ | ✓ |
| Rate Limiting / 트래픽 제어 | ✗ | ✗ | ✓ | ✓ |
| 실시간 성공률 집계 / 패턴 분석 | ✗ | ✗ | ✓ | ✓ |
| 중복·누락 없는 정확한 1회 처리 보장 | ✗ | △ | ✓ | ✓ |

### 3.4 NiFi vs Flink 상세 비교

| 비교 항목 | Apache NiFi | Apache Flink |
|----------|------------|-------------|
| **설계 목적** | 데이터 흐름 자동화 (수집, 라우팅, 전달) | 분산 스트림/일괄 데이터 처리 |
| **핵심 역할** | 데이터 이동 (Move) | 데이터 연산 (Compute) |
| **처리 방식** | Flow 기반 (FlowFile 단위) | Stream 기반 (Record 단위) |
| **개발 방식** | GUI 드래그&드롭 (코딩 최소) | Java/Scala/Python 코딩 |
| **초당 처리량** | 수만 ~ 수십만 건/초 | 수백만 ~ 수천만 건/초 |
| **지연 시간** | 수십 ms ~ 수백 ms | 수 ms ~ 수십 ms |
| **메모리 사용** | 디스크 기반 (안정성 우선) | 메모리 기반 (속도 우선) |
| **프로토콜 지원** | 300개 이상 (거의 모든 시스템 즉시 연결) | 제한적 (Kafka, DB 등 주요 시스템) |
| **상태 관리** | Stateless 위주 | Stateful 강점 (이전 상태 기억) |
| **정확한 1회 처리** | 미지원 | 지원 (Checkpoint + 2PC) |
| **이력 추적** | 내장 (데이터 흐름 경로 자동 추적) | 별도 구현 필요 |
| **운영 복잡도** | 낮음 | 높음 |

### 3.5 처리량 비교 및 AM 목표 달성 분석

**AM 목표:** 일 5,000만 건 (영업시간 11시간 기준, 초당 약 1,260건)

| 구분 | Apache NiFi | Apache Flink |
|------|------------|-------------|
| 초당 처리량 | 수만 ~ 수십만 건/초 | 수백만 ~ 수천만 건/초 |
| 일일 처리량 | 수억 ~ 수십억 건 | 수백억 ~ 수조 건 |
| 지연 시간 | 수십 ms ~ 수백 ms | 수 ms ~ 수십 ms |

NiFi 단독으로도 처리량 자체는 충분하나, 실시간 집계·분석·정확한 1회 처리 보장 등 연산 요구사항은 Flink가 담당해야 하며,
피크 트래픽 버퍼링은 Kafka가 보완해야 한다. 따라서 3종 조합이 최적 구성이다.

---

## 4. 발송 파이프라인 설계

### 4.1 전체 파이프라인 흐름

```
[발송 요청 채널]                  [코어 파이프라인]                    [채널 Adapter]
CI / AB캠페인                                                         SMS Adapter
제휴CRM          → HTTP/TCP →  NiFi  →  Kafka  →  Flink  →  Kafka  → MMS Adapter
직접 API                     (수집)   (버퍼링)   (처리)   (분배)    → RCS Adapter
                              ↓                    ↓                  → FAX Adapter
                           트랜잭션ID            Rate Limit           → Email Adapter
                           추적시작             채널분배
                                               검증/포맷팅
                                                   ↓
                                            [발송 결과 수신]
                                         Adapter → NiFi → Kafka → Flink
                                                                    ↓
                                                            [이력 DB + 모니터링]
```

### 4.2 1단계: 발송 요청 흐름

발송 요청 채널(CI, AB캠페인, 제휴CRM 등)에서 요청이 들어오면 다음 순서로 처리된다.

**Step 1. NiFi (수집·추적)**
- 상류 발송 시스템(CI, AB캠페인 등)이 생성한 **35자리 트랜잭션 ID를 수신**하고, 형식 검증(35자리 숫자, 발송방법코드 01~05) 후 추적을 시작한다.
- **txId는 NiFi가 생성하지 않는다.** 반드시 상류 발송 시스템이 요청 전문에 포함하여 전달해야 한다.
- HTTP, TCP, REST 등 다양한 방식으로 들어오는 요청을 수집한다.
- 수신된 메시지에 타임스탬프, 소스 식별자를 부착한다.
- Kafka `topic.send.request` 토픽으로 발행한다.

**Step 2. Kafka (버퍼링)**
- `topic.send.request` 토픽에서 메시지를 순서대로 보관한다.
- 파티션: 채널 유형별 분리 (SMS/MMS/RCS/FAX/Email)
- 요청이 갑자기 몰려도 유실 없이 버텨준다. (retention: 24h)

**Step 3. Flink (처리·분석)**
- **발송 전 검증:** 수신번호 유효성, 발신번호 등록 여부, 수신거부 DB 조회
- **메시지 포맷팅:** 채널별 규격 변환 (SMS 80byte, MMS 멀티미디어 등)
- **채널 분배:** 정책 기반 최적 채널 선택 (RCS 실패 시 SMS fallback 포함)
- **Rate Limiting:** 채널별 TPS 제한 적용 (sliding window 방식)
- 처리된 메시지를 `topic.send.dispatch` 토픽으로 발행한다.

**Step 4. MSA 발송 서비스 (채널별 Adapter)**
- `topic.send.dispatch` 토픽 구독
- 채널별 실제 연동 (TCP, REST API, SMTP 등)
- 실발송 후 결과를 `topic.send.result` 토픽으로 발행

### 4.3 2단계: 발송 결과 수신 흐름

통신사, 카카오, 메일서버 등에서 발송 결과가 돌아오면 다음 순서로 처리된다.

**Step 1. Adapter (결과 정규화)**
- 채널별 상이한 응답 형식을 공통 결과 포맷(JSON)으로 변환
- 트랜잭션 ID 매핑 (발송 요청과 결과 연결)
- 결과 코드 표준화 (성공: `10000`, 실패: `4xxxx`, 재처리: `5xxxx`)

**Step 2. NiFi (수집·추적)**
- `topic.send.result` 토픽에서 결과 수집
- 최초 발송 요청 이력과 연결 (트랜잭션 ID 기준)
- 실패 건: `topic.send.retry` 토픽으로 라우팅

**Step 3. Kafka (버퍼링)**
- 결과 이벤트 버퍼링 (`topic.send.result`)
- 재처리 대상 분리 (`topic.send.retry`)

**Step 4. Flink (처리·분석)**
- 채널별·시간별 성공률 실시간 집계
- 실패 패턴 분석 (특정 채널/번호대역 집중 실패 감지)
- 재처리 정책 적용 (최대 3회, 지수 백오프 간격)
- 집계 결과를 이력 DB(PostgreSQL)에 저장

**Step 5. 이력 저장 + 통합 모니터링**
- 전 구간 이력을 `msg_send_history` 테이블에 저장
- Grafana 대시보드에 실시간 반영

### 4.4 Kafka 연동 구조 — Connector 방식

AM 플랫폼은 Kafka를 **producer/consumer 직접 코딩 방식이 아닌 Kafka Connect Connector 방식**으로 운용한다.  
각 컴포넌트(NiFi, Flink, DB, Adapter)는 Connector를 통해 Kafka와 연결되며, 연동 설정은 JSON 기반 Connector 설정 파일로 관리한다.

#### Connector 방식 채택 이유

| 항목 | producer/consumer 직접 코딩 | Kafka Connector 방식 |
|------|--------------------------|-------------------|
| 연동 코드 | 각 서비스마다 Kafka 클라이언트 코드 작성 | Connector 설정 파일(JSON)만 작성 |
| 장애 복구 | 서비스별 별도 구현 필요 | Kafka Connect 프레임워크가 자동 처리 |
| 오프셋 관리 | 직접 관리 | Connect 프레임워크 자동 관리 |
| 모니터링 | 서비스별 개별 구현 | Connect REST API로 통합 모니터링 |
| 확장성 | 서비스 재배포 필요 | Worker 추가만으로 확장 |
| 재사용성 | 낮음 | 동일 Connector 타입 재사용 가능 |

#### Connector 유형별 역할

| Connector | 방향 | 연결 구간 | 설명 |
|-----------|------|---------|------|
| `NiFiKafkaSink` | NiFi → Kafka | 발송 요청 수집 결과 → `topic.send.request` | NiFi가 수집·가공한 메시지를 Kafka에 적재 |
| `FlinkKafkaSource` | Kafka → Flink | `topic.send.request` → Flink 처리 Job | Flink가 Kafka 토픽을 Source로 읽음 |
| `FlinkKafkaSink` | Flink → Kafka | Flink 처리 결과 → `topic.send.dispatch.*` | Flink가 채널 분배 결과를 Kafka에 발행 |
| `AdapterKafkaSource` | Kafka → Adapter | `topic.send.dispatch.{channel}` → 각 Adapter | Adapter가 할당 토픽을 Source로 읽음 |
| `AdapterKafkaSink` | Adapter → Kafka | 발송 결과 → `topic.send.result` | Adapter가 실발송 결과를 Kafka에 발행 |
| `JdbcSink` | Kafka → PostgreSQL/TiberoDB | `topic.send.result` → 이력 DB | 발송 결과를 DB에 직접 적재 |
| `MongoSink` | Kafka → MongoDB | `topic.send.result` → MongoDB 컬렉션 | 고객 발송 이력 MongoDB 적재 |
| `PrometheusMetricsSink` | Kafka → Prometheus | `topic.monitor.metrics` → Prometheus | 실시간 지표 수집 |

#### Connector 연동 흐름도

```
[NiFi]
  └─(NiFiKafkaSink)─────────────────────────────────┐
                                                     ↓
                                          [topic.send.request]
                                                     ↓
                                          (FlinkKafkaSource)
                                                     ↓
                                               [Flink Job]
                                          (FlinkKafkaSink)
                                                     ↓
                              ┌────────────────────────────────────┐
                              ↓                                    ↓
                   [topic.send.dispatch.sms]          [topic.send.dispatch.email] ...
                              ↓                                    ↓
                   (AdapterKafkaSource)                (AdapterKafkaSource)
                              ↓                                    ↓
                        [SMS Adapter]                      [Email Adapter]
                   (AdapterKafkaSink)                  (AdapterKafkaSink)
                              └──────────────┬─────────────────────┘
                                             ↓
                                  [topic.send.result]
                                             ↓
                         ┌───────────────────┼───────────────────┐
                    (JdbcSink)          (MongoSink)    (PrometheusMetricsSink)
                         ↓                  ↓                    ↓
                 [이력 DB (RDB)]         [MongoDB]         [Prometheus]
```

### 4.5 Kafka 토픽 설계

| 토픽명 | 파티션 수 | Retention | 연결 Connector | 설명 |
|--------|---------|-----------|--------------|------|
| `topic.send.request` | 12 | 24h | NiFiKafkaSink → FlinkKafkaSource | 발송 요청 수신 |
| `topic.send.dispatch.sms` | 6 | 6h | FlinkKafkaSink → AdapterKafkaSource | SMS 발송 분배 |
| `topic.send.dispatch.mms` | 6 | 6h | FlinkKafkaSink → AdapterKafkaSource | MMS 발송 분배 |
| `topic.send.dispatch.rcs` | 6 | 6h | FlinkKafkaSink → AdapterKafkaSource | RCS 발송 분배 |
| `topic.send.dispatch.fax` | 3 | 6h | FlinkKafkaSink → AdapterKafkaSource | FAX 발송 분배 |
| `topic.send.dispatch.email` | 3 | 6h | FlinkKafkaSink → AdapterKafkaSource | Email 발송 분배 |
| `topic.send.result` | 12 | 48h | AdapterKafkaSink → JdbcSink/MongoSink | 발송 결과 수신 |
| `topic.send.retry` | 6 | 72h | Flink RetryJob Source | 재처리 대상 |
| `topic.send.dlq` | 3 | 7d | - (수동 확인용) | Dead Letter Queue (최종 실패) |
| `topic.monitor.metrics` | 3 | 1h | PrometheusMetricsSink | 실시간 지표 스트리밍 |

### 4.6 재처리(Retry) 정책

| 항목 | 설정값 |
|------|--------|
| 최대 재시도 횟수 | 3회 |
| 초기 대기 간격 | 30초 |
| 대기 증가 방식 | 지수 백오프 (30s → 60s → 120s) |
| DLQ 이동 조건 | 3회 실패 후 `topic.send.dlq`로 이동 |
| DLQ 알림 | Slack/이메일 알림 발송 |

---

## 5. 멀티채널 발송 인터페이스 표준화 설계

### 5.1 설계 원칙

코어 발송 엔진은 **채널 독립적**으로 설계하여, 어떤 채널이든 동일한 인터페이스 구조로 연결 가능하도록 한다.
신규 채널 추가 시 Adapter만 개발하면 코어 파이프라인 수정 없이 확장 가능한 구조를 확보한다.

### 5.2 표준 메시지 포맷 (공통 JSON 규격)

```json
{
  "txId": "35자리-숫자-문자열 (상류-발송시스템-생성, 예: 12345678901230308400700000000000001)",
  "requestId": "외부-요청-식별자",
  "channel": "SMS|MMS|RCS|FAX|EMAIL",
  "priority": 1,
  "sender": "발신번호",
  "receiver": "수신번호",
  "subject": "제목(MMS/EMAIL only)",
  "body": "메시지 본문",
  "attachments": [],
  "scheduledAt": "2026-03-24T10:00:00+09:00",
  "requestedAt": "2026-03-24T09:59:00+09:00",
  "source": "CI|AB_CAMPAIGN|CRM|DIRECT_API",
  "meta": {
    "campaignId": "캠페인ID",
    "templateId": "템플릿ID",
    "retryCount": 0
  }
}
```

### 5.3 채널별 Adapter 표준 규격

모든 Adapter는 아래 표준 인터페이스를 구현한다.

| 항목 | 규격 |
|------|------|
| 요청 수신 | Kafka Topic 구독 (`topic.send.dispatch.{channel}`) |
| 요청 포맷 | 공통 메시지 규격 (JSON) |
| 응답 반환 | Kafka Topic 발행 (`topic.send.result`) |
| 필수 필드 | txId, channel, receiver, sender, body, resultCode |
| 연동 방식 | 채널별 상이 (TCP, REST API, SMTP 등) → Adapter 내부에서 변환 |
| 헬스체크 | `GET /health` 응답 (200 OK) |
| 재처리 신호 | resultCode `5xxxx` 시 retry 토픽으로 자동 라우팅 |

### 5.4 채널별 발송 현황 및 확장 계획

| 채널 | 현행 지원 | POC 범위 | 향후 확장 |
|------|----------|---------|----------|
| SMS | ✓ (nMIMO/cMIMO) | ✓ Mock Adapter | - |
| MMS | ✓ (nMIMO/cMIMO) | ✓ Mock Adapter | - |
| RCS | ✓ (일부) | ✓ Mock Adapter | 전면 확대 |
| FAX | ✓ (CI 경유) | ✓ Mock Adapter | Adapter 신규 |
| Email | ✓ (CI 경유) | ✓ Mock Adapter | Adapter 신규 |
| 카카오톡 | - | 검토 중 | Adapter 신규 |
| Push | - | 검토 중 | Adapter 신규 |

> **POC 방침:** 실제 통신사 연동 없이 Mock Adapter로 파이프라인 정합성 검증.
> Mock Adapter는 설정 가능한 성공률(기본 95%)과 응답 지연(기본 50ms)을 시뮬레이션한다.

---

## 6. 기능별 독립 서비스 기반 가용성·확장성 설계

### 6.1 구조 전환

현행 하나로 묶인 구조에서, 기능별 독립 서비스 구조로 전환하여 모듈 단위 독립 배포·장애 격리·자동 확장을 확보한다.

| 항목 | 현행 (일체형) | 신규 (기능별 독립 서비스) |
|------|-------------|----------------------|
| 배포 단위 | 전체 시스템 일괄 배포 | 서비스 단위 독립 배포 |
| 장애 영향 | 엔진 1개 장애 시 전체 발송 약 30% 저하 | 장애가 해당 서비스에만 한정, 나머지 정상 운영 |
| 확장 방식 | 수동 TPS 조정 | 필요한 서비스만 자동 확장 |
| 수정 범위 | 단일 기능 변경에도 전체 재배포 | 해당 서비스만 수정·배포 |

### 6.2 서비스 목록 및 역할

| 서비스명 | 역할 | 기술 스택 | POC 레플리카 수 |
|---------|------|---------|--------------|
| `nifi` | `poc-nifi` | 데이터 수집·라우팅·추적 | Apache NiFi 2.0 | 1 |
| `kafka` | `poc-kafka` | 메시지 버퍼링 | Apache Kafka 3.6 + ZooKeeper | 1 |
| `flink-jobmanager` | `poc-jobmanager` | Flink 마스터 로드. 분산 처리 작업의 스케줄링 및 체크포인트 관리 수행 | Apache Flink 1.18 | 1 |
| `flink-taskmanager` | `docker-taskmanager-N` | Flink 워커 노드. 메시지 검증, 양식 변환, Rate limit 등 실제 연산 수행 | Apache Flink 1.18 | 2 (확장 테스트 시 4) |
| `sms-adapter` | `poc-sms-adapter` | SMS Mock 발송 | Python FastAPI | 2 |
| `mms-adapter` | `poc-mms-adapter` | MMS Mock 발송 | Python FastAPI | 2 |
| `rcs-adapter` | `poc-rcs-adapter` | RCS Mock 발송 | Python FastAPI | 1 |
| `fax-adapter` | `poc-fax-adapter` | FAX Mock 발송 | Python FastAPI | 1 |
| `email-adapter` | `poc-email-adapter` | Email Mock 발송 | Python FastAPI | 1 |
| `postgres` | `poc-postgres` | 발송 이력 및 통계 RDBMS | PostgreSQL 15 | 1 |
| `mongodb` | `poc-mongodb` | 월별 이력 보관 NoSQL | MongoDB 6.0 | 1 |
| `prometheus` | `poc-prometheus` | 메트릭 수집 | Prometheus 2.x | 1 |
| `grafana` | `poc-grafana` | 시각화 대시보드 | Grafana 10.x | 1 |

### 6.3 확장성 검증 목표

- 레플리카 N개 유지 시, 1개 Pod 장애에도 서비스 정상 운영
- `flink-taskmanager` 2개 → 4개 확장 시 처리량 1.7배 이상 증가
- `sms-adapter` 2개 → 4개 확장 시 처리량 1.8배 이상 증가
- 결과 수신 및 처리 정합성 99.9% 이상

---

## 7. 통합 모니터링 및 VOC 대응 체계

### 7.1 모니터링 구성 방향

| 구분 | 현행 | 신규 |
|------|------|------|
| 모니터링 범위 | MIMO·CI 각각 분리 | 전 구간 단일 화면 통합 |
| VOC 처리 | 파일 수동 확인 (약 30분) | 번호 1건 조회로 즉시 확인 (5분 이내) |
| 이상징후 탐지 | 사후 대응 | 실시간 탐지 (1분 이내) |
| 발송 이력 | 시스템 간 분리, 역추적 어려움 | 트랜잭션 ID 기반 전 구간 통합 추적 |

### 7.2 핵심 기능

- 트랜잭션 ID 기반 발송 이력 통합 조회 (조회 응답시간 3초 이내)
- 실시간 발송 성공률·TPS 대시보드
- 이상징후 자동 탐지 및 알림 (리드타임 1분 이내)
- 발송 실패 건 자동 재처리(Retry) 현황 모니터링

### 7.3 Grafana 대시보드 구성

| 패널 | 지표 | 갱신 주기 |
|------|------|---------|
| 전체 발송 TPS | 채널별 초당 발송 건수 | 5초 |
| 실시간 성공률 | 채널별 성공/실패 비율 | 10초 |
| 재처리 현황 | retry 토픽 메시지 수, DLQ 적재 수 | 30초 |
| 파이프라인 지연 | 요청~실발송 평균 지연 ms | 10초 |
| 이상징후 알림 | 성공률 95% 이하 시 경보 | 실시간 |

### 7.4 VOC 대응 API

```
GET /api/v1/history/tx/{txId}          # 트랜잭션 ID로 전 구간 이력 조회
GET /api/v1/history/receiver/{phone}   # 수신번호로 발송 이력 조회
GET /api/v1/metrics/success-rate       # 실시간 성공률 조회
GET /api/v1/metrics/tps                # 실시간 TPS 조회
```

---

## 8. 현행 대비 개선 효과 요약

| 항목 | 현행 (AS-IS) | 목표 (TO-BE) |
|------|-------------|-------------|
| 일 발송 처리량 | ~1,500만 건 | 5,000만 건 |
| 전 고객 발송 소요 | 약 1주 | 당일(One-Day) |
| VOC 처리 시간 | 약 30분 | 5분 이내 |
| 장애 시 재발송 | 불가 | 자동 재처리 (성공률 99% 이상) |
| TPS 조정 | 수동 | 자동 (레플리카 기반) |
| 이상징후 탐지 | 사후 대응 | 실시간 1분 이내 |
| 배포 방식 | 전체 배포 (일체형) | 모듈 단위 독립 배포 |
| 공통 로직 관리 | MIMO·CI 분산 | 코어 영역 통합 |
| 채널 확장 | 구조적 수정 필요 | Adapter 연결만으로 확장 |

---

## 9. POC 환경 구성 계획

### 9.1 POC 범위

실제 통신사 연동 없이 **Mock Adapter** 기반으로 파이프라인 정합성 및 성능을 검증한다.

| 검증 항목 | 목표 수치 |
|---------|---------|
| 발송 요청 → 실발송까지 처리 지연 | 500ms 이내 |
| 초당 처리량(TPS) | 2,000 TPS 이상 |
| 파이프라인 정합성 | 투입 메시지 수 = 처리 메시지 수 (±0.1%) |
| 장애 격리 | Adapter 1개 장애 시 나머지 채널 정상 |
| 자동 재처리 | 실패 건 99% 이상 자동 재처리 성공 |

### 9.2 POC 환경 구성도

```
Docker Compose 환경
┌─────────────────────────────────────────────────────┐
│                                                     │
│  [Load Generator]  →  [NiFi:8080]                  │
│                           ↓                         │
│                    [Kafka:9092]                     │
│                           ↓                         │
│              [Flink JobManager:8081]               │
│              [Flink TaskManager x2]                │
│                           ↓                         │
│   [SMS-Adapter] [MMS-Adapter] [RCS-Adapter]        │
│   [FAX-Adapter] [Email-Adapter]  (Mock)            │
│                           ↓                         │
│              [PostgreSQL:5432]                      │
│                           ↓                         │
│   [Prometheus:9090]  →  [Grafana:3000]             │
│                                                     │
└─────────────────────────────────────────────────────┘
```

### 9.3 POC 디렉토리 구조

```
am-platform/
├── README.md                       # 작업 계획 및 진행 현황
├── docs/
│   ├── architecture/
│   │   └── AM_ARCHITECTURE.md      # 본 설계서
│   └── diagrams/                   # 아키텍처 다이어그램
├── poc/
│   ├── docker/
│   │   └── docker-compose.yml      # 전체 환경 정의
│   ├── config/
│   │   └── kafka-topics.sh         # Kafka 토픽 초기화
│   ├── init/
│   │   └── init.sql                # DB 초기화 스크립트
│   ├── nifi/                       # NiFi 플로우 템플릿
│   ├── flink/                      # Flink Job 소스
│   ├── services/                   # Mock Adapter 서비스
│   └── monitoring/                 # Prometheus/Grafana 설정
└── tests/
    ├── load/                       # 부하 테스트 스크립트
    └── validation/                 # 정합성 검증 스크립트
```

---

## 10. 데이터 모델 설계

### 10.1 핵심 테이블

#### msg_send_history (발송 이력 메인 테이블)

| 컬럼명 | 타입 | 설명 |
|--------|------|------|
| id | BIGSERIAL PK | 자동 증가 ID |
| tx_id | VARCHAR(35) | 트랜잭션 ID — 35자리 숫자 구조 |
| request_id | VARCHAR(100) | 외부 요청 식별자 |
| channel | VARCHAR(20) | 발송 채널 (SMS/MMS/RCS/FAX/EMAIL) |
| sender | VARCHAR(50) | 발신번호 |
| receiver | VARCHAR(50) | 수신번호 |
| status | VARCHAR(20) | PENDING/SENT/DELIVERED/FAILED/RETRYING |
| result_code | VARCHAR(10) | 결과 코드 (성공: `10000` / 실패: `4xxxx` / 재처리: `5xxxx`) |
| retry_count | SMALLINT | 재시도 횟수 |
| source | VARCHAR(30) | 요청 출처 (CI/AB_CAMPAIGN/CRM) |
| scheduled_at | TIMESTAMPTZ | 예약 발송 시각 |
| requested_at | TIMESTAMPTZ | 요청 수신 시각 |
| dispatched_at | TIMESTAMPTZ | 실발송 시각 |
| delivered_at | TIMESTAMPTZ | 결과 수신 시각 |
| created_at | TIMESTAMPTZ DEFAULT NOW() | 레코드 생성 시각 |

#### msg_send_metrics (집계 지표 테이블)

| 컬럼명 | 타입 | 설명 |
|--------|------|------|
| id | BIGSERIAL PK | 자동 증가 ID |
| metric_time | TIMESTAMPTZ | 집계 시각 (1분 단위) |
| channel | VARCHAR(20) | 발송 채널 |
| total_count | INTEGER | 총 발송 건수 |
| success_count | INTEGER | 성공 건수 |
| fail_count | INTEGER | 실패 건수 |
| send_method_code | CHAR(2)  | 발송 방법 코드 | 
| retry_count | INTEGER | 재처리 건수 |
| dlq_count        | INTEGER  | DLQ 적재 건수 |                                       
| avg_latency_ms | INTEGER | 평균 처리 지연(ms) |
| p95_latency_ms   | INTEGER  | 95 백분위 처리 지연(ms) |                     
| created_at | TIMESTAMPTZ DEFAULT NOW() | 레코드 생성 시각 |

### 10.2 인덱스 설계

```sql
-- VOC 조회 최적화 (수신번호 기준)
CREATE INDEX idx_send_history_receiver ON msg_send_history(receiver, requested_at DESC);

-- 트랜잭션 ID 조회
CREATE INDEX idx_send_history_tx_id ON msg_send_history(tx_id);

-- 상태별 조회 (재처리 대상 필터링)
CREATE INDEX idx_send_history_status ON msg_send_history(status, channel);

-- 시계열 집계
CREATE INDEX idx_send_metrics_time ON msg_send_metrics(metric_time DESC, channel);
```

---

## 11. 장애 시나리오 및 대응 설계

### 11.1 장애 유형별 대응

| 장애 유형 | 영향 범위 | 자동 대응 | 수동 대응 |
|---------|---------|---------|---------|
| Adapter 1개 장애 | 해당 채널만 영향 | 다른 Adapter로 요청 분산 | Adapter 재기동 |
| Flink TaskManager 장애 | 처리 지연 발생 | Checkpoint 기반 자동 복구 | TaskManager 재기동 |
| Kafka 브로커 장애 | 메시지 버퍼링 중단 | 레플리카 파티션으로 자동 전환 | 브로커 재기동 |
| NiFi 장애 | 신규 요청 수집 중단 | - | NiFi 재기동 (큐 데이터 보존) |
| DB 장애 | 이력 저장 실패 | 메시지는 Kafka에 보존 | DB 복구 후 재적재 |

### 11.2 데이터 유실 방지

| 구간 | 유실 방지 방법 |
|------|-------------|
| 요청 수신 ~ Kafka | NiFi 내부 큐 (디스크 기반, 재기동 시 보존) |
| Kafka ~ Flink | Kafka offset 관리 + Flink Checkpoint |
| Flink ~ Adapter | Kafka DLQ 패턴 |
| Adapter ~ 이력 DB | Kafka retention 기간 내 재적재 가능 |

---

---

<!-- ================================================================ -->
<!-- 아래 섹션 12~14는 초안에 포함된 발송 방식/트랜잭션ID/AS-IS/MongoDB  -->
<!-- 내용을 구체화하여 추가한 항목입니다. (2026-03-24 신규)             -->
<!-- ================================================================ -->

## 12. 발송 방식 및 트랜잭션 ID 구조

### 12.1 발송 방식 구분

AM 플랫폼은 크게 두 가지 발송 방식을 지원한다.

| 구분 | 실시간성 발송 | 배치성(예약성) 발송 |
|------|------------|-----------------|
| 정의 | 단건 요청을 즉시 처리하는 구조 | 예약 시각이 지정된 다수 건을 일괄 처리하는 구조 |
| 요청 단위 | 건별 (1건씩) | 다건 (N건 묶음) |
| 처리 경로 | HTTP/TCP → NiFi → Kafka → Flink → Adapter | EAI/Queue → Batch 프로세스 → NiFi → Kafka → Flink → Adapter |
| 발송 방법코드 | `03` (온라인 발송) | `01`, `02` (배치성) |
| 지연 허용 범위 | 500ms 이내 | 예약 시각 기준 ±60초 이내 |
| 주요 발송 시스템 | CI, 직접 API | AB캠페인, 제휴CRM, Rater, KOS-Online |

> **준실시간성 발송(발송 방법코드 `04`, `05`)** 은 실시간과 배치의 중간 형태로,  
> EAI Queue를 통해 인입되지만 건별 즉시 처리에 준하는 속도를 목표로 한다.

### 12.2 트랜잭션 ID (35자리) 구조

모든 개별 발송 건은 고유한 **35자리 트랜잭션 ID**를 보유한다.

```
┌──────────────────────────────────────────────────────────────────┐
│ 트랜잭션 ID 구조 (총 35자리)                                      │
│                                                                  │
│  [메시지ID 13자리] [발송방법코드 2자리] [일자카운트 3자리]          │
│  [발송처코드 3자리] [시퀀스 14자리]                                │
│                                                                  │
│  예시: 1234567890123 | 03 | 084 | 007 | 00000000000001           │
└──────────────────────────────────────────────────────────────────┘
```

| 구성 요소 | 자리수 | 설명 | 예시 |
|---------|------|------|------|
| 메시지 ID | 13자리 | 유니크 숫자, 발송 요청 식별자 | `1234567890123` |
| 발송 방법 코드 | 2자리 | 01~02: 배치성, 03: 온라인(실시간), 04~05: 준실시간 | `03` |
| 일자 카운트 | 3자리 | 365일 중 오늘이 몇 번째 날 (001~366) | `084` (3월 25일) |
| 발송처 코드 | 3자리 | 발송 시스템 식별 코드 (유니크 숫자) | `007` |
| 시퀀스 | 14자리 | 동일 조건 내 순번 (0 패딩) | `00000000000001` |

**발송 방법 코드 정의:**

| 코드 | 발송 방식 | 처리 경로 |
|------|---------|---------|
| `01` | 배치성 발송 (EAI Queue 인입형) | 발송시스템 → EAI → 배치 Queue → AM |
| `02` | 배치성 발송 (I/F 테이블 polling형) | 발송시스템 I/F 테이블 → Python BIF → AM |
| `03` | 온라인(실시간) 발송 | 발송시스템 → ESB/직접 API → AM |
| `04` | 준실시간 발송 (Rater) | Rater → EAI → 준실시간 Queue → AM |
| `05` | 준실시간 발송 (KOS-Online) | KOS-Online → EAI → 준실시간 Queue → AM |

**트랜잭션 ID 생성 위치 및 원칙:**
- **생성 주체:** NiFi 앞단의 **상류 발송 시스템**(CI, AB캠페인, Rater, KOS-Online 등)이 발송 요청 전문 생성 시 함께 생성하여 요청에 포함한다.
- **NiFi의 역할:** txId를 생성하지 않으며, 수신된 요청에서 txId를 추출하여 35자리 형식 검증(숫자 여부, 발송방법코드 01~05 포함 여부) 후 이력 추적을 시작한다.
- **전파 방식:** 최초 수신된 txId가 이후 전 구간(Kafka → Flink → Adapter → 결과 수신 → DB 적재)에 걸쳐 동일 ID로 전파된다.
- **유효하지 않은 txId 수신 시:** NiFi에서 즉시 거부하고 오류 로그를 기록한다. (발송 처리 불가)

### 12.3 발송 방식별 파이프라인 흐름

#### 실시간성 발송 흐름 (발송방법코드 03)

```
[발송 시스템] ── txId 생성(35자리) + 요청 전문에 포함
      ↓ HTTP/REST/TCP
   [NiFi] ── txId 형식 검증(35자리 숫자, 방법코드 01~05) + 추적 시작
      ↓
   [Kafka: topic.send.request]
      ↓
   [Flink] ── 검증 / 포맷팅 / 채널분배 / Rate Limiting
      ↓
   [Kafka: topic.send.dispatch.{channel}]
      ↓
   [Channel Adapter] ── 실발송
      ↓
   [Kafka: topic.send.result]
      ↓
   [Flink + NiFi] ── 결과 이력 저장
      ↓
   [PostgreSQL] + [MongoDB]
```

#### 배치성 발송 흐름 (발송방법코드 01, 02)

```
[발송 시스템] ── 건별 txId 생성(35자리) + 배치 요청 전문에 포함
      ↓ EAI Queue (코드01) 또는 I/F 테이블 polling (코드02)
   [배치 프로세스] ── 예약시각 도래 확인
      ↓
   [NiFi] ── 묶음 수집 → 건별 분리 → txId 형식 검증 + 추적 시작
      ↓
   [Kafka: topic.send.request] ── 다건 순서 보장
      ↓
   [Flink] ── 검증 / 포맷팅 / 채널분배 / 배치 Rate Limiting
      ↓
   [Kafka: topic.send.dispatch.{channel}]
      ↓
   [Channel Adapter] ── 실발송
      ↓
   [Kafka: topic.send.result]
      ↓
   [Flink + NiFi] ── 결과 이력 저장
      ↓
   [PostgreSQL] + [MongoDB]
```

---

## 13. AS-IS 연동 구조 상세

현행 시스템의 4가지 연동 유형을 구조적으로 정리한다.  
TO-BE 설계 시 각 경로의 병목·복잡도 개선 포인트 파악을 위해 기록한다.

### 13.1 실시간 연동 (온라인 발송)

```
발송 시스템
    ↓ (발송 전문 포함)
  [ESB]
    ↓
  [CI MSA]
    ↓ TCP
  [InfiniGW]
    ↓ TCP
  [MIMO]
    ↓ 6개 발송엔진 → 6개 발송 테이블에 분배
  [발송 모듈] ── 테이블 polling
    ↓
  [실발송 시스템] ── 실제 발송 수행
    ↓ 결과 반환
  [InfiniGW]
    ↓
  [CI MSA] ── CI 내부 이력 테이블 적재
```

**현행 한계:**
- 연동 구간 4단계(CI MSA → InfiniGW → MIMO → 발송모듈)로 지연 누적
- MIMO 내부 분배 엔진이 일체형 → 1개 엔진 장애 시 전체 발송 약 30% 저하
- 발송 결과가 CI 내부 테이블에만 적재 → 통합 이력 조회 불가

### 13.2 준실시간 연동

발송 시스템: Rater, KOS-Online 2개 시스템

```
[Rater] ─┐
           ├─ EAI → [준실시간 발송 요청 Queue]
[KOS-Online]─┘               ↓
                           [CI MSA]
                              ↓ TCP
                           [InfiniGW]
                              ↓ TCP
                           [MIMO] → 6개 엔진 → 6개 테이블 → 발송모듈
                              ↓
                           [실발송 시스템]
                              ↓ 결과
                           [InfiniGW] → [CI MSA] → CI 이력 테이블
```

**현행 한계:**
- 2개 시스템이 동일 Queue로 합산 → 피크 시 경합 발생
- Queue 인입 이후 경로는 실시간 연동과 동일하여 병목 구조 공유

### 13.3 배치성 연동 1 (EAI Queue 인입형, 코드 01)

```
발송 시스템
    ↓
  [EAI] → [배치 발송 요청 Queue]
                ↓
           [CI 배치 내부 프로세스] ── 테이블 간 이관·가공
                ↓
           [Python BIF] → MIMO I/F 테이블 이관
                ↓
           [MIMO] → 6개 엔진 → 6개 테이블 → 발송모듈
                ↓
           [실발송 시스템]
                ↓ 결과
           [MIMO I/F 테이블] ← 결과 적재
                ↑
           [Python BIF] ← 결과 수거 → CI 내부 이력 테이블
```

**현행 한계:**
- 다단계 테이블 이관(Queue → CI배치 테이블 → MIMO I/F 테이블)으로 처리 지연 발생
- Python BIF가 결과를 polling 방식으로 수거 → 결과 적재 지연

### 13.4 배치성 연동 2 (I/F 테이블 polling형, 코드 02)

```
발송 시스템 I/F 테이블
    ↑ Python BIF polling
           ↓
      [Python BIF] → CI 배치 내부 프로세스 (테이블 이관·가공)
           ↓
      [Python BIF] → MIMO I/F 테이블 이관
           ↓
      [MIMO] → 6개 엔진 → 6개 테이블 → 발송모듈
           ↓
      [실발송 시스템]
           ↓ 결과
      [MIMO I/F 테이블] ← 결과 적재
           ↑
      [Python BIF] ← 결과 수거 → CI 내부 이력 테이블
```

**현행 한계:**
- Python BIF가 발송 시스템 I/F 테이블을 직접 polling → 발송 시스템 DB에 부하
- 코드01과 동일한 다단계 이관 지연

### 13.5 AS-IS → TO-BE 전환 매핑

| AS-IS 컴포넌트 | AS-IS 역할 | TO-BE 대체 컴포넌트 | 개선 효과 |
|-------------|----------|----------------|---------|
| ESB | 프로토콜 변환·라우팅 | NiFi | GUI 기반 플로우 관리, 300+ 프로토콜 즉시 지원 |
| InfiniGW | TCP 통신 게이트웨이 | Kafka (Topic 기반) | 동기 TCP 제거, 비동기 버퍼링으로 병목 해소 |
| MIMO 6개 발송엔진 | 채널 분배·처리 | Flink (채널분배 Job) | 독립 확장 가능, 장애 격리 |
| 6개 발송 테이블 | 채널별 큐 역할 | Kafka 채널별 Topic | 영속성·파티셔닝·재처리 자동 지원 |
| Python BIF | 결과 수거·이관 | Flink 결과처리 Job | 실시간 처리, polling 방식 제거 |
| EAI Queue | 배치 인입 버퍼 | Kafka (topic.send.request) | 통합 토픽으로 단순화 |
| CI 내부 이력 테이블 | 발송 이력 저장 | PostgreSQL + MongoDB | 통합 조회 + 고객별 상세 이력 |

---

## 14. MongoDB 고객 발송 이력 적재 구조

### 14.1 적재 목적

고객 단위의 전체 발송 이력 및 수신 연락처 정보를 MongoDB에 통합 적재하여,  
PostgreSQL(트랜잭션 이력)과 역할을 분리한다.

| 저장소 | 저장 목적 | 주요 조회 패턴 |
|--------|---------|------------|
| PostgreSQL | 트랜잭션 단위 발송 이력, 집계 지표 | txId, 상태별 조회, 시계열 집계 |
| MongoDB | 고객 단위 발송 이력, 수신 연락처 정보 | 고객ID 기준 전체 발송 이력 조회 |

### 14.2 MongoDB 컬렉션 설계

### 14.2 MongoDB 컬렉션 설계

#### 설계 원칙

- **고객 단위 + 월 단위** 묶음 구조: 한 도큐먼트 = 한 고객의 한 달 치 전체 발송 이력
- 컬렉션명: `send_histories_{YYYYMM}` (월별 컬렉션 분리)
- 도큐먼트 내 `sends` 필드에 해당 월 발송 내역 전체가 Array로 포함

#### send_histories_{YYYYMM} (예: send_histories_202603)

```json
{
  "_id": "CUS0000001234_202603",
  "customerId": "CUS0000001234",
  "yearMonth": "202603",
  "contacts": {
    "phoneNumbers": [
      { "type": "MOBILE", "number": "01012345678", "primary": true },
      { "type": "HOME",   "number": "0212345678",  "primary": false }
    ],
    "emailAddresses": [
      { "type": "PERSONAL", "address": "user@example.com", "primary": true }
    ]
  },
  "sendPermissions": {
    "smsAgree":   true,
    "emailAgree": true,
    "nightBanEnd": "22:00"
  },
  "totalCount": 2,
  "sends": [
    {
      "txId": "12345678901234503084007000000000000001",
      "channel": "SMS",
      "sendMethodCode": "03",
      "sender": "15881234",
      "receiver": "01012345678",
      "messageBody": "고객님, 서비스 안내드립니다.",
      "scheduledAt": null,
      "requestedAt": "2026-03-24T09:59:00+09:00",
      "dispatchedAt": "2026-03-24T09:59:00.450+09:00",
      "deliveredAt":  "2026-03-24T09:59:01.210+09:00",
      "status": "DELIVERED",
      "resultCode": "10000",
      "retryCount": 0,
      "source": "CI",
      "meta": { "campaignId": null, "templateId": "TPL0001" }
    },
    {
      "txId": "12345678901235003084007000000000000002",
      "channel": "EMAIL",
      "sendMethodCode": "01",
      "sender": "no-reply@company.com",
      "receiver": "user@example.com",
      "messageBody": "3월 청구서 안내",
      "scheduledAt": "2026-03-25T09:00:00+09:00",
      "requestedAt": "2026-03-24T23:00:00+09:00",
      "dispatchedAt": "2026-03-25T09:00:02.110+09:00",
      "deliveredAt":  "2026-03-25T09:00:03.580+09:00",
      "status": "DELIVERED",
      "resultCode": "10000",
      "retryCount": 0,
      "source": "AB_CAMPAIGN",
      "meta": { "campaignId": "CAMP_202603_001", "templateId": "TPL0088" }
    }
  ],
  "updatedAt": "2026-03-25T09:00:04+09:00"
}
```

#### 구조 설명

| 필드 | 설명 |
|------|------|
| `_id` | `{customerId}_{YYYYMM}` — 복합 키, 조회 성능 보장 |
| `customerId` | 고객 고유 ID |
| `yearMonth` | 월 단위 파티셔닝 기준 (`YYYYMM`) |
| `contacts` | 해당 월 기준 수신 연락처 정보 (스냅샷) |
| `sendPermissions` | 해당 월 기준 발송 동의 여부 (스냅샷) |
| `totalCount` | 해당 월 누적 발송 건수 |
| `sends[]` | 해당 월 발송 이력 전체 Array |

### 14.3 MongoDB 인덱스 설계

```javascript
// _id가 이미 customerId_YYYYMM 복합키이므로, 고객+월 조회는 인덱스 불필요

// txId로 특정 발송 건 단건 조회 (sends 배열 내부 검색)
db.send_histories_202603.createIndex({ "sends.txId": 1 });

// 고객의 특정 기간 내 발송 이력 조회 (yearMonth 범위 조회)
db.send_histories_202603.createIndex({ "customerId": 1, "yearMonth": 1 });

// 상태 기준 모니터링 조회 (sends 배열 내부)
db.send_histories_202603.createIndex({ "sends.status": 1, "sends.channel": 1 });
```

> **도큐먼트 크기 제한:** MongoDB 도큐먼트 최대 크기는 16MB. 고객 1명이 한 달에 최대 약 500건 발송한다고 가정하면 도큐먼트 1건 약 100~150KB 이내로 충분히 수용 가능. 월 500건 초과가 예상되는 대량 발송 고객은 별도 처리 정책(예: 주 단위 분리) 검토 필요.

### 14.4 적재 시점 및 담당 컴포넌트

| 적재 시점 | 담당 컴포넌트 | 적재 내용 |
|---------|------------|---------|
| 발송 요청 수신 시 | Flink 요청처리 Job | txId, 요청 정보, status=`PENDING` |
| 실발송 완료 시 | Flink 결과처리 Job | dispatchedAt, status=`SENT` 업데이트 |
| 결과 수신 시 | Flink 결과처리 Job | deliveredAt, resultCode, status=`DELIVERED` or `FAILED` 업데이트 |

> **POC 범위:** PostgreSQL 중심으로 구현하고, MongoDB 적재는 Flink 결과처리 Job에서 동일 트랜잭션으로 연동한다.  
> MongoDB 컨테이너는 Day2 docker-compose.yml에 포함된다.

---

## 15. RDBMS 선택 분석 — PostgreSQL vs TiberoDB

> **배경:** 현재 CI는 TiberoDB를 사용 중이고, N-MIMO는 PostgreSQL을 사용 중이다.  
> AM은 두 시스템을 통합하는 신규 플랫폼이므로, 어느 DB를 이력 저장소로 채택할지 분석한다.

### 15.1 비교 분석

| 비교 항목 | PostgreSQL | TiberoDB |
|---------|-----------|---------|
| **라이선스** | 오픈소스 (무료) | 상용 라이선스 (비용 발생) |
| **현재 사용 시스템** | N-MIMO | CI (고객접점이력통합관리) |
| **기술 계보** | 독립 오픈소스 | Sybase ASE 계열 (SAP 계열) |
| **SQL 표준 준수** | 높음 (SQL:2016 대부분 지원) | 보통 (Transact-SQL 방언 기반) |
| **JSON / 반정형 데이터** | 네이티브 지원 (JSONB) | 미지원 또는 제한적 |
| **파티셔닝** | 네이티브 범위/해시 파티셔닝 | 지원하나 설정 복잡 |
| **인덱스 유형** | B-tree, Hash, GIN, GiST, BRIN 등 다양 | B-tree 위주 |
| **시계열 데이터 최적화** | BRIN 인덱스로 효율적 처리 | 별도 최적화 필요 |
| **확장 생태계** | 풍부 (TimescaleDB, Citus, PostGIS 등) | 제한적 |
| **오픈소스 연동** (NiFi·Kafka·Flink) | JDBC 드라이버 광범위 지원, Kafka JdbcSink Connector 공식 지원 | JDBC 드라이버 존재하나 오픈소스 Connector 공식 지원 부족 |
| **Docker/컨테이너 지원** | 공식 Docker 이미지 제공, POC 즉시 활용 가능 | 컨테이너화 비공식, POC 환경 구성 복잡 |
| **커뮤니티/문서** | 방대 | 제한적 (공식 문서 위주) |
| **운영 팀 숙련도** | N-MIMO 운영 경험 보유 | CI 운영 경험 보유 |
| **CI 연동 호환성** | 별도 연동 레이어 필요 | CI와 동일 DB → 직접 연동 가능 |

### 15.2 AM 플랫폼 관점 핵심 판단 기준

| 판단 기준 | PostgreSQL 유리 | TiberoDB 유리 |
|---------|--------------|------------|
| 오픈소스 3종(NiFi·Kafka·Flink) Connector 연동 | ✅ 공식 JDBC Sink Connector 즉시 사용 | ⚠️ 커스텀 JDBC 설정 필요 |
| POC Docker 환경 즉시 구성 | ✅ 공식 이미지 존재 | ❌ 컨테이너 환경 구성 어려움 |
| 비용 | ✅ 무료 | ❌ 상용 라이선스 |
| CI 기존 데이터 연동 | ❌ 별도 연동 필요 | ✅ 동일 DB 직접 조회 가능 |
| 대용량 시계열 이력 최적화 | ✅ BRIN + 파티셔닝 | ⚠️ 추가 최적화 필요 |
| 조직 내 이관 용이성 | ✅ 신규 시스템은 PostgreSQL로 표준화 추세 | ⚠️ 레거시 CI 의존 구조 유지 |

### 15.3 판단 결론

**AM 플랫폼 이력 저장소: PostgreSQL 채택을 권장한다.**

**근거:**

1. **오픈소스 파이프라인 연동 적합성:** NiFi, Kafka Connect(JdbcSink), Flink는 PostgreSQL JDBC 기반의 공식 Connector를 제공한다. TiberoDB는 커스텀 드라이버 설정이 필요하여 POC 및 운영 모두에서 연동 공수가 증가한다.

2. **POC 환경 즉시 구성:** PostgreSQL은 공식 Docker 이미지(`postgres:15`)를 통해 `docker compose up` 즉시 사용 가능하다. TiberoDB는 컨테이너 환경 구성 자체가 별도 작업이다.

3. **비용:** AM은 신규 플랫폼이다. 라이선스 비용이 없는 오픈소스 DB를 선택하는 것이 플랫폼 독립성 확보에 유리하다.

4. **N-MIMO 운영 경험 활용:** 현행 N-MIMO 운영팀이 PostgreSQL 운영 경험을 보유하고 있어 전환 비용이 낮다.

5. **CI 연동 방안:** CI와의 이력 연동은 AM API 레이어(History API)를 통해 추상화하여, DB를 직접 공유하지 않는 구조로 설계한다. 이는 CI의 DB 교체 가능성에도 AM이 영향을 받지 않는 구조를 만든다.

> **단, 아래 조건에 해당하면 TiberoDB 재검토 필요:**
> - CI와 AM이 동일 이력 테이블을 **실시간 직접 공유**해야 하는 기능 요건이 확정된 경우
> - 조직 내 DB 표준화 정책이 TiberoDB로 명시된 경우
> - AM의 라이선스 비용을 감수할 수 있는 예산과 근거가 확보된 경우

### 15.4 POC DB 구성

| 구성 요소 | DB | 용도 |
|---------|-----|------|
| 발송 이력 메인 테이블 (`msg_send_history`) | PostgreSQL | txId 기반 이력, 상태 추적 |
| 집계 지표 테이블 (`msg_send_metrics`) | PostgreSQL | 1분 단위 채널별 집계 |
| 고객 발송 이력 | MongoDB | 고객+월 단위 Array 이력 |
| 임시 배치 처리 테이블 | PostgreSQL | 배치 발송 예약 관리 |

---

*문서 끝 — 다음 업데이트: POC 환경 구축 완료 후 실측 수치 반영 예정*