#!/usr/bin/env python3
# poc/nifi/deploy_flow.py
"""
NiFi 발송 요청 수집 플로우 자동 배포 스크립트

[목적]
  poc/nifi/send-request-flow.json 파일을 읽어서 NiFi 캔버스에 Processor와 Connection을
  자동 생성·설정·Start 한다. UI에서 드래그&드롭으로 수동 구성하는 것과 100% 동일한 결과.

[전체 흐름]
  1. NiFi 기동 대기 (최대 60초)
  2. 기존 플로우 전체 삭제 (멱등성 보장 - 몇 번 실행해도 결과 동일)
  3. Processor 6개 신규 생성
  4. 각 Processor properties 설정
  5. Connection 8개 연결
  6. Processor 전체 Start
  7. 배포 결과 검증

[환경변수 오버라이드]
  NIFI_BASE_URL  : NiFi UI 주소 (기본: http://localhost:8080)
  FLOW_JSON_PATH : 플로우 정의 파일 경로 (기본: 스크립트 위치 기준 send-request-flow.json)
  KAFKA_BOOTSTRAP: Kafka 브로커 주소 (기본: kafka:9092 - 컨테이너 간 통신용)

[실행]
  python poc/nifi/deploy_flow.py

[안전장치]
  - 기존 플로우 삭제 전 경고 메시지 출력 + 3초 대기 (Ctrl+C 취소 가능)
  - 각 단계마다 상세 로그 출력
  - 실패 시 명확한 에러 메시지와 복구 가이드 출력
"""

import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any

import requests

# ── 로깅 설정 ─────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("deploy_flow")

# ── 환경변수 (절대경로 금지, 모두 환경변수 기반) ─────────────
NIFI_BASE_URL   = os.getenv("NIFI_BASE_URL", "http://localhost:8080")
KAFKA_BOOTSTRAP = os.getenv("KAFKA_BOOTSTRAP", "kafka:9092")

# 스크립트 위치 기준 상대경로로 JSON 파일 탐색 (절대경로 금지)
SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_JSON_PATH = SCRIPT_DIR / "send-request-flow.json"
FLOW_JSON_PATH = Path(os.getenv("FLOW_JSON_PATH", str(DEFAULT_JSON_PATH)))

# NiFi API 엔드포인트 (버전: NiFi 1.23.2)
API_ROOT = f"{NIFI_BASE_URL}/nifi-api"
TIMEOUT  = 10  # 기본 HTTP 타임아웃 (초)


# ═══════════════════════════════════════════════════════════════
# 유틸 함수
# ═══════════════════════════════════════════════════════════════

def banner(title: str):
    """단계별 배너 출력"""
    log.info("")
    log.info("=" * 64)
    log.info(f"  {title}")
    log.info("=" * 64)


def api_get(path: str, **kwargs) -> dict:
    """GET 호출 헬퍼"""
    r = requests.get(f"{API_ROOT}{path}", timeout=TIMEOUT, **kwargs)
    r.raise_for_status()
    return r.json()


def api_post(path: str, payload: dict, **kwargs) -> dict:
    """POST 호출 헬퍼"""
    r = requests.post(f"{API_ROOT}{path}", json=payload, timeout=TIMEOUT, **kwargs)
    r.raise_for_status()
    return r.json()


def api_put(path: str, payload: dict, **kwargs) -> dict:
    """PUT 호출 헬퍼"""
    r = requests.put(f"{API_ROOT}{path}", json=payload, timeout=TIMEOUT, **kwargs)
    r.raise_for_status()
    return r.json()


def api_delete(path: str, params: dict = None) -> None:
    """DELETE 호출 헬퍼"""
    r = requests.delete(f"{API_ROOT}{path}", params=params or {}, timeout=TIMEOUT)
    if r.status_code not in (200, 404):
        r.raise_for_status()


# ═══════════════════════════════════════════════════════════════
# 1. NiFi 기동 대기
# ═══════════════════════════════════════════════════════════════

