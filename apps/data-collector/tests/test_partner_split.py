"""Доля потока держится за напарником, только пока он собирает (спринт 196).

ЖИВОЙ ОТКАЗ 6–7 сентября 2026. Поток публичных матчей делился пополам
между OpenDota и STRATZ, а включалось деление по ФАКТУ НАЛИЧИЯ
`STRATZ_API_TOKEN`. Токен протух: STRATZ отвечал 403 каждый цикл сутки
подряд, контейнер при этом стоял `Up 30 hours`, и половина кандидатов не
собиралась НИКЕМ. Со стороны всё было здорово — второй источник честно
отрабатывал свою половину, жаловаться ему было не на что, и ни один
сторож на это не смотрел.

Проверялось присутствие настройки, а не пригодность источника.

ПО ЧЕМУ СУДИМ ТЕПЕРЬ. По собранным матчам, а не по процессу, логам или
расходу квоты. Расход не годится в принципе: STRATZ потратил 52 вызова за
сутки, ни один из которых ничем не кончился, — счётчик попыток выглядит
как жизнь. `CollectedMatches` хранит РЕЗУЛЬТАТ, и вопрос «принёс ли
напарник хоть один матч» отвечает ровно на то, что нас волнует.
"""
import pathlib
import sys
from datetime import datetime, timedelta, timezone

SRC = pathlib.Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

from collector.sources import PartnerSplit, SourceSplit  # noqa: E402

NOW = datetime(2026, 9, 7, 12, 0, tzinfo=timezone.utc)
WINDOW_S = 6 * 3600

# Идентификаторы подобраны так, что половины РАЗНЫЕ: SourceSplit делит по
# (match_id // 10) % 2, поэтому 8000000000 достаётся доле 0, а 8000000010 —
# доле 1. Фикстура, где обе половины совпадают, скрыла бы всё, что здесь
# проверяется (эта ошибка уже ловилась мутацией в спринте 195).
MINE = 8000000000
PARTNERS = 8000000010


def split(partner_age_h, my_age_h, refresh_s=300.0):
    """Делитель, у которого напарник и я собирали столько часов назад."""
    def probe(name):
        age = {"me": my_age_h, "partner": partner_age_h}[name]
        return None if age is None else NOW - timedelta(hours=age)

    return PartnerSplit(SourceSplit(split_id=0, count=2),
                        my_name="me", partner_name="partner",
                        last_collected=probe, window_s=WINDOW_S,
                        refresh_s=refresh_s, clock=lambda: NOW)


def test_the_fixture_really_has_two_different_halves():
    """Страховка: подобранные id обязаны лежать в РАЗНЫХ половинах.

    Совпади они — все проверки ниже стали бы тождественно зелёными, и
    подмена доли ничего бы не меняла.
    """
    mine = SourceSplit(split_id=0, count=2)
    assert mine.accepts(MINE) and not mine.accepts(PARTNERS)


def test_a_working_partner_keeps_its_half():
    """Оба собирают — деление в силе, чужая половина не берётся.

    Ради этого деление и заводили: пока оба живы, взятие чужой половины
    означает, что обоих источников доедут одни и те же матчи, а витрина
    ReplacingMergeTree оставит вставленную последней — со строкой STRATZ
    вместо строки OpenDota, то есть без фич трека F.
    """
    s = split(partner_age_h=1, my_age_h=1)
    assert s.accepts(MINE)
    assert not s.accepts(PARTNERS)


def test_a_silent_partner_loses_its_half():
    """ГЛАВНОЕ: напарник молчит дольше окна — его половина забирается.

    Тот самый случай 6–7 сентября. Без этого половина потока не
    собирается никем, и увидеть это можно только сверив приток с тем,
    сколько матчей вообще существует.
    """
    s = split(partner_age_h=30, my_age_h=1)
    assert s.accepts(MINE)
    assert s.accepts(PARTNERS), "половина мёртвого напарника не забрана"


def test_a_partner_that_never_collected_is_silent_too():
    """Напарник, не собравший НИ ОДНОГО матча, тоже молчит.

    Именно так выглядит источник, у которого токен был недействителен с
    самого начала: строки в CollectedMatches нет вовсе. Трактовать
    отсутствие как «данных пока нет, подождём» значило бы ждать вечно.
    """
    s = split(partner_age_h=None, my_age_h=1)
    assert s.accepts(PARTNERS)


