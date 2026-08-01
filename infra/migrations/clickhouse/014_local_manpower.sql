-- A14 (спринт 83): локальный перевес и собранность команды.
--
-- alive_diff считает живых героев по ВСЕЙ карте, поэтому «пятеро против
-- пятерых, но трое наших на другом краю» выглядит как равенство. Эти две
-- колонки описывают то, что решает исход стычки: сколько героев каждой
-- стороны РЯДОМ с точкой контакта и насколько команда собрана.
--
-- DEFAULT nan, а не 0: у матчей из JSON-источников (OpenDota, STRATZ)
-- позиций нет вовсе, и ноль означал бы «стороны сошлись ровно поровну» —
-- ложный сигнал. NaN LightGBM обрабатывает нативно как пропуск.
ALTER TABLE manta.MatchTimelineFeatures
    ADD COLUMN IF NOT EXISTS local_manpower_diff Float32 DEFAULT nan
    AFTER alive_diff;

ALTER TABLE manta.MatchTimelineFeatures
    ADD COLUMN IF NOT EXISTS spread_diff Float32 DEFAULT nan
    AFTER local_manpower_diff;
