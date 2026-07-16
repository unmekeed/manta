-- Миграция 006: мета героев (спринт 31, Гл. 3: Meta Engine —
-- бейзлайн; Гл. 7: GET /api/v1/meta/heroes).
--
-- Материализуется Report Generator'ом после каждого отчёта: агрегаты
-- по героям из PlayerMatchFeatures (ClickHouse). shrunk_winrate —
-- байесовское сглаживание к 0.5 (см. reportgen.meta): у героя с двумя
-- матчами винрейт 100% ничего не значит. ban_rate из контракта Гл. 7
-- пока недоступен: события драфта не извлекаются из реплеев.

BEGIN;

CREATE TABLE IF NOT EXISTS MetaHeroes (
    hero            TEXT PRIMARY KEY,  -- npc_dota_hero_*
    hero_id         INT  NOT NULL DEFAULT 0,
    matches         INT  NOT NULL DEFAULT 0,
    wins            INT  NOT NULL DEFAULT 0,
    winrate         REAL NOT NULL DEFAULT 0,
    shrunk_winrate  REAL NOT NULL DEFAULT 0.5,
    pick_rate       REAL NOT NULL DEFAULT 0,   -- доля матчей с героем
    avg_gpm         REAL NOT NULL DEFAULT 0,
    updated_at      TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

COMMIT;
