"""«Не измерено» не должно выглядеть как отличный результат (спринт 92).

Инцидент 2026-08-03: на дашборде висело «Brier на про-эталоне 0.0000» и
рядом зелёная подпись «цель ≤ 0.18 ✓». Brier ноль недостижим в принципе,
а галочка утверждала, что цель проекта перевыполнена.

Причина: prometheus_client создаёт Gauge со значением 0.0, а
BRIER_BENCHMARK выставлялся только внутри переобучения. После каждого
`make recover` процесс auto-train стартовал заново, и до первой
тренировки метрика честно отдавала ноль — который дашборд честно
показывал и честно сравнивал с порогом.

Тот же класс отказа, что «пустой bash-скрипт вернул 0, значит успех»:
отсутствие данных прошло по всем проверкам как хороший результат.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import dashboard  # noqa: E402


def test_nan_metric_reads_as_missing():
    metrics = {("wp_brier_benchmark_pro", ()): float("nan")}
    assert dashboard._pick(metrics, "wp_brier_benchmark_pro") is None


def test_absent_metric_reads_as_missing():
    assert dashboard._pick({}, "wp_brier_benchmark_pro") is None


def test_real_zero_is_still_a_value():
    """Обратная сторона: ноль — законное значение для счётчиков вроде
    retrains_total, и глушить его нельзя."""
    metrics = {("retrains_total", (("outcome", "promoted"),)): 0.0}
    assert dashboard._pick(metrics, "retrains_total",
                           outcome="promoted") == 0.0


def test_nan_survives_the_prometheus_parser():
    """Сквозная проверка: NaN обязан доехать от текста экспозиции до
    _pick. Если парсер уронит строку, метрика станет «отсутствующей» по
    другой причине — совпадение, на которое опираться нельзя."""
    text = "# HELP wp_brier_valid x\n# TYPE wp_brier_valid gauge\nwp_brier_valid NaN\n"
    parsed = dashboard._parse_prom(text)
    assert ("wp_brier_valid", ()) in parsed, "парсер потерял строку с NaN"
    assert dashboard._pick(parsed, "wp_brier_valid") is None


@pytest.mark.parametrize("value", [float("nan"), None])
def test_history_never_records_missing(value):
    """Спарклайн живёт весь срок процесса, и масштаб считается по
    min/max: один NaN испортил бы график навсегда."""
    key = "brier_bm_test"
    dashboard._history[key].clear()
    dashboard._record(key, value)
    assert list(dashboard._history[key]) == []


def test_history_records_real_values():
    key = "brier_bm_test2"
    dashboard._history[key].clear()
    dashboard._record(key, 0.1682)
    assert list(dashboard._history[key]) == [0.1682]
