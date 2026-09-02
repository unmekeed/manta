"""Детектор застрявшего гейта продвижения (спринт 174).

ЗАЧЕМ ОН ЕСТЬ. Гейт может честно отклонять кандидата циклами подряд — и
это НЕ отказ, он ровно для того и стоит. Беда в том, что со стороны
застой неотличим от исправной работы: в обоих случаях автообучение
крутится, метрики публикуются, ошибок нет.

ЖИВОЙ СЛУЧАЙ 2026-09-01. Четыре цикла подряд «NOT promoted», а запросы
обслуживала модель, обученная 25 августа. Заметно это стало только
потому, что владелец руками заглянул в лог, — то есть система про своё
застревание не сказала ничего.

ЧТО ЗДЕСЬ ДОРОГО ОШИБИТЬСЯ. Алерт на один отказ был бы ложной тревогой:
гейт обязан отклонять слабых кандидатов, и делает это регулярно. Алерт
на возраст сам по себе — тоже: свежая production может просто не
требовать замены. Новость — СОЧЕТАНИЕ, и различить эти три случая
умеет только проверка обоих признаков сразу.
"""
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from training import auto


class FakeNotifier:
    enabled = True

    def __init__(self):
        self.sent: list[str] = []

    def send(self, text):
        self.sent.append(text)
        return True


def _reset(monkeypatch, notifier):
    monkeypatch.setattr(auto, "_gate_rejections", 0)
    monkeypatch.setattr(auto, "_gate_alerted", False)
    monkeypatch.setattr(auto, "_notifier", notifier)


def prod(hours_ago: float | None):
    """Метаданные production, обученной столько часов назад."""
    if hours_ago is None:
        return {}
    when = datetime.now(timezone.utc) - timedelta(hours=hours_ago)
    return {"trained_at": when.isoformat()}


# -- когда молчать --------------------------------------------------------------

def test_a_single_rejection_is_not_an_alert(monkeypatch):
    """Один отказ — работа гейта, а не поломка.

    Он отклоняет слабых кандидатов регулярно, и сообщать о каждом значит
    завести шум, который научит не читать канал.
    """
    n = FakeNotifier()
    _reset(monkeypatch, n)
    monkeypatch.setenv("GATE_FROZEN_ALERT_H", "72")
    auto._check_gate_freeze(prod(2), promoted=False)
    assert n.sent == []
    assert auto.GATE_FROZEN._value.get() == 0


def test_an_old_production_alone_is_not_an_alert(monkeypatch):
    """Старая production при удачном продвижении — тоже не новость.

    Возраст считается у той версии, что обслуживает запросы; сразу после
    продвижения он мал, но даже если бы был велик — продвижение
    состоялось, значит гейт не застрял.
    """
    n = FakeNotifier()
    _reset(monkeypatch, n)
    monkeypatch.setenv("GATE_FROZEN_ALERT_H", "72")
    auto._check_gate_freeze(prod(500), promoted=True)
    assert n.sent == []
    assert auto.GATE_FROZEN._value.get() == 0


def test_an_unknown_age_does_not_alert(monkeypatch):
    """Не смогли узнать возраст — молчим, а не пугаем.

    «Не знаю» и «плохо» — разные вещи, и подменять первое вторым значит
    вызывать владельца на исправную систему.
    """
    n = FakeNotifier()
    _reset(monkeypatch, n)
    for meta in ({}, {"trained_at": "не-дата"}, None):
        auto._check_gate_freeze(meta, promoted=False)
    assert n.sent == []


def test_an_unknown_age_is_not_reported_as_zero(monkeypatch):
    """И метрика возраста при этом НЕ ноль.

    Ноль читался бы как «только что обучена» — самый благополучный
    ответ на вопрос, ответа на который у нас нет.
    """
    n = FakeNotifier()
    _reset(monkeypatch, n)
    auto._check_gate_freeze({}, promoted=False)
    assert auto.PROD_AGE_H._value.get() != 0


# -- когда звать ----------------------------------------------------------------

def test_rejections_plus_an_old_production_alert(monkeypatch):
    """ГЛАВНОЕ утверждение спринта: сочетание — это новость."""
    n = FakeNotifier()
    _reset(monkeypatch, n)
    monkeypatch.setenv("GATE_FROZEN_ALERT_H", "72")
    auto._check_gate_freeze(prod(100), promoted=False)
    assert len(n.sent) == 1
    assert "застрял" in n.sent[0]
    assert auto.GATE_FROZEN._value.get() == 1


def test_the_alert_fires_once_per_episode(monkeypatch):
    """Повторять его каждый цикл нельзя.

    Автообучение крутится непрерывно; ежецикловое напоминание об одном и
    том же превратилось бы в тот самый шум, от которого алерт и защищает.
    """
    n = FakeNotifier()
    _reset(monkeypatch, n)
    monkeypatch.setenv("GATE_FROZEN_ALERT_H", "72")
    for _ in range(5):
        auto._check_gate_freeze(prod(100), promoted=False)
    assert len(n.sent) == 1


