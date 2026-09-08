"""Выбор якоря среди существующих версий (спринт 209).

ЗАЧЕМ ЭТОТ МОДУЛЬ ВООБЩЕ. Спринт 207 заводил якорь на первой
продвинутой версии, то есть на той, что подвернётся следующей. А
подвернётся, по построению, ХУДШАЯ из виденных: планка встала бы там,
куда съехал прод. Замер 08.09.2026, пять последних версий по их
собственному про-эталону: 0.1581 → 0.1572 → 0.1599 → 0.1594 → 0.1617
(production).

ГЛАВНОЕ СВОЙСТВО, КОТОРОЕ ЗДЕСЬ СТЕРЕЖЁТСЯ: выбор делается ЧЕСТНЫМ
сравнением — все кандидаты считаются сейчас и на одном holdout, — а не
по сохранённым в реестре числам. Те посчитаны на разных данных (эталон
пересобирается с приходом про-матчей) и годятся лишь сузить круг.
Соблазн взять готовое число велик ровно потому, что оно уже лежит
рядом.
"""
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from training import anchor  # noqa: E402

Y = np.array([1, 0, 1, 0, 1, 0])
GROUPS = np.array([1, 1, 2, 2, 3, 3])
X = np.zeros((len(Y), 1))

# Насколько каждая версия ошибается: Brier получается ровно e².
SHIFT = {"v1": 0.10, "v2": 0.20, "v3": 0.30}


class Reg:
    def __init__(self, versions, stages=None, broken=()):
        self._versions = list(versions)
        self.stages = dict(stages or {})
        self.broken = set(broken)
        self.promoted = []
        self.resolved = []

    def list_versions(self, name):
        return list(self._versions)

    def stage_version(self, name, stage="production"):
        return self.stages.get(stage)

    def resolve(self, name, ref):
        version = self.stages.get(ref, ref)
        self.resolved.append(version)
        if version in self.broken:
            raise KeyError("битая контрольная сумма")
        return version.encode(), {}

    def promote(self, name, version, stage="production"):
        self.promoted.append((version, stage))
        self.stages[stage] = version


def patched(monkeypatch):
    """Подменить загрузку весов и предсказание: версия — это её имя."""
    import joblib

    from training import train_winprob as tw

    monkeypatch.setattr(joblib, "load", lambda buf: buf.read().decode())
    monkeypatch.setattr(
        tw, "predict_calibrated",
        lambda art, _X: np.where(Y == 1, 1.0 - SHIFT[art], SHIFT[art]))


def test_the_best_version_wins_on_todays_holdout(monkeypatch):
    """Побеждает та, что лучше СЕЙЧАС, а не по записанному числу."""
    patched(monkeypatch)
    reg = Reg(["v1", "v2", "v3"])
    scored = anchor.evaluate(reg, ["v3", "v1", "v2"], X, Y, GROUPS)
    scored.sort(key=lambda p: p[1])
    assert scored[0][0] == "v1", scored
    assert abs(scored[0][1] - 0.01) < 1e-9, "Brier посчитан не на этих данных"
    assert len(scored[0][2]) == len(Y), (
        "предсказания не возвращены — разброс разницы посчитать нечем")


def test_a_broken_version_is_skipped_not_fatal(monkeypatch):
    """Битая версия пропускается: без якоря остаться хуже.

    Одна не прошедшая проверку целостности версия — не повод отменить
    выбор среди остальных семи.
    """
    patched(monkeypatch)
    reg = Reg(["v1", "v2", "v3"], broken={"v1"})
    got = {v for v, _, _ in anchor.evaluate(reg, ["v1", "v2", "v3"],
                                            X, Y, GROUPS)}
    assert got == {"v2", "v3"}, got


def test_production_is_always_among_the_candidates():
    """Текущий production рассматривается, даже выпав из окна последних.

    Иначе сравнение отвечало бы на вопрос «кто лучший среди старых»,
    умалчивая о том, кого мы крутим прямо сейчас, — и владелец не увидел
    бы главного числа: насколько прод отстал от лучшего.
    """
    reg = Reg([f"v{i}" for i in range(1, 11)],
              stages={"production": "v1", "champion": "v2"})
    got = anchor.candidates(reg, limit=3, keep={"v1", "v2"})
    assert "v1" in got and "v2" in got, got
    assert {"v8", "v9", "v10"} <= set(got), got


