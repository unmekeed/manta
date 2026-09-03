"""Состав команд по свойствам героев (спринт 187, ревизия 190).

ЗАЧЕМ. Модель не знала о героях НИЧЕГО. Драфт-приор (винрейты на патче)
снят ревизией трека F в спринте 134 — измеримого выигрыша он не дал;
эмбеддинги требуют порядка 20 тысяч матчей, которых пока нет.

Свойства героев — средний путь. Их пять вместо ста двадцати семи имён,
и они ОБОБЩАЮТСЯ: узнав что-то про составы из четырёх ближников, модель
применит это и к герою, которого видела трижды. Ни поимённые категории
(переобучение на редких), ни эмбеддинги (нечем учить) этого сейчас не
умеют.

ПОЧЕМУ ПЯТЬ, А НЕ ТРИНАДЦАТЬ. Восемь ролей (`role_*_diff`) сняты
ревизией спринта 190: абляция на 2355 матчах показала, что с ними Brier
ХУЖЕ на 0.00227 — они не бесполезны, они вредны. Пять оставшихся
(ближний бой и четыре атрибута) дали +0.00255 ± 0.00115, а на ранней
фазе +0.00399. Роли остаются в справочнике героев, но в витрину не
идут — см. docs/ML-PLAN.md.

ЧТО ЗДЕСЬ ДОРОГО ОШИБИТЬСЯ. Ноль и пропуск, снова. Ноль в разности
состава означает «поровну» — утверждение о матче; отсутствие данных о
составе означает «неизвестно». Спутать их значит сказать модели, что
команды сбалансированы, когда мы просто не знаем героев.
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from collector import signals as S  # noqa: E402

MIN = [60, 120]


def by_name(*names):
    """Матч из десяти героев: первые пять — Radiant, вторые — Dire."""
    ids = [S._HEROES[n]["id"] for n in names]
    return {"players": [{"player_slot": i if i < 5 else 123 + i,
                         "hero_id": h} for i, h in enumerate(ids)]}


MELEE = "npc_dota_hero_axe"          # Melee, str
RANGED = "npc_dota_hero_lina"        # Ranged, int


def test_melee_count_is_a_difference():
    """Ближники считаются разностью R−D, а не двумя счётчиками.

    Все фичи витрины — командные дифференциалы: «три ближника против
    двух» модели важнее абсолютных чисел.
    """
    m = by_name(MELEE, MELEE, MELEE, RANGED, RANGED,
                MELEE, RANGED, RANGED, RANGED, RANGED)
    out = S.composition_features(m, MIN)
    assert out["melee_diff"] == [2.0, 2.0]


def test_the_features_are_constant_over_time():
    """Состав статичен: одинаков во всех минутах.

    Он известен с нулевой минуты и не меняется — как patch и tier.
    """
    m = by_name(*([MELEE] * 5 + [RANGED] * 5))
    out = S.composition_features(m, [0, 600, 3600])
    assert out["melee_diff"] == [5.0, 5.0, 5.0]


def test_attributes_are_counted():
    """Атрибуты считаются по справочнику."""
    m = by_name(*([MELEE] * 5 + [RANGED] * 5))
    out = S.composition_features(m, MIN)
    assert out["attr_str_diff"][0] == 5.0     # Axe — str
    assert out["attr_int_diff"][0] == -5.0    # Lina — int


def test_every_declared_feature_is_always_present():
    """Все пять колонок отдаются всегда, даже нулевые.

    Колонка, появляющаяся только когда в матче был силовик, давала бы
    ПРОПУСК там, где на самом деле «силовиков поровну». Это разные вещи:
    первое — «не знаем», второе — «знаем, что поровну».
    """
    m = by_name(*([MELEE] * 10))
    out = S.composition_features(m, MIN)
    expected = {"melee_diff"} | {f"attr_{a}_diff" for a in S.ATTRS}
    assert set(out) == expected
    assert out["melee_diff"] == [0.0, 0.0], "десять ближников — разность ноль"


def test_roles_do_not_come_back_by_themselves():
    """Ролей в витрине нет — и они не должны вернуться сами собой.

    Роли лежат в справочнике героев и никуда оттуда не делись: их читает
    отчёт, их может захотеть будущая фича. Но ревизия 190 доказала, что
    МОДЕЛИ они вредят, и цена возврата — молчаливая деградация Brier,
    которую заметит только следующая абляция. Поэтому запрет проверяется
    здесь, а не держится на памяти.
    """
    m = by_name(*([MELEE] * 5 + [RANGED] * 5))
    out = S.composition_features(m, MIN)
    assert not [k for k in out if k.startswith("role_")], (
        "роль вернулась в витрину: она вредна (абляция 190, Δ −0.00227)")


# -- ноль против пропуска -------------------------------------------------------

def test_an_incomplete_roster_gives_no_features():
    """ГЛАВНОЕ: неполный состав — это неизвестность, а не «поровну».

    Нули здесь означали бы симметричный состав, которого никто не видел.
    """
    m = by_name(*([MELEE] * 10))
    m["players"] = m["players"][:9]
    assert S.composition_features(m, MIN) == {}


def test_an_unknown_hero_does_not_void_the_match():
    """Герой, которого нет в справочнике, не выбрасывает матч.

    Справочник — снимок, и он отстаёт от патча на дни. Выбрасывать матч
    из-за героя, добавленного вчера, значило бы терять данные ровно
    тогда, когда мета самая интересная.
    """
    m = by_name(*([MELEE] * 10))
    m["players"][0]["hero_id"] = 99999
    out = S.composition_features(m, MIN)
    assert out, "матч выброшен из-за одного неизвестного героя"
    assert out["melee_diff"] == [-1.0, -1.0], "неизвестный внёс вклад"


def test_the_reference_has_the_properties_we_read():
    """Справочник содержит свойства, а не только id и имя.

    До спринта 187 в нём лежали только id и localized_name — читать
    оттуда свойства значило бы молча получать пустой состав у всех матчей.
    """
    sample = S._HEROES["npc_dota_hero_axe"]
    assert sample["attack_type"] in ("Melee", "Ranged")
    assert sample["primary_attr"] in S.ATTRS


def test_the_features_reach_the_aggregate():
    """Фичи доезжают до `all_minute_features`."""
    m = by_name(*([MELEE] * 5 + [RANGED] * 5))
    assert S.all_minute_features(m, MIN).get("melee_diff") == [5.0, 5.0]


def test_each_attribute_has_its_own_column():
    """Каждый атрибут — своя колонка, и все они названы.

    Список фич композиции строится из ATTRS, а не выписан руками. Но
    имена, под которыми они доезжают до модели, зафиксированы здесь:
    переименование втихую сделало бы старые артефакты несовместимыми, а
    align_to_artifact отбирает колонки ПО ИМЕНАМ.

    Перечислены поимённо намеренно — так их видит и проверка покрытия
    фич в ml-service (каждая фича модели обязана упоминаться в тестах).
    """
    m = by_name(*([MELEE] * 5 + [RANGED] * 5))
    out = S.composition_features(m, MIN)
    for name in ("melee_diff",
                 "attr_str_diff", "attr_agi_diff", "attr_int_diff",
                 "attr_all_diff"):
        assert name in out, f"колонка {name} не отдана"
