-- Миграция 001: аналитический слой ClickHouse (Гл. 4.4 спецификации).

CREATE TABLE IF NOT EXISTS manta.ReplayEvents (
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
    inflictor     String
) ENGINE = ReplacingMergeTree()
PARTITION BY toYYYYMM(FROM_UNIXTIME(game_time))
ORDER BY (match_id, event_type, tick, player_id);

CREATE TABLE IF NOT EXISTS manta.EconomyTimeline (
    match_id       UInt64,
    player_id      UInt64,
    game_time      Int32,
    net_worth      Int32,
    total_gold     Int32,
    total_xp       Int32,
    lh             UInt16,
    dn             UInt16
) ENGINE = MergeTree()
PARTITION BY toYYYYMM(FROM_UNIXTIME(game_time))
ORDER BY (match_id, player_id, game_time);

CREATE TABLE IF NOT EXISTS manta.PositionSnapshots (
    match_id   UInt64,
    player_id  UInt64,
    game_time  Int32,
    x          Float32,
    y          Float32,
    is_alive   UInt8
) ENGINE = MergeTree()
PARTITION BY toYYYYMM(FROM_UNIXTIME(game_time))
ORDER BY (match_id, game_time, player_id);
