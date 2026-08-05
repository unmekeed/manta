"""«Не измерено» не должно выглядеть как отличный результат (спринт 92).

Инцидент 2026-08-03: на дашборде висело «Brier на про-эталоне 0.0000» и
рядом зелёная подпись «цель ≤ 0.18 ✓». Brier ноль недостижим в принципе,
а подпись утверждала, что цель проекта перевыполнена.

Причина: prometheus_client создаёт Gauge со значением 0.0, а
BRIER_BENCHMARK выставлялся только внутри переобучения. Каждый
`make recover` перезапускает auto-train, и до первой тренировки метрика
честно отдавала ноль — который дашборд честно показывал и честно
сравнивал с порогом.

Проверяем через ЭКСПОЗИЦИЮ, а не через внутренности Gauge: потребитель
(дашборд, Prometheus) видит именно текст, и важно, что NaN доезжает до
него в разбираемом виде.
"""
import math
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from prometheus_client import generate_latest  # noqa: E402

from training import auto  # noqa: E402


def _gauge(name: str) -> float:
    text = generate_latest().decode()
    line = next(l for l in text.splitlines() if l.startswith(name + " "))
    return float(line.split()[1])


def test_no_metrics_yield_nan_not_zero():
    auto._publish_production_metrics({})
    assert math.isnan(_gauge("wp_brier_benchmark_pro"))
    assert math.isnan(_gauge("wp_brier_valid"))


def test_real_metrics_are_published():
    """Обратная сторона: измеренные значения обязаны доезжать."""
    auto._publish_production_metrics({"brier_calibrated": 0.1495,
                                      "brier_benchmark_pro": 0.1739})
    assert _gauge("wp_brier_valid") == 0.1495
    assert _gauge("wp_brier_benchmark_pro") == 0.1739


def test_partial_metrics_leave_the_rest_nan():
    """У старой версии модели может не быть про-эталона. Отсутствующий
    ключ даёт NaN — не ноль и не значение соседней метрики."""
    auto._publish_production_metrics({"brier_calibrated": 0.15})
    assert _gauge("wp_brier_valid") == 0.15
    assert math.isnan(_gauge("wp_brier_benchmark_pro"))


def test_dashboard_reads_that_exposition_as_missing():
    """Сквозная проверка настоящим парсером дашборда: NaN из экспозиции
    обязан стать прочерком, а не числом.

    Без этой половины фикс на стороне auto-train ничего бы не изменил —
    дашборд просто показал бы «NaN» вместо «0.0000».
    """
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[3]
                           / "scripts"))
    import dashboard

    auto._publish_production_metrics({})
    parsed = dashboard._parse_prom(generate_latest().decode())
    assert dashboard._pick(parsed, "wp_brier_benchmark_pro") is None


# -- фазовые Brier описывают production, а не последнего кандидата (117) ------

def _phase(name: str) -> float:
    text = generate_latest().decode()
    prefix = f'wp_brier_phase{{phase="{name}"}}'
    line = next(l for l in text.splitlines() if l.startswith(prefix))
    return float(line.split()[1])


def test_phase_brier_published_with_production_metrics():
    auto._publish_production_metrics(
        {"brier_calibrated": 0.11, "brier_benchmark_pro": 0.17,
         "brier_early": 0.21, "brier_mid": 0.14, "brier_late": 0.08})
    assert _phase("early") == 0.21
    assert _phase("mid") == 0.14
    assert _phase("late") == 0.08


def test_phase_brier_is_nan_when_not_measured():
    """Тот же принцип, что у главных плиток: «не измерено» обязано
    отличаться от значения. Ноль в Brier по фазе выглядел бы идеальным
    предсказанием ранней игры — то есть ровно наоборот."""
    auto._publish_production_metrics({})
    for ph in ("early", "mid", "late"):
        assert math.isnan(_phase(ph)), ph


def test_phase_brier_not_set_outside_promotion():
    """Раньше фазовые метрики выставлялись в переобучении, ДО гейта и
    безусловно: после первого же отклонённого кандидата они описывали
    модель, которая никогда не обслуживала запросы, стоя рядом с
    плитками production. Гейт отклонял пять кандидатов подряд — случай
    не гипотетический.

    Проверка структурная: установка фазовых метрик должна существовать
    РОВНО в одном месте — внутри публикатора production-метрик.
    """
    src = (pathlib.Path(__file__).resolve().parents[1] / "src" / "training"
           / "auto.py").read_text(encoding="utf-8")
    assert src.count("BRIER_PHASE.labels") == 1, (
        "фазовые метрики выставляются не только в _publish_production_metrics")
    body = src.split("def _publish_production_metrics", 1)[1].split("\ndef ", 1)[0]
    assert "BRIER_PHASE.labels" in body
