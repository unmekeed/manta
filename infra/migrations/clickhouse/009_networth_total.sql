-- 009: networth_total — суммарный net worth обеих команд (спринт 35, A6).
--
-- Нужен для производной фичи networth_rel = networth_diff / networth_total:
-- доля преимущества вместо абсолюта (5k на 10-й минуте и 5k на 40-й —
-- разные вселенные). Нормированная фича выучивается на меньшем числе
-- матчей — критично на текущем размере датасета. Абсолютный networth_diff
-- остаётся — LightGBM сам выберет.
--
-- Float32 DEFAULT nan: старые строки честно «не знают» значения.

ALTER TABLE manta.MatchTimelineFeatures ADD COLUMN IF NOT EXISTS networth_total Float32 DEFAULT nan;