def wait_for_nifi(max_seconds: int = 60) -> None:
    """NiFi REST API가 응답할 때까지 대기"""
    banner("1. NiFi 기동 상태 확인")
    log.info(f"NiFi 주소: {NIFI_BASE_URL}")
    log.info(f"최대 대기 시간: {max_seconds}초")

    start = time.time()
    while time.time() - start < max_seconds:
        try:
            r = requests.get(f"{API_ROOT}/flow/about", timeout=3)
            if r.status_code == 200:
                version = r.json().get("about", {}).get("version", "unknown")
                log.info(f"✅ NiFi 응답 확인 (버전: {version})")
                return
        except requests.exceptions.RequestException:
            pass
        log.info(f"   대기 중... ({int(time.time() - start)}초 경과)")
        time.sleep(3)

    log.error(f"❌ NiFi가 {max_seconds}초 내에 응답하지 않음")
    log.error("   확인 사항:")
    log.error("   1) docker compose ps 로 am-nifi 컨테이너 Up 상태 확인")
    log.error("   2) docker logs am-nifi 로 에러 로그 확인")
    log.error(f"   3) 브라우저에서 {NIFI_BASE_URL}/nifi/ 접속 가능 여부 확인")
    sys.exit(1)


# ═══════════════════════════════════════════════════════════════
# 2. Root Process Group ID 조회
# ═══════════════════════════════════════════════════════════════

def get_root_pg_id() -> str:
    """루트 Process Group ID 조회 (모든 플로우가 여기 아래에 생성됨)"""
    data = api_get("/flow/process-groups/root")
    pg_id = data["processGroupFlow"]["id"]
    log.info(f"Root Process Group ID: {pg_id}")
    return pg_id


# ═══════════════════════════════════════════════════════════════
# 3. 기존 플로우 전체 삭제 (멱등성)
# ═══════════════════════════════════════════════════════════════

def clear_existing_flow(pg_id: str) -> None:
    """
    기존 Processor와 Connection을 전부 삭제.
    - 몇 번 실행해도 동일 결과 보장 (init.sql 원칙과 동일)
    - 삭제 순서: Processor Stop → Connection 삭제 → Processor 삭제
    """
    banner("2. 기존 플로우 삭제 (멱등성 보장)")

    flow = api_get(f"/flow/process-groups/{pg_id}")["processGroupFlow"]["flow"]
    processors = flow.get("processors", [])
    connections = flow.get("connections", [])

    if not processors and not connections:
        log.info("기존 플로우 없음 — 스킵")
        return

    log.info(f"기존 플로우 발견: Processor {len(processors)}개, Connection {len(connections)}개")
    log.warning("⚠️  3초 후 전체 삭제 — 취소하려면 Ctrl+C")
    time.sleep(3)

    # 1) 모든 Processor 중지 (연결된 Connection이 있으면 삭제가 막히기 때문)
    for proc in processors:
        proc_id = proc["id"]
        try:
            _stop_processor(proc_id, proc["revision"]["version"])
        except Exception as e:
            log.warning(f"   Processor {proc_id} 중지 실패 (무시): {e}")

    # 2) Connection 전부 삭제 (Processor보다 먼저)
    for conn in connections:
        conn_id = conn["id"]
        version = conn["revision"]["version"]
        try:
            api_delete(f"/connections/{conn_id}", params={"version": version})
            log.info(f"   Connection 삭제: {conn_id}")
        except Exception as e:
            log.warning(f"   Connection 삭제 실패 (무시): {e}")

    # 3) Processor 전부 삭제
    for proc in processors:
        proc_id = proc["id"]
        # 버전이 stop 후 증가했을 수 있으므로 다시 조회
        try:
            fresh = api_get(f"/processors/{proc_id}")
            version = fresh["revision"]["version"]
            api_delete(f"/processors/{proc_id}", params={"version": version})
            log.info(f"   Processor 삭제: {proc['component']['name']} ({proc_id})")
        except Exception as e:
            log.warning(f"   Processor 삭제 실패 (무시): {e}")

    log.info("✅ 기존 플로우 삭제 완료")


def _stop_processor(proc_id: str, version: int) -> None:
    """Processor를 STOPPED 상태로 전환"""
    api_put(f"/processors/{proc_id}/run-status", {
        "revision": {"version": version},
        "state":    "STOPPED",
        "disconnectedNodeAcknowledged": False,
    })


# ═══════════════════════════════════════════════════════════════
# 4. Processor 생성
# ═══════════════════════════════════════════════════════════════

