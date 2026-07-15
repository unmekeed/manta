-- Миграция 002: Outbox-паттерн (Гл. 2.5 спецификации).
-- Событие записывается в одной транзакции с бизнес-сущностью; фоновый relay
-- публикует его в Kafka и помечает published_at (at-least-once, NFR-REL-02).
BEGIN;

CREATE TABLE EventOutbox (
    outbox_id     BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    topic         VARCHAR(100) NOT NULL,
    partition_key VARCHAR(100) NOT NULL,
    envelope      JSONB NOT NULL,
    created_at    TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    published_at  TIMESTAMP WITH TIME ZONE
);

CREATE INDEX idx_outbox_unpublished ON EventOutbox (outbox_id) WHERE published_at IS NULL;

-- Курсоры Data Collector: последняя обработанная позиция по каждому источнику.
CREATE TABLE CollectorCursor (
    source_name  VARCHAR(50) PRIMARY KEY,
    cursor_value VARCHAR(100) NOT NULL,
    updated_at   TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()
);

-- Дедупликация собранных матчей (NFR-REL-04).
CREATE TABLE CollectedMatches (
    match_id     BIGINT PRIMARY KEY,
    source_name  VARCHAR(50) NOT NULL,
    replay_url   TEXT NOT NULL,
    collected_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()
);

COMMIT;
