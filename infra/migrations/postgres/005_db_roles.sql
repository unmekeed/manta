-- Миграция 005: раздельные роли БД (рекомендация §6.3 обзора безопасности,
-- спринт 73).
--
-- Было: все сервисы ходят под одним пользователем `dota` с полными правами.
-- Компрометация любого — например коллектора, который разбирает внешний JSON
-- из интернета, — давала полный доступ ко всей базе, включая отчёты с PII.
--
-- Стало: групповые роли по функции. Принцип наименьших привилегий из Гл. 9.3
-- применён к БД, а не только к HTTP-ручкам.
--
-- ВАЖНО: здесь создаются только ГРУППОВЫЕ роли (NOLOGIN) с грантами. Роли
-- для входа с паролями создаёт scripts/create-db-users.sh — пароли берутся
-- из окружения, и в git их нет и быть не может. Разделение намеренное:
-- миграции лежат в репозитории, секреты — нет.
--
-- Миграция ничего не отбирает у существующего пользователя `dota`: стенд
-- продолжает работать как раньше. Переход на раздельных пользователей —
-- осознанный шаг администратора (см. README, «Раздельные роли БД»).

BEGIN;

-- Роли создаются идемпотентно: миграция может прогоняться на базе, где
-- часть ролей уже есть (журнал SchemaMigrations защищает от повтора, но
-- дешевле перестраховаться, чем разбираться с частично применённой схемой).
DO $$
BEGIN
    -- Только чтение: дашборды, аналитика, ad-hoc запросы. Отдельная роль
    -- нужна, чтобы «посмотреть» не требовало прав «изменить».
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'manta_ro') THEN
        CREATE ROLE manta_ro NOLOGIN;
    END IF;
    -- Коллектор: дедуп собранных матчей и курсор источников. Самый
    -- «внешний» сервис — разбирает JSON из интернета, поэтому его права
    -- урезаны сильнее всех: две таблицы, ничего про отчёты и PII.
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'manta_collector') THEN
        CREATE ROLE manta_collector NOLOGIN;
    END IF;
    -- Report Generator: единственный писатель отчётов.
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'manta_reports') THEN
        CREATE ROLE manta_reports NOLOGIN;
    END IF;
    -- API Gateway: задания анализа, outbox, а также UPDATE отчётов —
    -- он выполняет GDPR-стирание (Гл. 9.7), без этого права erasure
    -- перестал бы работать.
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'manta_gateway') THEN
        CREATE ROLE manta_gateway NOLOGIN;
    END IF;
END
$$;

-- Чтение — всем ролям: сервисы читают больше, чем пишут, и запрет на
-- SELECT ломал бы их без выигрыша в безопасности (данные всё равно видны
-- своему сервису).
GRANT USAGE ON SCHEMA public TO manta_ro, manta_collector, manta_reports,
                               manta_gateway;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO manta_ro, manta_collector,
                                               manta_reports, manta_gateway;

-- Запись — точечно, по функции сервиса.
GRANT INSERT, UPDATE, DELETE ON CollectedMatches, CollectorCursor
    TO manta_collector;
GRANT INSERT, UPDATE, DELETE ON MatchReports TO manta_reports;
GRANT INSERT, UPDATE, DELETE ON AnalysisJobs, EventOutbox TO manta_gateway;
GRANT INSERT, UPDATE ON Matches, MatchPlayers, Players, Accounts,
                        Subscriptions TO manta_gateway;
-- GDPR-стирание правит jsonb отчётов, но не удаляет их: UPDATE без DELETE.
GRANT UPDATE ON MatchReports TO manta_gateway;

-- Последовательности: без USAGE на них INSERT в таблицы с serial падает.
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public
    TO manta_collector, manta_reports, manta_gateway;

-- Будущие таблицы (следующие миграции) — чтобы права не разъезжались с
-- каждой новой миграцией и не приходилось помнить про GRANT вручную.
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT SELECT ON TABLES TO manta_ro, manta_collector, manta_reports,
                              manta_gateway;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT USAGE, SELECT ON SEQUENCES TO manta_collector, manta_reports,
                                         manta_gateway;

COMMIT;
