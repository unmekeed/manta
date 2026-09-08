"""Храповик гейта: планка, которая не съезжает вместе с production (207).

ЧТО ИМЕННО ПРОВЕРЯЕТСЯ. Не «есть ли в гейте сравнение с якорем» — такое
проверил бы и grep, — а то, ради чего якорь заведён: ЦЕПОЧКА ничтожных по
отдельности ухудшений обязана быть остановлена. Ровно это и произошло на
живой машине 07.09.2026, когда про-эталон production прошёл 0.1553 →
0.1565 → 0.1565 → 0.1578 четырьмя шагами, каждый «в пределах шума».

ПОЧЕМУ ЧИСЛА, А НЕ ОБУЧЕНИЕ. Настоящие модели на синтетике расходятся
случайно, и цепочка «каждый шаг чуть хуже» получилась бы у них не
всегда. Тогда тест проверял бы везение. Здесь предсказания задаются
прямо: модель v предсказывает истину, сдвинутую к 0.5 на e_v, поэтому её
Brier равен ровно e_v², σ бутстрапа равна нулю, и допуск гейта известен
до запуска. Сдвиги подобраны так, что КАЖДЫЙ шаг проходит сравнение с
предыдущим, а сумма двух — нет.
"""
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from training import ratchet  # noqa: E402

TOL = 0.001

# Brier каждой версии: 0.0100, затем шаги по +0.0008 — вдвое меньше
# допуска поодиночке и вдвое больше него в сумме двух.
BRIERS = [0.0100, 0.0108, 0.0116, 0.0124]
SHIFTS = {f"v{i}": float(np.sqrt(b)) for i, b in enumerate(BRIERS)}

# Четыре матча по две строки. Строк и матчей РАЗНОЕ число намеренно: с
# одной строкой на матч мутация «считать по строкам» прошла бы мимо.
Y = np.array([1, 0, 1, 0, 1, 0, 1, 0])
GROUPS = np.array([1, 1, 2, 2, 3, 3, 4, 4])
X = np.zeros((len(Y), 1))


def preds(name: str) -> np.ndarray:
    """Истина, сдвинутая к 0.5 на e: Brier получается ровно e²."""
    e = SHIFTS[name]
    return np.where(Y == 1, 1.0 - e, e)


class OneHoldout:
    """Датасет с единственной сопоставимой выборкой."""

    def eval_holdouts(self):
        return [(X, Y, GROUPS, "benchmark_pro")]


@pytest.fixture
def gate(monkeypatch):
    """evaluate_gate, где предсказания версии задаются её именем."""
    from training import train_winprob as tw

    monkeypatch.setattr(tw, "predict_calibrated",
                        lambda art, _X: preds(art["name"]))
    return tw


def art(name: str) -> dict:
    return {"name": name}


# -- ГЛАВНОЕ ------------------------------------------------------------------

def test_a_chain_of_insignificant_losses_is_stopped(gate):
    """РАДИ ЭТОГО ВСЁ И ЗАТЕВАЛОСЬ: сумма «незначимых» шагов отклоняется.

    Каждый кандидат хуже предыдущего прода на +0.0008 при допуске 0.001,
    то есть по отдельности проходит. Якорь остаётся на v0, и второй шаг
    даёт против него +0.0016 — отказ.

    Проверяется и то, что отказ наступает НЕ РАНЬШЕ: храповик, режущий
    первый же шаг, остановил бы и законное обновление модели.
    """
    champion, prod, verdicts = art("v0"), art("v0"), []
    for i in (1, 2, 3):
        cand = art(f"v{i}")
        ok, reason = gate.evaluate_gate(cand, prod, OneHoldout(),
                                        tol_floor=TOL, champion=champion)
        verdicts.append(ok)
        if ok:
            prod = cand

    assert verdicts[0] is True, (
        "храповик отклонил первый шаг — он режет и законное обновление")
    assert verdicts[1] is False, (
        "цепочка «незначимых» ухудшений прошла: ровно та беда, ради "
        "которой якорь и заводился")


