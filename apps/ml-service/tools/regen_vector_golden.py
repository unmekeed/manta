"""Генератор голден-фикстуры для ВЕКТОРА фич модели (спринт 137).

    make signals-golden-update

Голдены коллектора и производных закрепляют по куску: 14 фич из JSON и 15
темпов. Здесь закрепляется точка сборки — `row_to_features`: строка
витрины на входе, все 41 число в порядке FEATURES на выходе.

Это самый дорогой из трёх голденов, потому что он единственный проверяет
ПОРЯДОК. Его расхождение уже стоило проекту простоя: спринты 90 и 91
добавили фичи в FEATURES и не добавили в сборку вектора, обучение падало
IndexError внутри зеркальной аугментации несколько суток, а production
стоял на старой модели. Список тогда перевели на сборку по именам, но
сама расстановка по порядку FEATURES так и осталась непроверенной
численно: перестановка двух колонок не ломает ничего заметного, а модель
молча учится на перепутанных признаках.

Второе, что здесь закреплено, — политика пропусков. Отсутствующая фича
обязана давать NaN, а не ноль: ноль для разностной фичи означает «ровно
посередине», то есть ложный сигнал вместо честного пропуска. Правило
живёт в `_f`, и подмена его на `or 0.0` не ломает ни один тип.
"""
from __future__ import annotations

import json
import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT.parents[1] / "libs"))

from wp_rates import RATE_METRICS, RATE_WINDOWS, prev_col, prev_time_col  # noqa: E402

from training.dataset import FEATURES, row_to_features  # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures"

# Уровни, которые нужны производным. Значения разного порядка и разных
# знаков: на одинаковых числах перестановка колонок была бы незаметна.
_LEVELS_PREV = {
    60:  {"networth_diff": 4100.0, "xp_diff": -2800.0, "towers_diff": 1.0,
          "vision_coverage_diff": 0.100, "levels_diff": -6.0},
    180: {"networth_diff": 2000.0, "xp_diff": -1500.0, "towers_diff": 1.0,
          "vision_coverage_diff": 0.075, "levels_diff": -2.0},
    300: {"networth_diff": -500.0, "xp_diff": 900.0, "towers_diff": 0.0,
          "vision_coverage_diff": 0.000, "levels_diff": 1.0},
}


def _windows(game_time: int) -> dict:
    out: dict = {}
    for w in RATE_WINDOWS:
        out[prev_time_col(w)] = game_time - w
        for m in RATE_METRICS:
            out[prev_col(m, w)] = _LEVELS_PREV[w][m]
    return out


def full_row() -> dict:
    """Строка, где есть ВСЁ: полный путь реплея после миграции 021."""
    return {
        "game_time": 900,
        "networth_diff": 5000, "networth_total": 80000,
        "xp_diff": -3200,
        "kills_radiant": 14, "kills_dire": 9,
        "position_advance": 0.35, "alive_diff": 2,
        "towers_diff": 2, "rax_diff": -1,
        "local_manpower_diff": 1.5, "spread_diff": -0.25,
        # Числа намеренно попарно РАЗНЫЕ. Сравнение вектора поймает
        # перестановку двух колонок только если у них разные значения; на
        # трёх фичах, равных единице, порядок не проверяется вовсе.
        "roshan_diff": 3, "aegis_alive": -1, "buybacks_diff": 2,
        "first_blood": 1, "item_value_diff": 3925, "unspent_gold_diff": 1150,
        "buyback_availability": -2, "key_items_diff": 4,
        "obs_wards_diff": 5, "vision_coverage_diff": 0.125,
        "sen_wards_diff": -3, "runes_diff": 6, "neutral_tier_diff": 7,
        "levels_diff": -4,
        # Спринт 185: готовые ряды OpenDota. Числа опять же попарно
        # РАЗНЫЕ — эталон ловит перестановку колонок только тогда, когда
        # значения различаются; пять одинаковых пропусков дали бы пять
        # взаимозаменяемых фич, и тест это сразу показал.
        "lh_diff": 42, "dn_diff": -8, "hero_damage_diff": 15300,
        "hero_healing_diff": -640, "camps_stacked_diff": 9,
        # Спринт 186: драки. Снова попарно разные числа — иначе эталон не
        # заметит перестановки колонок.
        "fights_won_diff": 2, "fight_gold_diff": 1750,
        "fight_deaths_diff": -3, "since_fight_s": 95,
        **_windows(900),
    }