# send-request-flow.json의 "type" 필드를 NiFi API가 요구하는 정식 클래스명으로 매핑
# (JSON의 type이 이미 정식 클래스명이면 그대로 사용)
PROCESSOR_TYPE_MAP = {
    "org.apache.nifi.processors.standard.ListenHTTP":        "org.apache.nifi.processors.standard.ListenHTTP",
    "org.apache.nifi.processors.standard.EvaluateJsonPath":  "org.apache.nifi.processors.standard.EvaluateJsonPath",
    "org.apache.nifi.processors.standard.RouteOnAttribute":  "org.apache.nifi.processors.standard.RouteOnAttribute",
    "org.apache.nifi.processors.standard.UpdateAttribute":   "org.apache.nifi.processors.attributes.UpdateAttribute",
    "org.apache.nifi.processors.standard.LogMessage":        "org.apache.nifi.processors.standard.LogMessage",
    "org.apache.nifi.processors.kafka.pubsub.PublishKafka_2_6": "org.apache.nifi.processors.kafka.pubsub.PublishKafka_2_6",
}

# ⭐️ 신규 추가: property 값 패치 규칙
# JSON에 있는 값 중 NiFi가 실제로 받아들이는 enum과 다른 경우 여기서 변환.
# 예) RouteOnAttribute의 Routing Strategy: 'matched' → 'match'
#
# 구조: {property_name: {json_value: nifi_value}}
# property_name과 일치하는 property만 적용됨. 다른 property는 영향 없음.
VALUE_PATCH_MAP = {
    # RouteOnAttribute: NiFi 1.23.2 Routing Strategy allowed enum
    # - "Route to Property name"
    # - "Route to 'match' if all match"    ← JSON은 'matched' 사용 중
    # - "Route to 'match' if any matches"
    "Routing Strategy": {
        "Route to 'matched' if all match":  "Route to 'match' if all match",
        "Route to 'matched' if any match":  "Route to 'match' if any matches",
    },
    # ⭐️ PublishKafka_2_6: max.request.size는 바이트 수(1048576)가 아니라 "1 MB" 형식 요구
    # Data Size 단위: B, KB, MB, GB, TB
    "max.request.size": {
        "1048576":  "1 MB",
        "2097152":  "2 MB",
        "4194304":  "4 MB",
        "10485760": "10 MB",
    },
}

# ⭐️ 신규 추가: property 이름 매핑
# JSON의 display name을 NiFi API가 받는 identifier로 변환
# 매핑 규칙: {processor_type: {json_key: nifi_key}}
# 동일 키 이름이 여러 Processor에 쓰일 수 있어서 타입별로 분리
PROPERTY_NAME_PATCH_MAP = {
    "org.apache.nifi.processors.kafka.pubsub.PublishKafka_2_6": {
        "Message Key Field":   "kafka-key",
        "Delivery Guarantee":  "acks",
        "Request Max Bytes":   "max.request.size",
        "Compression Type":    "compression.type",
    },
    "org.apache.nifi.processors.standard.LogMessage": {
        "Log Level":   "log-level",
        "Log Message": "log-message",
    },
}


# ⭐️ 신규 추가: Dynamic Properties 지원 Processor 목록
# 이 타입들은 사용자가 임의 이름의 property를 추가할 수 있음
# → descriptors 기반 필터링에서 제외하고 JSON의 모든 키를 그대로 전송
DYNAMIC_PROPERTY_TYPES = {
    "org.apache.nifi.processors.standard.EvaluateJsonPath",   # txId, channel 등 JSON path 추출
    "org.apache.nifi.processors.attributes.UpdateAttribute",  # am.received.at 등 attribute 추가
    "org.apache.nifi.processors.standard.RouteOnAttribute",   # txid_valid 등 routing 조건
    "org.apache.nifi.processors.kafka.pubsub.PublishKafka_2_6",   # ⭐️ 신규
}

# ⭐️ 신규 추가: NiFi 지원 Processor 타입 및 번들 정보 조회
def load_processor_types() -> dict:
    """
    NiFi 인스턴스가 지원하는 모든 Processor 타입과 번들 정보 조회.

    왜 필요한가:
      - Processor 생성 시 일부 타입(UpdateAttribute 등)은 bundle(group/artifact/version)을
        명시하지 않으면 HTTP 409 Conflict 에러 발생
      - 동일 클래스명이 여러 NAR에 있을 수 있어서 NiFi가 모호함을 거부함
      - NiFi 버전마다 번들 버전이 달라지므로 하드코딩 대신 동적 조회 필요

    반환 형태:
      {
        "org.apache.nifi.processors.standard.UpdateAttribute": {
          "group":    "org.apache.nifi",
          "artifact": "nifi-update-attribute-nar",
          "version":  "1.23.2"
        },
        ...
      }
    """
    data = api_get("/flow/processor-types")
    type_map: dict = {}
    for proc_type in data.get("processorTypes", []):
        type_name = proc_type["type"]
        bundle    = proc_type.get("bundle", {})
        type_map[type_name] = {
            "group":    bundle.get("group"),
            "artifact": bundle.get("artifact"),
            "version":  bundle.get("version"),
        }
    log.info(f"   NiFi 지원 Processor 타입 로드: {len(type_map)}개")
    return type_map


