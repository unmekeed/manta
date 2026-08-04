-- Спринт 99: тайминги предметов и способностей по героям.
--
-- ITEM_PURCHASE, ABILITY_CAST и BUYBACK писались в ReplayEvents с самого
-- начала и не читались НИ ОДНОЙ строкой кода. Через 14 дней их стирает
-- TTL (миграция 007), а сам .dem удаляется сразу после разбора
-- (PURGE_PARSED_REPLAYS) — то есть данные исчезали безвозвратно примерно
-- по 50 матчам в сутки.
--
-- Ради чего сохраняем (каталог сигналов, раздел C):
--   ult_availability_diff  — ценность 5/5, «самое недооценённое»: в
--       поздней игре исход решающей драки определяется доступностью
--       ультов, а модель о них не знает вовсе;
--   power_spike_diff       — кто сейчас на пике силы по ключевым
--       предметам; для этого нужен ТАЙМИНГ покупки, а не факт.
--
-- Форма — агрегат, а не копия событий: одна строка на (герой, предмет
-- или способность). Первое применение плюс счётчики по фазам весят
-- десятки байт против тысяч кастов сырьём, и этого достаточно и для
-- тайминга спайка, и для оценки, как часто сторона могла использовать
-- ульт в каждой фазе.
--
-- ReplacingMergeTree по полному ключу: переразбор матча ЗАМЕЩАЕТ строку.
CREATE TABLE IF NOT EXISTS manta.MatchHeroTimings (
    match_id    UInt64,
    team        UInt8,                      -- 2 Radiant, 3 Dire
    hero        LowCardinality(String),     -- нормализованное имя
    kind        Enum8('item' = 1, 'ability' = 2, 'buyback' = 3),
    name        LowCardinality(String),     -- предмет/способность
    first_time  Int32,                      -- первое применение, сек
    last_time   Int32,
    casts_early UInt16,                     -- < 10 мин
    casts_mid   UInt16,                     -- 10-25 мин
    casts_late  UInt16,                     -- > 25 мин
    computed_at DateTime DEFAULT now()
) ENGINE = ReplacingMergeTree(computed_at)
PARTITION BY intDiv(match_id, 1000000)
ORDER BY (match_id, team, hero, kind, name);
