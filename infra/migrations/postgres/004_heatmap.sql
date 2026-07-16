-- Миграция 004: тепловая карта позиций (спринт 28, Гл. 7:
-- GET /api/v1/matches/{id}/heatmap).
--
-- Heatmap материализуется Report Generator'ом вместе с отчётом (та же
-- философия, что analysis/timeline: путь чтения — один SELECT, без
-- обращений к ClickHouse). Источник — PositionSnapshots (downsampled
-- ~1 Гц, Гл. 5), агрегация в сетку 64x64 по игрокам. NULL — отчёт
-- сгенерирован до этой миграции; перегенерация заполнит.

BEGIN;

ALTER TABLE MatchReports
    ADD COLUMN IF NOT EXISTS heatmap JSONB;

COMMIT;