def create_processors(pg_id: str, flow_def: dict) -> dict:
    """
    Processor 생성 (JSON 정의 개수만큼).
    반환: {logical_id: nifi_uuid} 매핑 (Connection 생성 시 사용)
    """
    banner(f"3. Processor 생성 ({len(flow_def['processors'])}개)")
    id_map: dict[str, str] = {}

    # ⭐️ 신규: NiFi 지원 타입 및 번들 정보 조회 (한 번만)
    nifi_type_map = load_processor_types()

    for p in flow_def["processors"]:
        logical_id = p["id"]
        proc_type  = PROCESSOR_TYPE_MAP.get(p["type"], p["type"])
        position   = p.get("position", {"x": 0, "y": 0})
        name       = p["name"]

        # ⭐️ 신규: 해당 타입의 번들 정보 확인
        if proc_type not in nifi_type_map:
            log.error(f"❌ NiFi가 지원하지 않는 Processor 타입: {proc_type}")
            log.error(f"   사용 가능한 유사 타입:")
            for t in nifi_type_map:
                if proc_type.split(".")[-1] in t:
                    log.error(f"     - {t}")
            raise ValueError(f"Processor 타입 미지원: {proc_type}")

        bundle_info = nifi_type_map[proc_type]

        payload = {
            "revision": {"version": 0, "clientId": "deploy-flow-script"},
            "component": {
                "name":     name,
                "type":     proc_type,
                "position": position,
                # ⭐️ 신규: bundle 명시 — 409 Conflict 에러 방지
                "bundle": {
                    "group":    bundle_info["group"],
                    "artifact": bundle_info["artifact"],
                    "version":  bundle_info["version"],
                },
            },
        }

        result = api_post(f"/process-groups/{pg_id}/processors", payload)
        nifi_uuid = result["id"]
        id_map[logical_id] = nifi_uuid

        log.info(f"   생성: {name}")
        log.info(f"      logical_id={logical_id}  →  nifi_uuid={nifi_uuid}")
        log.info(f"      bundle={bundle_info['artifact']}:{bundle_info['version']}")

        # properties / schedulingPeriod / autoTerminate 설정
        _configure_processor(nifi_uuid, p)

    log.info(f"✅ Processor {len(flow_def['processors'])}개 생성 완료")
    return id_map


# ⭐️ 신규 추가: Processor 기본 지원 property 목록 조회
def get_processor_default_config(proc_uuid: str) -> dict:
    """
    생성된 Processor의 기본 config(지원 property descriptor 포함) 조회.

    왜 필요한가:
      - 각 Processor 타입마다 지원하는 property 이름이 다름
      - JSON에 "Delivery Guarantee" 같은 display name을 넣었지만
        NiFi API는 실제 property key(예: "acks")를 요구하는 경우가 있음
      - Processor 생성 후 /processors/{id} 응답에 descriptors 포함됨
      - 이걸로 "이 타입이 지원하는 실제 property 키 목록"을 확보해서
        JSON의 값 중 지원 안 되는 건 스킵

    반환:
      set of supported property keys
    """
    detail = api_get(f"/processors/{proc_uuid}")
    descriptors = detail["component"]["config"].get("descriptors", {})
    return set(descriptors.keys())

