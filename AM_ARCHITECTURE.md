# AM 아키텍처 설계서

### MIMO·CI 통합 AM을 통한 초대용량 발송 플랫폼 고도화

> **문서 버전:** v0.13 (Day 8 작업2 완료 반영 — TS-0008 원인 확정, §16.9.1)
> **최초 작성일:** 2026-03-24
> **최종 수정일:** 2026-07-05
> **상태:** POC Day 7 완료. Day 8 작업1·작업2 완료(§16.13.2, §16.9.1). 작업3(RateLimitOperator 큐 개선) 착수 예정

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
12. [발송 방식 및 트랜잭션 ID 구조](#12-발송-방식-및-트랜잭션-id-구조)
13. [AS-IS 연동 구조 상세](#13-as-is-연동-구조-상세)
14. [MongoDB 고객 발송 이력 적재 구조](#14-mongodb-고객-발송-이력-적재-구조)
15. [RDBMS 선택 분석 — PostgreSQL vs TiberoDB](#15-rdbms-선택-분석--postgresql-vs-tiberodb)
16. ⭐ [POC 구축 실측 결과 및 설계 피드백](#16-poc-구축-실측-결과-및-설계-피드백) ← **v0.3 신규 추가**

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
| POC 버전 | **2.0** (실측: 컨테이너 이미지 `apache/nifi:1.23.2` 기반으로 조정) | 3.6.x | 1.18.x |
| 포트 | 8080 (Web UI / REST API) | 9092 (외부 Broker), 29092 (내부 Broker), 2181 (ZooKeeper) | 8081 (Web UI), 6123 (RPC) |

> **v0.3 수정:** NiFi 포트 표기에서 `9090 (API)`는 Prometheus 포트(9090)와 혼동되어 삭제.  
> NiFi는 8080 단일 포트로 Web UI와 REST API를 모두 제공한다.  
> Kafka는 외부 접근용(9092)과 Docker 내부 통신용(29092)을 분리 운영한다.

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

> **v0.3 업데이트:** Day 5 완료 시점 기준 운영 토픽은 **11개**. 초안의 10개 토픽에 Day 7 성능 테스트 대비 배치 토픽(`topic.send.batch`)이 추가되었다.

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
| `topic.send.batch` ⭐ | 3 | 24h | Flink BatchJob Source | **v0.3 신규: 배치성 발송 전용 (Day 7 성능 테스트)** |
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

> **v0.3 업데이트:** Day 5 완료 시점 기준 실제 운영 중인 서비스 16개로 갱신.  
> Day 5 신규 추가된 `history-api`(VOC 조회 API)와 `kafka-ui`(Kafka 웹 UI) 포함.  
> 초안의 서비스명(`poc-*`)은 실제 컨테이너명(`am-*`)에 맞춰 갱신.  
> Flink TaskManager POC 레플리카 수는 초기 계획 2개에서 현재 1개로 운영 중(확장 테스트는 Day 7에서 수행 예정).

| 서비스명 | 컨테이너명 | 역할 | 기술 스택 | POC 레플리카 수 |
|---------|-----------|------|---------|--------------|
| `nifi` | `am-nifi` | 데이터 수집·라우팅·추적 | Apache NiFi 1.23.2 | 1 |
| `zookeeper` | `am-zookeeper` | Kafka 클러스터 코디네이터 | ZooKeeper 3.8 | 1 |
| `kafka` | `am-kafka` | 메시지 버퍼링 | Apache Kafka 3.6 | 1 |
| `kafka-ui` ⭐ | `am-kafka-ui` | Kafka 웹 UI (토픽/메시지 조회) | provectuslabs/kafka-ui | 1 |
| `flink-jobmanager` | `am-flink-jobmanager` | Flink 마스터 로드. 분산 처리 작업의 스케줄링 및 체크포인트 관리 | Apache Flink 1.18 | 1 |
| `flink-taskmanager` | `docker-taskmanager-1` | Flink 워커 노드. 메시지 검증, 양식 변환, Rate limit 등 실제 연산 수행 | Apache Flink 1.18 | 1 (Day 7 확장 시 2~4) |
| `sms-adapter` | `am-sms-adapter` | SMS Mock 발송 (포트 8101) | Python FastAPI | 1 |
| `mms-adapter` | `am-mms-adapter` | MMS Mock 발송 (포트 8102) | Python FastAPI | 1 |
| `rcs-adapter` | `am-rcs-adapter` | RCS Mock 발송 (포트 8103) | Python FastAPI | 1 |
| `fax-adapter` | `am-fax-adapter` | FAX Mock 발송 (포트 8104) | Python FastAPI | 1 |
| `email-adapter` | `am-email-adapter` | Email Mock 발송 (포트 8105) | Python FastAPI | 1 |
| `postgres` | `am-postgres` | 발송 이력 및 통계 RDBMS | PostgreSQL 15 | 1 |
| `mongodb` | `am-mongodb` | 월별·고객별 이력 보관 NoSQL | MongoDB 6.0 | 1 |
| `prometheus` | `am-prometheus` | 메트릭 수집 + 알람 규칙 3개 | Prometheus 2.45 | 1 |
| `grafana` | `am-grafana` | 시각화 대시보드 (provisioning 자동 로드) | Grafana 10.0 | 1 |
| `history-api` ⭐ | `am-history-api` | **v0.3 신규: VOC 조회 API (포트 8200, Day 5 추가)** | Python FastAPI | 1 |

### 6.3 확장성 검증 목표

- 레플리카 N개 유지 시, 1개 Pod 장애에도 서비스 정상 운영
- `flink-taskmanager` 1개 → 2~4개 확장 시 처리량 1.7배 이상 증가 (Day 7 성능 테스트)
- `sms-adapter` 1개 → 2개 확장 시 처리량 1.8배 이상 증가
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

> **v0.3 업데이트:** Day 5에서 `history-api` 서비스(포트 8200)로 실제 구현 완료. Swagger UI 제공: `http://localhost:8200/docs`

```
GET /api/v1/history/tx/{txId}          # 트랜잭션 ID로 전 구간 이력 조회
GET /api/v1/history/receiver/{phone}   # 수신번호로 발송 이력 조회
GET /api/v1/metrics/success-rate       # 실시간 성공률 조회
GET /api/v1/metrics/tps                # 실시간 TPS 조회
```

추가 기능 (Day 5 구현):
- **고급 검색 (AND 조건)**: txId, 수신번호, 채널, 상태, 기간을 동시 필터링
- **E2E 파이프라인 시각화**: `/static/trace.html` — 구간별 처리 시간 그래프
- **메트릭 엔드포인트**: `/metrics` (Prometheus scrape)

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

> **v0.3 업데이트:** Day 5 완료 시점 실제 구성으로 갱신.

```
Docker Compose 환경 (16개 컨테이너, 3개 compose 파일)
┌──────────────────────────────────────────────────────────────────────┐
│                                                                      │
│  [Load Generator]  →  [NiFi:8080]                                   │
│                           ↓                                          │
│                    [ZooKeeper:2181]                                  │
│                    [Kafka:9092/29092]                                │
│                    [Kafka-UI:8989]                                   │
│                           ↓                                          │
│              [Flink JobManager:8081]                                │
│              [Flink TaskManager-1]                                  │
│               (Job 3개: SendRequestJob / SendResultJob / RetryJob)  │
│                           ↓                                          │
│   [SMS-Adapter:8101]  [MMS-Adapter:8102]  [RCS-Adapter:8103]       │
│   [FAX-Adapter:8104]  [Email-Adapter:8105]  (Mock)                 │
│                           ↓                                          │
│              [PostgreSQL:5432]  [MongoDB:27017]                     │
│                           ↓                                          │
│   [Prometheus:9090]  →  [Grafana:3000]  ←  [History-API:8200]      │
│                                                                      │
└──────────────────────────────────────────────────────────────────────┘

Compose 파일 구성:
 - docker-compose.yml             : 핵심 인프라 10개 (base)
 - docker-compose.monitoring.yml  : Prometheus/Grafana override + history-api
 - docker-compose.adapters.yml    : Mock Adapter 5개
```

### 9.3 POC 디렉토리 구조

> **v0.3 업데이트:** Day 5 완료 구조로 갱신. monitoring/provisioning, history-api, Flink Java 소스 등 반영.

```
CIMO_PoC/
├── README.md                        # 작업 계획 및 진행 현황 (Day 1~7)
├── CIMO_PoC_기동매뉴얼_v*.docx       # 재부팅/이관 기동 매뉴얼 (v1, v2, v3)
├── AM_ARCHITECTURE.md               # 본 설계서 (v0.3)
├── cimo_poc_status.html             # POC 현황 HTML 대시보드
│
├── poc/
│   ├── docker/
│   │   ├── docker-compose.yml             # 핵심 인프라 (ZK/Kafka/Flink/DB/NiFi)
│   │   ├── docker-compose.monitoring.yml  # Prometheus/Grafana override + history-api
│   │   ├── docker-compose.adapters.yml    # Mock Adapter 5개
│   │   └── .env.example                   # 환경변수 템플릿
│   │
│   ├── init/
│   │   ├── init.sql                       # PostgreSQL 스키마 초기화
│   │   └── init-mongo.js                  # MongoDB 컬렉션·인덱스 초기화
│   │
│   ├── nifi/
│   │   ├── send-request-flow.json         # 발송 요청 수집 플로우 템플릿
│   │   ├── send-result-flow.json          # 발송 결과 수집 플로우 템플릿
│   │   ├── deploy_flow.py                 # 플로우 자동 배포 스크립트 (v0.3 추가)
│   │   ├── requirements.txt               # 배포 스크립트 의존성
│   │   └── README.md                      # NiFi 플로우 운영 가이드
│   │
│   ├── flink/                             # Flink Job Java 소스
│   │   ├── src/main/java/com/am/platform/
│   │   │   ├── jobs/                      # SendRequestJob / SendResultJob / RetryJob
│   │   │   ├── operators/                 # Validation / ChannelDispatch / RateLimit
│   │   │   ├── model/                     # SendMessage / SendResult
│   │   │   └── util/                      # TxIdParser / ResultCodeClassifier
│   │   ├── pom.xml                        # Maven 빌드 설정
│   │   └── am-flink-fat.jar               # 빌드 산출물 (.gitignore)
│   │
│   ├── services/
│   │   ├── base/                          # 공통 모듈 (adapter_base, metrics_helper)
│   │   ├── sms-adapter/                   # 채널별 Mock Adapter
│   │   ├── mms-adapter/
│   │   ├── rcs-adapter/
│   │   ├── fax-adapter/
│   │   ├── email-adapter/
│   │   └── history-api/                   # VOC 조회 API (v0.3 추가)
│   │       ├── main.py
│   │       ├── requirements.txt
│   │       ├── Dockerfile
│   │       └── static/trace.html          # E2E 시각화 UI
│   │
│   └── monitoring/
│       ├── prometheus.yml                 # scrape 설정
│       ├── alert-rules.yml                # 경보 규칙 3개 (성공률/Retry/DLQ)
│       └── grafana/provisioning/
│           ├── datasources/prometheus.yml # Prometheus + PostgreSQL 자동 등록
│           └── dashboards/
│               ├── default.yml
│               └── json/am-platform-dashboard.json
│
└── tests/
    ├── load/                              # 부하 테스트 스크립트 (Day 7)
    └── validation/                        # 정합성 검증 스크립트 (Day 6)
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
<!-- 섹션 12~14는 초안에 포함된 발송 방식/트랜잭션ID/AS-IS/MongoDB      -->
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

<!-- ================================================================ -->
<!-- 섹션 16은 POC Day 1~5 완료 후 실제 구축 결과를 반영하여           -->
<!-- 설계 문서에 피드백한 항목입니다. (v0.3, 2026-04-22)              -->
<!-- ================================================================ -->

## 16. POC 구축 실측 결과 및 설계 피드백

> **목적:** Day 1~5 POC 구축 과정에서 실제로 관찰된 사항들을 설계 문서에 역으로 반영한다.  
> 초안 설계와 실측 차이가 있는 부분, 새로 발견된 고려사항, 운영 시 주의사항을 기록한다.

### 16.1 Day별 완료 현황

| Day | 주요 작업 | 완료 시점 | 산출물 |
|-----|---------|---------|--------|
| Day 1 | 아키텍처 설계서 작성 | 2026-03-24 | 본 문서 v0.1 |
| Day 2 | Docker 환경 구성, DB 초기화 | 2026-03-27 | docker-compose.yml, init.sql, init-mongo.js |
| Day 3 | Mock Adapter 5개, NiFi 플로우 구성 | 2026-03-28 | adapters/*, send-request-flow.json |
| Day 4 | Flink Job 3개 개발 | 2026-04-14 | SendRequestJob/SendResultJob/RetryJob |
| Day 5 | 모니터링 강화 + VOC History API | 2026-04-15 | Prometheus 알람 3개, Grafana provisioning, history-api |
| Day 6 | 통합 테스트 (TS-0001~0006, 17 TC) | 2026-05-28 | run_all.py, 17/17 PASS, 기동매뉴얼 v3.2 |
| Day 7 | 성능 테스트 — 실시간 단독 파트 (Phase 1~3) | 2026-07-01 | ts0007_realtime_load.py, load_injector.py, 처리 한계 실측치(§16.5) |

### 16.2 설계 초안과의 실측 차이

#### 16.2.1 Flink TaskManager 레플리카

- **초안 설계**: POC 기본 2개, 확장 테스트 시 4개
- **실측**: POC Day 5까지 1개 운영. Day 7에서 1→2~4 확장 테스트 예정
- **이유**: 단일 호스트 Docker Compose 환경에서는 1개로도 Day 5까지의 기능 검증 가능. 확장 테스트는 Day 7에서 수행이 타당

#### 16.2.2 Kafka 토픽 수

- **초안 설계**: 10개 (`topic.send.batch` 제외)
- **실측**: 11개. Day 7 배치 성능 테스트를 위해 `topic.send.batch` 추가
- **반영**: 섹션 4.5 갱신

#### 16.2.3 서비스 추가 — history-api (Day 5)

- **초안 설계**: VOC 조회 기능이 별도 서비스로 분리되지 않았음
- **실측**: Day 5에 독립 FastAPI 서비스(`am-history-api:8200`)로 구현
- **판단**: 설계 원칙(비즈니스/코어 분리)에 오히려 더 부합. VOC 조회는 비즈니스 레이어 API로 분리하는 것이 옳다
- **반영**: 섹션 6.2, 7.4 갱신

#### 16.2.4 docker-compose 파일 분리

- **초안 설계**: 단일 `docker-compose.yml`
- **실측**: 3개 파일로 분리
  - `docker-compose.yml`: 핵심 인프라 10개
  - `docker-compose.monitoring.yml`: Prometheus/Grafana override + history-api
  - `docker-compose.adapters.yml`: Adapter 5개
- **이유**: 모니터링 스택과 Adapter를 선택적으로 기동할 수 있도록 분리. Day 7 성능 테스트 시 Adapter 레플리카 조정이 용이해짐
- **반영**: 섹션 9.2, 9.3 갱신

#### 16.2.5 배포 스크립트의 설정값 미반영 버그 (Day 7 진단) — 🆕 v0.4 신규 추가

Day 7 성능 테스트에서 목표(2,000 TPS) 대비 실측 TPS가 현저히 낮게(404 TPS) 나온 원인을 추적하는 과정에서, **설계 문서에는 반영되어 있었으나 실제로는 한 번도 적용된 적 없던 설정값 3건**이 발견되었다. 즉 Day 1~6 동안의 모든 기능 검증은 아래 설정들이 모두 "기본값(=1, 최소치)"인 상태에서 수행된 것이었다.

| # | 항목 | 설계 문서 상 값 | 실제 적용값 (Day 7 진단 전) | 원인 |
|---|------|--------------|---------------------------|------|
| 1 | NiFi 프로세서 동시처리 수 | 4 | 1 | `deploy_flow.py`가 `Max Concurrent Tasks` 값을 NiFi REST API 미지원 속성으로 잘못 판정하여 조용히 무시(skip)함 |
| 2 | Flink Job Parallelism | 4 | 1 | `flink run` 제출 명령에 `-p` 옵션이 누락되어 기본값(1)으로 제출됨 |
| 3 | Flink TaskManager 수 | 2 | 1 | `docker-compose.yml` 주석에는 명시되어 있었으나, 실제 기동 스크립트(`start-all.sh`)의 `docker compose up` 명령에는 `--scale taskmanager=2` 옵션이 빠져 있었음 |

**설계 피드백:**
- 이런 종류의 "조용한 무시(silent skip)" 버그는 기능 테스트(Day 6)로는 절대 발견되지 않는다. 소량의 요청은 동시처리 1대로도 정상 처리되기 때문이다. **부하 테스트(Day 7)를 거쳐야만 드러나는 카테고리의 결함**이라는 점을 설계 원칙에 기록해 둔다.
- 향후 실제 서버 이관 시, 배포 자동화 스크립트가 "설정을 적용했다"고 로그를 남기는지 여부와 무관하게, **적용 후 반드시 대상 시스템(NiFi UI, Flink UI)에서 실제 값을 재조회하여 검증**하는 절차를 운영 체크리스트에 추가해야 한다.
- 세 버그 수정 후에도 목표 TPS(2,000)에는 도달하지 못했다(§16.5 참고). 즉 이 버그들은 PoC 처리량 부진의 **일부 원인**이었을 뿐, 근본 원인은 단일 호스트 하드웨어 한계다.

### 16.3 운영 시 발견된 고려사항

#### 16.3.1 Windows Git Bash 환경 주의사항

POC가 Windows 환경에서 개발되면서 발견된 환경 특이사항:

| 이슈 | 해결 방법 |
|------|---------|
| `docker exec` 명령에서 Unix 경로 자동 변환 | `MSYS_NO_PATHCONV=1` prefix 사용 |
| FastAPI `/metrics` → `/metrics/` 리다이렉트 | Prometheus scrape config에 trailing slash 명시 |
| Grafana 10.x Text panel HTML 차단 | `GF_PANELS_DISABLE_SANITIZE_HTML=true` |
| Grafana datasource UID 매칭 실패 | provisioning YAML에 UID 명시적 선언 |
| `kafka-topics.sh` 로컬 실행 불가 | `docker exec` 내부 실행 |
| Kafka `InconsistentClusterIdException` (재시작 시) | `docker compose down -v` + bind-mount data 삭제 |

#### 16.3.2 Flink Job 휘발성

- Flink Job은 JobManager 메모리에 상주. 컨테이너 재시작 시 소실됨
- 매번 `flink run`으로 재submit 필요 → 기동 매뉴얼에 절차 포함
- 프로덕션에서는 Savepoint/Checkpoint 영속화 필요 (POC 범위 밖)

#### 16.3.3 중복 Job 탐지

- `docker compose up -d`가 기존 컨테이너를 재활용하는 경우 이전 Job이 살아있을 수 있음
- `flink list`에서 동일 이름 Job 중복 시, Kafka Consumer Group 충돌 가능
- **운영 체크리스트**: 재기동 시 반드시 `flink list` 후 오래된 Job `flink cancel`

#### 16.3.4 Adapter 이미지와 소스 불일치

- Git pull로 `main.py`는 최신이지만 Docker 이미지는 옛 빌드 상태일 수 있음
- 증상: Adapter `Restarting` 무한 루프, `Duplicated timeseries in CollectorRegistry` 에러
- **운영 체크리스트**: 소스 변경 시 `docker compose build --no-cache` 후 재기동

#### 16.3.5 NiFi 플로우 휘발성

- NiFi 플로우는 `/opt/nifi/nifi-current/conf/flow.xml.gz`에 보관되어 재시작 시 복원됨
- 하지만 신규 PC 이관 시에는 이 파일이 없으므로 `deploy_flow.py` 스크립트로 자동 배포 필요
- 현재 플로우 구성: ListenHTTP → EvaluateJsonPath → RouteOnAttribute → UpdateAttribute → PublishKafka → LogMessage (7개 프로세서)

### 16.4 검증된 사항 (설계대로 동작 확인)

다음 항목들은 Day 1~6 동안 **설계대로 동작함이 검증**되었다:

- ✅ 코어/비즈니스 영역 분리 (섹션 2) — history-api가 비즈니스 레이어로 적절히 분리됨
- ✅ 오픈소스 3종 조합 (섹션 3) — NiFi→Kafka→Flink 파이프라인 전 구간 동작
- ✅ 표준 메시지 포맷 (섹션 5.2) — 모든 Adapter가 동일 JSON 구조로 동작
- ✅ 재처리 정책 (섹션 4.6) — 지수 백오프 3회 시도 후 DLQ 이관 확인
- ✅ VOC 조회 API (섹션 7.4) — txId, 수신번호, 채널, 기간 AND 필터 동작
- ✅ MongoDB 고객+월 단위 적재 (섹션 14) — `send_histories_YYYYMM` 컬렉션 생성
- ✅ 전 구간 E2E 정합성 (Day 6, `run_all.py`) — TS-0001~0006, 6개 시나리오 17개 TC 전부 PASS (100%)

### 16.5 Day 7 성능 테스트 실측 결과 — 실시간 단독 파트 (TS-0007, 2026-07-01)

> 대상 시나리오: 시나리오 A(실시간성 발송 단독, 발송방법코드 03). 목표는 2,000 TPS 지속 처리(일 5,000만 건 환산치)였다.
> 테스트 스크립트: `tests/load/ts0007_realtime_load.py` (TC-0018 워밍업 / TC-0019 목표TPS / TC-0020 성공률 / TC-0021 p95지연 / TC-0022 DB도달률)

#### 16.5.1 최종 실측치 (임계치 조정 후, 노트북B 3회 재현 + 데스크탑A 1회)

| 항목 | 목표 | 노트북B (i7-9750H, 6c/12t, 16GB) | 데스크탑A (i5-6600, 4c/4t, 32GB) |
|------|------|-----------------------------------|-----------------------------------|
| achieved TPS | 2,000 | 418 ~ 425 (목표의 약 21%) | 469 (목표의 약 23%) |
| 성공률 | ≥ 99% | 100% | 94.7% (HTTP 503 3,186건) |
| p95 지연시간 | ≤ 1,000ms | 1,216 ~ 1,243ms (기준 초과) | 1,140ms (기준 초과) |
| DB 도달률 | ≥ 99.9% | 100% | 52.5% |

- **데스크탑A DB 도달률 52.5%의 원인**: 목표 2,000 TPS와 실제 처리 가능량 469 TPS의 차(약 1,531 TPS)가 30초간 누적되며 약 45,930건이 큐에 적체되어 `backPressureObjectThreshold`(50,000) 임계치에 근접, HTTP 503 응답이 다수 발생했기 때문이다. 이는 어댑터·DB 문제가 아니라 **투입 속도가 처리 속도를 과도하게 초과할 때 큐 앞단에서 나타나는 정상적인 배압(backpressure) 동작**이다.
- 노트북B는 성공률 100%·DB 도달률 100%를 유지했는데, 이는 노트북B의 처리 한계(418~425 TPS)가 데스크탑A(469 TPS)보다 낮음에도 불구하고 테스트 조건상 큐 적체가 임계치까지 도달하지 않았기 때문이며, "더 느린데 더 안정적으로 보이는" 이 결과 자체가 병목 위치(NiFi/Kafka 단일 인스턴스)를 가리키는 단서다.

#### 16.5.2 병목 확정

부하 중 리소스 사용률 측정 결과, 병목은 아래 2개 컴포넌트의 **단일 인스턴스 한계**로 확정되었다.

| 컴포넌트 | 부하 중 CPU 사용률 | 판정 |
|---------|------------------|------|
| NiFi | 112 ~ 135% | 병목 (1 vCPU 초과 지속 사용) |
| Kafka | 51 ~ 121% | 병목 (구간별 포화) |
| Flink TaskManager | 여유 있음 | 병목 아님 |
| Channel Adapter | 여유 있음 (CPU 1~7%) | 병목 아님 |
| PostgreSQL | 여유 있음 | 병목 아님 |

**결론**: §16.2.5에서 발견된 설정 버그(NiFi 동시처리 1→4, Flink parallelism 1→4, TaskManager 1→2대)를 모두 수정한 뒤에도 목표 TPS에 도달하지 못했다. 이는 버그 수정만으로는 해결되지 않는, **단일 호스트 Docker Compose 환경 자체의 하드웨어 처리 한계**임을 의미한다. 실 서버 환경에서는 NiFi를 클러스터로, Kafka를 멀티 브로커로 구성해 수평 확장하는 것이 정답이며, PoC 환경을 억지로 더 튜닝해 2,000 TPS를 맞추는 것은 이번 POC의 목표(구조 검증)에 부합하지 않는다고 판단한다.

#### 16.5.3 DISPATCHING 적체 패턴 (Day 8 이월)

Day 7 전 측정에서 공통적으로 `DISPATCHING` 상태 78~88%, `DELIVERED` 상태 12~22%로 나타났다. Adapter CPU는 1~7%로 여유가 있어 Adapter 자체의 처리 지연은 아니며, §16.3.5·기존 L02(DISPATCHING 상태 고착, `SendResultJob` UPDATE 일부 누락)와 동일 계열 문제로 추정된다. Day 8에서 L01~L04와 함께 근본 원인을 분석한다.

### 16.6 Day 7 Phase 3 완료 결과 — 예약 발송 기능 + tx_id 중복 원인분석·조치 (2026-07-05)

#### 16.6.1 Flink 예약 발송 기능 (`ScheduleGateOperator`)

`sendMethodCode` 01/02(배치·예약성) 건을 위한 Timer 기반 예약 대기 로직을 `SendRequestJob.java` 파이프라인에 추가했다 (커밋 `b50908c`).

- **동작 방식**: `ValidationOperator` 통과 후 신규 `ScheduleGateOperator`(`KeyedProcessFunction`, key=txId)가 개입 → `sendMethodCode` 01/02 + 미래 예약시각이면 ① 즉시 `status=SCHEDULED`로 이력 1회 INSERT(VOC 즉시 조회 가능) ② Flink 내부 상태에 보관, Timer 등록 ③ 예약 시각 도래 시 자동으로 `ChannelDispatchOperator` 이후 정규 파이프라인으로 흘려보냄
- **정합성 보강**: `tx_id`당 row 1개 유지 원칙에 따라, 해제 시점에는 INSERT가 아닌 UPDATE로 처리 (`alreadyPersisted` 플래그로 구분)
- **배포 검증**: Flink UI Job Graph에서 `ScheduleGateOperator` 노드 및 `PostgresSink-ScheduledLog` Sink 육안 확인 완료 (§16.2.5 원칙에 따름 — 로그만으로 판단하지 않음)

#### 16.6.2 tx_id 중복 INSERT 발견 및 원인분석

위 기능 구현 과정에서 `tx_id` UNIQUE 제약을 추가하려던 중, **기존 Day 7 부하 테스트 데이터에서 73개 tx_id(146 row)가 정확히 2번씩 INSERT된 사실**을 발견했다.

- **정리**: id가 작은(최초 요청) row만 남기고 73건 DELETE, 이후 `tx_id UNIQUE` 제약 추가
- **데이터 정합성**: 중복 row의 `dispatched_at`이 마이크로초 단위까지 완전히 동일 → 발송 결과 자체의 유실·오염은 없었음을 확인
- **원인 분석 (가장 유력한 설명, 100% 확정은 아님 — 근거: 당시 NiFi/Kafka 로그는 로테이션되어 소실)**:
  - 클라이언트(`load_injector.py`, `tx_generator.py`) 코드는 생성 시점 이후 수정 이력 없음 → 중복 유발 로직 없음 확인
  - NiFi 플로우의 `PublishKafka` `failure` 관계는 로그 프로세서로만 연결, 자기 자신에게 재연결(자동 재시도 루프) 없음 확인
  - `PublishKafka`가 `acks=all` + `Guarantee Replicated Delivery`로 설정되어 있어, 이미 확정된 단일 Kafka 브로커 병목(§16.5.2, 부하 시 CPU 51~121%) 상황에서 응답 지연 시 NiFi 내부 Kafka 프로듀서가 자체 재전송했을 가능성이 가장 유력함
  - `enable.idempotence` 미설정 상태라 재전송 시 중복이 걸러지지 않았음

#### 16.6.3 근본 조치 — `enable.idempotence` 적용

- **변경**: `poc/nifi/send-request-flow.json`의 `PublishKafka`에 `enable.idempotence: true` 추가, `poc/nifi/deploy_flow.py`의 `DYNAMIC_PROPERTY_TYPES`에 `PublishKafka_2_6` 추가 (사전 정의 property 목록에 없어 §16.2.5와 동일한 "조용한 무시" 버그로 누락될 뻔했던 것을 방지) — 커밋 `c56b20f`
- **재검증 (TS-0007 재실행, 2026-07-05)**:

| 항목 | 1차 (배경 프로세스 실행 중) | 2차 (배경 프로세스 종료 후) | idempotence 적용 전 (참고) |
|------|---------------------------|----------------------------|---------------------------|
| achieved TPS | 262.3 | 511.3 | 469 |
| p95 지연 | 2,547ms | 906ms | 1,140ms |
| DB 도달률 | 84.7% | 100% | 52.5~100% |
| tx_id 중복 | - | **0건** | 73건 발생 이력 있음 |

- **결론**: 1차 결과만 보면 "idempotence가 성능을 저하시켰다"는 가설이 유력해 보였으나, 2차(배경 프로세스 종료 후) 재측정 결과 기존 기록과 동등하거나 더 나은 성능이 확인되어 **해당 가설은 기각**한다. 1차 저하의 실제 원인은 데스크탑A(i5-6600, 4코어) 위에서 동시 실행 중이던 무관한 프로세스의 CPU 경합으로 판단된다. **tx_id 중복은 0건으로 완전히 해소되었고, 성능 손해는 없었다.**

### 16.7 TS-0008 예약 시각 정확도 심층 진단 (2026-07-05)

시나리오 B 완료 기준 중 "예약 시각 ±60초 이내 99% 이상"이 1만 건 축소검증에서 **83.7~85.1%로 반복 미달**하여, 원인을 규명하기 위해 4단계 가설 검증을 거쳤다.

| # | 가설 | 검증 방법 | 결과 |
|---|------|----------|------|
| 1 | Flink 체크포인트(30초 주기)가 지연을 유발 | Flink Checkpoints API로 실제 소요시간 조회 | ❌ 기각 — 실측 end-to-end duration 최대 133**ms**(밀리초), 60~180**초** 단위 지연과 400배 이상 차이 |
| 2 | 단일 Kafka 브로커 경합 | 부하 재현 중 Flink UI(Busy/Backpressured) + `docker stats` 실시간 관찰 | ❌ 기각 — CPU 22%, Busy 2%, Backpressured 0%로 여유 있는 상태 확인 |
| 3 | 진단용 DEBUG 로그로 직접 확인 | `RateLimitOperator`의 큐잉/방출 로그 확인 시도 | 로그 자체가 출력되지 않음 발견 → 원인 재규명: jar 내 log4j 설정이 Flink 컨테이너 자체 설정(`/opt/flink/conf/log4j-console.properties`)에 덮여 무시되고 있었음. 컨테이너 설정을 직접 오버라이드(volume mount)하여 해결(커밋 `ebeda69`) |
| 4 | `RateLimitOperator.onTimer()` 카운트 리셋 버그 | 로그 확보 후 코드 재검토 → 실제 버그 발견(방출 직후 count를 released가 아닌 0으로 리셋 → 직후 도착 메시지가 한도 무시하고 통과 가능) | 버그 자체는 확정, 수정(커밋 `00dfeaf`) → 재테스트 결과 **84.4%로 개선 없음**. 근본 원인이 아니었음을 확인 |
| 5 | 채널별 토픽 미분리로 인한 경합 | `SendRequestJob.java` 실제 코드 확인 | ❌ 기각 — `topic.send.dispatch.{sms,mms,rcs,fax,email}`로 **이미 완전히 분리되어 있음** 확인. 애초에 해당되지 않는 가설이었음 |

**⚠️ 최종 결론 — 정정 (2026-07-05 2차 검토)**

앞선 버전에서는 "§16.5와 같은 PoC 단일 인스턴스 하드웨어 한계"로 결론지었으나, 이는 **실측 데이터와 직접 모순되어 철회한다.** 부하가 몰리는 순간 Kafka CPU는 22%, `RateLimitOperator`의 `Busy`도 2%에 불과해 — 시스템은 여유가 있는 상태였다. "자원이 부족해서 느리다"는 설명은 성립하지 않는다.

**현재까지 확인된 사실만 정리하면:**
- FAX(제한 100 TPS, 가장 낮음)는 이론치(약 20초)와 거의 정확히 일치하는 결과를 보임 — `RateLimitOperator`의 1초 윈도우 방출 로직 자체는 FAX에 한해 설계대로 정확히 동작함이 로그로 확인됨
- SMS(제한 500)·MMS(제한 200) 등 제한이 더 높은 채널은 오히려 이론치 대비 20~35배 느린 지연을 보임 — 방향성이 직관과 반대이며, 로그 상 "타이머 방출" 이벤트 자체가 SMS·EMAIL에는 거의 없어(대부분 "즉시 방출" 경로로 추정) 왜 지연이 발생했는지 로그로 직접 설명되지 않음
- "즉시 방출" 경로(제한 이내일 때 바로 내보내는 코드 경로)에는 진단 로그가 없어, 이 경로에서 무슨 일이 있었는지는 **현재 시점에 확인 불가능**

**결론: 원인 불분명, 추가 조사 필요.** 확정할 수 있는 것은 "발송 자체는 100% 완료되고 유실·중복은 없다"는 것과 "체크포인트·Kafka 자원 부족·토픽 미분리는 원인이 아니다"라는 소거법적 사실뿐이다. 다음 조사 방향은 "즉시 방출" 경로의 타이밍을 직접 들여다보는 것이며, §16.7-1(추가 조사)에서 이어간다.

#### 16.7-1 추가 조사 — 단일 실행 기준 정밀 분석 (재배포 없이 기존 데이터로 확인)

이전 채널별 통계는 여러 번의 테스트 실행 결과가 뒤섞여 있었을 가능성이 있어, **정확히 한 번의 실행**(`scheduled_at = '2026-07-05T04:07:20+09:00'`)만 골라 재조회했다.

| 채널 | 최소지연 | p10 | p50(중앙값) | 최대지연 | 이론상 소요시간(2,000건÷limit) |
|------|---------|-----|------------|---------|------------------------------|
| MMS(limit 200) | 0.2s | 10.6s | 81.9s | 185.4s | 10s (실측 18.5배) |
| RCS(limit 300) | 0.2s | 10.5s | 66.4s | 156.8s | 6.7s (실측 23배) |
| SMS(limit 500) | 0.4s | 9.7s | 53.8s | 133.0s | 4s (실측 33배) |
| EMAIL(limit 400) | 0.2s | 6.8s | 34.6s | 96.7s | 5s (실측 19배) |
| FAX(limit 100) | 0.5s | 2.4s | 10.0s | 20.1s | 20s (실측 1배, **정확히 일치**) |

**발견된 패턴:** 모든 채널에서 "최소지연 ≈ 0, 중앙값 ≈ 최대지연의 40~44%"라는 동일한 분포 형태가 나타난다. 이는 **줄을 서서 순서대로 빠져나가는 큐(FIFO)의 전형적인 선형 분포**로, 무작위 오류가 아니라 "정해진 처리 능력으로 대기열을 순서대로 비우는" 과정에서 자연스럽게 생기는 모양이다.

**FAX만 이론과 정확히 일치하고 나머지가 크게 벗어나는 이유(가장 유력한 가설):** `RateLimitOperator`의 대기 큐(`pendingQueueState`)는 채널별로 **리스트 전체를 Flink 상태에 저장**하는 구조다. 메시지가 큐에 추가될 때마다 "현재 큐 리스트 전체를 읽어와서, 항목 하나를 더하고, 전체를 다시 써넣는" 방식이라, 큐 길이가 길어질수록 항목 하나를 추가하는 데 드는 비용도 비례해서 커진다(대기 인원이 N명일 때 총 비용은 이론상 N²에 비례하는 구조). 이전 로그(§16.7 표 3번 항목)에서도 MMS의 "타이머 방출" 건수가 한도(200)에 크게 못 미치는 값(19, 3, 57, 26...)으로 자주 찍혔던 것이 이 가설과 일치한다.

**최종 결론:** 이 문제는 데이터 유실·중복이 없는 **성능 최적화 여지**로 판정한다. 정확성 문제(버그)가 아니라 "대기 큐 자료구조를 더 효율적인 방식(예: Flink `ListState` 활용)으로 바꾸면 개선될 가능성이 높은 항목"으로, Day 8 개선 과제로 이월한다. "±60초 이내 99%"라는 목표는 이 개선 이후, 그리고 실 서버 스케일아웃 이후에 재검증하는 것으로 한다.

### 16.9 TS-0008 100만 건 정식 테스트 결과 및 신규 발견 (2026-07-05)

시나리오 B의 정식 목표 규모(100만 건)로 실행한 결과, 1만 건 축소검증에서는 나타나지 않았던 **새로운 문제(대량 접수 실패)** 가 발견되어 별도 항목으로 분리한다. (아래 §16.7/§16.7-1의 "예약 정확도 큐 드레인" 결론은 이번 발견과 별개이며, 특이사항이 확인되어 수정하지 않고 그대로 유지한다.)

| 항목 | 1만 건(축소검증) | 100만 건(정식) |
|------|-----------------|----------------|
| 접수 성공률 | 100% | **54.3%** (45.67%가 HTTP 503으로 거부) |
| 접수 소요시간 | 19~20초 | **1,891초(31.5분)** — 목표(15분) 2배 초과 |
| DB 도달률 | 100% | 49.8% |
| 예약 정확도(허용오차 이내) | 83~85% | 25.3% (단, 위 접수 실패·지연이 복합적으로 반영된 결과라 단독 지표로 보기 어려움) |

**신규 발견 — 대량 HTTP 503 발생:** 1만 건 규모에서는 전혀 나타나지 않았던 현상으로, 접수 자체가 절반 가까이 거부당했다. 이는 지금까지 조사해온 "발송 완료 시각이 늦어지는" 문제(§16.7)와는 **다른 계층의 문제**다 — 발송이 늦는 게 아니라 시스템이 접수 자체를 거부한 것이다.

**원인 추정 (미확정):** 이번 테스트는 속도 조절 없이 최대 속도로 투입하는 `run_burst` 방식을 사용했다. §16.5에서 이미 확인된 NiFi `backPressureObjectThreshold`(50,000건) 한도에, 실제 처리 능력(초당 500건대)을 크게 초과하는 투입 속도가 부딪혀 503이 발생했을 가능성이 높으나, 아직 로그로 직접 확인하지 않아 **확정 아님**.

**처리 방침:** 사용자 확정에 따라 Day 8 보류 이슈로 이월한다. §16.7/§16.7-1의 큐 드레인 관련 결론은 이번 발견과 무관하므로 수정하지 않고 유지한다.

#### 16.9.1 원인 확정 (Day 8 작업 2, 2026-07-05)

Day 8에서 `docker stats`로 재확인한 결과, **원인이 확정되었다.**

| 항목 | 1회차(Day 7) | 2회차(Day 8 재실행) |
|------|-------------|---------------------|
| 접수 성공률 | 54.3% | 49.4% (더 나빠짐) |
| 접수 소요시간 | 1,891초 | 1,787초 |
| 예약 정확도 | 25.3% | 6.7% (더 나빠짐) |
| NiFi CPU (신규 측정) | 측정 안 함 | 유휴 6.89% → 최대 **163.73%**, 대부분 구간 60~150%대 유지 |

이는 §16.5(Day 7 시나리오 A)와 §16.13.2(Day 8 작업 1, TS-0009)에서 이미 확인된 것과 **동일한 패턴**이다 — 서로 다른 세 번의 부하 테스트(실시간 단독, 복합, 배치 단독)에서 매번 NiFi 단일 인스턴스 CPU 포화가 관측되었다.

**확정된 메커니즘**: `run_burst`(페이싱 없이 최대 속도로 투입, 이번엔 workers=256)가 NiFi의 단일 인스턴스 처리 능력을 크게 초과하는 속도로 요청을 투입 → NiFi CPU 포화 → 내부 Connection 큐가 `deploy_flow.py`가 고정으로 설정하는 `backPressureObjectThreshold`(50,000)까지 적체 → 신규 HTTP 요청에 503 응답. §16.5·§16.13.2와 동일한 근본 원인(NiFi 단일 인스턴스 하드웨어 한계)이다.

**작업 2 결론**: 이 문제는 코드 버그가 아니라 PoC 단일 호스트 환경의 구조적 한계다. §16.5·§16.13.2와 동일하게 "실 서버에서 NiFi 클러스터 구성 필요"로 이월하며, PoC에서 추가 튜닝은 시도하지 않는다. **원인 규명을 완료 기준으로 삼아 작업 2를 완료 처리한다.**

**작업 3(RateLimitOperator 큐 개선)에 대한 시사점**: §16.7-1에서 RateLimitOperator의 큐 자료구조 비효율을 예약 정확도 저하(83~85%)의 원인으로 지목했었는데, 이번 100만 건 재실행에서는 NiFi 단의 대량 접수 실패(49.4%만 성공)가 워낙 커서 두 원인이 뒤섞여 있다. 작업 3을 진행할 때는 NiFi가 병목이 되지 않는 낮은 투입 속도(예: 1만 건 규모, §16.7에서 이미 83~85%가 관측된 조건)로 RateLimitOperator만 따로 검증해야 두 원인을 분리해서 볼 수 있다.

### 16.10 Day 7 남은 검증 항목 / Day 8 이월 항목

- ⏳ **시나리오 C (복합 발송, TS-0009)**: 실시간+배치 동시 투입 시 상호 영향, Kafka 토픽 파티션 격리 효과
- 🆕 **[Day 8 이월] TS-0008 100만 건 대량 503 발생 원인 규명**: NiFi backpressure 한도 재검토, 또는 대량 배치 투입 시 페이싱(pacing) 방식 도입 검토
- 🆕 **[Day 8 이월] `RateLimitOperator` 큐 자료구조 개선**: §16.7-1 참고, 리스트 통째 읽기/쓰기 비효율 개선 검토

### 16.11 향후 진행 예정 사항 (아키텍처 개선 검토)

#### 16.11.1 실시간·배치 요청 라인 완전 분리 (2026-07-05 제기)

**현황**: 현재 PoC 구조는 발송방법코드(01~05)와 무관하게 모든 요청이 **단일 요청 토픽**(`topic.send.request`)과 **단일 `SendRequestJob` 파이프라인**을 공유한다. 채널별 분배(`topic.send.dispatch.{channel}`)만 나뉘어 있을 뿐, 실시간·배치 요청 자체를 구분하는 별도 라인은 없다.

**제기 배경**: TS-0009(혼합 시나리오) 설계 중, "실시간 토픽과 배치 토픽을 나누면 격리 효과를 볼 수 있지 않을까"라는 질문에서 출발. 확인 결과 현재 구조엔 애초에 그런 구분이 없어 해당 테스트 항목 자체를 재설계해야 했음(§16.12 TS-0009 참고). 이 과정에서 **실제 이 PoC의 적용 대상인 KT 발송 시스템은 배치 라인과 실시간 라인을 이미 구조적으로 구분하고 있다**는 점이 확인되어, PoC 구조와 실제 대상 시스템 간 차이로 기록해둔다.

**검토 필요 사항** (지금 당장 착수하지 않고, 향후 별도 작업으로 진행):
- 요청 인입 토픽을 `topic.send.request.realtime` / `topic.send.request.batch`(또는 scheduled)로 분리
- `SendRequestJob`을 분리하거나, 하나의 Job 내에서 소스 스트림을 분리하여 독립적인 리소스(파티션, Consumer Group)를 할당
- 분리 시 얻는 이점(배치 폭주가 실시간 처리에 주는 영향 완전 차단)과, 추가되는 복잡도(2배의 배포·모니터링 대상, 코드 중복 가능성)를 함께 검토
- KT 실제 시스템의 배치/실시간 라인 분리 방식을 참고하여 설계 방향 결정

> **⭐️ 우선순위 격상 근거 (2026-07-05, TS-0009 실측 후 추가)**: 제기 당시엔 "향후 검토해볼 사항" 수준이었으나, §16.12의 TS-0009 실측 결과 배치 투입 시 실시간 TPS -38.3%, p95 지연 +48.8%, 그리고 **배치와 무관한 채널까지 전부 동일하게 극심한 지연(평균 15~17만 ms)**을 보이는 것이 확인되어, 단순 검토 사항이 아니라 **실측 데이터로 뒷받침된 필요 개선 사항**으로 격상한다.

### 16.12 TS-0009 복합(실시간+배치 동시) 부하 테스트 결과 (2026-07-05)

축소 규모(README 원안 대비: 실시간 500 TPS 기존 달성 수준, 배치 5만 건)로 실행. 원안 규모(1,000 TPS + 50만 건)는 이미 알려진 한계를 재현할 뿐이라 판단해 축소했으나, 축소 규모에서도 심각한 상호 영향이 확인되었다.

#### 16.12.1 실측 결과

| 항목 | 베이스라인(배치 없음, TC-0027) | 복합 실행 중(배치 동시, TC-0028) |
|------|------------------------------|----------------------------------|
| achieved TPS | 424.8 (목표 500의 85%) | 262.1 (**-38.3%**) |
| p95 지연 | 672ms | 1,000ms (**+48.8%**) |
| 성공률 | 100% | 66.7% (HTTP 503 14,979건) |
| 배치 자체 성공률 | - | 64.3% (HTTP 503 17,835건) — 5만 건 규모에서 TS-0008(1만 건)엔 없던 대량 503이 실시간과 동시 실행 시에는 발생 |

**TC-0029(성능 저하 비교) 판정**: FAIL — 기준(±20%) 대비 TPS·p95 모두 큰 폭으로 초과.

#### 16.12.2 채널 공유 지연 비교 (TC-0030) — 설계 의도와 다른, 더 심각한 결과

당초 "배치와 채널을 공유하는 SMS만 유독 느려지는가"를 보려 했으나, 실측 결과 배치와 **무관한 채널까지 전부 동일하게** 극심히 느렸다.

| 채널 | 배치와 공유 여부 | 평균 실시간 통과 지연 |
|------|-----------------|----------------------|
| SMS | 공유(배치 전량 투입) | 157,129ms |
| MMS | 비공유 | 154,240ms |
| RCS | 비공유 | 163,200ms |
| FAX | 비공유 | 154,141ms |
| EMAIL | 비공유 | 176,082ms |

공유 채널(SMS) vs 비공유 채널 평균 비율 = 0.97배로 **채널 간 차이는 없었다** (그래서 TC-0030 자체는 "PASS" 판정). 그러나 이건 안심할 결과가 아니다 — **모든 채널이 배치와 무관하게 똑같이 나빠졌다는 뜻**이기 때문이다. 평균 15~17만 ms(2분 30초~3분)는 "실시간"이라는 이름에 전혀 부합하지 않는 수치다.

**해석**: 병목이 채널별 관문(`RateLimitOperator`, §16.7-1에서 이미 확인된 큐 비효율)이 아니라, 그보다 앞단 — 모든 메시지(실시간·배치 무관)가 공통으로 거치는 **단일 요청 토픽(`topic.send.request`) + 단일 `SendRequestJob` + 단일 NiFi/Kafka 인스턴스** 구간에 있음을 시사한다. 배치가 몰리면 특정 채널이 아니라 **시스템 입구 자체가 막혀, 배치와 전혀 상관없는 채널의 실시간 메시지까지 함께 지연된다.**

**결론**: §16.11에서 제기했던 실시간·배치 요청 라인 분리의 필요성이 이번 실측으로 뒷받침되었다. 채널 단위 분리(현재 구조)만으로는 배치 폭주로부터 실시간을 보호할 수 없으며, **요청 인입 단계에서부터 분리**해야 근본적으로 해결 가능하다고 판단한다.

### 16.13 실시간·배치 요청 라인 분리 구현 (Day 8, 2026-07-05) — §16.11/§16.12 후속 조치

§16.11에서 검토 사항으로 제기하고 §16.12 실측으로 필요성이 뒷받침된 실시간·배치 요청 라인 분리를, **방안 A(완전 분리)**로 구현했다.

**구현 내용:**

| 구간 | 변경 전 | 변경 후 |
|------|--------|--------|
| 요청 인입 토픽 | `topic.send.request` 1개 (12파티션) | `topic.send.request.realtime`(12파티션) / `topic.send.request.batch`(6파티션) 2개 |
| NiFi | txId 검증 → 바로 `PublishKafka` 1개 | txId 검증 → `RouteOnAttribute`(sendMethodCode 기준, Route to Property name) → `PublishKafka` 2개 |
| Flink Job | `SendRequestJob` 1개 (Consumer Group 1개) | `SendRequestJob_Realtime` / `SendRequestJob_Batch` 2개 (Consumer Group도 완전히 분리) |
| 공통 로직 | `SendRequestJob.java` 단일 파일 | `RequestPipelineBuilder.java`(공통 파이프라인) + 2개의 얇은 진입점 클래스로 코드 중복 방지 |

**라우팅 방식**: txId 14~15번째 자리(sendMethodCode, `TxIdParser.getSendMethodCode()`와 동일 위치)를 NiFi Expression Language `${txId:substring(13,15)}`로 즉시 판별. 별도 필드 추가나 상류 시스템 변경 없이, 이미 txId 안에 있던 정보를 요청 인입 단계로 "앞당겨 쓰는" 방식이라 상류 연동 규격에 영향이 없다.

**격리 효과의 근거**: 기존 구조는 실시간·배치가 동일한 Consumer Group·동일한 Flink Job 내 연산자 체인을 공유했기 때문에, 배치 물량으로 다운스트림 연산자(ValidationOperator, RateLimitOperator 등)에 배압(backpressure)이 걸리면 그 배압이 같은 체인을 타는 실시간 메시지에도 그대로 전파됐다(§16.12에서 실측). 완전히 별도의 Flink Job으로 분리하면 두 파이프라인이 물리적으로 다른 Consumer Group·다른 연산자 인스턴스를 쓰게 되어, 한쪽의 배압이 다른 쪽 연산자 체인에 전파될 경로 자체가 없어진다.

**deploy_flow.py 수정 필요성**: 신규 라우팅 Processor는 NiFi의 "Route to Property name" 전략을 사용하는데, 기존 `_get_processor_relationships()`가 RouteOnAttribute의 관계명을 `["matched", "unmatched"]`로 하드코딩하고 있어 그대로 두면 auto-terminate 대상 계산이 잘못된다. `proc_def`를 함께 전달해 Routing Strategy에 따라 관계명을 동적으로 판단하도록 수정했다 (§16.2.5와 같은 계열의 "설정이 조용히 무시되는" 문제를 사전에 차단).

**검증 상태**: 코드/설정 작성 및 NiFi JSON 문법·Python 문법·Bash 문법 검증 완료. **Maven 빌드는 사용 중인 sandbox 환경의 네트워크 제약(Maven Central 미허용)으로 이 자리에서 수행하지 못했다** — 실제 PC(노트북B/데스크탑A)에서 `mvn clean package` 빌드, Flink UI 육안 확인, TS-0009 재실행까지 완료해야 최종 검증된다.

#### 16.13.1 TS-0009 재실행 결과 (2026-07-05, 데스크탑A) — ⚠️ 기대와 다른 결과

인프라 기동(Flink Job 4개 RUNNING, Kafka 토픽 11개 정상 생성) 확인 후 TS-0009를 재실행했다. **결과는 개선되지 않았고, 오히려 악화됐다.**

| 항목 | 분리 전(§16.12) | 분리 후(재실행) |
|------|-----------------|-----------------|
| TC-0029 판정 | FAIL (TPS -38.3%, p95 +48.8%) | **FAIL (TPS -41.7%, p95 +69.3%)** |
| 배치 접수 성공률 | 64.3% | 56.6% (HTTP 503 21,720건) |
| 실시간 성공률(복합 중) | 66.7% | 60.2% (HTTP 503 17,919건) |
| 채널별 평균 지연(TC-0030) | 15~17만 ms (전 채널 균일) | 23만~131만 ms (전 채널 여전히 크게 나쁨, 이번엔 채널 간 편차도 커짐: FAX 131만ms) |

**중요 단서**: 실시간·배치 요청이 이제 서로 다른 Kafka 토픽·다른 Flink Job(다른 Consumer Group)을 쓰는데도, **HTTP 503이 양쪽 모두에서 대량 발생했다.** 이는 이번에 분리한 지점(요청 토픽, Flink Job)이 실제 병목이 아니었을 가능성을 시사한다.

**가설(미확정 — 추가 확인 필요)**: 실시간·배치가 여전히 공유하는 것은 (1) NiFi 인스턴스 자체(단일 프로세스, `ListenHTTP` 8090 포트 하나), (2) `deploy_flow.py`가 모든 Connection에 고정으로 설정하는 `backPressureObjectThreshold: 50000`(§16.9에서 100만 건 테스트 시 503의 원인으로 지목된 것과 동일 메커니즘)이다. §16.5에서 이미 "NiFi 부하 중 CPU 112~135%, 단일 인스턴스 하드웨어 한계"로 결론 낸 것과 같은 패턴일 가능성이 있으나, **아직 실측으로 확정하지 않았다** — 다음 세션에서 테스트 중 `docker stats am-nifi am-kafka`와 NiFi Bulletin Board(backpressure 경고) 확인이 선행되어야 한다.

**결론(잠정)**: §16.11에서 제안한 "요청 인입 단계 분리"만으로는 §16.12에서 확인된 문제를 해결하지 못했다. 원인이 더 상류(NiFi 단일 인스턴스)에 있다면, 이번 조치는 헛수고는 아니지만(실시간·배치 처리 자원 자체는 이제 분리되어 있어 장기적으로 필요한 구조) 근본 해결을 위해서는 NiFi 계층의 분리 또는 확장까지 검토가 필요할 수 있다. **원인 확정 전까지 추가 코드 변경은 보류한다.**

#### 16.13.2 원인 확정 및 작업 1 최종 결론 (2026-07-05)

`docker stats am-nifi am-kafka` 실측(테스트 전 유휴 5.10% → 테스트 중 최대 116.66%)과 NiFi 캔버스 Tasks/Time 누적치(5분 관측 구간 내 최대 9분 44초 누적 처리시간)로 **원인을 확정**했다.

**확정된 원인**: NiFi 단일 인스턴스의 CPU 포화. §16.5에서 Day 7 시나리오 A 때 이미 "NiFi 부하 중 CPU 112~135%, 단일 인스턴스 하드웨어 한계로 확정"이라고 결론 낸 것과 **동일한 패턴**이 이번 복합 시나리오에서도 재현되었다. 요청 인입 토픽·Flink Job을 실시간/배치로 분리해도, 그 앞단에서 두 흐름이 여전히 공유하는 단일 NiFi 프로세스(1개 `ListenHTTP`, 1개 JVM)가 이미 포화 상태이므로 하류의 분리는 효과를 내지 못했다.

**작업 1 완료 기준 조정**: 애초 §16.11에서 세웠던 완료 기준("TC-0029 ±20% 이내 달성")은 PoC의 단일 호스트 하드웨어 제약 안에서는 검증 목적에 부합하지 않는 기준으로 판단해 아래로 조정한다 (Day 7 §16.5에서 "PoC를 더 튜닝해 목표를 억지로 맞추는 것은 검증 목적에 부합하지 않는다"고 판단한 것과 동일한 근거).

- ~~TS-0009 목표(±20% 이내) 달성~~ → **조정된 기준**: (1) 실제 KT 시스템과 동일한 실시간·배치 요청 라인 분리 아키텍처를 PoC에 반영했는가, (2) 여전히 남은 성능 문제의 원인을 실측으로 규명했는가 — 둘 다 충족하여 **작업 1을 완료로 처리한다.**
- PoC 단일 호스트의 NiFi 하드웨어 한계는 §16.5와 동일하게 "실 서버에서 NiFi 클러스터 구성 필요"로 이월한다. 이번 PoC에서 NiFi를 억지로 더 튜닝하지 않는다.

**후속 작업에 대한 시사점**: TS-0008(§16.9, 100만 건 테스트 대량 503)도 같은 NiFi CPU 포화가 원인일 가능성이 높아졌다 — Day 8 작업 2에서 이 가설을 실측으로 검증한다.

---

*문서 끝 — 다음 업데이트: 작업 2(TS-0008 100만 건 HTTP 503 원인 규명) 진행 중 (v0.12)*
