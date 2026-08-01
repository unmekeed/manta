-- A14 (спринт 84): агрегат драк матча.
--
-- Мотив — TTL. ReplayEvents живёт 14 дней (миграция 007) и является
-- единственным источником того, кто кого убил; после истечения драки
-- восстановить неоткуда, реплеи Valve тоже удаляет. Агрегат занимает
-- десятки байт против сотен килобайт сырых событий, поэтому хранится
-- БЕЗ TTL и копится с первого дня — иначе к моменту, когда данных
-- хватит на модель исхода стычек, истории не окажется.
--
-- ReplacingMergeTree по (match_id, fight_id): переразбор того же матча
-- не задваивает драки, побеждает строка с бОльшим computed_at.
CREATE TABLE IF NOT EXISTS manta.MatchFights (
    match_id             UInt64,
    fight_id             UInt16,   -- порядковый номер драки в матче
    start_time           Int32,    -- секунда первой смерти
    end_time             Int32,    -- секунда последней смерти
    -- Центр стычки. DEFAULT nan, потому что при отсутствии позиций
    -- координаты не пишутся вовсе: json.dumps(nan) даёт нестандартный
    -- литерал NaN, который JSONEachRow не примет.
    x                    Float32 DEFAULT nan,
    y                    Float32 DEFAULT nan,
    radiant_participants UInt8,    -- живые рядом на начало + погибшие
    dire_participants    UInt8,
    radiant_deaths       UInt8,
    dire_deaths          UInt8,
    outcome              Int8,     -- +1 Radiant разменял выгоднее, −1 Dire
    computed_at          DateTime DEFAULT now()
) ENGINE = ReplacingMergeTree(computed_at)
PARTITION BY intDiv(match_id, 1000000)
ORDER BY (match_id, fight_id);
