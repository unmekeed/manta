-- 010: patch матча в витрине WP (A9 роадмапа: даунвейт старого патча).
--
-- Номер патча OpenDota (constants/patch id, например 58/59/60). После
-- баланс-патча мета меняется: матчи старого патча не выбрасываются, а
-- получают пониженный вес при обучении (PATCH_OLD_WEIGHT^возраст,
-- training/dataset.py). 0 = патч неизвестен (строки до этой миграции,
-- вес 1.0 — поведение прежнее, даунвейт включается по мере накопления
-- новых строк с патчем).

ALTER TABLE manta.MatchTimelineFeatures
    ADD COLUMN IF NOT EXISTS patch UInt16 DEFAULT 0 AFTER tier;
