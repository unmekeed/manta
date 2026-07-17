-- 007: TTL сырых событий реплея — экономия диска (Гл. 4.4).
--
-- ReplayEvents занимает ~93% витрины (~0.55 МиБ на матч), но читается
-- только конвейером в первые минуты жизни матча: feature-extractor строит
-- по нему фичи, report-generator — отчёт; дальше это мёртвый груз.
-- Обучению TTL не вредит: датасет Win Probability читает исключительно
-- MatchTimelineFeatures (крошечная, хранится вечно). EconomyTimeline и
-- PositionSnapshots малы (~25 КиБ/матч суммарно) и остаются без TTL —
-- их читает API (/timeline) для старых матчей.
--
-- ingested_at проставляется при вставке; для существующих строк
-- материализуется временем миграции — они истекут через 14 дней после неё.

ALTER TABLE manta.ReplayEvents ADD COLUMN IF NOT EXISTS ingested_at DateTime DEFAULT now();
ALTER TABLE manta.ReplayEvents MATERIALIZE COLUMN ingested_at;
ALTER TABLE manta.ReplayEvents MODIFY TTL ingested_at + INTERVAL 14 DAY;
