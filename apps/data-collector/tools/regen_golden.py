"""Генератор голден-фикстуры для регресса значений фич (спринт 137).

Запускается руками, когда формулу МЕНЯЮТ ОСОЗНАННО:

    make signals-golden-update

Отдельный скрипт, а не флаг в тесте: обновление эталона — это решение
(«да, я хотел изменить число»), и оно должно быть отдельным действием с
отдельной строкой в diff. Автообновление из-под pytest превратило бы
регресс в самоподтверждающийся: любая правка молча переписывала бы
эталон под себя.
"""
from __future__ import annotations

import json
import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from collector.signals import all_minute_features  # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures"

# Сетка намеренно доходит до 19-й минуты, а не до 8-й: тир нейтралки
# растёт по времени (7, 17, 27… минута), и на короткой сетке фича
# успевала бы дойти только до первого тира из пяти — то есть ступенька
# 1→2 не проверялась бы вовсе.
MINUTES = [60, 120, 180, 240, 300, 360, 420, 480, 600, 720, 900, 1020, 1140]

RADIANT_SLOTS = [0, 1, 2, 3, 4]
DIRE_SLOTS = [128, 129, 130, 131, 132]


def _series(base: int, step: int, n: int = 21) -> list[int]:
    """Накопительный поминутный ряд (gold_t/xp_t у OpenDota именно такие)."""
    return [base + step * i for i in range(n)]