def test_the_half_comes_back_as_soon_as_the_partner_collects():
    """Отдаём сразу: одного собранного напарником матча достаточно.

    Асимметрия намеренная — забираем медленно, возвращаем быстро. Пока
    оба берут всё, они дерутся за одни матчи и портят витрину, поэтому
    задерживать возврат опаснее, чем задерживать захват.
    """
    s = split(partner_age_h=30, my_age_h=1, refresh_s=0.0)
    assert s.accepts(PARTNERS)
    s._probe = lambda name: NOW - timedelta(minutes=5)
    assert not s.accepts(PARTNERS), "половина не возвращена ожившему напарнику"


def test_a_cold_start_keeps_the_split():
    """Никто ничего не собрал — деление держится.

    У пустой базы молчат оба. Без второго условия («а я-то собираю») оба
    источника разом решили бы, что они одни, и подрались бы ровно так,
    как деление и должно предотвращать. Ошибка была бы незаметной: на
    новой машине это выглядит как обычный первый запуск.
    """
    s = split(partner_age_h=None, my_age_h=None)
    assert not s.accepts(PARTNERS)


def test_i_do_not_grab_a_half_i_cannot_collect_myself():
    """Молчу сам — чужую половину не беру, даже если напарник молчит.

    Расширяться, ничего не собирая, значит записать себе половину,
    которую точно так же не обработаешь. Хуже: когда напарник оживёт, он
    получит назад долю, всё это время простаивавшую у двоих.
    """
    s = split(partner_age_h=30, my_age_h=30)
    assert not s.accepts(PARTNERS)


def test_a_database_failure_does_not_change_the_split():
    """База молчит — расклад остаётся прежним, а не «расширяемся».

    Недоступность базы это НЕЗНАНИЕ, а не свидетельство смерти напарника.
    Расширение по ошибке чтения развело бы оба источника драться за одни
    и те же матчи ровно в тот момент, когда с системой и так что-то не
    так.
    """
    def broken(_name):
        raise RuntimeError("postgres недоступен")

    s = PartnerSplit(SourceSplit(split_id=0, count=2), my_name="me",
                     partner_name="partner", last_collected=broken,
                     window_s=WINDOW_S, refresh_s=0.0, clock=lambda: NOW)
    assert not s.accepts(PARTNERS)


def test_the_answer_is_cached_between_candidates():
    """Живость спрашивается не на каждого кандидата.

    За ответом стоит поход в базу, а `accepts` вызывается на каждый
    match_id в листинге. Без кэша проверка живости стоила бы дороже
    самого сбора.
    """
    calls = []

    def probe(name):
        calls.append(name)
        return NOW - timedelta(hours=1)

    s = PartnerSplit(SourceSplit(split_id=0, count=2), my_name="me",
                     partner_name="partner", last_collected=probe,
                     window_s=WINDOW_S, refresh_s=300.0, clock=lambda: NOW)
    for mid in range(8000000000, 8000000100):
        s.accepts(mid)
    assert len(calls) == 2, f"база опрошена {len(calls)} раз вместо двух"


# -- имена источников: дефис против подчёркивания ------------------------------

def test_the_liveness_name_is_the_one_the_source_actually_writes():
    """ГЛАВНОЕ ПРО ИМЕНА: спрашиваем тем именем, каким источник пишет.

    У одного источника ДВА имени. Процесс зовётся `opendota-timeline`
    (через дефис: так в compose, в SHARES и в ApiBudget), а в
    CollectedMatches кладёт `opendota_timeline` — это `source.name`.

    Спроси живость дефисным именем — запрос не найдёт НИЧЕГО. Напарник
    вечно выглядит мёртвым, доля забирается всегда, и деление молча
    перестаёт существовать: то самое, от чего спринт 196 и лечит,
    вернулось бы через опечатку в одном символе. Проверяется сверкой с
    настоящим объектом источника, а не со вторым списком имён.
    """
    import os

    os.environ.setdefault("STRATZ_API_TOKEN", "dummy-for-construction")
    from collector.__main__ import COLLECTED_AS, build_source

    for process_name, collected_as in COLLECTED_AS.items():
        assert build_source(process_name).name == collected_as, (
            f"{process_name}: живость спрашивается по имени {collected_as!r}, "
            f"а источник пишет {build_source(process_name).name!r}")


def test_every_partner_has_a_collected_name():
    """Обе стороны пары умеют быть спрошенными.

    Пара без имени в `COLLECTED_AS` уронила бы сборку делителя KeyError'ом
    при старте — громко, но уже на живой машине.
    """
    from collector.__main__ import COLLECTED_AS, LISTING_PARTNERS

    for name, partner in LISTING_PARTNERS.items():
        assert name in COLLECTED_AS and partner in COLLECTED_AS
