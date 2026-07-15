-- Миграция 005: позиционная фича таймлайна (спринт 18).
--
-- position_advance — территориальное продвижение: среднее положение всех
-- героев вдоль диагонали карты «фонтан Radiant → фонтан Dire»,
-- нормированное в [-1, 1] (0 — центр карты, +1 — база Dire). Игра у базы
-- Dire означает контроль карты Radiant'ами и наоборот (прокси Map
-- Control, Гл. 6.1.3). Источник — PositionSnapshots.

ALTER TABLE dota_analyst.MatchTimelineFeatures
    ADD COLUMN IF NOT EXISTS position_advance Float32 DEFAULT 0 AFTER kills_dire;
