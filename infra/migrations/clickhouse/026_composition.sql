-- Миграция 026: состав команд по свойствам героев (спринт 187).
--
-- ЗАЧЕМ. Модель не знала о героях НИЧЕГО. Драфт-приор (винрейты героев на
-- патче) снят ревизией трека F в спринте 134 — измеримого выигрыша он не
-- дал; эмбеддинги требуют порядка 20 тысяч матчей, которых пока нет.
--
-- Свойства героев — средний путь, работающий на нынешнем объёме. Их
-- тринадцать вместо ста двадцати семи имён, и они ОБОБЩАЮТСЯ: узнав
-- что-то про составы из четырёх ближников, модель применит это и к
-- герою, которого видела трижды. Ни поимённые категории (переобучение на
-- редких), ни эмбеддинги (нечем учить) этого сейчас не умеют.
--
-- Все тринадцать — разности R−D, как и прочие фичи витрины: «три
-- ближника против двух» важнее абсолютных чисел.
--
-- Фичи СТАТИЧЕСКИЕ: состав известен с нулевой минуты и не меняется.
-- Записываются в каждую поминутную строку — так же, как patch и tier.
--
-- DEFAULT nan: у матчей, разобранных до этого спринта, состава в витрине
-- нет, и ноль означал бы «составы симметричны» — утверждение, которого
-- никто не проверял.

ALTER TABLE manta.MatchTimelineFeatures
    ADD COLUMN IF NOT EXISTS melee_diff             Float32 DEFAULT nan;
ALTER TABLE manta.MatchTimelineFeatures
    ADD COLUMN IF NOT EXISTS attr_str_diff          Float32 DEFAULT nan;
ALTER TABLE manta.MatchTimelineFeatures
    ADD COLUMN IF NOT EXISTS attr_agi_diff          Float32 DEFAULT nan;
ALTER TABLE manta.MatchTimelineFeatures
    ADD COLUMN IF NOT EXISTS attr_int_diff          Float32 DEFAULT nan;
ALTER TABLE manta.MatchTimelineFeatures
    ADD COLUMN IF NOT EXISTS attr_all_diff          Float32 DEFAULT nan;
ALTER TABLE manta.MatchTimelineFeatures
    ADD COLUMN IF NOT EXISTS role_carry_diff        Float32 DEFAULT nan;
ALTER TABLE manta.MatchTimelineFeatures
    ADD COLUMN IF NOT EXISTS role_support_diff      Float32 DEFAULT nan;
ALTER TABLE manta.MatchTimelineFeatures
    ADD COLUMN IF NOT EXISTS role_nuker_diff        Float32 DEFAULT nan;
ALTER TABLE manta.MatchTimelineFeatures
    ADD COLUMN IF NOT EXISTS role_disabler_diff     Float32 DEFAULT nan;
ALTER TABLE manta.MatchTimelineFeatures
    ADD COLUMN IF NOT EXISTS role_durable_diff      Float32 DEFAULT nan;
ALTER TABLE manta.MatchTimelineFeatures
    ADD COLUMN IF NOT EXISTS role_escape_diff       Float32 DEFAULT nan;
ALTER TABLE manta.MatchTimelineFeatures
    ADD COLUMN IF NOT EXISTS role_initiator_diff    Float32 DEFAULT nan;
ALTER TABLE manta.MatchTimelineFeatures
    ADD COLUMN IF NOT EXISTS role_pusher_diff       Float32 DEFAULT nan;
