-- Миграция 024: готовые поминутные ряды OpenDota (спринт 185).
--
-- ЗАЧЕМ. Инвентаризация полей API (спринт 184) показала, что за один
-- вызов `/matches/{id}` OpenDota отдаёт 149 полей на игрока, а читаем мы
-- четырнадцать. Пять из невзятых — ГОТОВЫЕ поминутные ряды той же длины
-- и той же сетки, что `gold_t`, который мы уже берём. Их не надо ни
-- собирать из логов, ни восстанавливать по событиям: они лежат в том же
-- ответе, за тот же вызов, и всё это время выбрасывались.
--
-- ЧТО ОНИ ДАЮТ.
--
--   lh_diff            добитки. Модель знает только networth_diff и не
--                      различает «богат, потому что фармит» и «богат,
--                      потому что убивает». Это разные игры с разным
--                      будущим: фарм устойчив, серия убийств — нет.
--   dn_diff            денаи: давление на линии, невидимое в золоте.
--   hero_damage_diff   урон ПО ГЕРОЯМ — сильнейший из пятёрки.
--                      Показывает, кто ведёт бой, а не кто копит.
--   hero_healing_diff  лечение: цена размена в драках.
--   camps_stacked_diff стаки лагерей — работа саппорта, которой нет ни
--                      в одной другой фиче витрины.
--
-- DEFAULT nan, а не 0. Ноль означал бы «никто не фармил», и это была бы
-- НЕПРАВДА для матчей, разобранных до этого спринта, и для тех, что
-- пришли реплейным путём. LightGBM обрабатывает пропуск нативно, а ноль
-- он примет за факт — и выучит его.
--
-- Заполнить старые матчи можно без единого вызова API:
--     python -m collector.backfill --only-missing
-- сырой JSON лежит в MinIO с 60-го спринта (2344 матча на 2026-09-02).

ALTER TABLE manta.MatchTimelineFeatures
    ADD COLUMN IF NOT EXISTS lh_diff            Float32 DEFAULT nan;
ALTER TABLE manta.MatchTimelineFeatures
    ADD COLUMN IF NOT EXISTS dn_diff            Float32 DEFAULT nan;
ALTER TABLE manta.MatchTimelineFeatures
    ADD COLUMN IF NOT EXISTS hero_damage_diff   Float32 DEFAULT nan;
ALTER TABLE manta.MatchTimelineFeatures
    ADD COLUMN IF NOT EXISTS hero_healing_diff  Float32 DEFAULT nan;
ALTER TABLE manta.MatchTimelineFeatures
    ADD COLUMN IF NOT EXISTS camps_stacked_diff Float32 DEFAULT nan;
