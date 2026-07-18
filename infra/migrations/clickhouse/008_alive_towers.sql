-- 008: фичи alive_diff / towers_diff / rax_diff (Гл. 6.2.2, спринт 32).
--
-- alive_diff  — живые герои Radiant − Dire на момент снапшота: вайп в
--               тимфайте двигает реальную WP на десятки процентов, экономика
--               этого не видит. Источник — PositionSnapshots.is_alive
--               (только реплей-путь; у JSON-матчей NaN).
-- towers_diff — накопительная разница снесённых башен (снесено Radiant'ом −
--               снесено Dire'ом). Источники: combat log (реплеи) и
--               objectives (JSON-путь) — фича есть у ОБОИХ путей сбора.
-- rax_diff    — то же по баракам (мега-крипы экономикой не описываются).
--
-- Float32: NaN = «фича недоступна» (старые строки, alive у JSON-матчей) —
-- нативный пропуск LightGBM; DEFAULT nan закрывает существующие строки.

ALTER TABLE manta.MatchTimelineFeatures ADD COLUMN IF NOT EXISTS alive_diff  Float32 DEFAULT nan;
ALTER TABLE manta.MatchTimelineFeatures ADD COLUMN IF NOT EXISTS towers_diff Float32 DEFAULT nan;
ALTER TABLE manta.MatchTimelineFeatures ADD COLUMN IF NOT EXISTS rax_diff    Float32 DEFAULT nan;
