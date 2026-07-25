"""Калибраторы вероятностей, разделяемые обучением и сервингом.

Модуль вынесен отдельно НАМЕРЕННО. Калибратор попадает в артефакт как
pickle, а pickle сохраняет путь к классу (`модуль.Класс`). Пока класс жил
в `training/train_winprob.py`, запуск обучения через
`python -m training.train_winprob` делал этот модуль `__main__`, и в
артефакт записывалось `__main__._PlattCalibrator`. При загрузке в другом
процессе (ml-service, где `__main__` — это `app.py`) распаковка падала:

    AttributeError: module '__main__' has no attribute '_PlattCalibrator'

Здесь путь к классу стабилен (`calibration.PlattCalibrator`) независимо от
того, как запущено обучение. Совместимость со старыми артефактами —
`register_legacy_aliases()` ниже.
"""
from __future__ import annotations

import sys

import numpy as np


class PlattCalibrator:
    """Platt scaling: логистическая регрессия на сыром скоре бустера.

    Интерфейс совместим с IsotonicRegression (predict → вероятности),
    сериализуется joblib'ом как часть артефакта.
    """

    def __init__(self):
        from sklearn.linear_model import LogisticRegression
        self._lr = LogisticRegression()

    def fit(self, raw: np.ndarray, y: np.ndarray) -> "PlattCalibrator":
        self._lr.fit(np.asarray(raw).reshape(-1, 1), y)
        return self

    def predict(self, raw: np.ndarray) -> np.ndarray:
        return self._lr.predict_proba(np.asarray(raw).reshape(-1, 1))[:, 1]


def register_legacy_aliases() -> None:
    """Сделать загружаемыми артефакты, обученные до выделения модуля.

    В них калибратор записан как `__main__._PlattCalibrator` (обучение
    запускалось как `python -m training.train_winprob`). Регистрируем имя
    в текущем `__main__`, чтобы pickle его нашёл. Без этого пришлось бы
    переобучать модель только ради загрузки — при том что сам артефакт
    (бустер + коэффициенты) полностью валиден.

    Вызывается предиктором перед joblib.load; идемпотентна.
    """
    main = sys.modules.get("__main__")
    if main is not None and not hasattr(main, "_PlattCalibrator"):
        main._PlattCalibrator = PlattCalibrator      # type: ignore[attr-defined]
