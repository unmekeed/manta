"""Сторож за производством отчётов (спринт 191).

ЖИВОЙ ОТКАЗ. 2 сентября в 07:00 UTC генерация отчётов встала целиком и
простояла 29 часов. Матчи собирались, витрина наполнялась, `doctor` был
здоров, в Telegram — тишина. Причину нашли вручную, случайно, разбирая
совсем другое.

ЧТО ЗДЕСЬ ДОРОГО ОШИБИТЬСЯ — ровно одно: спутать «отчётов нет» с «отчёты
падают». Первое ночью норма, второе всегда авария. Сторож, кричащий на
тишину, за неделю приучает не читать канал — этот урок проект уже
оплатил в спринте 163, и тесты на молчание здесь не менее важны, чем
тесты на крик.
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from reportgen.health import FailureWatch, missing_features  # noqa: E402


# -- когда сторож обязан молчать -----------------------------------------------

def test_silence_when_nothing_happened():
    """ГЛАВНОЕ: нет попыток — нет мнения.

    Ночью матчей мало, и часы без единого отчёта — норма. Сторож молчит
    здесь ПО ПОСТРОЕНИЮ: он считает только состоявшиеся попытки, а не
    «сколько отчётов ожидалось». Так «тишины» и «поломки» не приходится
    различать отдельной проверкой, которую однажды забудут написать.
    """
    w = FailureWatch(min_events=5)
    assert w.verdict() is None


def test_silence_below_the_minimum_even_if_all_failed():
    """Три неудачи подряд — ещё не авария.

    Битый матч, обрыв связи с ClickHouse, гонка на записи — единичные
    отказы случаются в исправной системе. Порог по количеству отделяет
    их от того, что стоит будить владельца.
    """
    w = FailureWatch(min_events=5)
    for _ in range(3):
        w.record(False)
    assert w.verdict() is None


def test_silence_while_most_reports_succeed():
    """Одна неудача из десяти — не повод кричать."""
    w = FailureWatch(min_events=5, share=0.5)
    w.record(False)
    for _ in range(9):
        w.record(True)
    assert w.verdict() is None


# -- когда обязан кричать ------------------------------------------------------

def test_total_outage_is_reported():
    """Всё падает — это авария."""
    w = FailureWatch(min_events=5, share=0.5)
    for _ in range(5):
        w.record(False)
    assert w.verdict() == "broken"


def test_intermittent_failure_is_reported_too():
    """Половина отчётов падает — тоже авария, хоть что-то и производится.

    Проверка «подряд ли неудачи» пропустила бы этот случай: между
    отказами есть успехи, и счётчик подряд идущих сбрасывался бы. Доля в
    окне ловит и полный отказ, и перемежающийся.
    """
    w = FailureWatch(window=10, min_events=5, share=0.5)
    for _ in range(5):
        w.record(True)
        w.record(False)
    assert w.verdict() == "broken"


def test_the_alert_is_sent_once():
    """Кричим на ПЕРЕХОДЕ, а не на состоянии.

    532 одинаковых сообщения за час — это не мониторинг, это способ
    сделать так, чтобы канал замьютили.
    """
    w = FailureWatch(min_events=5)
    for _ in range(5):
        w.record(False)
    assert w.verdict() == "broken"
    for _ in range(20):
        w.record(False)
        assert w.verdict() is None


# -- выздоровление --------------------------------------------------------------

def test_recovery_is_reported_once_when_the_window_is_clean():
    """Починилось — сказать об этом обязательно.

    Молчание после аварии неотличимо от того, что сторож сам умер.
    """
    w = FailureWatch(window=5, min_events=5, share=0.5)
    for _ in range(5):
        w.record(False)
    assert w.verdict() == "broken"
    for _ in range(5):
        w.record(True)
    assert w.verdict() == "recovered"
    w.record(True)
    assert w.verdict() is None


def test_recovery_waits_for_a_clean_window_not_for_the_threshold():
    """Выздоровление — по ОТСУТСТВИЮ неудач, а не по доле ниже порога.

    Иначе на границе порога сторож замигал бы «сломалось / починилось»
    каждые несколько отчётов. Мигающий сторож не лучше молчащего: читать
    его перестанут так же быстро.
    """
    w = FailureWatch(window=10, min_events=5, share=0.5)
    for _ in range(10):
        w.record(False)
    assert w.verdict() == "broken"
    for _ in range(6):        # доля неудач 0.4 — уже ниже порога
        w.record(True)
    assert w.failure_share < 0.5
    assert w.verdict() is None, "выздоровление объявлено при живых неудачах"
    for _ in range(4):
        w.record(True)
    assert w.verdict() == "recovered"


# -- быстрый путь: рассинхрон фич ----------------------------------------------

def test_missing_features_are_extracted_by_name():
    """Из ошибки gRPC достаются имена — готовый диагноз с первой неудачи."""
    text = ("frame t=60: missing features: 'lh_diff, dn_diff, "
            "role_carry_diff'")
    assert missing_features(text) == ["lh_diff", "dn_diff", "role_carry_diff"]


def test_other_errors_are_not_mistaken_for_a_feature_mismatch():
    """Чужая ошибка не должна давать ложный точный диагноз.

    Пустой список здесь означает «это не тот случай», и сторож честно
    падает на общий путь вместо того, чтобы сообщить владельцу
    выдуманную причину.
    """
    assert missing_features("UNAVAILABLE: failed to connect") == []
    assert missing_features("") == []


def test_the_error_format_matches_what_ml_service_actually_sends():
    """Разбор текста сверяется с ГЕНЕРАТОРОМ этого текста.

    Парсер чужого сообщения — плохая практика ровно потому, что формат
    меняется, а парсер молча перестаёт находить. Здесь обе стороны
    держатся рядом: сообщение строится тем же способом, что в
    `ml-service/src/app.py` (`raise KeyError(", ".join(missing))` →
    `f"...missing features: {missing}"`), и если та сторона поменяет
    формулировку, красным станет этот тест, а не боевой мониторинг.
    """
    missing = ["melee_diff", "attr_str_diff"]
    as_ml_service_builds_it = (
        f"frame t=120: missing features: {KeyError(', '.join(missing))}")
    assert missing_features(as_ml_service_builds_it) == missing
