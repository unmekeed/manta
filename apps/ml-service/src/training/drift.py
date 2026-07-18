"""Дрейф-мониторинг фич через PSI (Гл. 10.4; риск R-02 — дрейф меты).

PSI (Population Stability Index) сравнивает распределение фичи в текущей
витрине с референсным распределением, зафиксированным при обучении
production-модели: Σ (p_cur − p_ref) · ln(p_cur / p_ref) по бинам.

Интерпретация (общепринятые пороги): < 0.1 — стабильно; 0.1–0.2 — умеренный
сдвиг; > 0.2 — значимый дрейф, модель смотрит на другую игру, чем та, на
которой училась (типично после баланс-патча Dota). Значимый дрейф — повод
переобучиться, даже если новых матчей меньше обычного порога.

Бины — децили референса (quantile binning): устойчиво к выбросам и не
требует доменных знаний о масштабе фичи.
"""
from __future__ import annotations

import numpy as np

DEFAULT_BINS = 10
# Значимый дрейф по конвенции PSI; порог триггера переобучения.
PSI_SIGNIFICANT = 0.2
_EPS = 1e-4


def compute_reference(X: np.ndarray, features: list[str],
                      bins: int = DEFAULT_BINS) -> dict:
    """Референс распределения фич для артефакта модели.

    Для каждой фичи — границы децильных бинов по обучающим данным и доли
    строк в каждом бине. Крайние границы расширяются до ±inf при применении
    (новые данные за пределами референсного диапазона попадают в крайние
    бины, а не теряются).
    """
    ref: dict[str, dict] = {}
    for i, name in enumerate(features):
        col = X[:, i].astype(float)
        col = col[~np.isnan(col)]  # NaN — «фичи нет» (JSON-матчи), не сигнал
        if len(col) == 0:
            ref[name] = {"edges": [], "props": [1.0]}
            continue
        qs = np.quantile(col, np.linspace(0, 1, bins + 1))
        edges = np.unique(qs)  # константная фича схлопывается в один бин
        if len(edges) < 2:
            ref[name] = {"edges": edges.tolist(), "props": [1.0]}
            continue
        props = _bin_props(col, edges)
        ref[name] = {"edges": edges.tolist(), "props": props.tolist()}
    return ref


def _bin_props(col: np.ndarray, edges: np.ndarray) -> np.ndarray:
    """Доли строк по бинам; крайние бины открыты (±inf), NaN исключаются
    (PSI сравнивает распределения НАБЛЮДАЕМЫХ значений; рост доли
    JSON-матчей без position_advance — не дрейф самой фичи)."""
    col = col[~np.isnan(col)]
    open_edges = np.concatenate(([-np.inf], edges[1:-1], [np.inf]))
    counts, _ = np.histogram(col, bins=open_edges)
    total = counts.sum()
    if total == 0:
        return np.full(len(counts), 1.0 / len(counts))
    return counts / total


def psi(ref_props: np.ndarray, cur_props: np.ndarray) -> float:
    """PSI двух распределений по одинаковым бинам (доли, в сумме 1)."""
    p = np.clip(np.asarray(ref_props, dtype=float), _EPS, None)
    q = np.clip(np.asarray(cur_props, dtype=float), _EPS, None)
    p, q = p / p.sum(), q / q.sum()
    return float(np.sum((q - p) * np.log(q / p)))


def psi_report(reference: dict, X: np.ndarray, features: list[str]) -> dict[str, float]:
    """PSI каждой фичи текущих данных против референса модели.

    Фичи, отсутствующие в референсе (старый артефакт), пропускаются.
    """
    out: dict[str, float] = {}
    for i, name in enumerate(features):
        entry = reference.get(name)
        if not entry or len(entry.get("edges", [])) < 2:
            continue
        edges = np.asarray(entry["edges"], dtype=float)
        cur = _bin_props(X[:, i].astype(float), edges)
        out[name] = round(psi(np.asarray(entry["props"]), cur), 4)
    return out


def max_psi(report: dict[str, float]) -> float:
    return max(report.values(), default=0.0)
