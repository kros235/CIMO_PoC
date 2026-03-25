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