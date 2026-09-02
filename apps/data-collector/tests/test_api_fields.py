"""Инвентаризация полей OpenDota держится в актуальном виде (спринт 184).

ЗАЧЕМ. За один вызов API отдаёт 43 поля матча и 149 на игрока, а читаем
мы двадцать с небольшим. Остальное оплачено тем же вызовом и выброшено.
Понять глазами, что именно выброшено, нельзя — полей почти двести.

Форма та же, что у списка таблиц в спринте 156: поле обязано лежать в
ОДНОМ из трёх списков, незнакомое роняет тест. Так «мы про это поле не
подумали» перестаёт быть неотличимым от «мы его сознательно не берём».
"""
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

import pytest  # noqa: E402

from collector import api_fields as A  # noqa: E402


def test_every_field_is_accounted_for():
    """ГЛАВНОЕ: незнакомых полей нет.

    Незнакомое поле — это не «лишнее», а «мы про него не подумали».
    Разница в том, что первое безобидно, а второе однажды окажется тем
    самым, которого не хватало модели.
    """
    inv = A.inventory()
    for scope, groups in inv.items():
        assert not groups["НЕ РАЗОБРАНО"], (
            f"{scope}: поля вне трёх списков: {groups['НЕ РАЗОБРАНО']}")


def test_the_fixture_comes_from_a_live_response():
    """Состав ответа снят с ЖИВОГО API, а не выписан по памяти.

    Перечень по памяти — ровно та ошибка, ради поимки которой модуль и
    написан: я сам утверждал, что `life_state` даст поминутное «жив/
    мёртв», а он оказался сводкой.
    """
    data = json.loads(A.FIXTURE.read_text(encoding="utf-8"))
    assert data["match_id"] > 0, "фикстура не привязана к настоящему матчу"
    assert data["снято"], "не указано, когда снят состав"
    assert len(data["player"]) > 100, "ответ игрока подозрительно беден"


def test_nothing_is_in_two_lists_at_once():
    """Поле лежит РОВНО в одном списке.

    Иначе «берём» и «не берём» уживались бы рядом, и отчёт врал бы, не
    падая.
    """
    for used, cand, skipped, scope in (
            (A.MATCH_USED, A.MATCH_CANDIDATE, A.MATCH_SKIPPED, "матч"),
            (A.PLAYER_USED, A.PLAYER_CANDIDATE, A.PLAYER_SKIPPED, "игрок")):
        pairs = (("used/candidate", used, cand),
                 ("used/skipped", used, skipped),
                 ("candidate/skipped", cand, skipped))
        for name, a, b in pairs:
            both = set(a) & set(b)
            assert not both, f"{scope}: {name} пересекаются: {sorted(both)}"


def test_every_skip_has_a_reason():
    """У каждого пропуска есть причина, и она не отписка.

    Без причины через полгода никто не вспомнит, почему поле лежит
    здесь, — и его либо возьмут зря, либо побоятся трогать.
    """
    for scope, skipped in (("матч", A.MATCH_SKIPPED),
                           ("игрок", A.PLAYER_SKIPPED)):
        for field, why in skipped.items():
            assert len(why) > 15, f"{scope}.{field}: причина слишком куцая: {why!r}"


def test_every_candidate_says_what_it_gives():
    """Кандидат объясняет, ЧТО он даст модели.

    Список кандидатов — план работ, а не свалка «интересного». Строка
    без обещания превращает его в список того, что кто-то когда-то
    заметил.
    """
    for scope, cand in (("матч", A.MATCH_CANDIDATE),
                        ("игрок", A.PLAYER_CANDIDATE)):
        for field, why in cand.items():
            assert len(why) > 15, f"{scope}.{field}: не сказано, что даёт"


def test_the_used_list_matches_what_the_code_reads():
    """USED — это то, что код ЧИТАЕТ, а не то, что мы про него думаем.

    Разъедься они — инвентаризация показывала бы как используемое поле,
    которое никто не берёт, и наоборот. Проверяется по самому коду
    сигналов, а не по второму перечню.
    """
    src = (pathlib.Path(__file__).resolve().parents[1] / "src" / "collector")
    text = "\n".join((src / n).read_text(encoding="utf-8")
                     for n in ("signals.py", "timeline_runner.py"))
    missing = [f for f in A.PLAYER_USED
               if f not in text and f not in ("account_id", "rank_tier")]
    assert not missing, (
        f"объявлены используемыми, но в коде не встречаются: {missing}")


@pytest.mark.parametrize("field", ["lh_t", "dn_t", "hero_damage_t",
                                  "hero_healing_t", "camps_stacked_t"])
def test_the_cheap_per_minute_series_are_taken(field):
    """Готовые поминутные ряды ВЗЯТЫ (спринт 185).

    Это было самое дешёвое из неиспользованного: те же массивы по 34
    значения, что gold_t, в том же ответе, за те же деньги. Вернуться в
    кандидаты они могут только вместе с удалением фичи — и тогда это
    осознанное решение, а не забывчивость.
    """
    assert field in A.PLAYER_USED


def test_teamfights_are_still_on_the_list():
    """Драки пока не взяты, но и не потеряны.

    Следующая волна: `teamfights` — единственное, что прямо отвечает на
    вопрос «кто выигрывает бои». Модель знает только, кто богаче.
    """
    assert "teamfights" in A.MATCH_CANDIDATE


def test_a_new_field_is_actually_noticed():
    """Незнакомое поле ПОПАДАЕТ в «НЕ РАЗОБРАНО».

    Поймано мутацией: проверка «список пуст» проходит и на коде, где
    список пуст ВСЕГДА. Такая инвентаризация выглядела бы исправной и
    молча пропускала бы каждое новое поле API — то есть ровно то, ради
    чего написана.
    """
    fields = json.loads(A.FIXTURE.read_text(encoding="utf-8"))
    fields["player"] = list(fields["player"]) + ["совершенно_новое_поле"]
    inv = A.inventory(fields)
    assert "совершенно_новое_поле" in inv["игрок"]["НЕ РАЗОБРАНО"]


def test_a_known_field_does_not_land_in_the_unknown_list():
    """А знакомое туда не попадает.

    Иначе предыдущую проверку удовлетворял бы код, объявляющий
    незнакомым вообще всё.
    """
    inv = A.inventory()
    assert "gold_t" in inv["игрок"]["используется"]
    assert "gold_t" not in inv["игрок"]["НЕ РАЗОБРАНО"]