def test_an_unknown_kept_version_does_not_appear():
    """Указатель на исчезнувшую версию не превращается в кандидата.

    Стейдж может указывать на версию, вычищенную из реестра; попытка
    скачать её кончилась бы предупреждением на каждом прогоне.
    """
    reg = Reg(["v1", "v2"])
    assert anchor.candidates(reg, limit=5, keep={"v9"}) == ["v1", "v2"]


def test_a_gap_smaller_than_the_noise_is_called_undetectable():
    """ГЛАВНОЕ: разрыв меньше шума назван неразличимым.

    Храповик отклоняет кандидата при Δ > max(порог, σ). Разрыв МЕНЬШЕ σ
    он не заметит — сколько бы его ни показывали в таблице. Планка на
    неразличимой разнице выглядит работающей и не делает ничего, и это
    ровно тот род «сторожа», из-за которого этот проект теряет сутки:
    присутствие ≠ пригодность.

    Фикстура строится по КВАДРАТИЧНОЙ ошибке, а не по вероятностям:
    первая редакция сдвигала предсказания симметрично и получала разрыв
    ВЧЕТВЕРО больше шума, то есть демонстрировала ровно обратное тому,
    ради чего написана. Квадрат выпуклый — симметричный сдвиг
    вероятности даёт несимметричный Brier.

    Здесь версия B лучше на половине матчей ровно настолько, насколько
    хуже на другой, плюс маленький общий проигрыш: средний Brier хуже на
    0.002 при разбросе разницы 0.013.
    """
    n_m, per = 24, 6
    y = np.tile([1, 1, 1, 0, 0, 0], n_m)
    groups = np.repeat(np.arange(n_m), per)
    base = 0.09
    lo, hi = np.sqrt(base - 0.06), np.sqrt(base + 0.064)
    good = np.where(y == 1, 1 - np.sqrt(base), np.sqrt(base))
    better_half = (np.arange(n_m) % 2 == 0)[groups]
    worse = np.where(better_half,
                     np.where(y == 1, 1 - lo, lo),
                     np.where(y == 1, 1 - hi, hi))

    gap, sigma, tol = anchor.detectable(y, worse, good, groups)
    assert gap > 0, "фикстура не хуже — проверять нечего"
    assert gap <= tol, (
        f"разрыв {gap:.5f} при шуме {sigma:.5f} назван различимым — "
        f"храповик такого не заметит, и говорить обратное значит обещать "
        f"защиту, которой нет")


def test_a_gap_bigger_than_the_noise_is_detectable():
    """Контроль: крупный разрыв при малом шуме различим.

    Без него тест выше был бы зелёным и при вечно неразличимом вердикте,
    то есть при мере, которая всегда говорит «не знаю».
    """
    y = np.repeat([1, 0], 60)
    groups = np.repeat(np.arange(20), 6)
    good = np.where(y == 1, 0.9, 0.1)
    bad = np.where(y == 1, 0.6, 0.4)

    gap, sigma, tol = anchor.detectable(y, bad, good, groups)
    assert gap > tol, f"крупный разрыв объявлен шумом: Δ{gap} σ{sigma}"


def test_the_saved_metrics_are_not_used_for_the_verdict():
    """Приговор не выносится по числам из реестра.

    Они посчитаны на разных данных: у каждой версии свой прогон и свой
    эталон, который пересобирается с приходом про-матчей. Соблазн взять
    готовое число велик ровно потому, что оно уже лежит рядом, — а
    разъезд не упал бы, он дал бы неверный якорь, и храповик держал бы
    не ту планку.

    Проверяется отсутствие ЧТЕНИЯ метаданных, а не отсутствие слова:
    `evaluate` получает метаданные вторым элементом `resolve` и обязана
    их игнорировать.
    """
    src = (Path(anchor.__file__)).read_text(encoding="utf-8")
    code = "\n".join(ln for ln in src.splitlines()
                     if not ln.lstrip().startswith("#"))
    assert 'metrics' not in code.split('"""')[-1], (
        "выбор якоря заглядывает в сохранённые метрики")
