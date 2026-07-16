-- Миграция 007: идентификатор игрока (спринт 30, профиль игрока).
--
-- account_id — steam64 из CDotaGameInfo (ядро парсера читал его всегда,
-- но не отдавал в summary). 0 — бот/аноним/строки, посчитанные до этой
-- миграции (реплеи вычищены, бэкфилл невозможен — профиль накапливается
-- с новых матчей). Ключ агрегатов PlayerProfiles.

ALTER TABLE manta.PlayerMatchFeatures
    ADD COLUMN IF NOT EXISTS account_id UInt64 DEFAULT 0 AFTER player_id;
