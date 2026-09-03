"""Карточка матча для списка `/api/v1/matches` (спринт 192).

ЗАЧЕМ ОТДЕЛЬНЫМ МОДУЛЕМ. Карточка — это КОНТРАКТ с сайтом, а не деталь
генерации отчёта. Держать её сборку внутри `runner.generate()` значило
бы, что проверить её можно только вместе с Kafka, ClickHouse и gRPC. Так
её и не проверяли бы.

ЧТО ЗДЕСЬ ДОРОГО ОШИБИТЬСЯ — то же, что и везде в этом проекте: ноль
против пропуска. Финальная WP у матча без обслуживаемой модели
ОТСУТСТВУЕТ, и подставить 0.5 значило бы показать пользователю
выдуманное число, неотличимое от честной ничьей. Счёт 0:0 у матча без
убийств — правда; счёт 0:0 у матча, чьи данные не доехали, — ложь.
Поэтому неизвестная WP остаётся None, а вот счёт и длительность берутся
только из строк, которые есть.

СТОРОНЫ. В `PlayerMatchFeatures` команда кодируется как 2 (Radiant) и 3
(Dire) — это коды Valve, а не 0/1. Перепутать их местами нельзя заметить
глазами: составы просто поменяются, и все карточки будут выглядеть
правдоподобно. Поэтому коды заданы константами и закреплены тестом.
"""
from __future__ import annotations

RADIANT_TEAM = 2
DIRE_TEAM = 3


def build_summary(match_id: int, rows: list[dict], players: list[dict],
                  analysis: dict) -> dict:
    """Карточка матча из того, что уже прочитано ради отчёта.

    `rows` — поминутная витрина (последняя строка несёт исход, патч и
    уровень), `players` — PlayerMatchFeatures, `analysis` — готовый
    разбор (из него берётся только финальная WP).
    """
    last = rows[-1]
    radiant, dire = [], []
    for p in players:
        hero = str(p.get("hero") or "")
        if not hero:
            continue
        (radiant if int(p.get("team", 0)) == RADIANT_TEAM else dire).append(hero)

    return {
        "match_id": int(match_id),
        "radiant_win": bool(int(last.get("radiant_win", 0))),
        "kills_radiant": int(last.get("kills_radiant", 0) or 0),
        "kills_dire": int(last.get("kills_dire", 0) or 0),
        "duration_s": _duration(players, rows),
        "patch": int(last.get("patch", 0) or 0),
        "tier": str(last.get("tier", "") or ""),
        "radiant_heroes": radiant,
        "dire_heroes": dire,
        "final_radiant_wp": _final_wp(analysis),
    }


def _duration(players: list[dict], rows: list[dict]) -> int:
    """Длительность матча в секундах.

    Берётся из `duration_s` витрины игроков — это настоящая длительность
    из ответа API. Запасной вариант — время последней точки таймлайна, но
    он ХУЖЕ и это надо понимать: сетка поминутная, поэтому такая оценка
    всегда занижена и округлена вниз до минуты. Использовать её как
    основную значило бы молча укоротить каждый матч.
    """
    for p in players:
        d = int(p.get("duration_s", 0) or 0)
        if d > 0:
            return d
    return int(rows[-1].get("game_time", 0) or 0) if rows else 0


def _final_wp(analysis: dict) -> float | None:
    """Финальная вероятность победы Radiant; None — модель не считала.

    Разбор хранит её строкой в `win_probability.final_radiant`. Пустая
    строка и мусор дают None, а не ноль: ноль — это «Radiant точно
    проиграл», утверждение куда более сильное, чем «мы не знаем».
    """
    raw = ((analysis.get("win_probability") or {}).get("final_radiant"))
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


UPSERT_SQL = """
INSERT INTO MatchSummaries
       (match_id, radiant_win, kills_radiant, kills_dire, duration_s,
        patch, tier, radiant_heroes, dire_heroes, final_radiant_wp,
        generated_at)
VALUES (%(match_id)s, %(radiant_win)s, %(kills_radiant)s, %(kills_dire)s,
        %(duration_s)s, %(patch)s, %(tier)s, %(radiant_heroes)s,
        %(dire_heroes)s, %(final_radiant_wp)s, NOW())
ON CONFLICT (match_id) DO UPDATE SET
       radiant_win      = EXCLUDED.radiant_win,
       kills_radiant    = EXCLUDED.kills_radiant,
       kills_dire       = EXCLUDED.kills_dire,
       duration_s       = EXCLUDED.duration_s,
       patch            = EXCLUDED.patch,
       tier             = EXCLUDED.tier,
       radiant_heroes   = EXCLUDED.radiant_heroes,
       dire_heroes      = EXCLUDED.dire_heroes,
       final_radiant_wp = EXCLUDED.final_radiant_wp,
       generated_at     = NOW()
"""
