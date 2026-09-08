"""Осознанный промоушен выбранной версии (спринт 211).

ЗАЧЕМ ИНСТРУМЕНТ. Замер 08.09.2026: обслуживает запросы НЕ лучшая из
имеющихся моделей — production 0.1622 против 0.1598 у версии суточной
давности, 2.4σ на общем holdout. Откатить было нечем: production назначал
только гейт, и только вперёд. Ждать нельзя вдвойне — отчёты всё это время
считает заведомо худшая модель, а храповик (207) теперь отклоняет всё,
что значимо хуже якоря, то есть застрявший прод сам себя не вылечит.

ЧТО СТЕРЕЖЁТСЯ. Что ручной путь НЕ ОТМЕНЯЕТ правило гейта. Инструмент
отката, которым можно поставить что угодно, обесценил бы храповик
целиком: планка держалась бы ровно до первой команды.
"""
import sys
from pathlib import Path

import numpy as np

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

ROOT = Path(__file__).resolve().parents[3]

# Три версии с разным качеством. Ошибка каждой ровная по строкам,
# поэтому Brier равен e², σ бутстрапа нулевая, и допуск равен порогу
# гейта (0.001) — вердикт известен до запуска.
SHIFT = {"good": 0.10, "same": 0.1002, "bad": 0.14}
Y = np.tile([1, 1, 0, 0], 6)
GROUPS = np.repeat(np.arange(6), 4)
X = np.zeros((len(Y), 1))


class FakeDS:
    n_matches = 6

    def eval_holdouts(self):
        return [(X, Y, GROUPS, "benchmark_pro")]


class FakeReg:
    def __init__(self, stages):
        self.stages = dict(stages)
        self.promoted = []
        self.resolved = []

    def list_versions(self, name):
        return sorted(SHIFT)

    def stage_version(self, name, stage="production"):
        return self.stages.get(stage)

    def resolve(self, name, ref):
        version = self.stages.get(ref, ref)
        self.resolved.append(version)
        return version.encode(), {}

    def promote(self, name, version, stage="production"):
        self.promoted.append((version, stage))
        self.stages[stage] = version


def run(monkeypatch, capsys, argv, stages):
    """Запустить main() с подставными реестром, датасетом и весами."""
    import joblib
    import registry

    from training import dataset, promote
    from training import train_winprob as tw

    reg = FakeReg(stages)
    monkeypatch.setattr(registry, "registry_from_env", lambda: reg)
    monkeypatch.setattr(dataset, "load_from_clickhouse",
                        lambda *a, **k: FakeDS())
    monkeypatch.setattr(joblib, "load", lambda buf: buf.read().decode())
    monkeypatch.setattr(
        tw, "predict_calibrated",
        lambda art, _X: np.where(Y == 1, 1.0 - SHIFT[art], SHIFT[art]))
    monkeypatch.setattr(sys, "argv", ["promote", *argv])
    rc = promote.main()
    return rc, capsys.readouterr().out, reg


def test_a_version_worse_than_the_anchor_is_refused(monkeypatch, capsys):
    """ГЛАВНОЕ: ручной путь не отменяет правило гейта.

    Инструмент отката, которым можно поставить что угодно, обесценил бы
    храповик целиком: планка держалась бы ровно до первой команды.
    """
    rc, out, reg = run(monkeypatch, capsys, ["bad", "--apply"],
                       {"production": "same", "champion": "good"})
    assert rc != 0
    assert reg.promoted == [], "версия хуже якоря всё-таки назначена"
    assert "ОТКАЗ" in out, out


def test_the_refusal_names_the_number_to_beat(monkeypatch, capsys):
    """Отказ сужает диагноз, а не сообщает о себе.

    Без разрыва и σ читателю остаётся только «нельзя», и следующий его
    шаг — искать, чем это обойти, вместо того чтобы понять, насколько не
    хватило.
    """
    _, out, _ = run(monkeypatch, capsys, ["bad", "--apply"],
                    {"production": "same", "champion": "good"})
    assert "относительно якоря" in out and "σ" in out, out


def test_force_is_obeyed_and_says_so(monkeypatch, capsys):
    """`--force` работает и оставляет след.

    Смена меты — законная причина поставить версию вопреки якорю. Но
    промоушен вопреки проверке обязан выглядеть иначе, чем обычный,
    иначе через месяц никто не вспомнит, что правило обходили.
    """
    rc, out, reg = run(monkeypatch, capsys, ["bad", "--apply", "--force"],
                       {"production": "same", "champion": "good"})
    assert rc == 0
    assert reg.stages["production"] == "bad"
    assert "force" in out.lower(), out


