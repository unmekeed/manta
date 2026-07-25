-- 011: пер-матчевые таблицы драфта и событий (трек F, docs/ML-PLAN.md).
--
-- MatchDraft — одна строка на матч: составы и баны. Основа Draft Prior
-- Model (F3), которая даёт WP осмысленный прайор в минуту 0 вместо 0.5.
-- Составы бэкфиллятся из PlayerMatchFeatures для уже собранных матчей;
-- баны есть только у новых (нужен сырой JSON, F1).
--
-- MatchEvents — разнородные события матча в одной таблице (Рошан, аегис,
-- бэйбеки, руны, варды, first blood). Отдельная таблица, а не колонки
-- витрины: событий переменное число на матч, а витрина поминутная.
-- Поминутные фичи (roshan_diff и пр.) считаются из неё экстрактором.

CREATE TABLE IF NOT EXISTS manta.MatchDraft (
    match_id        UInt64,
    patch           UInt16 DEFAULT 0,
    tier            String DEFAULT '',
    radiant_win     UInt8,
    radiant_heroes  Array(String),   -- npc_dota_hero_*
    dire_heroes     Array(String),
    bans            Array(String),   -- пусто, если picks_bans недоступен
    first_pick_team UInt8 DEFAULT 0, -- 2 Radiant | 3 Dire | 0 неизвестно
    source          String DEFAULT '',  -- json | backfill-mart
    computed_at     DateTime DEFAULT now()
) ENGINE = ReplacingMergeTree(computed_at)
PARTITION BY intDiv(match_id, 1000000)
ORDER BY match_id;

CREATE TABLE IF NOT EXISTS manta.MatchEvents (
    match_id   UInt64,
    game_time  Int32,
    kind       LowCardinality(String),  -- roshan|aegis|aegis_stolen|aegis_denied
                                        -- |firstblood|courier|buyback|rune
                                        -- |ward_obs|ward_sen|ward_killed
    team       UInt8 DEFAULT 0,         -- 2 Radiant | 3 Dire | 0 нет стороны
    player_slot Int16 DEFAULT -1,
    subtype    String DEFAULT '',       -- тип руны, ключ объектива и т.п.
    x          Float32 DEFAULT nan,     -- для вардов
    y          Float32 DEFAULT nan,
    computed_at DateTime DEFAULT now()
) ENGINE = ReplacingMergeTree(computed_at)
PARTITION BY intDiv(match_id, 1000000)
ORDER BY (match_id, game_time, kind, player_slot, subtype);

-- Прайор матча (инференс Draft Prior Model). Хранится здесь, а не в
-- поминутной витрине: значение пер-матчевое, дублировать его на каждую
-- минуту — денормализация. Датасет WP берёт его LEFT JOIN'ом.
ALTER TABLE manta.MatchDraft
    ADD COLUMN IF NOT EXISTS prior Float32 DEFAULT nan;