def build_match() -> dict:
    """Матч, в котором ЗАДЕЙСТВОВАНА каждая фича.

    Синтетический, а не реальный, и это осознанный выбор:

      * реальный JSON матча — это мегабайты в репозитории и account_id
        живых людей, которые проект специально псевдонимизирует;
      * синтетический читается глазами, и когда тест упадёт, видно не
        только КАКОЕ число уехало, но и от какого входа.

    Что синтетика НЕ ловит — неожиданную форму реальных данных. Это
    задача поведенческих тестов рядом (их 37) и самого сбора, а не
    регресса значений.

    Стороны намеренно асимметричны: на симметричном входе все разностные
    фичи равны нулю, и тест был бы зелёным при почти любой ошибке знака.
    """
    players = []
    for i, slot in enumerate(RADIANT_SLOTS + DIRE_SLOTS):
        radiant = slot < 128
        p: dict = {
            "player_slot": slot,
            "hero_id": 1 + i,
            # Radiant богаче и опытнее — знак разностных фич проверяем.
            "gold_t": _series(300 if radiant else 250,
                              420 if radiant else 360),
            "xp_t": _series(200 if radiant else 150,
                            520 if radiant else 460),
            "purchase_log": [],
            "runes_log": [],
            "obs_log": [],
            "obs_left_log": [],
            "sen_log": [],
            "buyback_log": [],
        }
        players.append(p)

    r0, r1, r2, r3, r4 = players[0], players[1], players[2], players[3], players[4]
    d0, d1 = players[5], players[6]

    # Предметы. `aether_lens` намеренно НЕ из KEY_ITEMS: он поднимает
    # item_value_diff, но обязан не двигать key_items_diff — иначе две
    # фичи стали бы копиями друг друга и тест этого бы не заметил.
    r0["purchase_log"] = [{"time": 200, "key": "blink"},
                          {"time": 420, "key": "black_king_bar"},
                          {"time": 900, "key": "heart"}]
    r1["purchase_log"] = [{"time": 300, "key": "aether_lens"}]
    d0["purchase_log"] = [{"time": 260, "key": "manta"}]
    d1["purchase_log"] = [{"time": 800, "key": "sheepstick"}]

    # Варды с координатами — из них считается площадь обзора.
    #
    # Пара r1 стоит ВПЛОТНУЮ (100,100) и (104,104), и это главное место
    # фикстуры. Площадь считается как ОБЪЕДИНЕНИЕ дисков, ради чего фича
    # и заводилась: «три варда по карте» должны отличаться от «трёх в
    # одном лесу». На разнесённых вардах объединение численно совпадает
    # с суммой площадей, и подмена одного другим прошла бы незамеченной.
    r0["obs_log"] = [{"time": 100, "x": 100, "y": 100},
                     {"time": 260, "x": 150, "y": 140}]
    r0["obs_left_log"] = [{"time": 400}]     # снят раньше своих 6 минут
    r1["obs_log"] = [{"time": 700, "x": 100, "y": 100},
                     {"time": 760, "x": 104, "y": 104}]
    d0["obs_log"] = [{"time": 130, "x": 120, "y": 120}]
    d1["obs_log"] = [{"time": 720, "x": 170, "y": 170}]

    r1["sen_log"] = [{"time": 170, "x": 110, "y": 110}]
    r3["sen_log"] = [{"time": 1000, "x": 90, "y": 90}]
    d1["sen_log"] = [{"time": 190, "x": 130, "y": 130},
                     {"time": 350, "x": 135, "y": 125}]

    # Руны и выкупы. Выкуп есть у обеих сторон, чтобы buybacks_diff
    # прошёл через ноль, а не просто уехал в минус и там остался.
    r2["runes_log"] = [{"time": 240, "key": 5}, {"time": 480, "key": 2}]
    r4["runes_log"] = [{"time": 900, "key": 3}]
    d1["runes_log"] = [{"time": 300, "key": 1}]
    d0["buyback_log"] = [{"time": 380}]
    r3["buyback_log"] = [{"time": 700}]

    # Нейтралки: тир приписывается по времени, имя роли не играет, но
    # СЧЁТ предметов играет — вклад равен (число у Radiant − у Dire) на
    # тир. Поэтому предметов у сторон разное количество: на равном счёте
    # фича тождественно ноль при любом коде.
    r0["item_neutral"] = "trusty_shovel"
    r1["item_neutral"] = "pig_pole"
    d0["item_neutral"] = "faded_broach"

    return {
        "match_id": 7777777777,
        "radiant_win": True,
        "patch": 60,
        "duration": 1200,
        "players": players,
        "objectives": [
            {"time": 90, "type": "CHAT_MESSAGE_FIRSTBLOOD", "player_slot": 0},
            {"time": 300, "type": "CHAT_MESSAGE_ROSHAN_KILL", "team": 2},
            {"time": 305, "type": "CHAT_MESSAGE_AEGIS", "player_slot": 1},
            {"time": 450, "type": "CHAT_MESSAGE_ROSHAN_KILL", "team": 3},
            {"time": 460, "type": "CHAT_MESSAGE_AEGIS", "player_slot": 128},
            # Кража аегиса переводит его другой стороне — ветка, которой
            # без этого события в фикстуре просто нет.
            {"time": 600, "type": "CHAT_MESSAGE_AEGIS_STOLEN",
             "player_slot": 2},
            {"time": 1000, "type": "CHAT_MESSAGE_ROSHAN_KILL", "team": 2},
        ],
    }


def _jsonable(value):
    """NaN в JSON не пролезает — пишем как null и так же читаем обратно."""
    if isinstance(value, float) and math.isnan(value):
        return None
    return value


def main() -> int:
    match = build_match()
    features = all_minute_features(match, MINUTES)
    if not features:
        print("ОШИБКА: фикстура не дала ни одной фичи", file=sys.stderr)
        return 2

    FIXTURES.mkdir(parents=True, exist_ok=True)
    (FIXTURES / "golden_match.json").write_text(
        json.dumps(match, ensure_ascii=False, indent=1) + "\n",
        encoding="utf-8")
    (FIXTURES / "golden_features.json").write_text(
        json.dumps({"minutes": MINUTES,
                    "features": {k: [_jsonable(x) for x in v]
                                 for k, v in sorted(features.items())}},
                   ensure_ascii=False, indent=1) + "\n",
        encoding="utf-8")
    print(f"эталон обновлён: {len(features)} фич, {len(MINUTES)} минут")
    for name in sorted(features):
        print(f"  {name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