def test_without_the_anchor_the_same_chain_passes_entirely(gate):
    """Контроль: без якоря та же цепочка проходит вся.

    Без него предыдущий тест был бы зелёным и при полностью выключенном
    храповике — достаточно, чтобы кандидаты просто оказались плохи.
    Разница между двумя тестами и есть вклад спринта.
    """
    prod = art("v0")
    for i in (1, 2, 3):
        cand = art(f"v{i}")
        ok, _ = gate.evaluate_gate(cand, prod, OneHoldout(), tol_floor=TOL)
        assert ok, f"шаг {i} не прошёл даже сравнение с prod — фикстура не та"
        prod = cand


def test_the_rejection_says_it_was_the_ratchet(gate):
    """Отказ называет причину, и называет её ПЕРВОЙ.

    «Не хуже prod, но отклонён» без объяснения выглядит поломкой гейта.
    Читатель приходит сюда, когда у него меньше всего времени, и порядок
    строк — это порядок, в котором он будет думать.
    """
    ok, reason = gate.evaluate_gate(art("v3"), art("v2"), OneHoldout(),
                                    tol_floor=TOL, champion=art("v0"))
    assert ok is False
    assert reason.startswith("ХРАПОВИК"), reason
    assert "якорь" in reason


def test_a_candidate_better_than_the_anchor_passes(gate):
    """Якорь не запрещает улучшаться.

    Планка держит не старую модель, а лучший результат: кандидат лучше
    якоря проходит, каким бы старым тот ни был.
    """
    ok, reason = gate.evaluate_gate(art("v0"), art("v3"), OneHoldout(),
                                    tol_floor=TOL, champion=art("v3"))
    assert ok is True, reason


# -- движение планки ----------------------------------------------------------

def test_the_bar_rises_only_on_a_proven_improvement(gate):
    """Планка движется по ДОКАЗАННОМУ улучшению, а не по ничьей.

    Подними её на «не хуже» — и храповик завёлся бы заново, только с
    лишним шагом: каждый ничтожный проигрыш переносил бы точку отсчёта.
    """
    beat: list = []
    gate.evaluate_gate(art("v1"), art("v0"), OneHoldout(), tol_floor=TOL,
                       champion=art("v0"), beat_champion=beat)
    assert beat == [], "планка поднялась на ухудшении"

    beat.clear()
    gate.evaluate_gate(art("v0"), art("v3"), OneHoldout(), tol_floor=TOL,
                       champion=art("v3"), beat_champion=beat)
    assert len(beat) == 1, "доказанное улучшение не подняло планку"


def test_the_ratchet_judgement_reaches_the_history(gate):
    """Оценка по якорю попадает в журнал и помечена как якорная.

    Без пометки строка неотличима от сравнения с prod, и вопрос
    «насколько съехала планка за десять промоушенов» снова стал бы
    археологией — тем, чем он был, когда храповик обнаружили.
    """
    holdouts: list = []
    gate.evaluate_gate(art("v1"), art("v0"), OneHoldout(), tol_floor=TOL,
                       champion=art("v0"), holdouts=holdouts)
    marked = [h for h in holdouts if h.get("ratchet")]
    assert len(marked) == 1, holdouts
    assert marked[0]["ref"] == "якорь"
    assert marked[0]["brier_prod"] == pytest.approx(BRIERS[0], abs=1e-6)


# -- отсутствие якоря ---------------------------------------------------------

def test_a_missing_anchor_does_not_freeze_training(gate):
    """Недоступный якорь — отсутствие проверки, а не отказ.

    Отклонять всё, пока не починят хранилище, значит заморозить модель на
    неопределённый срок из-за беды, к качеству модели отношения не
    имеющей.
    """
    ok, reason = gate.evaluate_gate(art("v1"), art("v0"), OneHoldout(),
                                    tol_floor=TOL, champion=None,
                                    champion_note="якорь v0-x недоступен")
    assert ok is True, reason


def test_a_missing_anchor_says_so_out_loud(gate):
    """ОТСУТСТВИЕ ≠ НОЛЬ: «не применён» выглядит иначе, чем «пройден».

    Молчаливый откат к поведению до спринта 207 — ровно та форма отказа,
    которая в этом проекте обходилась дороже всего: проверки нет, а
    объяснение гейта не отличить от того, где она была.
    """
    _, reason = gate.evaluate_gate(art("v1"), art("v0"), OneHoldout(),
                                   tol_floor=TOL, champion=None,
                                   champion_note="якорь v0-x недоступен")
    assert "храповик не применён" in reason, reason
    assert "v0-x" in reason, "не сказано, какой именно якорь недоступен"


