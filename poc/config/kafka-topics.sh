#!/bin/bash
# AM Platform POC Kafka Topics Initialization Script
# 실행 위치: poc/docker/ 또는 poc/config/ 어디서든 실행 가능
# 사용법: bash ../config/kafka-topics.sh

KAFKA_CONTAINER="am-kafka"

echo "Waiting for Kafka to be ready..."
sleep 10

echo "Creating Kafka topics via container: $KAFKA_CONTAINER"

# 1. 수집 토픽 — 발송 요청 최초 인입
docker exec $KAFKA_CONTAINER kafka-topics \
  --create --if-not-exists \
  --topic topic.send.request \
  --bootstrap-server localhost:9092 \
  --partitions 12 --replication-factor 1 \
  --config retention.ms=86400000
echo "  ✅ topic.send.request"

# 2. 채널별 분배 토픽 (5개)
docker exec $KAFKA_CONTAINER kafka-topics \
  --create --if-not-exists \
  --topic topic.send.dispatch.sms \
  --bootstrap-server localhost:9092 \
  --partitions 6 --replication-factor 1 \
  --config retention.ms=21600000
echo "  ✅ topic.send.dispatch.sms"

docker exec $KAFKA_CONTAINER kafka-topics \
  --create --if-not-exists \
  --topic topic.send.dispatch.mms \
  --bootstrap-server localhost:9092 \
  --partitions 6 --replication-factor 1 \
  --config retention.ms=21600000
echo "  ✅ topic.send.dispatch.mms"

docker exec $KAFKA_CONTAINER kafka-topics \
  --create --if-not-exists \
  --topic topic.send.dispatch.rcs \
  --bootstrap-server localhost:9092 \
  --partitions 6 --replication-factor 1 \
  --config retention.ms=21600000
echo "  ✅ topic.send.dispatch.rcs"

docker exec $KAFKA_CONTAINER kafka-topics \
  --create --if-not-exists \
  --topic topic.send.dispatch.fax \
  --bootstrap-server localhost:9092 \
  --partitions 3 --replication-factor 1 \
  --config retention.ms=21600000
echo "  ✅ topic.send.dispatch.fax"

docker exec $KAFKA_CONTAINER kafka-topics \
  --create --if-not-exists \
  --topic topic.send.dispatch.email \
  --bootstrap-server localhost:9092 \
  --partitions 3 --replication-factor 1 \
  --config retention.ms=21600000
echo "  ✅ topic.send.dispatch.email"

# 3. 결과 수신 토픽
docker exec $KAFKA_CONTAINER kafka-topics \
  --create --if-not-exists \
  --topic topic.send.result \
  --bootstrap-server localhost:9092 \
  --partitions 12 --replication-factor 1 \
  --config retention.ms=172800000
echo "  ✅ topic.send.result"

# 4. 재처리 대상 토픽
docker exec $KAFKA_CONTAINER kafka-topics \
  --create --if-not-exists \
  --topic topic.send.retry \
  --bootstrap-server localhost:9092 \
  --partitions 6 --replication-factor 1 \
  --config retention.ms=259200000
echo "  ✅ topic.send.retry"

# 5. DLQ 토픽
docker exec $KAFKA_CONTAINER kafka-topics \
  --create --if-not-exists \
  --topic topic.send.dlq \
  --bootstrap-server localhost:9092 \
  --partitions 3 --replication-factor 1 \
  --config retention.ms=604800000
echo "  ✅ topic.send.dlq"

# 6. 메트릭 스트리밍 토픽
docker exec $KAFKA_CONTAINER kafka-topics \
  --create --if-not-exists \
  --topic topic.monitor.metrics \
  --bootstrap-server localhost:9092 \
  --partitions 3 --replication-factor 1 \
  --config retention.ms=3600000
echo "  ✅ topic.monitor.metrics"

echo ""
echo "Created topics:"
docker exec $KAFKA_CONTAINER kafka-topics \
  --list --bootstrap-server localhost:9092 | grep "^topic\."

echo ""
echo "✅ 10 Kafka topics created successfully!"