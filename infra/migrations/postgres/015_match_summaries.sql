-- Миграция 015: карточка матча для публичного API (спринт 192).
--
-- ЗАЧЕМ. `GET /api/v1/matches` до сих пор был заглушкой: последние 50
-- записей без пагинации, а из полей — только match_id, финальная WP,
-- narrative и дата. Сайту нужна карточка: команды, счёт, длительность,
-- патч, уровень матча, пики. Ничего этого в Postgres нет — всё лежит в
-- ClickHouse.
--
-- ПОЧЕМУ НЕ ХОДИТЬ В CLICKHOUSE ИЗ ШЛЮЗА. Потому что путь чтения в этом
-- проекте устроен ровно наоборот, и записано это ещё в миграции 003:
-- «отчёт материализуется при генерации, чтение — O(1), без обращений к
-- ClickHouse/ML». Список матчей — самый горячий запрос будущего сайта, и
-- делать его самым тяжёлым значило бы отменить решение, ради которого
-- отчёты вообще материализуются.
--
-- Данные для карточки у report-generator уже НА РУКАХ в момент
-- генерации: витрину и игроков он и так читает, чтобы построить отчёт.
-- Карточка — это то, что он выбрасывал.
--
-- ПОЧЕМУ ОТДЕЛЬНАЯ ТАБЛИЦА, А НЕ КОЛОНКИ В MatchReports. Строки
-- MatchReports тяжёлые: два JSONB, у крупных матчей сотни килобайт.
-- Список из пятидесяти карточек не должен поднимать с диска мегабайты
-- разборов, которые никто не откроет.
--
-- ПОРЯДОК И ПАГИНАЦИЯ — ПО match_id, А НЕ ПО generated_at. Это не
-- вкусовщина: `generated_at` МЕНЯЕТСЯ. Отчёт перегенерируется при новой
-- версии модели (UPSERT ставит NOW()), и старый матч прыгает в начало
-- списка. Клиент, листающий по такому ключу, получит дубли и пропуски —
-- причём тихо, потому что каждая отдельная страница выглядит
-- правильной. `match_id` у Valve монотонно растёт со временем, он
-- неизменен, и «сначала свежие матчи» по нему выражается точно.

BEGIN;

CREATE TABLE IF NOT EXISTS MatchSummaries (
    match_id         BIGINT PRIMARY KEY,
    radiant_win      BOOLEAN NOT NULL,
    kills_radiant    INTEGER NOT NULL DEFAULT 0,
    kills_dire       INTEGER NOT NULL DEFAULT 0,
    duration_s       INTEGER NOT NULL DEFAULT 0,
    patch            INTEGER NOT NULL DEFAULT 0,
    tier             TEXT    NOT NULL DEFAULT '',
    -- npc_dota_hero_* — то же представление, что в MatchDraft и в
    -- справочнике libs/data/heroes.json. Числовые id не храним: имя
    -- переживает смену нумерации у Valve, а картинку и локализованное
    -- название сайт берёт по имени из /api/v1/heroes.
    radiant_heroes   TEXT[]  NOT NULL DEFAULT '{}',
    dire_heroes      TEXT[]  NOT NULL DEFAULT '{}',
    -- Вероятность победы Radiant на последней точке таймлайна. NULL —
    -- это «модель не считала», а не «пятьдесят на пятьдесят»: у матча
    -- без обслуживаемой модели кривой нет вовсе, и подставлять 0.5
    -- значило бы показать пользователю выдуманное число.
    final_radiant_wp REAL,
    generated_at     TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()
);

-- Листание: ORDER BY match_id DESC с курсором `match_id < $1`.
-- Первичный ключ это уже обеспечивает, отдельного индекса не нужно.

-- Фильтры patch и tier. Составной индекс, а не два отдельных: запрос
-- всегда сортирует по match_id, и без него планировщик отберёт строки
-- по патчу, а потом отсортирует их целиком.
CREATE INDEX IF NOT EXISTS idx_match_summaries_patch
    ON MatchSummaries (patch, match_id DESC);
CREATE INDEX IF NOT EXISTS idx_match_summaries_tier
    ON MatchSummaries (tier, match_id DESC);

-- Поиск по герою. GIN по объединению составов: «матчи, где был Pudge»
-- не должен зависеть от того, за какую сторону он играл.
CREATE INDEX IF NOT EXISTS idx_match_summaries_heroes
    ON MatchSummaries USING GIN ((radiant_heroes || dire_heroes));

-- Права на ЗАПИСЬ приходится выдавать явно. `ALTER DEFAULT PRIVILEGES` в
-- миграции 005 покрывает будущие таблицы только для SELECT — и это
-- правильно, раздавать INSERT всем ролям заранее нельзя. Но значит, что
-- каждая новая таблица, в которую кто-то пишет, обязана привезти свой
-- GRANT с собой. Забыть его — получить `permission denied` только на
-- VPS: дома всё ходит под владельцем базы, и проверка глазами покажет
-- рабочую систему.
GRANT INSERT, UPDATE, DELETE ON MatchSummaries TO manta_reports;

COMMIT;
