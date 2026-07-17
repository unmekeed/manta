"""SHAP-объяснения Win Probability (Гл. 6.2: интерпретируемость).

Вклады считает сам LightGBM: `predict(X, pred_contrib=True)` — точный
TreeSHAP без внешних зависимостей. Возвращаемая матрица (n, f+1): вклад
каждой фичи + последний столбец bias; сумма строки равна сырому скору
модели (лог-оддсы), sigmoid(суммы) — некалиброванная вероятность.

Вклады выражены в лог-оддсах, а сервится калиброванная WP. Изотоническая
калибровка монотонна, поэтому ЗНАК и РАНЖИРОВАНИЕ вкладов переносятся на
итоговую вероятность корректно: фича, толкающая скор вверх, толкает вверх
и WP. Численно «доля процента WP» не гарантируется — и не обещается.

CLI: python -m explain.win_probability MATCH_ID — WP-кривая с топ-драйверами.
"""
from __future__ import annotations

import lightgbm as lgb
import numpy as np


def contributions(booster: lgb.Booster, X: np.ndarray
                  ) -> tuple[np.ndarray, np.ndarray]:
    """TreeSHAP-вклады фич: (contribs (n, f), bias (n,))."""
    raw = booster.predict(X, pred_contrib=True)
    return raw[:, :-1], raw[:, -1]


def top_drivers(contribs: np.ndarray, features: list[str], k: int = 3
                ) -> list[list[tuple[str, float]]]:
    """Для каждой строки — топ-k фич по |вкладу|, с сохранением знака."""
    out = []
    for row in contribs:
        order = np.argsort(-np.abs(row))[:k]
        out.append([(features[i], round(float(row[i]), 4)) for i in order])
    return out


def explain_matrix(artifact_booster: lgb.Booster, X: np.ndarray,
                   features: list[str], k: int = 3) -> list[list[tuple[str, float]]]:
    """Топ-драйверы для матрицы снапшотов (обёртка contributions+top_drivers)."""
    contribs, _ = contributions(artifact_booster, X)
    return top_drivers(contribs, features, k)
