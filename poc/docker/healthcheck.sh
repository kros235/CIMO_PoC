#!/bin/bash
# AM Platform POC Healthcheck Script

echo "Checking AM Platform POC Services Health..."
echo "----------------------------------------"

# 1. NiFi
http_code=$(curl --write-out "%{http_code}" ...)
if [[ "$http_code" -eq 200 ]]; then   # ← "200일 때만 성공"으로 정상화
  echo "✅ NiFi UI (Port 8080) is reachable ($status_code)"
else
  echo "❌ NiFi UI (Port 8080) is NOT reachable"
fi

# 2. Flink JobManager
status_code=$(curl --write-out %{http_code} --silent --output /dev/null http://localhost:8081)
if [[ "$status_code" -ne 0 ]]; then
  echo "✅ Flink UI (Port 8081) is reachable ($status_code)"
else
  echo "❌ Flink UI (Port 8081) is NOT reachable"
fi

# 3. Grafana
status_code=$(curl --write-out %{http_code} --silent --output /dev/null http://localhost:3000/login)
if [[ "$status_code" -ne 0 ]]; then
  echo "✅ Grafana UI (Port 3000) is reachable ($status_code)"
else
  echo "❌ Grafana UI (Port 3000) is NOT reachable"
fi

# 4. PostgreSQL
if docker exec am-postgres pg_isready -U am_user -d am_db -q 2>/dev/null; then
  echo "✅ PostgreSQL (Port 5432) is accepting connections"
else
  echo "❌ PostgreSQL (Port 5432) is NOT accepting connections"
fi

# 5. MongoDB
if docker exec am-mongodb mongosh --quiet --eval \
  "db.adminCommand('ping').ok" 2>/dev/null | grep -q "^1$"; then
  echo "✅ MongoDB (Port 27017) is accepting connections"
else
  echo "❌ MongoDB (Port 27017) is NOT accepting connections"
fi

# 6. Kafka Broker
if docker exec am-kafka kafka-broker-api-versions \
  --bootstrap-server localhost:9092 &>/dev/null; then
  echo "✅ Kafka Broker (Port 9092) is accepting connections"
else
  echo "❌ Kafka Broker (Port 9092) is NOT accepting connections"
fi

echo "----------------------------------------"
echo "Healthcheck complete!"