def test_a_rollback_to_the_anchor_is_allowed(monkeypatch, capsys):
    """Откат на сам якорь проходит: он не хуже себя.

    Ровно этот случай и есть повод для инструмента — production съехала,
    а лучшая версия лежит в реестре.
    """
    rc, out, reg = run(monkeypatch, capsys, ["good", "--apply"],
                       {"production": "bad", "champion": "good"})
    assert rc == 0, out
    assert reg.stages["production"] == "good"


def test_one_version_gets_one_line(monkeypatch, capsys):
    """Одна версия — одна строка, сколько бы ролей она ни совмещала.

    Списки пересекаются сплошь и рядом: ставим якорь, откатываемся на
    production, якорь совпал с продом. С повтором вывод читается как «две
    разные версии с одинаковым номером» — ровно та беда, что в спринте
    198e задваивала источники на странице состояния.

    Тест дописан ПОСЛЕ живого прогона: фикстура отката (тест выше) этот
    случай задевала, но смотрела только на стейджи, и задвоение прошло
    мимо неё. Проверять надо и то, что инструмент ПОКАЗЫВАЕТ, — по этому
    выводу принимают решение.
    """
    _, out, _ = run(monkeypatch, capsys, ["good", "--apply"],
                    {"production": "bad", "champion": "good"})
    lines = [l for l in out.splitlines() if "good" in l and l.startswith("  ")]
    assert len(lines) == 1, f"версия напечатана {len(lines)} раза:\n{out}"
    assert "← ставим" in lines[0] and "← якорь" in lines[0], (
        "совмещённые роли потерялись при склейке строк")


def test_a_proven_improvement_raises_the_bar(monkeypatch, capsys):
    """Версия лучше якоря поднимает планку — по тому же правилу, что гейт."""
    rc, out, reg = run(monkeypatch, capsys, ["good", "--apply"],
                       {"production": "bad", "champion": "bad"})
    assert rc == 0
    assert reg.stages["champion"] == "good", reg.stages
    assert "ЯКОРЬ" in out


def test_a_tie_does_not_move_the_bar(monkeypatch, capsys):
    """Ничья планку не двигает.

    Двигай — и храповик завёлся бы заново, только через ручную команду.
    """
    _, _, reg = run(monkeypatch, capsys, ["same", "--apply"],
                    {"production": "bad", "champion": "good"})
    assert reg.stages["champion"] == "good", "планка съехала на ничьей"


def test_nothing_happens_without_apply(monkeypatch, capsys):
    """Без `--apply` — только показ.

    Промоушен меняет то, чем сервис отвечает пользователю. На такое
    соглашаются, а не набирают по ошибке.
    """
    rc, out, reg = run(monkeypatch, capsys, ["good"],
                       {"production": "bad", "champion": "good"})
    assert rc == 0
    assert reg.promoted == [], "версия назначена без --apply"
    assert "--apply" in out


def test_an_unknown_version_is_refused_before_any_download(monkeypatch,
                                                           capsys):
    """Опечатка в версии — отказ ДО всякой работы.

    Проверяется именно «до»: без этого прогон дошёл бы до скачивания и
    упал бы там, где причина уже не видна, — а тест, требующий лишь
    ненулевого кода возврата, был бы зелёным и в этом случае. Мутация
    «убрать раннюю проверку» его и пережила: отказ приходил позже, из
    другого места, и выглядел так же.
    """
    rc, _, reg = run(monkeypatch, capsys, ["v-опечатка", "--apply"],
                     {"production": "bad", "champion": "good"})
    assert rc != 0 and reg.promoted == []
    assert reg.resolved == [], (
        "несуществующую версию пошли скачивать — отказ придёт оттуда, "
        "где причина уже не видна")


def test_the_make_target_exists():
    """Инструмент доступен так же, как остальные.

    Модуль, о котором знает только автор, не инструмент: в момент
    аварии его ищут среди `make help`, а не в исходниках.
    """
    mk = (ROOT / "Makefile").read_text(encoding="utf-8")
    assert "ml-promote:" in mk
    assert "-m training.promote" in mk


def test_the_rule_is_not_reimplemented():
    """«Значимо хуже» считается ОБЩИМ кодом, а не копией.

    Своя арифметика здесь означала бы, что ручной и автоматический пути
    судят по-разному, и разъезд проявился бы не падением, а разными
    вердиктами на одних числах.
    """
    src = (SRC / "training" / "promote.py").read_text(encoding="utf-8")
    assert "from .anchor import detectable" in src, (
        "правило продублировано вместо переиспользования")
    for own in ("_paired_bootstrap_delta", "GATE_TOL_FLOOR", "_brier"):
        assert own not in src.split('"""')[-1], (
            f"{own} считается на месте — это копия правила гейта")
