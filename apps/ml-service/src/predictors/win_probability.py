"""Предиктор Win Probability: загрузка артефакта и WP-кривая матча.

CLI: python -m predictors.win_probability MATCH_ID [--model PATH]
печатает поминутную кривую вероятности победы Radiant.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

import joblib
import lightgbm as lgb
import numpy as np
import requests

from training.dataset import row_to_features

DEFAULT_MODEL = Path(__file__).resolve().parents[2] / "models" / "win_probability.pkl"


class WinProbability:
    def __init__(self, artifact_path: str | os.PathLike = DEFAULT_MODEL):
        art = joblib.load(artifact_path)
        self.booster = lgb.Booster(model_str=art["booster"])
        self.calibrator = art["calibrator"]
        # Набор фич — из артефакта: предиктор обязан работать и со
        # старыми версиями модели (меньше фич), и с новыми.
        self.features = art["features"]
        self.version = art["model_version"]
        self.metrics = art["metrics"]

    def predict(self, X: np.ndarray) -> np.ndarray:
        """Калиброванная вероятность победы Radiant для матрицы фич."""
        raw = self.booster.predict(X)
        return self.calibrator.predict(raw)

    def wp_curve(self, match_id: int, ch_url: str, db: str,
                 user: str, password: str) -> list[dict]:
        """Кривая WP по снапшотам MatchTimelineFeatures матча."""
        resp = requests.post(
            ch_url,
            params={"database": db, "default_format": "JSONEachRow",
                    "param_match_id": str(match_id)},
            data="SELECT game_time, networth_diff, xp_diff,"
                 "       kills_radiant, kills_dire, position_advance,"
                 "       alive_diff, towers_diff, rax_diff"
                 "  FROM MatchTimelineFeatures FINAL"
                 " WHERE match_id = {match_id:UInt64} ORDER BY game_time",
            headers={"X-ClickHouse-User": user, "X-ClickHouse-Key": password},
            timeout=60)
        resp.raise_for_status()
        rows = [json.loads(line) for line in resp.text.splitlines() if line]
        if not rows:
            return []
        X = np.array([row_to_features(r) for r in rows])
        # Старый артефакт (меньше фич) — режем вектор до его набора.
        X = X[:, :len(self.features)]
        wp = self.predict(X)
        return [{"game_time": int(r["game_time"]), "wp_radiant": round(float(p), 4)}
                for r, p in zip(rows, wp)]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("match_id", type=int)
    ap.add_argument("--model", default=str(DEFAULT_MODEL))
    args = ap.parse_args()

    model = WinProbability(args.model)
    curve = model.wp_curve(
        args.match_id,
        os.getenv("CLICKHOUSE_URL", "http://localhost:8123"),
        os.getenv("CLICKHOUSE_DB", "manta"),
        os.getenv("CLICKHOUSE_USER", "dota"),
        os.getenv("CLICKHOUSE_PASSWORD", "dota_dev_password"))
    if not curve:
        print(f"нет таймлайна для матча {args.match_id}")
        return 1
    print(f"model v{model.version} metrics={model.metrics}")
    for pt in curve:
        bar = "#" * int(pt["wp_radiant"] * 40)
        print(f"{pt['game_time']:>6}s  {pt['wp_radiant']:.3f} {bar}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