# -- работа с реестром --------------------------------------------------------

class FakeReg:
    def __init__(self, stages=None, blobs=None, fail_resolve=False):
        self.stages = dict(stages or {})
        self.blobs = dict(blobs or {})
        self.fail_resolve = fail_resolve
        self.resolved = []
        self.promoted = []
        self.pushed = []

    def push(self, name, blob, metadata):
        version = f"cand-{len(self.pushed)}"
        self.pushed.append(version)
        self.blobs[version] = blob
        return version

    def stage_version(self, name, stage="production"):
        return self.stages.get(stage)

    def resolve(self, name, ref):
        # Настоящий реестр принимает и стейдж, и точную версию. Заглушка,
        # понимающая только версии, отправила бы `resolve(…, "production")`
        # в KeyError — то есть проверяла бы ветку «прода нет» вместо той,
        # ради которой написана.
        version = self.stages.get(ref, ref)
        self.resolved.append(version)
        if self.fail_resolve:
            raise KeyError("нет такого объекта")
        return self.blobs[version], {}

    def promote(self, name, version, stage="production"):
        self.promoted.append((version, stage))
        self.stages[stage] = version


def test_an_anchor_equal_to_production_is_not_downloaded():
    """Совпадающий с prod якорь не скачивается.

    Кандидат уже сравнивается с production; второй такой же прогон стоил
    бы скачивания весов из MinIO ради заведомо того же ответа. Проверяется
    ОТСУТСТВИЕ обращения, а не отсутствие слова в коде.
    """
    reg = FakeReg(stages={"champion": "v7", "production": "v7"})
    champ, note = ratchet.load_champion(reg, "win_probability", "v7", "v7")
    assert champ is None
    assert reg.resolved == [], "якорь скачан впустую"
    assert "совпадает с production" in note


def test_an_unreadable_anchor_becomes_a_note_not_an_exception():
    """Недоступный артефакт якоря не роняет обучение и называет себя."""
    reg = FakeReg(stages={"champion": "v7"}, fail_resolve=True)
    champ, note = ratchet.load_champion(reg, "win_probability", "v7", "v9")
    assert champ is None
    assert "v7" in note and "недоступен" in note


def test_a_missing_stage_does_not_raise():
    """Реестр, падающий на чтении стейджа, не роняет обучение."""
    class Broken:
        def stage_version(self, name, stage="production"):
            raise RuntimeError("MinIO недоступен")

    assert ratchet.stage_version(Broken(), "win_probability", "champion") is None


def test_setting_the_bar_never_breaks_a_promotion():
    """Неудача записи планки НЕ отменяет продвижение модели.

    Модель — продукт, планка — учёт. Обратный порядок приоритетов означал
    бы, что мелкая беда со вспомогательным указателем стоит пользователю
    хорошей модели.
    """
    class Broken:
        def promote(self, name, version, stage="production"):
            raise RuntimeError("MinIO недоступен")

    assert ratchet.set_champion(Broken(), "win_probability", "v1") is False


# -- проводка: гейт действительно зовёт храповик -------------------------------
#
# Проверяется ВЫЗОВ, а не наличие кода. В спринте 195 функция `_announce`
# существовала и была написана верно, но `collect_once` её не вызывал, и
# ни один тест этого не замечал: сбор «работал», события не публиковались.

def push(monkeypatch, tmp_path, reg, verdict=(True, "ok"), beats=False):
    """push_with_gate на подставном реестре; гейт подменён."""
    import logging

    import registry
    from training import history
    from training import train_winprob as tw

    monkeypatch.setattr(registry, "registry_from_env", lambda: reg)
    monkeypatch.setattr(tw.joblib, "load", lambda _b: art("v0"))
    monkeypatch.setattr(history, "record", lambda *a, **k: True)

    def fake_gate(new_art, prod_art, ds, tol_floor=None, holdouts=None,
                  champion=None, champion_note="", beat_champion=None):
        fake_gate.champion = champion
        fake_gate.note = champion_note
        if beats and beat_champion is not None:
            beat_champion.append({"delta": -1.0})
        return verdict

    fake_gate.champion, fake_gate.note = "не звали", None
    monkeypatch.setattr(tw, "evaluate_gate", fake_gate)

    model = tmp_path / "model.pkl"
    model.write_bytes(b"weights")
    artifact = {"model_version": "1.0.0", "algo": "t", "features": [],
                "metrics": {}, "dataset": {}, "trained_at": "now"}
    out = tw.push_with_gate(artifact, model, logging.getLogger("test"),
                            ds=OneHoldout())
    return out, fake_gate


