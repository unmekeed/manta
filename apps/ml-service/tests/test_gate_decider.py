"""Кто выносит вердикт гейта (спринт 214, пункт G2 роадмапа).

РЕШЕНИЕ ВЛАДЕЛЬЦА 10.09.2026: продукт — публичные матчи, про-эталон —
сторож. Отчёты делаются для собранных матчей, а они почти все публичные.

ЧТО ЭТО ЧИНИТ. Гейт судил по про-эталону единолично. До спринта 213
эталон перетасовывался при росте датасета, поэтому старые версии
мерились в том числе на матчах, которые видели при обучении, — и 9–10
сентября это дало семь отказов подряд с одинаковой картиной: на про
кандидат «значимо хуже» семь раз из семи, а на свежих матчах, которых не
видел НИКТО, — не хуже или лучше пять раз из пяти.

Свежие матчи — единственная оценка, свободная и от утечки, и от
перетасовки: их не видел ни один участник сравнения. Поэтому решают они,
когда набралось достаточно.

СТОРОЖ, А НЕ СУДЬЯ. Про-эталон остаётся, но ловит ОБВАЛ (0.01 Brier —
на порядок больше допуска гейта), а не дрейф. Мягкость не от доброты:
пока обе стороны сравнения не обучены после спринта 213, про-оценка
систематически льстит той, что старше, и судить по ней в этот период
нельзя, а заметить обвал — можно.
"""
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from training import train_winprob as tw  # noqa: E402

Y = np.tile([1, 1, 0, 0], 40)
GROUPS = np.repeat(np.arange(2000, 2160), 1)[: len(Y)]


def holdout(kind, n_matches, delta, ok, text=None):
    """Готовая оценка в том виде, в каком её отдаёт `_judge_holdout`."""
    detail = {"kind": kind, "n_matches": n_matches, "brier_new": 0.15,
              "brier_prod": 0.15 - delta, "delta": delta, "sigma": 0.001,
              "tol": 0.001, "ok": ok, "ref": "prod"}
    return (ok, text or f"{kind}: подстановка", kind, detail,
            (np.zeros((4, 1)), Y[:4], GROUPS[:4]))


class Holdouts:
    """Датасет, отдающий заранее заданные выборки."""

    def __init__(self, kinds):
        self.kinds = kinds

    def eval_holdouts(self):
        one = np.array([1])
        return [(one, one, one, k) for k in self.kinds]


def gate(monkeypatch, plan, **kw):
    """evaluate_gate поверх подставного `_judge_holdout`.

    Подставляется именно он: реальные модели на синтетике расходятся
    случайно, и тест про ПРАВИЛО ВЫБОРА зависел бы от того, повезло ли
    данным разойтись.
    """
    by_kind = dict(plan)
    monkeypatch.setattr(
        tw, "_judge_holdout",
        lambda na, pa, pm, X, y, g, kind, tol: by_kind[kind])
    return tw.evaluate_gate({}, {}, Holdouts(list(by_kind)), **kw)


# -- ГЛАВНОЕ ------------------------------------------------------------------

def test_fresh_decides_when_it_is_big_enough(monkeypatch):
    """ГЛАВНОЕ: при расхождении решают свежие матчи.

    Ровно та картина, что стояла в логах семь прогонов подряд: на про
    хуже, на свежих не хуже. До спринта 214 это означало отказ.
    """
    ok, reason = gate(monkeypatch, [
        ("benchmark_pro", holdout("benchmark_pro", 674, +0.0026, False)),
        ("fresh", holdout("fresh", 152, -0.0011, True)),
    ])
    assert ok is True, reason
    assert reason.startswith("решает свежие матчи"), reason


def test_the_pro_benchmark_no_longer_decides(monkeypatch):
    """Обратная сторона того же: про-эталон один не отклоняет.

    Без этой проверки предыдущая проходила бы и при правиле «решают оба»
    — достаточно, чтобы про случайно оказался мягче.
    """
    ok, _ = gate(monkeypatch, [
        ("benchmark_pro", holdout("benchmark_pro", 674, +0.0026, False)),
        ("fresh", holdout("fresh", 60, +0.0000, True)),
    ])
    assert ok is True


def test_a_small_fresh_sample_does_not_decide(monkeypatch):
    """Маленькая выборка свежих НЕ решает.

    Оценка на двух десятках матчей — это шум, названный вердиктом.
    Порог для показа ниже, чем для решения: одно дело привести справочную
    цифру, другое — судить по ней.
    """
    ok, reason = gate(monkeypatch, [
        ("benchmark_pro", holdout("benchmark_pro", 674, +0.0026, False)),
        ("fresh", holdout("fresh", tw.FRESH_DECIDES_MIN - 1, -0.01, True)),
    ])
    assert ok is False, reason
    assert reason.startswith("решает про-эталон"), reason


