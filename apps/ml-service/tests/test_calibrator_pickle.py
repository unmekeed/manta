"""Регрессия: артефакт с Platt-калибратором грузится в ЧУЖОМ процессе.

История: калибратор был объявлен внутри training/train_winprob.py, и при
запуске обучения как `python -m training.train_winprob` (то есть
`make ml-train`) pickle записывал в артефакт ссылку `__main__.
_PlattCalibrator`. ml-service, где `__main__` — это app.py, падал на
старте:

    AttributeError: module '__main__' has no attribute '_PlattCalibrator'

Ловилось только на малых датасетах (<PLATT_MAX_MATCHES матчей), поэтому
на большой витрине с изотоникой баг не проявлялся — ровно как на свежей
машине после bootstrap-обучения с синтетикой.
"""
import subprocess
import sys
import textwrap
from pathlib import Path

import joblib
import numpy as np

SRC = str(Path(__file__).resolve().parents[1] / "src")
sys.path.insert(0, SRC)

from calibration import PlattCalibrator, register_legacy_aliases  # noqa: E402


def _fit() -> PlattCalibrator:
    raw = np.linspace(0.0, 1.0, 60)
    y = (raw > 0.5).astype(int)
    return PlattCalibrator().fit(raw, y)


def test_calibrator_pickled_with_importable_path(tmp_path):
    """Класс должен ссылаться на стабильный модуль, а не на __main__."""
    art = tmp_path / "m.pkl"
    joblib.dump({"calibrator": _fit()}, art)
    cls = type(joblib.load(art)["calibrator"])
    assert cls.__module__ == "calibration", (
        f"калибратор сериализован как {cls.__module__}.{cls.__name__} — "
        "в чужом процессе он не найдётся")


def test_loads_in_separate_process(tmp_path):
    """Главная проверка: артефакт грузится там, где его не обучали."""
    art = tmp_path / "m.pkl"
    joblib.dump({"calibrator": _fit()}, art)
    code = textwrap.dedent(f"""
        import sys; sys.path.insert(0, {SRC!r})
        import joblib
        from calibration import register_legacy_aliases
        register_legacy_aliases()
        c = joblib.load({str(art)!r})["calibrator"]
        print(round(float(c.predict([0.9])[0]), 3))
    """)
    out = subprocess.run([sys.executable, "-c", code], capture_output=True,
                         text=True)
    assert out.returncode == 0, f"загрузка упала: {out.stderr}"
    assert 0.0 <= float(out.stdout.strip()) <= 1.0


def test_legacy_main_artifact_still_loads(tmp_path):
    """Артефакты, обученные ДО выделения модуля, должны оставаться
    рабочими: переобучать модель только ради загрузки — недопустимо."""
    art = tmp_path / "legacy.pkl"
    # Воспроизводим старую сериализацию: класс, живущий в __main__.
    trainer = textwrap.dedent(f"""
        import sys; sys.path.insert(0, {SRC!r})
        import joblib, numpy as np
        class _PlattCalibrator:
            def __init__(self):
                from sklearn.linear_model import LogisticRegression
                self._lr = LogisticRegression()
            def fit(self, raw, y):
                self._lr.fit(np.asarray(raw).reshape(-1, 1), y); return self
            def predict(self, raw):
                return self._lr.predict_proba(np.asarray(raw).reshape(-1, 1))[:, 1]
        raw = np.linspace(0, 1, 60)
        joblib.dump({{"calibrator": _PlattCalibrator().fit(raw, (raw > 0.5).astype(int))}},
                    {str(art)!r})
    """)
    assert subprocess.run([sys.executable, "-c", trainer],
                          capture_output=True).returncode == 0

    register_legacy_aliases()
    loaded = joblib.load(art)["calibrator"]
    assert 0.0 <= float(loaded.predict([0.9])[0]) <= 1.0


def test_trainer_alias_points_to_shared_class():
    """training.train_winprob._PlattCalibrator сохранён для совместимости
    кода и обязан быть тем же классом, что в calibration."""
    from training.train_winprob import _PlattCalibrator
    assert _PlattCalibrator is PlattCalibrator
