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
    from reportgen.runner import WP_PASSTHROUGH_FEATURES, WP_ROW_COLUMNS

    # draft_prior приходит джойном из MatchDraft, а не колонкой витрины.
    need = [f for f in WP_PASSTHROUGH_FEATURES if f != "draft_prior"]
    missing = [f for f in need if f not in WP_ROW_COLUMNS]
    assert not missing, f"{missing} не читаются из витрины — всегда NaN"
