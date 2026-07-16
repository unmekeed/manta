-- Миграция 005: профиль игрока (спринт 30, Гл. 7:
-- GET /api/v1/players/{id}/profile).
--
-- Материализуется Report Generator'ом после каждого отчёта: агрегаты по
-- PlayerMatchFeatures (ClickHouse) пересчитываются для игроков матча и
-- UPSERT'ятся сюда — путь чтения (шлюз) остаётся одним SELECT.
-- Ключ — account_id (steam64 из реплея, миграция ClickHouse 007);
-- account_id=0 (анонимы/старые строки) в профили не попадает.

BEGIN;

CREATE TABLE IF NOT EXISTS PlayerProfiles (
    account_id  BIGINT PRIMARY KEY,
    nickname    TEXT NOT NULL DEFAULT '',
    matches     INT  NOT NULL DEFAULT 0,
    wins        INT  NOT NULL DEFAULT 0,
    avg_gpm     REAL NOT NULL DEFAULT 0,
    avg_xpm     REAL NOT NULL DEFAULT 0,
    main_lane   TEXT NOT NULL DEFAULT '',
    top_heroes  JSONB NOT NULL DEFAULT '[]',  -- [{hero, matches}]
    updated_at  TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

COMMIT;