def _configure_processor(nifi_uuid: str, proc_def: dict) -> None:
    """
    생성된 Processor의 properties 및 스케줄링 설정.
    - JSON의 properties → NiFi component.config.properties
    - scheduling: TIMER_DRIVEN + 0 sec (즉시 실행)
    - 지원하지 않는 property는 자동 스킵 (Processor 타입별 descriptor 기준)
    - 값이 NiFi enum과 다른 경우 VALUE_PATCH_MAP으로 자동 변환
    - ⭐️ property 이름이 NiFi identifier와 다른 경우 PROPERTY_NAME_PATCH_MAP으로 변환
    - ⭐️ Dynamic Property 지원 Processor는 descriptors 필터링 제외
    """
    # 현재 버전 조회 (properties 업데이트 시 필요)
    fresh = api_get(f"/processors/{nifi_uuid}")
    version = fresh["revision"]["version"]

    proc_type = proc_def["type"]
    nifi_type = PROCESSOR_TYPE_MAP.get(proc_type, proc_type)

    # 이 Processor가 지원하는 property 키 목록 확인
    supported_keys = get_processor_default_config(nifi_uuid)

    # 디버그: 어떤 property가 지원되는지 로그로 확인 (NiFi property 이름 매핑 파악용)
    if os.getenv("DEBUG_PROCESSOR_PROPS") == "1":
        log.info(f"      [DEBUG] 지원 property 키: {sorted(supported_keys)}")

    # ⭐️ 신규: Dynamic Properties 지원 여부 확인
    is_dynamic = nifi_type in DYNAMIC_PROPERTY_TYPES

    # ⭐️ 신규: property 이름 매핑 테이블 (PublishKafka의 display name → identifier)
    name_patch = PROPERTY_NAME_PATCH_MAP.get(nifi_type, {})

    # JSON에 정의된 properties 가져오기
    raw_props = dict(proc_def.get("properties", {}))

    # ⭐️ 신규 추가 (Day 7 Phase 2.5 병목 진단 결과 반영):
    # "Max Concurrent Tasks" 는 NiFi 의 실제 Processor property 가 아니라
    # 스케줄링 설정(REST API 필드명: concurrentlySchedulableTaskCount) 이다.
    # raw_props 에 그대로 두면 아래 supported_keys 필터링에서
    # "지원하지 않는 property" 로 판정되어 skipped 처리되며 조용히 버려진다.
    #
    # 실제 영향: 이 버그 때문에 send-request-flow.json 에 적어둔
    # "Max Concurrent Tasks": "10" (ListenHTTP) 값이 지금까지 NiFi 에
    # 한 번도 반영되지 않았고, 모든 프로세서가 NiFi 진짜 기본값인
    # 동시처리 1 로 동작 중이었다 (Day 7 TS-0007 503 에러 / DISPATCHING
    # 적체의 1차 원인). 여기서 미리 꺼내 따로 보관하고 raw_props 에서는
    # 제거하여, 아래 PUT payload 의 config.concurrentlySchedulableTaskCount
    # 로 직접 전달한다.
    concurrent_tasks_raw = raw_props.pop("Max Concurrent Tasks", None)

    # property 필터링 + 이름 매핑 + 값 패치
    props: dict = {}
    skipped: list = []
    renamed: list = []
    for key, value in raw_props.items():
        # ⭐️ 신규: 이름 매핑 먼저 적용 (display name → identifier)
        if key in name_patch:
            new_key = name_patch[key]
            renamed.append(f"{key} → {new_key}")
            key = new_key

        # 값 패치 (VALUE_PATCH_MAP)
        if key in VALUE_PATCH_MAP and value in VALUE_PATCH_MAP[key]:
            patched = VALUE_PATCH_MAP[key][value]
            log.info(f"      값 패치: {key}: '{value}' → '{patched}'")
            value = patched

        # ⭐️ 신규: Dynamic Property 지원 Processor는 descriptors 체크 스킵
        if is_dynamic:
            props[key] = value
            continue

        # 지원하지 않는 property 스킵
        if key not in supported_keys:
            skipped.append(key)
            continue

        props[key] = value

    if renamed:
        log.info(f"      이름 매핑: {renamed}")
    if skipped:
        log.info(f"      지원하지 않는 property 스킵: {skipped}")
    if is_dynamic:
        log.info(f"      Dynamic Property 모드 — 전체 키 허용 ({len(props)}개)")

    # PublishKafka의 경우 bootstrap.servers를 환경변수 기반으로 오버라이드
    # (docker 내부에서는 'kafka:9092', 외부에서 테스트 시는 환경변수로 변경 가능)
    if "bootstrap.servers" in props:
        props["bootstrap.servers"] = KAFKA_BOOTSTRAP
        log.info(f"      bootstrap.servers 오버라이드: {KAFKA_BOOTSTRAP}")

    # 자동 종료할 relationship 결정
    # - relationships에 없는 기본 relationship은 auto-terminate 필요
    # - proc_def["relationships"]에 명시된 것은 Connection으로 연결될 예정이므로 제외
    all_rels = _get_processor_relationships(proc_def["type"])
    defined_rels = set(proc_def.get("relationships", {}).keys())
    auto_terminate = [r for r in all_rels if r not in defined_rels]

    # ⭐️ 신규 추가: concurrentlySchedulableTaskCount
    # JSON 에 "Max Concurrent Tasks" 가 명시되어 있으면 그 값을, 없으면
    # NiFi 기본값인 1 을 그대로 사용한다 (기존 동작과 동일하게 보존).
    concurrent_tasks = int(concurrent_tasks_raw) if concurrent_tasks_raw else 1
    if concurrent_tasks_raw:
        log.info(f"      동시처리 수(concurrentlySchedulableTaskCount): {concurrent_tasks}")

    payload = {
        "revision": {"version": version, "clientId": "deploy-flow-script"},
        "component": {
            "id": nifi_uuid,
            "config": {
                "properties":          props,
                "schedulingStrategy":  proc_def.get("schedulingStrategy", "TIMER_DRIVEN"),
                "schedulingPeriod":    proc_def.get("schedulingPeriod", "0 sec"),
                "concurrentlySchedulableTaskCount": concurrent_tasks,
                "autoTerminatedRelationships": auto_terminate,
            },
        },
    }
    api_put(f"/processors/{nifi_uuid}", payload)