def test_the_gate_is_actually_handed_the_anchor(monkeypatch, tmp_path):
    """ГЛАВНОЕ ПРО ПРОВОДКУ: якорь доезжает до гейта.

    Напиши храповик безупречно и не передай ему чемпиона — гейт вёл бы
    себя ровно как до спринта, а все тесты выше остались бы зелёными:
    они зовут `evaluate_gate` напрямую.
    """
    reg = FakeReg(stages={"production": "v9", "champion": "v0"},
                  blobs={"v9": b"prod", "v0": b"champ"})
    monkeypatch.setattr("joblib.load", lambda _b: art("v0"))
    (_, ok, _), gate_ = push(monkeypatch, tmp_path, reg)
    assert ok is True
    assert gate_.champion is not None, "гейт вызван без якоря"
    assert "v0" in reg.resolved, "артефакт якоря не скачан"


def test_the_first_promoted_version_becomes_the_anchor(monkeypatch, tmp_path):
    """Якорь заводится сам на первой же продвинутой версии.

    Иначе храповик начал бы работать неизвестно с какого дня: пока
    указателя нет, сравнивать не с чем, а завести его вручную некому.
    """
    reg = FakeReg(stages={"production": "v9"}, blobs={"v9": b"prod"})
    push(monkeypatch, tmp_path, reg)
    stages = [s for _, s in reg.promoted]
    assert ratchet.CHAMPION_STAGE in stages, reg.promoted


def test_a_promotion_that_did_not_beat_the_anchor_leaves_it_alone(monkeypatch,
                                                                  tmp_path):
    """Продвижение «не хуже» не двигает планку.

    Это и есть храповик: точка отсчёта переносится только вверх. Двигай
    её каждый промоушен — и она снова поехала бы за продом.
    """
    reg = FakeReg(stages={"production": "v9", "champion": "v0"},
                  blobs={"v9": b"prod", "v0": b"champ"})
    push(monkeypatch, tmp_path, reg, beats=False)
    assert reg.stages["champion"] == "v0", "планка съехала на ничьей"
    assert ("production" in [s for _, s in reg.promoted]), "модель не продвинута"


def test_a_proven_improvement_moves_the_anchor(monkeypatch, tmp_path):
    """Доказанное улучшение поднимает планку."""
    reg = FakeReg(stages={"production": "v9", "champion": "v0"},
                  blobs={"v9": b"prod", "v0": b"champ"})
    (version, _, _), _ = push(monkeypatch, tmp_path, reg, beats=True)
    assert reg.stages["champion"] == version, "планка не поднялась"


def test_a_rejected_candidate_never_becomes_the_anchor(monkeypatch, tmp_path):
    """Отклонённый кандидат не становится планкой.

    Стань — и якорем оказалась бы версия, которую гейт только что счёл
    негодной, то есть храповик работал бы против себя.
    """
    reg = FakeReg(stages={"production": "v9", "champion": "v0"},
                  blobs={"v9": b"prod", "v0": b"champ"})
    push(monkeypatch, tmp_path, reg, verdict=(False, "хуже"), beats=True)
    assert reg.stages["champion"] == "v0"
    assert reg.promoted == [], "непродвинутая версия попала в стейдж"


def test_the_bar_is_written_to_its_own_stage():
    """Планка живёт отдельным указателем, а не поверх production.

    Пиши она в тот же стейдж — храповик стал бы синонимом прода, то есть
    исчез бы вместе со смыслом: сравнивать было бы не с чем.
    """
    reg = FakeReg()
    assert ratchet.set_champion(reg, "win_probability", "v1") is True
    assert reg.promoted == [("v1", ratchet.CHAMPION_STAGE)]
    assert ratchet.CHAMPION_STAGE != "production"
