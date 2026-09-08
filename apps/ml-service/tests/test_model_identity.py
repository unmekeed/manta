"""По отчёту видно, КАКАЯ модель его посчитала (спринт 212).

ЧТО СЛОМАНО. `WinProbability.version` брался из артефакта
(`art["model_version"]`) — это semver, записанный при обучении, и он
ОДИН И ТОТ ЖЕ у всех версий реестра. 08.09.2026 их было 26, и все
«0.9.0». Именно это значение уходило в PredictResponse и оседало в
`MatchReports.model_version`.

ЖИВОЙ СЛУЧАЙ, из-за которого это вскрылось. В тот же день выяснилось,
что production съехала на 2.4σ (0.1622 против 0.1598 у версии суточной
давности), и её откатили. Проверить откат по отчёту не вышло:

    8987602981|0.9.0|2026-09-08 08:07:50
    8987602915|0.9.0|2026-09-08 08:03:46

Обе строки одинаковы и до отката, и после. Вопрос «какие отчёты сделаны
съехавшей моделью» задать было НЕЧЕМ, хотя ответ на него — список того,
что стоит перегенерировать.

ЧЕГО ЭТО НЕ ЧИНИТ. Отчёты, записанные до спринта, так и остаются с
«0.9.0»: чем их посчитали, уже не восстановить. Проверка ниже про то,
чтобы это не продолжалось.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import app as app_mod  # noqa: E402


class FakeReg:
    """Реестр, отдающий веса и метаданные одним вызовом — как настоящий."""

    def __init__(self, version="0.9.0-20260907T083343Z", staged=None):
        self.version = version
        # Что вернёт ОТДЕЛЬНЫЙ опрос стейджа. По умолчанию то же самое;
        # тест про гонку задаёт другое — так выглядит промоушен,
        # случившийся между скачиванием весов и опросом.
        self.staged = staged or version
        self.resolves = 0

    def resolve(self, name, ref):
        self.resolves += 1
        return b"weights", {"registry_version": self.version,
                            "model_version": "0.9.0"}

    def stage_version(self, name, stage="production"):
        return self.staged


class FakeModel:
    def __init__(self, path):
        self.path = path
        self.version = "0.9.0"      # semver из артефакта
        self.features = ["a"]


def patched(monkeypatch, reg):
    import registry

    monkeypatch.setattr(registry, "registry_from_env", lambda: reg)
    monkeypatch.setattr(app_mod, "WinProbability", FakeModel)


def test_a_model_from_the_registry_reports_its_registry_version(monkeypatch):
    """ГЛАВНОЕ: версия модели — та, что различает версии реестра.

    Semver «0.9.0» одинаков у всех 26 версий. Пока отчёт хранит его,
    вопрос «какая модель это посчитала» не имеет ответа, а откат нельзя
    ни проверить, ни оценить.
    """
    reg = FakeReg("0.9.0-20260907T083343Z")
    patched(monkeypatch, reg)
    model = app_mod.load_model("registry://win_probability/production")
    assert model.version == "0.9.0-20260907T083343Z", model.version


def test_the_version_comes_from_the_same_read_as_the_weights(monkeypatch):
    """Версия берётся из ТОГО ЖЕ `resolve`, что и веса.

    Спросить её отдельным `stage_version` было бы почти то же самое и
    всё-таки неверно: между скачиванием и опросом стейдж может смениться
    (промоушен идёт каждые шесть часов), и отчёт приписали бы НЕ ТОЙ
    версии. Ошибка такого рода не падает — она врёт в поле, по которому
    потом разбираются.

    Здесь воспроизводится сама гонка: `resolve` отдаёт веса версии A, а
    опрос стейджа уже отвечает B. Первая редакция теста считала ВЫЗОВЫ и
    мутацию «спросить отдельно» пережила — отдельный опрос идёт в другой
    метод, и счётчик обращений за весами не менялся.
    """
    reg = FakeReg(version="0.9.0-A", staged="0.9.0-B")
    patched(monkeypatch, reg)
    model = app_mod.load_model("registry://win_probability/production")
    assert model.version == "0.9.0-A", (
        "модель назвалась версией, которую вернул ОПРОС СТЕЙДЖА, а не та, "
        "чьи веса загружены: отчёт припишут не той модели")


def test_a_local_model_keeps_its_semver(monkeypatch):
    """Модель из файла остаётся при semver.

    Отсутствие ≠ ноль: «не из реестра» не должно выглядеть как «версия
    неизвестна». Локальный путь версии реестра не имеет по построению —
    это обычный режим тестов и ручных прогонов.
    """
    reg = FakeReg()
    patched(monkeypatch, reg)
    model = app_mod.load_model("/tmp/win_probability.pkl")
    assert model.version == "0.9.0"
    assert reg.resolves == 0, "за локальным файлом сходили в реестр"


def test_the_slot_serves_the_stamped_model(monkeypatch):
    """ПРОВОДКА: слот отдаёт модель с проставленной версией.

    Напиши `load_model` безупречно и не подставь его слоту — сервис вёл
    бы себя ровно как до спринта, а тесты выше остались бы зелёными: они
    зовут функцию напрямую. В спринте 195 ровно так и вышло — функция
    была верна, вызывать её забыли.
    """
    reg = FakeReg("0.9.0-20260907T083343Z")
    patched(monkeypatch, reg)
    slot = app_mod.ModelSlot("registry://win_probability/production")
    model = slot.get()
    assert model is not None
    assert model.version == "0.9.0-20260907T083343Z", (
        "слот отдал модель с semver — значит грузит не через load_model")


def test_the_registry_version_is_distinguishable_from_the_semver():
    """Страховка от проверки, которая ничего не различает.

    Совпади две формы записи — все утверждения выше проходили бы и на
    сломанном коде. Версия реестра ОБЯЗАНА отличаться от semver: она
    строится как `<semver>-<метка времени прогона>` (registry/store.py).
    """
    assert FakeReg().version != FakeModel("x").version