def test_the_decisive_holdout_is_named(monkeypatch):
    """Объяснение ГОВОРИТ, какая выборка решила.

    Пока решала всегда первая, порядок строк это и означал. Теперь
    решает не всегда она, и читатель, по привычке считающий причиной
    верхнюю строку, чинил бы не то.
    """
    _, reason = gate(monkeypatch, [
        ("benchmark_pro", holdout("benchmark_pro", 674, +0.0001, True)),
        ("fresh", holdout("fresh", 152, +0.0001, True)),
    ])
    assert reason.startswith("решает свежие матчи (никто не видел) —"), reason


# -- сторож про-домена --------------------------------------------------------

def test_a_collapse_on_pro_is_refused_even_if_fresh_is_fine(monkeypatch):
    """ГЛАВНОЕ ПРО СТОРОЖА: обвал на про отклоняет всё равно.

    Иначе «решают свежие» означало бы, что про-домен можно потерять
    целиком, лишь бы паблики не просели. Сторож на то и сторож.
    """
    ok, reason = gate(monkeypatch, [
        ("benchmark_pro", holdout("benchmark_pro", 674,
                                  tw.PRO_GUARD_MAX + 0.001, False)),
        ("fresh", holdout("fresh", 152, -0.005, True)),
    ])
    assert ok is False
    assert reason.startswith("СТОРОЖ ПРО"), reason


def test_the_guard_lets_drift_through(monkeypatch):
    """Дрейф в несколько допусков сторож НЕ ловит — и не должен.

    Сделай его строже, и он снова стал бы судьёй, только под другим
    именем: до тех пор пока обе стороны не обучены после спринта 213,
    про-оценка льстит той, что старше.
    """
    ok, _ = gate(monkeypatch, [
        ("benchmark_pro", holdout("benchmark_pro", 674, +0.0026, False)),
        ("fresh", holdout("fresh", 152, -0.0011, True)),
    ])
    assert ok is True


def test_the_guard_names_the_numbers(monkeypatch):
    """Отказ сторожа называет и разрыв, и допустимое.

    Без чисел читателю остаётся «нельзя», и первый его шаг — искать, чем
    это обойти, вместо того чтобы понять масштаб поломки.
    """
    _, reason = gate(monkeypatch, [
        ("benchmark_pro", holdout("benchmark_pro", 674, 0.05, False)),
        ("fresh", holdout("fresh", 152, 0.0, True)),
    ])
    assert "+0.0500" in reason and f"{tw.PRO_GUARD_MAX:.4f}" in reason


# -- храповик поверх нового решающего ------------------------------------------

def test_the_ratchet_runs_on_the_decisive_holdout(monkeypatch):
    """Храповик считается на ТОЙ ЖЕ выборке, что решила.

    Два вердикта на разных выборках противоречили бы друг другу не по
    существу, а по данным.
    """
    seen = {}

    def spy(new_art, ref_art, X, y, groups, kind, tol, ref="prod"):
        seen["kind"] = kind
        return True, f"{kind}: якорь", {"kind": kind, "delta": 0.0,
                                        "tol": 0.001, "n_matches": 9}

    monkeypatch.setattr(tw, "_compare", spy)
    gate(monkeypatch, [
        ("benchmark_pro", holdout("benchmark_pro", 674, +0.0026, False)),
        ("fresh", holdout("fresh", 152, -0.0011, True)),
    ], champion={"dataset": {}})
    assert seen.get("kind") == "fresh", seen


def test_freshness_is_measured_against_the_anchor_too(monkeypatch):
    """Граница «свежести» учитывает и якорь, а не только production.

    После отката якорь может быть обучен на БОЛЕЕ ПОЗДНЕМ датасете, чем
    prod. Возьми мы границу только по проду — якорь эти матчи видел, и
    «никто не видел» осталось бы названием, а не свойством.
    """
    seen = {}

    def fake_judge(na, pa, prod_max, X, y, g, kind, tol):
        seen["prod_max"] = prod_max
        return holdout(kind, 100, 0.0, True)

    monkeypatch.setattr(tw, "_judge_holdout", fake_judge)
    monkeypatch.setattr(tw, "_compare",
                        lambda *a, **k: (True, "якорь", {"delta": 0.0,
                                                         "tol": 0.001}))
    tw.evaluate_gate({}, {"dataset": {"max_match_id": 100}},
                     Holdouts(["valid"]),
                     champion={"dataset": {"max_match_id": 500}})
    assert seen["prod_max"] == 500, (
        f"граница взята по проду ({seen['prod_max']}), а якорь обучен "
        f"позже — матчи после 100 он видел")
