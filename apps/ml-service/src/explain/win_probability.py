"""CLI: WP-кривая матча с SHAP-драйверами каждой точки.

python -m explain.win_probability MATCH_ID [--model PATH] [--top K]

Поверх predictors.win_probability: тот же артефакт и таймлайн, плюс
топ-K фич, толкающих вероятность в этой минуте (знак = направление,
величина — в лог-оддсах; см. explain.winprob_shap).
"""
from __future__ import annotations

import argparse
import json
import os

import numpy as np
import requests

from predictors.win_probability import DEFAULT_MODEL, WinProbability
from training.dataset import row_to_features

from .winprob_shap import contributions, top_drivers


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("match_id", type=int)
    ap.add_argument("--model", default=str(DEFAULT_MODEL))
    ap.add_argument("--top", type=int, default=3)
    args = ap.parse_args()

    model = WinProbability(args.model)
    resp = requests.post(
        os.getenv("CLICKHOUSE_URL", "http://localhost:8123"),
        params={"database": os.getenv("CLICKHOUSE_DB", "manta"),
                "default_format": "JSONEachRow",
                "param_match_id": str(args.match_id)},
        data="SELECT game_time, networth_diff, xp_diff,"
             "       kills_radiant, kills_dire, position_advance,"
             "       alive_diff, towers_diff, rax_diff"
             "  FROM MatchTimelineFeatures FINAL"
             " WHERE match_id = {match_id:UInt64} ORDER BY game_time",
        headers={"X-ClickHouse-User": os.getenv("CLICKHOUSE_USER", "dota"),
                 "X-ClickHouse-Key": os.getenv("CLICKHOUSE_PASSWORD",
                                               "dota_dev_password")},
        timeout=60)
    resp.raise_for_status()
    rows = [json.loads(line) for line in resp.text.splitlines() if line]
    if not rows:
        print(f"нет таймлайна для матча {args.match_id}")
        return 1

    X = np.array([row_to_features(r) for r in rows])[:, :len(model.features)]
    wp = model.predict(X)
    contribs, _ = contributions(model.booster, X)
    drivers = top_drivers(contribs, model.features, args.top)

    print(f"model v{model.version}; вклады — лог-оддсы "
          f"(+ за Radiant, − за Dire)")
    for r, p, drv in zip(rows, wp, drivers):
        parts = "  ".join(f"{name}{val:+.2f}" for name, val in drv)
        bar = "#" * int(p * 30)
        print(f"{int(r['game_time']):>6}s  {p:.3f} {bar:<31} {parts}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