def _get_processor_relationships(proc_type: str) -> list[str]:
    """
    Processor 타입별 기본 relationship 목록.
    (실제 NiFi에서 Processor 생성 후 조회해도 되지만, 속도를 위해 하드코딩)
    """
    REL_MAP = {
        "org.apache.nifi.processors.standard.ListenHTTP":        ["success"],
        "org.apache.nifi.processors.standard.EvaluateJsonPath":  ["matched", "unmatched", "failure"],
        "org.apache.nifi.processors.standard.RouteOnAttribute":  ["matched", "unmatched"],
        "org.apache.nifi.processors.standard.UpdateAttribute":   ["success"],
        "org.apache.nifi.processors.standard.LogMessage":        ["success"],
        "org.apache.nifi.processors.kafka.pubsub.PublishKafka_2_6": ["success", "failure"],
    }
    return REL_MAP.get(proc_type, [])


# ═══════════════════════════════════════════════════════════════
# 5. Connection 생성
# ═══════════════════════════════════════════════════════════════

def create_connections(pg_id: str, flow_def: dict, id_map: dict) -> None:
    """Connection 생성 (relationships 기준)"""
    banner("4. Connection 생성")

    # 각 Processor의 relationships 정의를 기반으로 Connection 생성
    # send-request-flow.json 형식:
    #   proc["relationships"] = {"matched": "proc-validate-txid", ...}
    # → source=현재_proc, destination=proc-validate-txid, selected_relationships=[matched]
    #
    # ⭐️ JSON 값 파싱 관대화:
    # send-request-flow.json 에 "success": "EvaluateJsonPath proc-eval-json" 처럼
    # "타입이름 logical_id" 형식으로 기재된 경우가 있음. 공백 기준 마지막 토큰이 실제
    # logical_id. 가독성 목적의 타입 prefix는 파싱 시 제거해서 JSON 원본을 수정하지 않음.

    conn_count = 0
    for p in flow_def["processors"]:
        source_logical = p["id"]
        if source_logical not in id_map:
            continue

        source_uuid = id_map[source_logical]
        rels = p.get("relationships", {})

        for rel_name, dest_raw in rels.items():
            # ⭐️ 공백 분리 — 마지막 토큰이 실제 logical_id
            # 예) "EvaluateJsonPath proc-eval-json" → "proc-eval-json"
            # 예) "proc-validate-txid"              → "proc-validate-txid" (변경 없음)
            dest_logical = dest_raw.strip().split()[-1]

            # 자기 자신을 가리키는 LogMessage success → skip (auto-terminate로 처리)
            if dest_logical == source_logical:
                log.info(f"   자기참조 스킵: {p['name']} → {rel_name}")
                continue

            if dest_logical not in id_map:
                log.warning(f"   대상 Processor 없음: {dest_logical} (원본: '{dest_raw}') — 스킵")
                continue

            dest_uuid = id_map[dest_logical]
            _create_connection(pg_id, source_uuid, dest_uuid, rel_name)
            conn_count += 1
            log.info(f"   연결: {p['name']} --[{rel_name}]--> {dest_logical}")

    # LogMessage의 self-loop는 auto-terminate로 처리해야 함
    # → _configure_processor에서 처리 완료 (relationships에 정의된 것 제외하면
    #    LogMessage의 success가 자기참조이므로 defined_rels에 포함 → auto-terminate 안됨)
    # → 추가로 LogMessage 2개에 대해 success를 auto-terminate 처리
    _fix_logmessage_auto_terminate(flow_def, id_map)

    log.info(f"✅ Connection {conn_count}개 생성 완료")


