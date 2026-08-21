#!/usr/bin/env bash
set -euo pipefail

# Идемпотентно создаёт buckets, service users и прикрепляет политики.
# mc уже находится внутри MinIO-контейнера; host-зависимость не нужна.
CONTAINER="${MINIO_CONTAINER:-manta-minio-1}"
: "${MINIO_ROOT_PASSWORD:?MINIO_ROOT_PASSWORD is required}"
: "${MANTA_S3_PASS_INGEST:?MANTA_S3_PASS_INGEST is required}"
: "${MANTA_S3_PASS_PARSER:?MANTA_S3_PASS_PARSER is required}"
: "${MANTA_S3_PASS_MODEL_READER:?MANTA_S3_PASS_MODEL_READER is required}"
: "${MANTA_S3_PASS_MODEL_WRITER:?MANTA_S3_PASS_MODEL_WRITER is required}"

mc_exec() {
    docker exec "$CONTAINER" mc "$@"
}

mc_exec alias set local http://127.0.0.1:9000 dota "$MINIO_ROOT_PASSWORD" >/dev/null
for bucket in replays matches-json models; do
    mc_exec mb --ignore-existing "local/$bucket" >/dev/null
done

create_user() {
    local user="$1" pass="$2" policy="$3"
    if ! mc_exec admin user info local "$user" >/dev/null 2>&1; then
        mc_exec admin user add local "$user" "$pass" >/dev/null
    fi
    # create перезаписывает существующую policy: новые ограничения
    # применяются повторным bootstrap без пересоздания credentials.
    mc_exec admin policy create local "$policy" "/policies/$policy.json" >/dev/null
    mc_exec admin policy attach local "$policy" --user "$user" >/dev/null
}

create_user manta_ingest "$MANTA_S3_PASS_INGEST" manta-ingest
create_user manta_parser "$MANTA_S3_PASS_PARSER" manta-parser
create_user manta_model_reader "$MANTA_S3_PASS_MODEL_READER" manta-model-reader
create_user manta_model_writer "$MANTA_S3_PASS_MODEL_WRITER" manta-model-writer

echo "MinIO service users and bucket policies are ready"
