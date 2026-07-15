"""CLI слежения за обучением: python -m training.status

Показывает production-версию Win Probability и её метрики, разрыв между
накопленным датасетом и датасетом production (когда сработает
переобучение), и последние версии-кандидаты из реестра с исходом гейта.
Ходит только в реестр моделей (MinIO) — безопасно запускать в любой момент.
"""
from __future__ import annotations

import os

from registry import registry_from_env

MODEL = "win_probability"


def _dataset_size() -> int | None:
    """Текущее число матчей в витрине (для разрыва с production)."""
    try:
        import requests

        resp = requests.post(
            os.getenv("CLICKHOUSE_URL", "http://localhost:8123"),
            params={"database": os.getenv("CLICKHOUSE_DB", "manta")},
            data="SELECT countDistinct(match_id) FROM MatchTimelineFeatures",
            headers={"X-ClickHouse-User": os.getenv("CLICKHOUSE_USER", "dota"),
                     "X-ClickHouse-Key": os.getenv("CLICKHOUSE_PASSWORD",
                                                   "dota_dev_password")},
            timeout=10)
        resp.raise_for_status()
        return int(resp.text.strip())
    except Exception:  # noqa: BLE001 — статус не должен падать без ClickHouse
        return None


def main() -> int:
    reg = registry_from_env()
    prod = reg.stage_metadata(MODEL)
    versions = reg.list_versions(MODEL)

    print("=" * 60)
    print("  Manta · Win Probability — статус обучения")
    print("=" * 60)

    if prod is None:
        print("production-версия не назначена")
    else:
        m = prod["metrics"]
        print(f"PRODUCTION : {prod['registry_version']}")
        print(f"  Brier эталон (pro) : {m.get('brier_benchmark_pro', '—')}"
              f"   (цель ≤ 0.18)")
        print(f"  Brier валидация    : {m.get('brier_calibrated', '—')}")
        print(f"  обучена на         : {prod['dataset']['matches']} матчах")

    n = _dataset_size()
    if n is not None and prod is not None:
        gap = n - prod["dataset"]["matches"]
        thr = int(os.getenv("RETRAIN_MIN_NEW_MATCHES", "20"))
        sign = f"+{gap}" if gap >= 0 else str(gap)
        print(f"\nдатасет сейчас     : {n} матчей  ({sign} к production)")
        if gap < 0:
            print("порог переобучения : датасет меньше production "
                  "(перенаполняется) — переобучение при +"
                  f"{thr} к текущему")
        else:
            print(f"порог переобучения : +{thr}  →  "
                  f"{'готово к переобучению' if gap >= thr else 'ждём ещё матчей'}")

    print(f"\nвсего версий в реестре: {len(versions)}")
    print("последние кандидаты (гейт сравнивает по эталону):")
    for v in versions[-5:]:
        _, meta = reg.resolve(MODEL, v)
        bm = meta["metrics"].get("brier_benchmark_pro", "?")
        prod_flag = " ← PRODUCTION" if prod and v == prod["registry_version"] else ""
        print(f"  {v}  эталон={bm}  датасет={meta['dataset']['matches']}{prod_flag}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
