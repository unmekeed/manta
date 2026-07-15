-- Миграция 004: tier матча в витринах фич.
--
-- Обучающая выборка и эталон разделяются по уровню игры (см. Гл. 7.2):
-- 'Premium'      — высокоранговые паблики (Immortal, ~6000+ MMR) → train;
-- 'Professional' — матчи про-команд → эталонный holdout (никогда в train).
-- Значение прокидывается конвейером из Data Collector
-- (match.downloaded.payload.tier → replay.parsed → Feature Extractor).

ALTER TABLE dota_analyst.MatchTimelineFeatures
    ADD COLUMN IF NOT EXISTS tier String DEFAULT '' AFTER radiant_win;

ALTER TABLE dota_analyst.PlayerMatchFeatures
    ADD COLUMN IF NOT EXISTS tier String DEFAULT '' AFTER won;