def _create_connection(pg_id: str, source_uuid: str, dest_uuid: str, rel: str) -> None:
    """단일 Connection 생성"""
    payload = {
        "revision": {"version": 0, "clientId": "deploy-flow-script"},
        "component": {
            "source": {
                "id":      source_uuid,
                "groupId": pg_id,
                "type":    "PROCESSOR",
            },
            "destination": {
                "id":      dest_uuid,
                "groupId": pg_id,
                "type":    "PROCESSOR",
            },
            "selectedRelationships": [rel],
            "flowFileExpiration":     "0 sec",
            "backPressureDataSizeThreshold": "1 GB",
            "backPressureObjectThreshold":    50000,
        },
    }
    api_post(f"/process-groups/{pg_id}/connections", payload)


def _fix_logmessage_auto_terminate(flow_def: dict, id_map: dict) -> None:
    """
    LogMessage Processor는 success relationship이 자기자신을 가리키는 구조(JSON 정의)이지만
    실제로는 연결선을 그리지 않고 auto-terminate로 처리해야 NiFi가 Start 가능.
    """
    for p in flow_def["processors"]:
        if "LogMessage" not in p["type"]:
            continue
        nifi_uuid = id_map[p["id"]]

        fresh = api_get(f"/processors/{nifi_uuid}")
        version = fresh["revision"]["version"]
        current_config = fresh["component"]["config"]

        # auto-terminate에 success 추가
        auto_term = set(current_config.get("autoTerminatedRelationships", []))
        auto_term.add("success")

        payload = {
            "revision": {"version": version, "clientId": "deploy-flow-script"},
            "component": {
                "id": nifi_uuid,
                "config": {
                    **current_config,
                    "autoTerminatedRelationships": list(auto_term),
                },
            },
        }
        api_put(f"/processors/{nifi_uuid}", payload)
        log.info(f"   LogMessage auto-terminate 설정: {p['name']}")


# ═══════════════════════════════════════════════════════════════
# 6. Processor 일괄 Start
# ═══════════════════════════════════════════════════════════════

# ⭐️ 신규 함수 추가
def diagnose_validation_errors(pg_id: str) -> None:
    """
    Processor validation 실패 원인 진단.
    Start 후 STOPPED 상태인 Processor들의 validationErrors를 NiFi에서 직접 조회해서 출력.

    왜 필요한가:
      - Processor가 INVALID 상태이면 NiFi가 Start 거부
      - INVALID 원인은 property 미설정, 잘못된 값, 필수 relationship 미연결 등
      - 각 Processor의 /processors/{id} API 응답에 validationErrors 필드가 있음
      - 이걸 보면 "어떤 property가 뭐가 문제인지" 정확히 알 수 있음
    """
    banner("🔬 Validation 오류 진단")

    flow = api_get(f"/flow/process-groups/{pg_id}")["processGroupFlow"]["flow"]
    total_errors = 0

    for proc in flow.get("processors", []):
        proc_id = proc["id"]
        name    = proc["component"]["name"]
        state   = proc["component"]["state"]

        # RUNNING 상태는 건너뛰기
        if state == "RUNNING":
            continue

        # 개별 Processor 상세 조회
        detail = api_get(f"/processors/{proc_id}")
        validation_errors = detail["component"].get("validationErrors", [])
        validation_status = detail["component"].get("validationStatus", "UNKNOWN")

        log.error(f"")
        log.error(f"  ❌ {name}")
        log.error(f"     상태: {state} / ValidationStatus: {validation_status}")

        if validation_errors:
            for err in validation_errors:
                log.error(f"     - {err}")
                total_errors += 1
        else:
            log.error(f"     (validationErrors 없음 — VALIDATING 중이거나 다른 원인)")

    log.error(f"")
    log.error(f"총 validation 오류: {total_errors}건")
    log.error(f"위 오류들을 send-request-flow.json 의 properties 수정 또는")
    log.error(f"deploy_flow.py 의 _configure_processor 함수 로직 수정으로 해결 필요")


