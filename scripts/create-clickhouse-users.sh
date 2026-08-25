#!/usr/bin/env bash
set -euo pipefail

# Идемпотентно создаёт функциональные роли ClickHouse для VPS. Пароли не
# попадают в SQL открытым текстом: серверу передаются только SHA-256 hashes.
CONTAINER="${CLICKHOUSE_CONTAINER:-manta-clickhouse-1}"
ADMIN_USER="${CLICKHOUSE_ADMIN_USER:-dota}"
: "${CLICKHOUSE_ADMIN_PASSWORD:?CLICKHOUSE_ADMIN_PASSWORD is required}"
: "${MANTA_CH_PASS_READER:?MANTA_CH_PASS_READER is required}"
: "${MANTA_CH_PASS_WRITER:?MANTA_CH_PASS_WRITER is required}"
: "${MANTA_CH_PASS_TRAINER:?MANTA_CH_PASS_TRAINER is required}"

sha256() {
    printf '%s' "$1" | sha256sum | cut -d' ' -f1
}

READER_HASH=$(sha256 "$MANTA_CH_PASS_READER")
WRITER_HASH=$(sha256 "$MANTA_CH_PASS_WRITER")
TRAINER_HASH=$(sha256 "$MANTA_CH_PASS_TRAINER")

docker exec -i "$CONTAINER" clickhouse-client \
    --user "$ADMIN_USER" --password "$CLICKHOUSE_ADMIN_PASSWORD" \
    --multiquery <<SQL >/dev/null
CREATE ROLE IF NOT EXISTS manta_reader;
CREATE ROLE IF NOT EXISTS manta_writer;
CREATE ROLE IF NOT EXISTS manta_trainer;

REVOKE ALL ON *.* FROM manta_reader;
REVOKE ALL ON *.* FROM manta_writer;
REVOKE ALL ON *.* FROM manta_trainer;
GRANT SELECT ON manta.* TO manta_reader;
GRANT SELECT, INSERT, ALTER UPDATE, ALTER DELETE ON manta.* TO manta_writer;
GRANT SELECT ON manta.* TO manta_trainer;

CREATE USER IF NOT EXISTS manta_ch_reader IDENTIFIED WITH sha256_hash BY '$READER_HASH';
ALTER USER manta_ch_reader IDENTIFIED WITH sha256_hash BY '$READER_HASH';
GRANT manta_reader TO manta_ch_reader;
ALTER USER manta_ch_reader DEFAULT ROLE manta_reader;

CREATE USER IF NOT EXISTS manta_ch_writer IDENTIFIED WITH sha256_hash BY '$WRITER_HASH';
ALTER USER manta_ch_writer IDENTIFIED WITH sha256_hash BY '$WRITER_HASH';
GRANT manta_writer TO manta_ch_writer;
ALTER USER manta_ch_writer DEFAULT ROLE manta_writer;

CREATE USER IF NOT EXISTS manta_ch_trainer IDENTIFIED WITH sha256_hash BY '$TRAINER_HASH';
ALTER USER manta_ch_trainer IDENTIFIED WITH sha256_hash BY '$TRAINER_HASH';
GRANT manta_trainer TO manta_ch_trainer;
ALTER USER manta_ch_trainer DEFAULT ROLE manta_trainer;
SQL

echo "ClickHouse service users and grants are ready"
