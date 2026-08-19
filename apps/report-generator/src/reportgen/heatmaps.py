"""Тепловые карты матча для отчёта (спринт 110).

Запрошено владельцем: карты присутствия, фарм-маршруты, варды, смоки,
смерти и важные драки — по разделам игры (эрлигейм, мид, лейтгейм).
Спринты 97–99 добывали для этого сырьё (координаты событий, агрегат
MatchMapCells); здесь оно превращается в готовую секцию отчёта.

Формат — РАЗРЕЖЕННЫЙ: только непустые клетки списком [gx, gy, n].
Сетка 32×32 даёт 1024 клетки на каждую комбинацию фазы, стороны и вида,
то есть 1024 × 3 × 2 × 6 ≈ 37 тысяч чисел на матч, из которых заполнена
малая часть. Отчёт лежит в Postgres одним JSON, и плотная форма раздула
бы его на порядок без единого нового факта.

`farm_core` (спринт 140) — тот же безопасный фарм, но только позиций
1–3. Он ДОПОЛНЯЕТ `farm`, а не заменяет его: в таблице уже лежат матчи,
посчитанные по всей команде, и переопределение сделало бы одно имя
означающим разное в зависимости от даты разбора. У матчей, разобранных
до спринта 140, ключа просто нет — и это честнее пустого списка.

Нормировка (`max_n`) считается ЗДЕСЬ, а не на клиенте: цвет клетки — это
доля от максимума, и если каждый потребитель будет искать максимум сам,
две картинки одного матча разойдутся по шкале. Максимум берётся внутри
(фаза, вид), а НЕ по всей карте: присутствие даёт сотни попаданий в
клетку, смоки — единицы, и общая шкала превратила бы карту смоков в
пустой лист.
"""
from __future__ import annotations

GRID = 32                      # согласовано с extractor/mapcells.py
PHASES = ("early", "mid", "late")
KINDS = ("presence", "farm", "farm_core", "death", "ward", "smoke", "fight")
TEAMS = (2, 3)                 # 2 Radiant, 3 Dire


def build_heatmaps(cells: list[dict]) -> dict:
    """Секция `heatmaps` отчёта из строк MatchMapCells.

    Возвращает {"grid": 32, "phases": {фаза: {вид: {"max_n": N,
    "radiant": [[gx, gy, n], …], "dire": […]}}}}.

    Пустые сочетания НЕ включаются: отсутствие вардов в лейтгейме — это
    факт, и он честнее читается как отсутствующий ключ, чем как пустой
    список рядом с непустыми (по пустому списку не отличить «не было» от
    «не посчитали»). Вызывающий сообщает об этом явным `available`.
    """
    out: dict[str, dict] = {}
    for row in cells or []:
        phase = str(row.get("phase") or "")
        kind = str(row.get("kind") or "")
        if phase not in PHASES or kind not in KINDS:
            continue
        try:
            team = int(row.get("team") or 0)
            gx, gy = int(row.get("gx")), int(row.get("gy"))
            n = int(row.get("n") or 0)
        except (TypeError, ValueError):
            continue
        if team not in TEAMS or n <= 0:
            continue
        # Клетка вне сетки — испорченная строка, а не край карты:
        # extractor такие отбрасывает, но агрегат мог приехать из
        # бэкфилла более старой версии.
        if not (0 <= gx < GRID and 0 <= gy < GRID):
            continue
        side = "radiant" if team == 2 else "dire"
        block = out.setdefault(phase, {}).setdefault(
            kind, {"max_n": 0, "radiant": [], "dire": []})
        block[side].append([gx, gy, n])
        block["max_n"] = max(block["max_n"], n)

    # Порядок клеток фиксируем: отчёт пересобирается при переразборе
    # матча, и плавающий порядок давал бы «изменившийся» отчёт там, где
    # ничего не менялось.
    for kinds in out.values():
        for block in kinds.values():
            for side in ("radiant", "dire"):
                block[side].sort(key=lambda c: (c[0], c[1]))

    return {"grid": GRID,
            "phases": {p: out[p] for p in PHASES if p in out}}


