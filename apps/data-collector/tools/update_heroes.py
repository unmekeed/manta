"""Обновить справочник героев из OpenDota `/heroes` (спринт 187).

ЗАЧЕМ. До этого спринта libs/data/heroes.json хранил только id и имя —
ровно то, что нужно было для таблицы драфта. Свойства героя (атрибут,
тип атаки, роли) нужны модели: они позволяют говорить о составе команды,
не зная поимённо каждого героя.

ЭТО СНИМОК, А НЕ ЖИВОЙ ЗАПРОС. Справочник меняется парой строк за патч,
а читается в каждом разборе матча — ходить за ним в сеть значило бы
добавить сетевую зависимость туда, где её нет, и уронить разбор при
недоступном API.

    python tools/update_heroes.py          # переписать снимок
    python tools/update_heroes.py --check   # только показать расхождения

После обновления числа в diff коммита обязаны быть объяснены: новый
герой — это одна строка, а изменившиеся роли у старого — это изменение
СМЫСЛА фич композиции.
"""
from __future__ import annotations

import json
import pathlib
import sys

import requests

DEST = pathlib.Path(__file__).resolve().parents[3] / "libs" / "data" / "heroes.json"
API = "https://api.opendota.com/api/heroes"


def fetch() -> dict:
    resp = requests.get(API, timeout=30)
    resp.raise_for_status()
    out = {}
    for h in resp.json():
        name = h.get("name")
        if not name or "id" not in h:
            continue
        out[name] = {
            "id": int(h["id"]),
            "localized_name": h.get("localized_name", ""),
            "primary_attr": h.get("primary_attr", ""),
            "attack_type": h.get("attack_type", ""),
            "roles": sorted(h.get("roles") or []),
        }
    return dict(sorted(out.items()))


def main() -> int:
    fresh = fetch()
    if not fresh:
        print("API вернул пустой список — снимок не трогаю", file=sys.stderr)
        return 1
    old = json.loads(DEST.read_text(encoding="utf-8")) if DEST.exists() else {}
    added = sorted(set(fresh) - set(old))
    gone = sorted(set(old) - set(fresh))
    changed = [k for k in fresh if k in old and old[k] != fresh[k]]
    print(f"героев: {len(fresh)}; новых {len(added)}, "
          f"исчезло {len(gone)}, изменилось {len(changed)}")
    for k in added:
        print(f"  + {k}")
    for k in gone:
        print(f"  − {k}")
    if "--check" in sys.argv:
        return 0
    DEST.write_text(json.dumps(fresh, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8")
    print(f"снимок обновлён: {DEST}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
