-- Миграция 002: слой сырых событий реплея под фактический выход парсера.
--
-- 1. ReplayEvents пересоздаётся: ReplacingMergeTree с ключом
--    (match_id, event_type, tick, player_id) молча схлопывал события
--    с совпадающим тиком (несколько ударов за тик — обычное дело),
--    а числовых ID игроков в combat log нет — там имена юнитов
--    (npc_dota_hero_*); ID появляются после джойна с PlayerResource
--    на этапе Feature Extractor. Пересоздание допустимо: слой сырых
--    событий воспроизводим повторным прогоном реплеев.
-- 2. PositionSnapshots получает имя героя.

DROP TABLE IF EXISTS manta.ReplayEvents;
CREATE TABLE manta.ReplayEvents (
    match_id      UInt64,
    tick          UInt32,
    game_time     Int32,
    event_type    Enum8('DAMAGE'=1, 'HEAL'=2, 'KILL'=3, 'ABILITY_CAST'=4,
                        'ITEM_PURCHASE'=5, 'WARD_PLACE'=6),
    player_id     UInt64,
    target_id     UInt64,
    x             Float32,
    y             Float32,
    z             Float32,
    value_amount  Int32,
    inflictor     String,
    attacker      String,
    target        String
) ENGINE = MergeTree()
PARTITION BY intDiv(match_id, 1000000)
ORDER BY (match_id, event_type, tick, attacker, target);

ALTER TABLE manta.PositionSnapshots
    ADD COLUMN IF NOT EXISTS hero String DEFAULT '' AFTER is_alive;