def start_all_processors(pg_id: str) -> None:
    """Process Group 전체를 RUNNING 상태로"""
    banner("5. Processor 일괄 Start")

    payload = {
        "id":    pg_id,
        "state": "RUNNING",
        "disconnectedNodeAcknowledged": False,
    }
    api_put(f"/flow/process-groups/{pg_id}", payload)

    # 검증 — 모든 Processor가 RUNNING 상태인지 확인
    time.sleep(3)  # NiFi가 상태 반영하는 시간
    flow = api_get(f"/flow/process-groups/{pg_id}")["processGroupFlow"]["flow"]
    running_count = 0
    total = len(flow.get("processors", []))
    for proc in flow.get("processors", []):
        state = proc["component"]["state"]
        name = proc["component"]["name"]
        if state == "RUNNING":
            running_count += 1
            log.info(f"   ✅ {name}: RUNNING")
        else:
            log.warning(f"   ⚠️  {name}: {state}")

    log.info(f"✅ Processor {running_count}/{total}개 RUNNING 상태")

    # ⭐️ 신규: 일부가 STOPPED면 validation 오류 자동 진단
    if running_count < total:
        diagnose_validation_errors(pg_id)


# ═══════════════════════════════════════════════════════════════
# 7. 배포 결과 검증
# ═══════════════════════════════════════════════════════════════

def verify_deployment() -> None:
    """실제로 8090 포트가 열려있고 요청을 수신하는지 테스트"""
    banner("6. 배포 결과 검증 — 8090 포트 테스트 요청")

    # 참고: 이 테스트 요청은 txId가 유효하지 않으므로(35자리가 아님)
    # NiFi 내부에서는 RouteOnAttribute가 unmatched 분기를 타서 LogMessage로 폐기됨.
    # 200 또는 204 응답이 나오면 ListenHTTP가 정상 동작하는 것.
    test_url = "http://localhost:8090/am/send"
    log.info(f"테스트 요청: POST {test_url}")

    try:
        r = requests.post(
            test_url,
            json={"test": "deployment-check"},
            timeout=5,
        )
        if r.status_code in (200, 204):
            log.info(f"✅ ListenHTTP 정상 응답 (HTTP {r.status_code})")
            log.info("   → NiFi 플로우 배포 성공, 발송 요청 수신 준비 완료")
        else:
            log.warning(f"⚠️  예상치 못한 응답 코드: HTTP {r.status_code}")
    except requests.exceptions.ConnectionError:
        log.error("❌ 8090 포트 연결 실패")
        log.error("   확인 사항:")
        log.error("   1) docker-compose.yml 에서 am-nifi 컨테이너 포트 매핑 확인 (8090:8090)")
        log.error("   2) ListenHTTP Processor가 RUNNING 상태인지 NiFi UI에서 확인")
        sys.exit(1)
    except Exception as e:
        log.warning(f"⚠️  검증 중 예외 발생 (플로우는 정상일 수 있음): {e}")


# ═══════════════════════════════════════════════════════════════
# 메인
# ═══════════════════════════════════════════════════════════════

def main():
    banner("NiFi 발송 요청 수집 플로우 자동 배포")
    log.info(f"플로우 정의 파일: {FLOW_JSON_PATH}")
    log.info(f"NiFi API: {API_ROOT}")

    if not FLOW_JSON_PATH.exists():
        log.error(f"❌ 플로우 정의 파일 없음: {FLOW_JSON_PATH}")
        sys.exit(1)

    with open(FLOW_JSON_PATH, "r", encoding="utf-8") as f:
        flow_def = json.load(f)

    log.info(f"정의된 Processor: {len(flow_def['processors'])}개")

    try:
        wait_for_nifi()
        pg_id = get_root_pg_id()
        clear_existing_flow(pg_id)
        id_map = create_processors(pg_id, flow_def)
        create_connections(pg_id, flow_def, id_map)
        start_all_processors(pg_id)
        verify_deployment()

        banner("🎉 배포 완료")
        log.info(f"NiFi UI 확인: {NIFI_BASE_URL}/nifi/")
        log.info(f"발송 요청 엔드포인트: http://localhost:8090/am/send (POST)")
        log.info("")
        log.info("다음 단계: Day 6 Phase 1 — 테스트 공통 인프라 구축")

    except requests.exceptions.HTTPError as e:
        log.error(f"❌ NiFi API 호출 실패: {e}")
        if e.response is not None:
            log.error(f"   응답 본문: {e.response.text[:500]}")
        sys.exit(1)
    except Exception as e:
        log.error(f"❌ 배포 실패: {type(e).__name__}: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()