def build_minute_heatmaps(cells: list[dict]) -> dict:
    """Поминутный слой секции `heatmaps` (спринт 148).

    {"minutes": M, "kinds": {вид: {"max_n": N, "max_by_minute": [...],
    "radiant": [[minute, gx, gy, n], …], "dire": […]}}}

    ФОРМА: минута лежит В САМОЙ КЛЕТКЕ, а не образует ещё один уровень
    словаря. Вложенность {минута: {вид: {…}}} повторила бы служебные ключи
    сорок раз вместо одного, ничего не добавив: клиент всё равно
    фильтрует плоский список.

    ДВЕ НОРМИРОВКИ, И ЭТО НЕ ИЗБЫТОЧНОСТЬ. Ползунок показывает ОДНУ
    минуту, где попаданий в клетку единицы; переключатель «вся игра» —
    сумму по всем минутам, где их сотни. Одна общая шкала сделала бы
    минутные кадры почти пустыми. Считать же максимум кадра на клиенте
    нельзя по другой причине: тогда две картинки одного матча разойдутся
    по шкале, как только потребителей станет двое, — та же беда, ради
    которой max_n вообще считается здесь (см. докстроку модуля).

    `max_n` для всей игры — максимум СУММ по клетке, а не сумма
    максимумов и не максимум из max_by_minute. Второе завысило бы шкалу,
    третье занизило: герой, простоявший в клетке десять минут, даёт там
    сумму втрое большую любого отдельного кадра.

    ПРО ОБЪЁМ. Поминутный слой прибавляет к отчёту десятки килобайт:
    строк столько же, сколько замеров позиций (около 2400 на матч), и
    каждая — четыре числа. Отчёт лежит в Postgres одним JSON, так что
    цена заметна; но альтернатива — отдельный запрос к витрине на каждое
    движение ползунка, то есть поход в ClickHouse на кадр.
    """
    per_minute: dict[str, dict[int, dict[tuple[int, int, int], int]]] = {}
    minutes_seen: set[int] = set()

    for row in cells or []:
        kind = str(row.get("kind") or "")
        if kind not in KINDS:
            continue
        try:
            minute = int(row.get("minute"))
            team = int(row.get("team") or 0)
            gx, gy = int(row.get("gx")), int(row.get("gy"))
            n = int(row.get("n") or 0)
        except (TypeError, ValueError):
            continue
        if team not in TEAMS or n <= 0 or minute < 0:
            continue
        # Клетка вне сетки — испорченная строка, а не край карты.
        if not (0 <= gx < GRID and 0 <= gy < GRID):
            continue
        minutes_seen.add(minute)
        per_minute.setdefault(kind, {}).setdefault(minute, {})[
            (team, gx, gy)] = n

    if not minutes_seen:
        return {"minutes": 0, "kinds": {}}

    total = max(minutes_seen) + 1
    kinds: dict[str, dict] = {}
    for kind in KINDS:
        by_minute = per_minute.get(kind)
        if not by_minute:
            continue
        whole: dict[tuple[int, int, int], int] = {}
        block: dict = {"max_n": 0, "max_by_minute": [0] * total,
                       "radiant": [], "dire": []}
        for minute, cellmap in by_minute.items():
            for (team, gx, gy), n in cellmap.items():
                side = "radiant" if team == 2 else "dire"
                block[side].append([minute, gx, gy, n])
                block["max_by_minute"][minute] = max(
                    block["max_by_minute"][minute], n)
                whole[(team, gx, gy)] = whole.get((team, gx, gy), 0) + n
        block["max_n"] = max(whole.values())
        # Порядок фиксируем: отчёт пересобирается при переразборе матча, и
        # плавающий порядок давал бы «изменившийся» отчёт там, где ничего
        # не менялось.
        for side in ("radiant", "dire"):
            block[side].sort(key=lambda c: (c[0], c[1], c[2]))
        kinds[kind] = block

    return {"minutes": total, "kinds": kinds}


def heatmaps_available(section: dict) -> bool:
    """Есть ли в секции хоть одна клетка.

    Нужно отдельным флагом, потому что пустая секция и отсутствующая
    выглядят одинаково у потребителя, а причины разные: у JSON-матчей
    (источник opendota_timeline) координат нет в принципе, у реплейных
    они появляются только после разбора. Молчаливая пустота уже стоила
    нам спринтов 92 и 99.
    """
    return any(block[side]
               for kinds in section.get("phases", {}).values()
               for block in kinds.values()
               for side in ("radiant", "dire"))