def test_a_promotion_rearms_the_alert_and_says_so(monkeypatch):
    """Разморозка — тоже событие, и о ней сообщают.

    Иначе владелец, получивший тревогу, не узнает, что всё прошло, и
    будет проверять руками — то есть алерт создаст работу вместо того,
    чтобы её снять.
    """
    n = FakeNotifier()
    _reset(monkeypatch, n)
    monkeypatch.setenv("GATE_FROZEN_ALERT_H", "72")
    auto._check_gate_freeze(prod(100), promoted=False)
    auto._check_gate_freeze(prod(0), promoted=True)
    assert len(n.sent) == 2 and "снова продвигает" in n.sent[1]
    # И следующий застой снова позовёт: эпизод закрыт, а не заглушен.
    auto._check_gate_freeze(prod(100), promoted=False)
    assert len(n.sent) == 3


def test_the_alert_says_what_to_look_at(monkeypatch):
    """Сообщение объясняет, как читать отказ.

    «Гейт застрял» без продолжения — это приглашение лезть в код. Две
    оценки из спринта 174 дают готовую развилку, и назвать её стоит
    одной строки.
    """
    n = FakeNotifier()
    _reset(monkeypatch, n)
    monkeypatch.setenv("GATE_FROZEN_ALERT_H", "72")
    auto._check_gate_freeze(prod(100), promoted=False)
    assert "про-эталон" in n.sent[0] and "валидации" in n.sent[0]


# -- счётчики -------------------------------------------------------------------

def test_consecutive_rejections_are_counted_and_reset(monkeypatch):
    """Счётчик считает ПОДРЯД, а не всего.

    Общее число отказов за жизнь процесса ничего не говорит о том,
    застрял ли гейт сейчас: у здоровой системы оно тоже растёт.
    """
    n = FakeNotifier()
    _reset(monkeypatch, n)
    monkeypatch.setenv("GATE_FROZEN_ALERT_H", "72")
    for _ in range(3):
        auto._check_gate_freeze(prod(1), promoted=False)
    assert auto.GATE_REJECTIONS._value.get() == 3
    auto._check_gate_freeze(prod(0), promoted=True)
    assert auto.GATE_REJECTIONS._value.get() == 0


def test_the_age_metric_follows_the_serving_model(monkeypatch):
    """Метрика возраста показывает обслуживающую модель.

    Показывать возраст отклонённого кандидата значило бы рапортовать о
    свежести того, что никого не обслуживает.
    """
    n = FakeNotifier()
    _reset(monkeypatch, n)
    auto._check_gate_freeze(prod(50), promoted=False)
    assert 49 < auto.PROD_AGE_H._value.get() < 51


def test_a_naive_timestamp_is_read_as_utc(monkeypatch):
    """Метка без часового пояса — UTC, а не местное время.

    Прочитать её как местное значило бы получить возраст, смещённый на
    пояс машины: на VPS и дома по-разному, и одна из двух картин была бы
    неверной.

    Пояс машины подменяется НАРОЧНО. Контейнеры проекта живут в UTC, где
    `replace(tzinfo=utc)` и `astimezone(utc)` дают одно и то же, — и
    мутация «читать как местное» проходила тест незамеченной. Поймано
    мутацией: без чужого пояса эта проверка ничего не проверяла.
    """
    n = FakeNotifier()
    _reset(monkeypatch, n)
    monkeypatch.setenv("TZ", "Asia/Tokyo")     # UTC+9, без перехода на летнее
    time.tzset()
    try:
        naive = (datetime.now(timezone.utc)
                 - timedelta(hours=10)).replace(tzinfo=None).isoformat()
        age = auto._prod_age_hours({"trained_at": naive})
        assert 9.5 < age < 10.5, age
    finally:
        monkeypatch.delenv("TZ", raising=False)
        time.tzset()


# -- место вызова ---------------------------------------------------------------

def test_the_age_is_taken_from_production_when_the_candidate_is_rejected(
        monkeypatch):
    """При отказе возраст берётся у PRODUCTION, а не у кандидата.

    Кандидат только что обучен, ему всегда ноль часов. Возьми возраст у
    него — и «застрявший гейт» стал бы недостижимым состоянием: метрика
    вечно показывала бы свежесть той модели, которую как раз и НЕ
    продвинули. Детектор существовал бы, но не срабатывал никогда.

    Поймано мутацией: юнит-тесты самой функции этого не видят, ошибка
    живёт в МЕСТЕ ВЫЗОВА.
    """
    import inspect

    src = inspect.getsource(auto.check_and_train)
    assert "_check_gate_freeze(artifact if promoted else prod, promoted)" in src, (
        "возраст считается не у той версии, что обслуживает запросы")


def test_the_alert_is_not_sent_when_notifications_are_off(monkeypatch):
    """Выключенный Telegram не мешает считать метрики.

    Метрики нужны и без уведомлений — их читает дашборд. Связать одно с
    другим значило бы потерять и метрику вместе с каналом.
    """
    class Off:
        enabled = False

        def send(self, text):        # pragma: no cover — не должно звать
            raise AssertionError("послали при выключенном уведомлении")

    _reset(monkeypatch, Off())
    monkeypatch.setenv("GATE_FROZEN_ALERT_H", "72")
    auto._check_gate_freeze(prod(100), promoted=False)
    assert auto.GATE_FROZEN._value.get() == 1
    assert auto.GATE_REJECTIONS._value.get() == 1
