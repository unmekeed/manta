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


# -- отвергнутое напарником (спринт 204) ---------------------------------------
#
# ЗАМЕР 08.09.2026, три цикла подряд: из доли STRATZ он не смог 70%, что
# составляет 36% ВСЕГО публичного потока. Это не поломка, а следствие
# устройства деления: кандидаты берутся из листинга OpenDota, и источники
# на них НЕ РАВНОСИЛЬНЫ. OpenDota отдаст детали любого матча из своего же
# листинга; STRATZ — только тех, что успел разобрать у себя.
#
# Матч, который STRATZ забрал и не смог, до OpenDota не доходил НИКОГДА:
# фильтр доли отсекал его ещё до запроса деталей.

def split_with_declines(declined, partner_age_h=1, my_age_h=1):
    """Делитель, у которого напарник отверг перечисленные матчи."""
    def probe(name):
        age = {"me": my_age_h, "partner": partner_age_h}[name]
        return None if age is None else NOW - timedelta(hours=age)

    return PartnerSplit(SourceSplit(split_id=0, count=2),
                        my_name="me", partner_name="partner",
                        last_collected=probe, window_s=WINDOW_S,
                        refresh_s=300.0, clock=lambda: NOW,
                        declined_by_partner=lambda _p: set(declined))


def test_a_match_the_partner_gave_up_on_is_taken():
    """ГЛАВНОЕ: отвергнутый напарником матч берётся сверх своей доли.

    Без этого он не достаётся никому — и это не единичный случай, а
    36% потока.
    """
    s = split_with_declines({PARTNERS})
    assert s.accepts(PARTNERS), (
        "матч, который напарник забрал и не смог, снова потерян")


def test_the_rest_of_the_partner_share_stays_with_the_partner():
    """Берём только СДАВШИЕСЯ матчи, а не всю чужую долю.

    Иначе деление перестало бы существовать, и оба источника писали бы в
    витрину одни и те же матчи — ReplacingMergeTree оставит вставленную
    последней, и строка STRATZ затрёт строку OpenDota вместе с фичами
    трека F.
    """
    # PARTNERS_OTHER — ЕЩЁ ОДИН матч чужой доли, не отвергнутый. Шаг
    # именно 20, а не 10: деление идёт по (match_id // 10) % 2, и
    # соседняя десятка попадает в МОЮ долю. Первая редакция брала +10 и
    # проверяла бы, что я беру собственный матч, — то есть ничего.
    other = PARTNERS + 20
    assert not SourceSplit(split_id=0, count=2).accepts(other), (
        "фикстура сломана: контрольный матч попал в мою долю")

    s = split_with_declines({PARTNERS})
    assert not s.accepts(other), (
        "забрана вся доля напарника, а не только отвергнутое им")


def test_my_own_share_needs_no_lookup():
    """Свои матчи берутся без обращения к списку отказов.

    Порядок проверок не косметика: `accepts` вызывается на каждого
    кандидата, и спрашивать про свои же матчи значило бы платить за
    ответ, который известен заранее.
    """
    asked = []

    def probe(name):
        return NOW - timedelta(hours=1)

    s = PartnerSplit(SourceSplit(split_id=0, count=2), my_name="me",
                     partner_name="partner", last_collected=probe,
                     window_s=WINDOW_S, refresh_s=300.0, clock=lambda: NOW,
                     declined_by_partner=lambda p: asked.append(p) or set())
    s.accepts(MINE)
    assert len(asked) <= 1, (
        f"список отказов запрошен {len(asked)} раз на один свой матч")


def test_without_the_shared_memory_nothing_changes():
    """Без общей памяти делитель ведёт себя как прежде.

    Общая память — ускоритель, а не условие работы: машина без миграции
    (или с недоступной базой) обязана собирать так же, как до спринта
    204, а не хуже.
    """
    s = PartnerSplit(SourceSplit(split_id=0, count=2), my_name="me",
                     partner_name="partner",
                     last_collected=lambda n: NOW - timedelta(hours=1),
                     window_s=WINDOW_S, refresh_s=300.0, clock=lambda: NOW)
    assert s.accepts(MINE) and not s.accepts(PARTNERS)


def test_an_unreadable_decline_list_keeps_the_previous_one():
    """Ошибка чтения НЕ обнуляет уже известные отказы.

    Пустое множество означало бы «напарник вдруг всё смог», а недоступная
    база — это незнание. Обнулив список, мы вернули бы потерю тех самых
    36% ровно на время недоступности, и никто бы этого не заметил.
    """
    state = {"fail": False}

    def flaky(_partner):
        if state["fail"]:
            raise RuntimeError("база молчит")
        return {PARTNERS}

    s = PartnerSplit(SourceSplit(split_id=0, count=2), my_name="me",
                     partner_name="partner",
                     last_collected=lambda n: NOW - timedelta(hours=1),
                     window_s=WINDOW_S, refresh_s=0.0, clock=lambda: NOW,
                     declined_by_partner=flaky)
    assert s.accepts(PARTNERS)
    state["fail"] = True
    assert s.accepts(PARTNERS), "известные отказы забыты из-за сбоя чтения"
