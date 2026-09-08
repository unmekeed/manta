-- Миграция 017: история решений промоушен-гейта (спринт 202).
--
-- ЗАЧЕМ. Решения гейта до сих пор жили ТОЛЬКО в Telegram. Из-за этого
-- храповик (ROADMAP, G1) стал виден лишь тогда, когда владелец прислал
-- в чат четыре сообщения подряд и их можно было сравнить глазами:
--
--     про-эталон prod: 0.1553 → 0.1565 → 0.1565 → 0.1578
--
-- Каждый шаг «в пределах шума», сумма — заметное ухудшение. Гейт
-- сравнивает кандидата ТОЛЬКО с текущим prod, и планка пересчитывается
-- от неё же, поэтому серия незначимых ухудшений проходит беспрепятственно.
--
-- Без таблицы нельзя ни построить график, ни поднять алерт на
-- НАКОПЛЕННОЕ ухудшение, ни ответить «когда prod стал хуже». Чат — не
-- база: в нём нельзя сделать запрос, а сообщения теряются в потоке.
--
-- ПОЧЕМУ POSTGRES, А НЕ CLICKHOUSE. Здесь уже живёт вся операционная
-- мелочь — MatchReports, ApiBudget, MatchSummaries, — и шлюз читает
-- именно её. Строк тут единицы в сутки, аналитическая колоночная база
-- для такого не нужна, а страница состояния получает данные без единого
-- нового клиента.

BEGIN;

CREATE TABLE IF NOT EXISTS ModelVersions (
    model_name      VARCHAR(64)  NOT NULL,
    version         VARCHAR(64)  NOT NULL,

    -- Когда РЕШИЛИ, а не когда обучили: вопрос, на который таблица
    -- отвечает, — «когда prod стал хуже», и точка отсчёта у него
    -- момент промоушена.
    decided_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    promoted        BOOLEAN      NOT NULL,

    -- Объяснение гейта целиком, как оно ушло в лог и Telegram. Дублирует
    -- holdouts, и намеренно: разобранная форма годится для запросов, а
    -- текст — для человека, который читает строку через полгода и хочет
    -- понять, что тогда решали, не восстанавливая формат.
    reason          TEXT         NOT NULL DEFAULT '',

    dataset_matches INT          NOT NULL DEFAULT 0,
    metrics         JSONB        NOT NULL DEFAULT '{}'::jsonb,

    -- Разобранные оценки: [{kind, n_matches, brier_new, brier_prod,
    -- delta, sigma, ok}]. Ради ЭТОГО поля таблица и заводится — по нему
    -- считается дрейф prod между версиями, то есть храповик становится
    -- измеримым, а не замеченным вручную.
    holdouts        JSONB        NOT NULL DEFAULT '[]'::jsonb,

    PRIMARY KEY (model_name, version)
);

-- Запросы к таблице всегда «за последние N», и почти всегда по одной
-- модели: истории у win_probability и death_risk разные.
CREATE INDEX IF NOT EXISTS idx_modelversions_time
    ON ModelVersions (model_name, decided_at DESC);

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'manta_gateway') THEN
        GRANT SELECT ON ModelVersions TO manta_gateway;
    END IF;
END
$$;

COMMIT;
