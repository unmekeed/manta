"""Дрейф-мониторинг фич через PSI (Гл. 10.4, каталог алертов Гл. 11.6.2,
риск R-02 «дрейф меты после мажорного патча»).

PSI (Population Stability Index) сравнивает распределение фичи в текущей
витрине с эталонным распределением, зафиксированным в момент обучения
production-модели (artifact["feature_reference"]):

    PSI = Σ (actual_i - expected_i) * ln(actual_i / expected_i)

по децильным корзинам эталона. Интерпретация (индустриальная норма):
  < 0.10 — стабильно; 0.10–0.25 — умеренный сдвиг; > 0.25 — дрейф,
  модель смотрит на другую игру, чем та, на которой училась.
"""
from __future__ import annotations

import numpy as np

from .dataset import FEATURES

N_BINS = 10
_EPS = 1e-4  # защита пустых корзин: ln(0) и деление на 0


def reference_hist(X: np.ndarray) -> dict:
    """Эталон распределений фич на момент обучения — кладётся в артефакт.

    Для каждой фичи: границы децильных корзин (внутренние края квантилей;
    крайние корзины открыты в ±inf — устойчиво к выбросам новых данных)
    и ожидаемые доли по корзинам.
    """
    ref: dict[str, dict] = {}
    qs = np.linspace(0, 1, N_BINS + 1)[1:-1]
    for i, f in enumerate(FEATURES):
        col = X[:, i].astype(float)
        edges = np.unique(np.quantile(col, qs))  # unique: дискретные фичи
        counts, _ = np.histogram(col, bins=np.concatenate(
            ([-np.inf], edges, [np.inf])))
        ref[f] = {
            "edges": edges.tolist(),
            "expected": (counts / max(len(col), 1)).tolist(),
        }
    return ref


def psi(expected: np.ndarray, actual: np.ndarray) -> float:
    e = np.clip(np.asarray(expected, dtype=float), _EPS, None)
    a = np.clip(np.asarray(actual, dtype=float), _EPS, None)
    e, a = e / e.sum(), a / a.sum()
    return float(np.sum((a - e) * np.log(a / e)))


def feature_psi(reference: dict, X: np.ndarray) -> dict[str, float]:
    """PSI каждой фичи текущих данных X против эталона артефакта."""
    out: dict[str, float] = {}
    for i, f in enumerate(FEATURES):
        ref = reference.get(f)
        if ref is None:  # фича появилась после обучения prod — пропуск
            continue
        edges = np.asarray(ref["edges"], dtype=float)
        counts, _ = np.histogram(X[:, i].astype(float), bins=np.concatenate(
            ([-np.inf], edges, [np.inf])))
        actual = counts / max(len(X), 1)
        out[f] = psi(np.asarray(ref["expected"]), actual)
    return out
