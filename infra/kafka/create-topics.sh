#!/usr/bin/env bash
# Создание Kafka-топиков платформы по реестру Гл. 2.3.1 спецификации.
# Для локальной разработки: replication-factor=1, партиции уменьшены
# пропорционально (прод-значения указаны в комментариях).
set -euo pipefail

BROKER="${KAFKA_BROKER:-localhost:9092}"
KAFKA_TOPICS="${KAFKA_TOPICS_CMD:-docker exec dota-ai-analyst-kafka-1 /opt/kafka/bin/kafka-topics.sh}"
KAFKA_CONFIGS="${KAFKA_CONFIGS_CMD:-docker exec dota-ai-analyst-kafka-1 /opt/kafka/bin/kafka-configs.sh}"

# topic:partitions_dev:retention_ms   (prod partitions: см. комментарий)
TOPICS=(
  "match.downloaded:6:604800000"       # prod: 24, 7 дней
  "replay.parsed:12:259200000"         # prod: 48, 3 дня
  "features.calculated:6:1209600000"   # prod: 24, 14 дней
  "prediction.completed:3:1209600000"  # prod: 12, 14 дней
  "report.generated:3:604800000"       # prod: 6,  7 дней
  "meta.updated:1:2592000000"          # prod: 3,  30 дней
  "dlq.parser:3:2592000000"            # prod: 6,  30 дней
)

for entry in "${TOPICS[@]}"; do
  IFS=':' read -r topic partitions retention <<< "$entry"
  echo ">> creating ${topic} (partitions=${partitions}, retention.ms=${retention})"
  $KAFKA_TOPICS --bootstrap-server "$BROKER" --create --if-not-exists \
    --topic "$topic" --partitions "$partitions" --replication-factor 1 \
    --config "retention.ms=${retention}"
done

echo ""
echo ">> existing topics:"
$KAFKA_TOPICS --bootstrap-server "$BROKER" --list
