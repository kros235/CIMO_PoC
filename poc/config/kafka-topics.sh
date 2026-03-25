#!/bin/bash
# AM Platform POC Kafka Topics Initialization Script

echo "Waiting for Kafka to be ready..."
sleep 15

# Create 12 Topics for AM Pipeline
# 1. 수집 토픽
kafka-topics --create --topic topic.send.request --bootstrap-server kafka:9092 --partitions 12 --replication-factor 1 --config retention.ms=86400000

# 2. 채널별 분배 토픽 (6)
kafka-topics --create --topic topic.send.dispatch.sms --bootstrap-server kafka:9092 --partitions 6 --replication-factor 1 --config retention.ms=21600000
kafka-topics --create --topic topic.send.dispatch.mms --bootstrap-server kafka:9092 --partitions 6 --replication-factor 1 --config retention.ms=21600000
kafka-topics --create --topic topic.send.dispatch.rcs --bootstrap-server kafka:9092 --partitions 6 --replication-factor 1 --config retention.ms=21600000
kafka-topics --create --topic topic.send.dispatch.fax --bootstrap-server kafka:9092 --partitions 3 --replication-factor 1 --config retention.ms=21600000
kafka-topics --create --topic topic.send.dispatch.email --bootstrap-server kafka:9092 --partitions 3 --replication-factor 1 --config retention.ms=21600000

# 3. 결과 수신 토픽 (1)
kafka-topics --create --topic topic.send.result --bootstrap-server kafka:9092 --partitions 12 --replication-factor 1 --config retention.ms=172800000

# 4. 재처리 대상 토픽 (1)
kafka-topics --create --topic topic.send.retry --bootstrap-server kafka:9092 --partitions 6 --replication-factor 1 --config retention.ms=259200000

# 5. DLQ 토픽 (1)
kafka-topics --create --topic topic.send.dlq --bootstrap-server kafka:9092 --partitions 3 --replication-factor 1 --config retention.ms=604800000

# 6. 메트릭 스트리밍 토픽 (1)
kafka-topics --create --topic topic.monitor.metrics --bootstrap-server kafka:9092 --partitions 3 --replication-factor 1 --config retention.ms=3600000

echo "12 Kafka topics created successfully!"
