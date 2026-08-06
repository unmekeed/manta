"""Производные Win Probability — общий контракт (трек G, G1, спринт 131).

Почему в libs, а не внутри ml-service.

Фичи модели считаются в ДВУХ местах: ml-service собирает датасет и
предсказывает по витрине сам, а report-generator шлёт вектор фич по
именам через gRPC. Списки уже расходились дважды — спринты 90 и 91
добавили фичи в обучение и забыли в runner.py, и это чинилось задним
числом. С производными такое расхождение было бы хуже обычного: оно
проявилось бы не падением, а РАЗНЫМИ ЧИСЛАМИ у одной и той же модели в
обучении и в проде.

Здесь лежит всё, что задаёт производную целиком: список метрик, окна,
правило имён, SQL оконных колонок и арифметика темпа. Оба сервиса
берут это отсюда, поэтому разойтись им нечем.

Модуль намеренно без numpy и без зависимостей: report-generator их не
ставит.
"""
from __future__ import annotations

import math
from collections.abc import Mapping

# Метрики, для которых темп изменения имеет прямой игровой смысл:
# экономика и опыт (кто разгоняется), здания (кто продавливает карту),
# обзор (кто расширяет контроль), уровни (кто выигрывает темп развития).
RATE_METRICS = ("networth_diff", "xp_diff", "towers_diff",
                "vision_coverage_diff", "levels_diff")

# Окна назад, секунды. Три семейства, потому что скорее всего работает
# одно из трёх, и абляция должна уметь их различить: минута ловит рывок
# (тимфайт, тайминг), пять минут — устойчивый тренд.
RATE_WINDOWS = (60, 180, 300)


def rate_name(metric: str, window_s: int) -> str:
    """Имя фичи модели: networth_diff_rate_3m."""
    return f"{metric}_rate_{window_s // 60}m"


def prev_col(metric: str, window_s: int) -> str:
    """Служебная колонка запроса: значение метрики в начале окна."""
    return f"{metric}__prev{window_s}"


def prev_time_col(window_s: int) -> str:
    return f"game_time__prev{window_s}"


RATE_FEATURES: list[str] = [rate_name(m, w)
                            for w in RATE_WINDOWS for m in RATE_METRICS]


def window_columns() -> str:
    """Оконные колонки: чему равнялась метрика W секунд назад.

    RANGE, а не ROWS: кадр задаётся в СЕКУНДАХ игрового времени, поэтому
    пропущенная минута (а они встречаются: JSON-путь местами дырявый) не
    превращает пятиминутное окно в семиминутное. С ROWS такая дыра меняла
    бы смысл фичи молча.

    `first_value` берёт самую раннюю строку внутри кадра. Рядом с каждым
    набором метрик едет game_time той же строки: делить надо на
    ФАКТИЧЕСКИ прошедшее время, а не на номинал окна.

    `RANGE BETWEEN W PRECEDING AND CURRENT ROW` — строго НАЗАД. Любое
    заглядывание вперёд было бы утечкой целевой переменной: дало бы
    красивый Brier на валидации и мусор в проде, потому что в реальном
    матче будущего ещё нет.
    """
    parts: list[str] = []
    for w in RATE_WINDOWS:
        over = ("OVER (PARTITION BY match_id ORDER BY game_time"
                f" RANGE BETWEEN {w} PRECEDING AND CURRENT ROW)")
        parts.append(f"first_value(game_time) {over} AS {prev_time_col(w)}")
        for m in RATE_METRICS:
            parts.append(f"first_value({m}) {over} AS {prev_col(m, w)}")
    return ", ".join(parts)


def _num(value) -> float:
    """null из ClickHouse (→ None) и отсутствующий ключ — это NaN."""
    return float(value) if value is not None else math.nan


def rates_for_row(row: Mapping, levels: Mapping[str, float]
                  ) -> dict[str, float]:
    """Производные одной строки витрины.

    `row` — сырая строка запроса (нужны game_time и оконные колонки),
    `levels` — уже посчитанные УРОВНИ метрик из этой же строки.

    Делится на ФАКТИЧЕСКИ прошедшее время, а не на номинал окна: в начале
    матча пятиминутного окна ещё не существует, и деление на номинал
    занизило бы темп впятеро ровно в той фазе, ради которой производные и
    вводятся (Brier ранней игры 0.219 против 0.103 в поздней). Следствие:
    в начале матча все три окна совпадают — больше истории просто нет, и
    это честнее, чем держать NaN до пятой минуты.

    dt <= 0 (первая строка матча, где окно содержит только её саму) —
    NaN, а не ноль: ноль означал бы «ничего не менялось», то есть
    осмысленное измерение там, где измерения не существует.

    Строка без оконных колонок (ручной вызов, старый кэш) даёт NaN по
    всем производным и не падает: производная — надстройка, ломать
    существующие пути чтения витрины она не должна.
    """
    now = _num(row.get("game_time"))
    out: dict[str, float] = {}
    for w in RATE_WINDOWS:
        dt_min = (now - _num(row.get(prev_time_col(w)))) / 60.0
        for m in RATE_METRICS:
            out[rate_name(m, w)] = (
                (levels[m] - _num(row.get(prev_col(m, w)))) / dt_min
                if dt_min > 0 else math.nan)
    return out
