-- Миграция 003: витрина фич Feature Extractor (Гл. 6, спринт 9).
--
-- ReplacingMergeTree(computed_at): пересчёт фич того же матча (повторная
-- доставка replay.parsed, новая версия экстрактора) замещает старые строки
-- последней версией — идемпотентность at-least-once на уровне хранилища.

CREATE TABLE IF NOT EXISTS manta.PlayerMatchFeatures (
    match_id        UInt64,
    player_id       UInt64,           -- 0-4 Radiant, 5-9 Dire
    team            UInt8,            -- 2 = Radiant, 3 = Dire
    hero            String,           -- npc_dota_hero_*
    player_name     String,
    won             UInt8,            -- команда игрока выиграла матч
    duration_s      Int32,
    gpm             Float32,          -- total_gold / минуты
    xpm             Float32,
    lh_at_5         UInt16,           -- ласт-хиты к 5-й минуте
    dn_at_5         UInt16,
    lh_at_10        UInt16,
    dn_at_10        UInt16,
    net_worth_at_10 Int32,
    net_worth_at_20 Int32,
    net_worth_final Int32,
    gold_share      Float32,          -- доля в net worth команды (конец игры)
    feature_version String,
    computed_at     DateTime DEFAULT now()
) ENGINE = ReplacingMergeTree(computed_at)
PARTITION BY intDiv(match_id, 1000000)
ORDER BY (match_id, player_id);

-- Поминутный таймлайн командных дифференциалов — вход Win Probability
-- (Гл. 6.2.2): net worth diff, XP diff, суммарные убийства.
CREATE TABLE IF NOT EXISTS manta.MatchTimelineFeatures (
    match_id        UInt64,
    game_time       Int32,            -- секунды, шаг 60
    networth_diff   Int32,            -- Radiant - Dire
    xp_diff         Int32,
    kills_radiant   UInt16,           -- накопительно (убийства героев Dire)
    kills_dire      UInt16,
    radiant_win     UInt8,            -- метка исхода (target для обучения)
    feature_version String,
    computed_at     DateTime DEFAULT now()
) ENGINE = ReplacingMergeTree(computed_at)
PARTITION BY intDiv(match_id, 1000000)
ORDER BY (match_id, game_time);
