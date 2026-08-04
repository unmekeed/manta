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


# -- дашборд обязан подхватывать правки (спринт 101) --------------------------

def test_recover_restarts_stale_dashboard():
    """Дашборд — единственный процесс, который make stop намеренно не
    трогает (спринт 74: иначе умирает кнопка «Поднять всё»). Из-за этого
    он же оказался единственным, кто НЕ ПОДХВАТЫВАЛ правки: recover
    пропускал его как «уже работает».

    Инцидент 2026-08-04: фиксы спринтов 92 и 96 не доехали до страницы
    вовсе — на экране висела надпись из версии двухдневной давности, и
    оба «исправленных» дефекта выглядели неисправленными.
    """
    src = (Path(__file__).resolve().parents[1] / "dev-recover.sh"
           ).read_text(encoding="utf-8")
    assert "stat -c %Y scripts/dashboard.py" in src, (
        "recover не сверяет время правки дашборда со временем запуска")
    assert "дашборд старее своего кода" in src


def test_recover_does_not_kill_its_own_parent():
    """Если recover запущен КНОПКОЙ дашборда, убивать дашборд нельзя —
    он родитель этой задачи, и она оборвётся на середине. В этом случае
    ожидается предупреждение, а не kill."""
    src = (Path(__file__).resolve().parents[1] / "dev-recover.sh"
           ).read_text(encoding="utf-8")
    i = src.index("дашборд старее своего кода")
    guard = src[max(0, i - 600):i]
    assert "MANTA_DASHBOARD_JOB" in guard, (
        "нет защиты от самоубийства при запуске из панели управления")


def test_dashboard_marks_its_own_jobs():
    """Метку ставит сам дашборд — иначе проверка выше опирается на
    переменную, которую никто не выставляет."""
    src = (Path(__file__).resolve().parents[1] / "dashboard.py"
           ).read_text(encoding="utf-8")
    assert 'MANTA_DASHBOARD_JOB": "1"' in src
    assert "env=job_env" in src
