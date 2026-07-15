-- Миграция 006: линия игрока и исход лейнинга (спринт 20).
--
-- lane определяется по средним позициям героя на 2-8 минутах игры
-- (PositionSnapshots): проекция d = x - y разделяет линии
-- (top d << 0, mid |d| мал, bot d >> 0), см. extractor.features.
-- lane_nw_diff_at_10 — разница net worth на 10-й минуте против
-- среднего ПРЯМЫХ оппонентов по линии (пусто/нет оппонентов → 0);
-- источник честного laning_score в отчётах.

ALTER TABLE dota_analyst.PlayerMatchFeatures
    ADD COLUMN IF NOT EXISTS lane String DEFAULT '' AFTER dn_at_10;
ALTER TABLE dota_analyst.PlayerMatchFeatures
    ADD COLUMN IF NOT EXISTS lane_nw_diff_at_10 Int32 DEFAULT 0 AFTER lane;