def scenarios() -> list[dict]:
    json_row = full_row()
    # JSON-путь: геометрии в нём нет, эти три фичи не считаются вовсе.
    for key in ("position_advance", "alive_diff", "local_manpower_diff",
                "spread_diff"):
        json_row[key] = None

    replay_row = full_row()
    # Реплейный путь: готовых рядов OpenDota в демке нет — они приходят
    # только с JSON-ответом (спринт 185). Сценарий нужен, чтобы политика
    # «пропуск, а не ноль» проверялась и на них.
    for key in ("lh_diff", "dn_diff", "hero_damage_diff",
                "hero_healing_diff", "camps_stacked_diff",
                # Драки тоже приходят только с JSON-ответом (спринт 186).
                "fights_won_diff", "fight_gold_diff", "fight_deaths_diff",
                "since_fight_s"):
        replay_row[key] = None

    old_row = full_row()
    # Строки, собранные до миграции 008: зданий и живых героев нет.
    for key in ("alive_diff", "towers_diff", "rax_diff"):
        old_row[key] = None

    zero_total = full_row()
    zero_total["networth_total"] = 0

    no_total = full_row()
    no_total["networth_total"] = None

    bad_total = full_row()
    bad_total["networth_total"] = -80000

    stratz_row = full_row()
    # У STRATZ нет вардов вовсе: ноль означал бы «видят одинаково».
    for key in ("obs_wards_diff", "sen_wards_diff", "vision_coverage_diff",
                "unspent_gold_diff", "buyback_availability"):
        stratz_row[key] = None

    first_row = full_row()
    first_row["game_time"] = 0
    for w in RATE_WINDOWS:
        first_row[prev_time_col(w)] = 0

    return [
        {"name": "полная строка реплея",
         "why": "все фичи известны — базовый эталон и проверка порядка",
         "row": full_row()},
        {"name": "JSON-путь: нет геометрии",
         "why": "position_advance/alive_diff/manpower/spread считаются "
                "только из реплея. Ждём NaN, а не ноль: ноль у разностной "
                "фичи значит «ровно посередине» — ложный сигнал",
         "row": json_row},
        {"name": "реплейный путь: нет рядов OpenDota",
         "why": "lh/dn/урон/лечение/стаки приходят только с JSON-ответом "
                "(спринт 185). У матча, разобранного из демки, их нет — "
                "и это пропуск, а не «никто не фармил»",
         "row": replay_row},
        {"name": "строка до миграции 008",
         "why": "зданий и живых героев в старых строках нет; модель обязана "
                "получить пропуск, а не выдуманное равенство",
         "row": old_row},
        {"name": "networth_total = 0",
         "why": "networth_rel — это деление. Ноль в знаменателе обязан "
                "давать NaN, а не падение и не ноль",
         "row": zero_total},
        {"name": "networth_total неизвестен",
         "why": "старые строки без итога: доля преимущества не определена",
         "row": no_total},
        {"name": "networth_total отрицателен",
         "why": "сумма нетворса обеих сторон отрицательной быть не может, "
                "поэтому минус в этой колонке — испорченные данные, и "
                "защита в коде стоит именно на `> 0`, а не на `!= 0`. Без "
                "этого сценария разница между двумя условиями не "
                "проверяется: на нуле они ведут себя одинаково",
         "row": bad_total},
        {"name": "STRATZ: нет вардов и золота",
         "why": "пять фич отсутствуют целыми группами — проверяем, что "
                "пропуск не растекается на соседние колонки",
         "row": stratz_row},
        {"name": "первая строка матча",
         "why": "производных ещё не существует (dt = 0), а уровни уже есть: "
                "NaN обязан прийти ровно в 15 колонок из 41",
         "row": first_row},
    ]


def _jsonable(value):
    if isinstance(value, float) and math.isnan(value):
        return None
    return value


def main() -> int:
    out = []
    for sc in scenarios():
        vector = row_to_features(sc["row"])
        if len(vector) != len(FEATURES):
            print(f"ОШИБКА: {sc['name']}: вектор {len(vector)} против "
                  f"{len(FEATURES)} фич", file=sys.stderr)
            return 2
        out.append({**sc,
                    "row": {k: _jsonable(v) for k, v in sc["row"].items()},
                    "expected": [_jsonable(v) for v in vector]})

    FIXTURES.mkdir(parents=True, exist_ok=True)
    (FIXTURES / "golden_vector.json").write_text(
        json.dumps({"features": list(FEATURES), "scenarios": out},
                   ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(f"эталон вектора обновлён: {len(out)} сценариев, "
          f"{len(FEATURES)} фич в порядке FEATURES")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
