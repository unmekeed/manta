-- Миграция 003: отчёты по матчам (Report Generator, спринт 15).
--
-- Отчёт — материализованный JSON по контрактам OpenAPI (Timeline,
-- MatchAnalysis): строится Report Generator'ом по features.calculated
-- и отдаётся шлюзом как есть (чтение — O(1), без обращений к
-- ClickHouse/ML на пути запроса). Повторная генерация (at-least-once,
-- новая версия модели) замещает отчёт — UPSERT по match_id.

BEGIN;

CREATE TABLE IF NOT EXISTS MatchReports (
    match_id        BIGINT PRIMARY KEY,
    analysis        JSONB NOT NULL,   -- схема MatchAnalysis (Гл. 7)
    timeline        JSONB NOT NULL,   -- схема Timeline (Гл. 7)
    model_version   TEXT NOT NULL DEFAULT '',
    feature_version TEXT NOT NULL DEFAULT '',
    generated_at    TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

COMMIT;
