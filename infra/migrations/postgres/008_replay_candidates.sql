-- Миграция 008: очередь кандидатов на скачивание реплея (спринт 126).
--
-- Замер спринта 125: из живого потока Valve правилу отбора удовлетворяет
-- ~0.35% матчей, то есть порядка нескольких тысяч в сутки — больше, чем
-- способен скачать домашний канал (58 МиБ на матч). Значит отбор перестаёт
-- быть замером и становится очередью: находим кандидатов быстро и дёшево
-- (100 матчей за один вызов Valve), а скачиваем медленно и выборочно.
--
-- Очередь именно в БД, а не в памяти процесса: между «нашли» и «скачали»
-- лежат часы, перезапуски и суточные лимиты OpenDota.

BEGIN;

CREATE TABLE ReplayCandidates (
    match_id       BIGINT PRIMARY KEY,
    match_seq_num  BIGINT NOT NULL,

    -- Начало матча. Нужно не для отчётности: Valve хранит реплеи около
    -- двух недель, и кандидат старше этого срока мёртв. Без даты мы
    -- вечно долбились бы в 404 по матчам, которых больше нет.
    started_at     TIMESTAMPTZ,
    found_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    -- Почему матч прошёл отбор. Хранится ради ТОЧНОСТИ правила: без этих
    -- трёх чисел нельзя проверить, не набираем ли мы мусор, — а правило
    -- построено на двух известных рангах из десяти игроков и заслуживает
    -- проверки на реальных данных, а не доверия.
    known_ranks    SMALLINT NOT NULL DEFAULT 0,
    immortal_ranks SMALLINT NOT NULL DEFAULT 0,
    avg_known_rank SMALLINT NOT NULL DEFAULT 0,

    -- new — ждёт очереди; taken — отдан коллектору; done — скачан;
    -- no_salt — OpenDota так и не отдал ссылку; expired — реплей у Valve
    -- уже удалён; failed — постоянная ошибка скачивания.
    state          VARCHAR(16) NOT NULL DEFAULT 'new',
    attempts       INT NOT NULL DEFAULT 0,

    -- Когда можно пробовать снова. У свежего матча соли ещё нет, она
    -- появляется с задержкой; без отложенного повтора коллектор жёг бы
    -- квоту OpenDota на одном и том же матче каждый цикл.
    next_try_at    TIMESTAMPTZ,
    last_error     TEXT,
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Очередь на выдачу. Частичный индекс: строк в состоянии new всегда
-- меньшинство, а сканирование добавляет тысячи записей в сутки.
CREATE INDEX replaycandidates_queue_idx
    ON ReplayCandidates (next_try_at NULLS FIRST, found_at)
    WHERE state = 'new';

CREATE INDEX replaycandidates_state_idx ON ReplayCandidates (state);

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'manta_collector') THEN
        GRANT INSERT, UPDATE, DELETE ON ReplayCandidates TO manta_collector;
    END IF;
END
$$;

COMMIT;
