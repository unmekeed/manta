-- 012: поминутные фичи витрины из трека F (объективы, предметы, вижн,
-- нейтралки, драфт-прайор). Все — DEFAULT nan: строки, собранные до
-- появления фичи, честно «не знают» значения, а LightGBM обрабатывает
-- пропуск нативно (тот же приём, что в миграциях 005/008/009).
--
-- Знак всех *_diff согласован с networth_diff: положительное = в пользу
-- Radiant.

ALTER TABLE manta.MatchTimelineFeatures
    -- F2, объективы
    ADD COLUMN IF NOT EXISTS roshan_diff Float32 DEFAULT nan,
    ADD COLUMN IF NOT EXISTS aegis_alive Float32 DEFAULT nan,
    ADD COLUMN IF NOT EXISTS buybacks_diff Float32 DEFAULT nan,
    ADD COLUMN IF NOT EXISTS first_blood Float32 DEFAULT nan,
    -- F4, предметы
    ADD COLUMN IF NOT EXISTS item_value_diff Float32 DEFAULT nan,
    ADD COLUMN IF NOT EXISTS key_items_diff Float32 DEFAULT nan,
    -- F5, вижн и руны
    ADD COLUMN IF NOT EXISTS obs_wards_diff Float32 DEFAULT nan,
    ADD COLUMN IF NOT EXISTS sen_wards_diff Float32 DEFAULT nan,
    ADD COLUMN IF NOT EXISTS runes_diff Float32 DEFAULT nan,
    -- F6, нейтралки и уровни
    ADD COLUMN IF NOT EXISTS neutral_tier_diff Float32 DEFAULT nan,
    ADD COLUMN IF NOT EXISTS levels_diff Float32 DEFAULT nan,
    -- F3, прайор драфта (инференс Draft Prior Model)
    ADD COLUMN IF NOT EXISTS draft_prior Float32 DEFAULT nan;
