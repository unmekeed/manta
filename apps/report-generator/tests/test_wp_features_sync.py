"""Страж рассинхрона наборов фич между report-generator и ml-service.

Report Generator шлёт MLService словарь фич по именам, а сервер требует
ВСЕ имена из model.features и отвечает INVALID_ARGUMENT «missing
features», если чего-то нет. Списка два (сервисы не импортируют друг
друга), и после расширения FEATURES с 10 до 22 в треке F отчёты бы
молча перестали генерироваться, как только гейт продвинул бы новую
модель в production. Этот тест ловит расхождение в CI, а не в проде.

Список ml-service читается текстом (ставить numpy/lightgbm в окружение
report-generator ради одной константы — плохой обмен).
"""
import ast
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

DATASET_PY = (Path(__file__).resolve().parents[2]
              / "ml-service" / "src" / "training" / "dataset.py")

# Фичи, которых нет в витрине как колонок: считаются в обоих сервисах.
DERIVED = {"game_time", "networth_diff", "xp_diff", "kills_diff",
           "kills_total", "networth_rel"}


def _ml_service_features() -> list[str]:
    tree = ast.parse(DATASET_PY.read_text(encoding="utf-8"))
    for node in tree.body:
        if (isinstance(node, ast.Assign)
                and any(getattr(t, "id", None) == "FEATURES"
                        for t in node.targets)):
            return [ast.literal_eval(e) for e in node.value.elts]
    raise AssertionError("в dataset.py не найден список FEATURES")


@pytest.mark.skipif(not DATASET_PY.exists(),
                    reason="ml-service отсутствует (частичный чекаут)")
def test_report_generator_sends_every_wp_feature():
    from reportgen.runner import WP_PASSTHROUGH_FEATURES

    expected = _ml_service_features()
    sent = DERIVED | set(WP_PASSTHROUGH_FEATURES)
    missing = [f for f in expected if f not in sent]
    assert not missing, (
        f"report-generator не отправляет фичи {missing} — Predict ответит "
        "INVALID_ARGUMENT, как только модель с ними уедет в production; "
        "дописать в WP_PASSTHROUGH_FEATURES (runner.py)")


@pytest.mark.skipif(not DATASET_PY.exists(),
                    reason="ml-service отсутствует (частичный чекаут)")
def test_no_stale_features_sent():
    """Обратная сторона: имя, выброшенное из модели, не должно остаться
    в отправляемом словаре — иначе непонятно, живая это фича или мусор."""
    from reportgen.runner import WP_PASSTHROUGH_FEATURES

    expected = set(_ml_service_features())
    extra = [f for f in WP_PASSTHROUGH_FEATURES if f not in expected]
    assert not extra, f"фичи {extra} больше нет в модели — убрать из runner.py"


@pytest.mark.skipif(not DATASET_PY.exists(),
                    reason="ml-service отсутствует (частичный чекаут)")
def test_row_columns_cover_passthrough():
    """Каждая пробрасываемая фича должна и читаться из витрины —
    иначе она всегда NaN, и модель тихо слепнет."""
    from wp_rates import RATE_FEATURES

    from reportgen.runner import WP_PASSTHROUGH_FEATURES, WP_ROW_COLUMNS

    # draft_prior приходит джойном из MatchDraft, а производные (G1) —
    # оконными функциями; колонками витрины не являются ни те, ни другие.
    # Что они действительно доезжают, проверяет тест ниже.
    need = [f for f in WP_PASSTHROUGH_FEATURES
            if f != "draft_prior" and f not in RATE_FEATURES]
    missing = [f for f in need if f not in WP_ROW_COLUMNS]
    assert not missing, f"{missing} не читаются из витрины — всегда NaN"


@pytest.mark.skipif(not DATASET_PY.exists(),
                    reason="ml-service отсутствует (частичный чекаут)")
def test_sent_vector_covers_every_feature_of_the_model():
    """Итоговая проверка: то, что реально уходит в MLService.

    Список имён совпадать может, а вектор всё равно окажется неполным —
    достаточно забыть одну строку в сборке словаря. Сервер на это
    отвечает INVALID_ARGUMENT, то есть отчёты перестают генерироваться
    целиком, а не деградируют.
    """
    from wp_rates import RATE_METRICS, RATE_WINDOWS, prev_col, prev_time_col

    from reportgen.runner import wp_feature_values

    row = {"game_time": 600, "networth_diff": 5000, "xp_diff": 6000,
           "networth_total": 40000, "kills_radiant": 5, "kills_dire": 3}
    for w in RATE_WINDOWS:
        row[prev_time_col(w)] = 600 - w
        for m in RATE_METRICS:
            row[prev_col(m, w)] = 0

    sent = set(wp_feature_values(row))
    missing = [f for f in _ml_service_features() if f not in sent]
    assert not missing, f"в MLService не уедут фичи {missing}"


@pytest.mark.skipif(not DATASET_PY.exists(),
                    reason="ml-service отсутствует (частичный чекаут)")
def test_rate_matches_the_training_formula():
    """Число, а не только имя.

    Расхождение формул между обучением и продом не падает — оно даёт
    ОДНОЙ модели разные входы в двух местах. Здесь темп считается руками:
    5000 золота набежало за пять минут — тысяча в минуту.
    """
    from wp_rates import prev_col, prev_time_col

    from reportgen.runner import wp_feature_values

    row = {"game_time": 600, "networth_diff": 5000, "xp_diff": 0,
           "networth_total": 40000, "kills_radiant": 0, "kills_dire": 0,
           prev_time_col(300): 300, prev_col("networth_diff", 300): 0}
    assert wp_feature_values(row)["networth_diff_rate_5m"] == 1000.0


@pytest.mark.skipif(not DATASET_PY.exists(),
                    reason="ml-service отсутствует (частичный чекаут)")
def test_timeline_query_asks_for_the_window_columns():
    """Производные считаются из оконных колонок запроса.

    Освободить их от проверки «колонка есть в витрине» мало: если
    оконные колонки не попадут в SQL, производные молча станут NaN на
    каждом отчёте, и это будет выглядеть как «фича не работает», а не
    как «фича не доехала».
    """
    from wp_rates import RATE_METRICS, RATE_WINDOWS, prev_col, prev_time_col

    from reportgen.runner import ReportGenerator

    gen = ReportGenerator.__new__(ReportGenerator)
    seen = []
    gen._ch_select = lambda sql, match_id: seen.append(sql) or []
    gen._timeline_rows(1)

    sql = seen[0]
    for w in RATE_WINDOWS:
        assert f"AS {prev_time_col(w)}" in sql
        for m in RATE_METRICS:
            assert f"AS {prev_col(m, w)}" in sql
