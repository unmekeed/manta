"""Извлечение игровых сигналов из JSON распаршенного матча OpenDota.

Трек F (docs/ML-PLAN.md). До этого модуля коллектор использовал ~10%
полей уже скачанного JSON: экономику, убийства и снесённые здания.
Здесь достаются остальные сигналы, за которые квота УЖЕ заплачена:

  драфт          picks_bans / players[].hero_id      → MatchDraft (F1/F3)
  Рошан, аегис   objectives[CHAT_MESSAGE_ROSHAN_KILL…]→ MatchEvents (F2)
  first blood    objectives[CHAT_MESSAGE_FIRSTBLOOD]  → MatchEvents (F2)
  бэйбеки        players[].buyback_log                → MatchEvents (F2)
  руны           players[].runes_log                  → MatchEvents (F5)
  варды          players[].obs_log / sen_log          → MatchEvents (F5)
  предметы       players[].purchase_log               → поминутные (F4)
  нейтралки      players[].item_neutral               → поминутные (F6)

Стороны: player_slot < 128 → Radiant (team 2), иначе Dire (team 3).
Знак всех diff-фич согласован с networth_diff: плюс — в пользу Radiant.

Все функции чистые (dict → данные), поэтому тестируются без сети и БД.
"""
from __future__ import annotations

import math

from manta_data import load_json as _load

RADIANT, DIRE = 2, 3

# Стоимости и тиры — из констант OpenDota, снимок в libs/data. Точные
# цены меняются патчами; для diff-фичи важен порядок величин, а не копейки.
#
# Путь к снимку раньше считался здесь: `parents[4] / "libs" / "data"`.
# Четыре шага вверх — раскладка монорепо; в образе от /app/src/collector их
# всего три, и модуль падал с IndexError на ИМПОРТЕ, роняя все семь
# коллекторов в цикл перезапусков (VPS, 2026-08-18). Резолвер живёт в libs
# рядом с самими данными — см. libs/manta_data.py.
_ITEM_COST: dict[str, int] = _load("item_costs.json", {})
_HEROES: dict[str, dict] = _load("heroes.json", {})
_HERO_BY_ID: dict[int, str] = {v["id"]: k for k, v in _HEROES.items()
                               if isinstance(v, dict) and "id" in v}
# Публичное имя: словарь нужен и источнику STRATZ, который строит драфт
# из heroId, а не из JSON OpenDota (спринт 100).
HERO_BY_ID = _HERO_BY_ID

# Предметы, чей тайминг меняет ход игры (F4). Список намеренно короткий:
# считаем «сколько таких вех взято», а не «сколько предметов куплено».
KEY_ITEMS = {
    "blink", "black_king_bar", "aghanims_shard", "ultimate_scepter",
    "mekansm", "guardian_greaves", "pipe", "refresher", "octarine_core",
    "assault", "heart", "radiance", "desolator", "manta", "satanic",
    "silver_edge", "sheepstick", "orchid", "bloodthorn", "shivas_guard",
    "abyssal_blade", "butterfly", "daedalus", "skadi", "gungir",
    "travel_boots", "aeon_disk", "lotus_orb", "vladmir", "crimson_guard",
}

# Тир нейтрального предмета по имени (F6). Полного словаря нет — берём
# по времени появления тира: если предмет неизвестен, вклад 0.
_NEUTRAL_TIER_BY_TIME = ((7 * 60, 1), (17 * 60, 2), (27 * 60, 3),
                         (37 * 60, 4), (60 * 60, 5))


def team_of(player_slot: int) -> int:
    return RADIANT if int(player_slot) < 128 else DIRE


def _sign(team: int) -> int:
    """+1 для Radiant, −1 для Dire — знак вклада в diff-фичу."""
    return 1 if team == RADIANT else -1


# -- F1/F3: драфт --------------------------------------------------------------

def draft_row(m: dict) -> dict | None:
    """Строка MatchDraft. Составы берутся из players[] (есть всегда),
    баны и порядок пика — из picks_bans (есть не у всех матчей)."""
    players = m.get("players") or []
    if len(players) != 10:
        return None
    radiant, dire = [], []
    for p in players:
        npc = _HERO_BY_ID.get(int(p.get("hero_id") or 0))
        if not npc:
            return None                      # неизвестный герой — матч мимо
        (radiant if team_of(p.get("player_slot", 0)) == RADIANT
         else dire).append(npc)
    if len(radiant) != 5 or len(dire) != 5:
        return None

    bans, first_pick = [], 0
    for pb in m.get("picks_bans") or []:
        npc = _HERO_BY_ID.get(int(pb.get("hero_id") or 0))
        if not npc:
            continue
        if pb.get("is_pick"):
            if first_pick == 0:
                first_pick = RADIANT if int(pb.get("team", 0)) == 0 else DIRE
        else:
            bans.append(npc)
    return {
        "match_id": int(m["match_id"]),
        "patch": int(m.get("patch") or 0),
        "radiant_win": 1 if m.get("radiant_win") else 0,
        "radiant_heroes": radiant,
        "dire_heroes": dire,
        "bans": bans,
        "first_pick_team": first_pick,
        "source": "json",
    }


# -- F2/F5: события ------------------------------------------------------------

def _obj_team(obj: dict) -> int:
    """Сторона объектива: у части событий team, у части player_slot."""
    if obj.get("team") in (2, 3):
        return int(obj["team"])
    slot = obj.get("player_slot")
    return team_of(slot) if slot is not None else 0


def event_rows(m: dict) -> list[dict]:
    """Строки MatchEvents матча: объективы + пер-игроковые логи."""
    mid = int(m["match_id"])
    out: list[dict] = []

    def add(t, kind, team=0, slot=-1, subtype="", x=math.nan, y=math.nan):
        out.append({"match_id": mid, "game_time": int(t), "kind": kind,
                    "team": int(team), "player_slot": int(slot),
                    "subtype": str(subtype), "x": float(x), "y": float(y)})

    for obj in m.get("objectives") or []:
        typ = str(obj.get("type") or "")
        if "time" not in obj:
            continue
        t = obj["time"]
        if typ == "CHAT_MESSAGE_ROSHAN_KILL":
            # У ROSHAN_KILL team — это сторона, УБИВШАЯ Рошана.
            add(t, "roshan", _obj_team(obj))
        elif typ == "CHAT_MESSAGE_AEGIS":
            add(t, "aegis", _obj_team(obj), obj.get("player_slot", -1))
        elif typ == "CHAT_MESSAGE_AEGIS_STOLEN":
            add(t, "aegis_stolen", _obj_team(obj), obj.get("player_slot", -1))
        elif typ == "CHAT_MESSAGE_DENIED_AEGIS":
            add(t, "aegis_denied", _obj_team(obj), obj.get("player_slot", -1))
        elif typ == "CHAT_MESSAGE_FIRSTBLOOD":
            add(t, "firstblood", _obj_team(obj), obj.get("player_slot", -1))
        elif typ == "CHAT_MESSAGE_COURIER_LOST":
            add(t, "courier", _obj_team(obj))

    for p in m.get("players") or []:
        slot = int(p.get("player_slot", 0))
        team = team_of(slot)
        for b in p.get("buyback_log") or []:
            if "time" in b:
                add(b["time"], "buyback", team, slot)
        for r in p.get("runes_log") or []:
            if "time" in r:
                add(r["time"], "rune", team, slot, subtype=r.get("key", ""))
        for w in p.get("obs_log") or []:
            if "time" in w:
                add(w["time"], "ward_obs", team, slot,
                    x=w.get("x", math.nan), y=w.get("y", math.nan))
        for w in p.get("sen_log") or []:
            if "time" in w:
                add(w["time"], "ward_sen", team, slot,
                    x=w.get("x", math.nan), y=w.get("y", math.nan))
        for w in (p.get("obs_left_log") or []) + (p.get("sen_left_log") or []):
            if "time" in w:
                add(w["time"], "ward_killed", team, slot)
    return out


# -- Поминутные фичи из событий и логов ---------------------------------------

def _cum_diff(events: list[tuple[int, int]], minutes: list[int]) -> list[float]:
    """Накопительная сумма знаковых вкладов к каждой минуте сетки."""
    events = sorted(events)
    out, acc, i = [], 0.0, 0
    for t in minutes:
        while i < len(events) and events[i][0] <= t:
            acc += events[i][1]
            i += 1
        out.append(acc)
    return out


def objective_features(m: dict, minutes: list[int]) -> dict[str, list[float]]:
    """F2: roshan_diff, aegis_alive, buybacks_diff, first_blood."""
    evs = event_rows(m)
    rosh = [(e["game_time"], _sign(e["team"]))
            for e in evs if e["kind"] == "roshan" and e["team"]]
    bb = [(e["game_time"], _sign(e["team"]))
          for e in evs if e["kind"] == "buyback" and e["team"]]

    # Аегис: подобран — у стороны, украден — переходит, съеден/истёк —
    # ни у кого. Точного времени поедания в JSON нет, поэтому считаем
    # срок жизни 5 минут (правило Dota) — приближение, но честное.
    aegis: list[tuple[int, int]] = []
    for e in evs:
        if e["kind"] in ("aegis", "aegis_stolen") and e["team"]:
            aegis.append((e["game_time"], _sign(e["team"])))
    fb = next((e for e in evs if e["kind"] == "firstblood" and e["team"]), None)

    aegis_series = []
    for t in minutes:
        cur = 0.0
        for at, s in aegis:
            if at <= t <= at + 300:
                cur = s
        aegis_series.append(cur)

    return {
        "roshan_diff": _cum_diff(rosh, minutes),
        "buybacks_diff": _cum_diff(bb, minutes),
        "aegis_alive": aegis_series,
        "first_blood": [
            (0.0 if fb is None or t < fb["game_time"] else _sign(fb["team"]))
            for t in minutes],
    }


def vision_features(m: dict, minutes: list[int]) -> dict[str, list[float]]:
    """F5: активные обс-варды, поставленные сентри, подобранные руны.

    Обс-вард живёт 6 минут (или до снятия — obs_left_log). Считаем
    активные, а не поставленные: контроль карты — это состояние.
    """
    obs: list[tuple[int, int, int]] = []   # (поставлен, снят, знак)
    sen: list[tuple[int, int]] = []
    runes: list[tuple[int, int]] = []
    for p in m.get("players") or []:
        team = team_of(p.get("player_slot", 0))
        s = _sign(team)
        left = sorted(int(w["time"]) for w in (p.get("obs_left_log") or [])
                      if "time" in w)
        for i, w in enumerate(sorted((w for w in (p.get("obs_log") or [])
                                      if "time" in w),
                                     key=lambda w: w["time"])):
            t0 = int(w["time"])
            t1 = left[i] if i < len(left) else t0 + 360
            obs.append((t0, max(t1, t0), s))
        for w in p.get("sen_log") or []:
            if "time" in w:
                sen.append((int(w["time"]), s))
        for r in p.get("runes_log") or []:
            if "time" in r:
                runes.append((int(r["time"]), s))

    obs_series = []
    for t in minutes:
        obs_series.append(float(sum(s for t0, t1, s in obs if t0 <= t <= t1)))
    return {
        "obs_wards_diff": obs_series,
        "sen_wards_diff": _cum_diff(sen, minutes),
        "runes_diff": _cum_diff(runes, minutes),
    }


# -- Площадь под обзором (волна 1 каталога, спринт 90) ------------------------

# Границы карты и переводы координат — из общего модуля libs/dota_map.py.
# До спринта 139 они жили здесь своей копией (64..192), в mapcells.py —
# другой (MAP_HALF = 8000), и системы расходились на 2.4%. Под общей
# подложкой это развело бы тепловые пятна и варды на глаз.
#
# Заодно выяснилось, КТО был неправ: радиус обзора ниже выведен из 1600
# игровых юнитов, и при шаге клетки 128 он даёт ровно 12.5 — то есть
# клеточная система была согласована с ±8192, а 8000 в mapcells не
# следовало ниоткуда.
from dota_map import CELL_MAX, CELL_MIN, UNITS_PER_CELL, cell_to_unit, unit_to_grid

MAP_MIN, MAP_MAX = CELL_MIN, CELL_MAX
# Радиус обзора обс-варда в игровых юнитах — это игровая константа, а не
# свойство нашей сетки. В клетки он переводится, а не записывается.
WARD_VISION_UNITS = 1600.0
WARD_VISION_CELLS = WARD_VISION_UNITS / UNITS_PER_CELL
# Сторона растровой сетки. 64 даёт клетку примерно в две карт-клетки:
# мельче — квадратичный рост стоимости бэкфилла на тысячах матчей,
# крупнее — варды в одном лесу перестают отличаться от разнесённых.
VISION_GRID = 64


def _disc_offsets(radius_cells: float = WARD_VISION_CELLS,
                  grid: int = VISION_GRID) -> list[tuple[int, int]]:
    """Смещения клеток растра, накрытых одним вардом. Считается один раз:
    диск одинаков для всех вардов, а матчей десятки тысяч."""
    r = radius_cells * grid / (MAP_MAX - MAP_MIN)
    ri = int(math.ceil(r))
    return [(dx, dy)
            for dx in range(-ri, ri + 1)
            for dy in range(-ri, ri + 1)
            if dx * dx + dy * dy <= r * r]


_DISC = _disc_offsets()


def _covered(wards: tuple, grid: int = VISION_GRID) -> int:
    """Число клеток растра, накрытых ХОТЯ БЫ одним вардом.

    Именно объединение, а не сумма площадей: перекрытие радиусов — это и
    есть разница между «три варда по карте» и «три варда в одном лесу»,
    ради которой фича заводится. Сумма площадей их не различила бы, как
    не различает и счётчик obs_wards_diff.
    """
    cells: set[tuple[int, int]] = set()
    for x, y in wards:
        gx, gy = unit_to_grid(*cell_to_unit(x, y), grid)
        for dx, dy in _DISC:
            cx, cy = gx + dx, gy + dy
            if 0 <= cx < grid and 0 <= cy < grid:
                cells.add((cx, cy))
    return len(cells)


def _ward_positions(m: dict) -> list[tuple[int, int, int, float, float]]:
    """Обс-варды матча: (поставлен, снят, знак стороны, x, y).

    Сентри не берём: они дают истинное зрение, а не обзор, и складывать
    их с обсами значило бы смешать две разные величины.

    Время снятия берётся из obs_left_log по порядку постановки — та же
    логика, что в vision_features; при отсутствии записи вард живёт свои
    360 секунд.
    """
    out = []
    for p in m.get("players") or []:
        s = _sign(team_of(p.get("player_slot", 0)))
        placed = sorted((w for w in (p.get("obs_log") or []) if "time" in w),
                        key=lambda w: w["time"])
        left = sorted(int(w["time"]) for w in (p.get("obs_left_log") or [])
                      if "time" in w)
        for i, w in enumerate(placed):
            x, y = w.get("x"), w.get("y")
            if x is None or y is None:
                continue
            try:
                x, y = float(x), float(y)
            except (TypeError, ValueError):
                continue
            if math.isnan(x) or math.isnan(y):
                continue
            t0 = int(w["time"])
            t1 = left[i] if i < len(left) else t0 + 360
            out.append((t0, max(t1, t0), s, x, y))
    return out


def vision_coverage(m: dict, minutes: list[int]) -> dict[str, list[float]]:
    """Доля карты под обзором Radiant минус то же для Dire, в [-1, 1].

    Пустой словарь (колонка останется NaN) возвращается, только если у
    матча НЕТ НИ ОДНОГО варда с координатами: у матчей STRATZ их нет
    вовсе, и ноль означал бы «видят одинаково» — ложный сигнал. Если
    варды есть хотя бы у одной стороны, ноль у второй честен.
    """
    wards = _ward_positions(m)
    if not wards:
        return {}
    total = float(VISION_GRID * VISION_GRID)
    # Набор активных вардов меняется куда реже, чем идут минуты, поэтому
    # площадь считается один раз на набор. Без этого бэкфилл тысяч
    # матчей упирался бы в перебор одних и тех же дисков.
    memo: dict[tuple, int] = {}

    def frac(active: tuple) -> float:
        if active not in memo:
            memo[active] = _covered(active)
        return memo[active] / total

    out = []
    for t in minutes:
        r = tuple(sorted((x, y) for t0, t1, s, x, y in wards
                         if s > 0 and t0 <= t <= t1))
        d = tuple(sorted((x, y) for t0, t1, s, x, y in wards
                         if s < 0 and t0 <= t <= t1))
        out.append(frac(r) - frac(d))
    return {"vision_coverage_diff": out}


def item_features(m: dict, minutes: list[int]) -> dict[str, list[float]]:
    """F4: стоимость закупа и число взятых ключевых предметов (diff)."""
    cost_events: list[tuple[int, float]] = []
    key_events: list[tuple[int, int]] = []
    for p in m.get("players") or []:
        s = _sign(team_of(p.get("player_slot", 0)))
        for it in p.get("purchase_log") or []:
            if "time" not in it:
                continue
            name = str(it.get("key", ""))
            t = int(it["time"])
            cost = _ITEM_COST.get(name)
            if cost:
                cost_events.append((t, s * float(cost)))
            if name in KEY_ITEMS:
                key_events.append((t, s))
    return {
        "item_value_diff": _cum_diff(cost_events, minutes),   # type: ignore[arg-type]
        "key_items_diff": _cum_diff(key_events, minutes),
    }


# -- Резерв и мёртвое золото (волна 1, спринт 91) ------------------------------

# Стоимость выкупа: 100 + нетворс/13. Формула менялась патчами, поэтому
# держим её здесь явно, а не размазываем по коду: когда Valve поменяет
# делитель, править надо будет одно место.
BUYBACK_BASE = 100
BUYBACK_NETWORTH_DIV = 13
# Кулдаун бэйбека. Пока он идёт, «резерв» существует только на бумаге.
BUYBACK_COOLDOWN_S = 480


def _gold_earned_at(gold_t: list, t: int) -> float:
    """Накопленное золото игрока к секунде t по поминутному ряду gold_t.

    Ряд идёт с шагом минуту, индекс равен номеру минуты. За хвостом ряда
    берём последнее известное значение: матч мог закончиться раньше
    последней минуты сетки.
    """
    if not gold_t:
        return 0.0
    i = min(max(t // 60, 0), len(gold_t) - 1)
    try:
        return float(gold_t[i] or 0)
    except (TypeError, ValueError):
        return 0.0


def _player_economy(p: dict) -> tuple[list, list[tuple[int, float]], list[int]]:
    """(ряд накопленного золота, покупки [(время, цена)], времена бэйбеков)."""
    purchases: list[tuple[int, float]] = []
    for it in p.get("purchase_log") or []:
        if "time" not in it:
            continue
        cost = _ITEM_COST.get(str(it.get("key", "")))
        if cost:
            purchases.append((int(it["time"]), float(cost)))
    purchases.sort()
    buybacks = sorted(int(b["time"]) for b in (p.get("buyback_log") or [])
                      if "time" in b)
    return (p.get("gold_t") or []), purchases, buybacks


def economy_reserve_features(m: dict, minutes: list[int]
                             ) -> dict[str, list[float]]:
    """unspent_gold_diff и buyback_availability из JSON OpenDota.

    Золото в кармане = заработано − вложено в предметы − потрачено на
    выкупы. `gold_t` OpenDota — накопленное ЗАРАБОТАННОЕ золото, поэтому
    разность и даёт остаток.

    Оценка неизбежно приблизительная, и врёт она в одну сторону — вверх:
    продажа предметов возвращает золото, но в purchase_log не попадает, а
    словарь цен (снимок констант) неполон. Для diff-фичи это терпимо:
    обе стороны считаются одинаково, и систематический сдвиг сокращается.
    Отрицательный остаток обрезается нулём — отрицательного золота не
    бывает, и минус означал бы только накопленную ошибку оценки.

    Пустой словарь (колонки останутся NaN), если поминутного золота нет
    ни у кого: у матчей STRATZ его нет вовсе, и ноль означал бы «карманы
    пусты» и «выкупиться не может никто» — оба ложные сигналы.
    """
    if not _ITEM_COST:
        # Без словаря цен «вложено в предметы» равно нулю, и остаток
        # выродился бы в накопленное золото — то есть в копию
        # networth_diff под другим именем. Лучше пропуск.
        return {}
    players = []
    for p in m.get("players") or []:
        gold_t, purchases, buybacks = _player_economy(p)
        if not gold_t:
            continue
        players.append((_sign(team_of(p.get("player_slot", 0))),
                        gold_t, purchases, buybacks))
    if not players:
        return {}

    unspent_series, avail_series = [], []
    for t in minutes:
        unspent_diff = 0.0
        avail_diff = 0.0
        for s, gold_t, purchases, buybacks in players:
            earned = _gold_earned_at(gold_t, t)
            spent = sum(c for pt, c in purchases if pt <= t)
            # Выкуп тоже тратит золото, и его цена считается по нетворсу
            # НА МОМЕНТ ВЫКУПА, а не на текущую минуту.
            used = [bt for bt in buybacks if bt <= t]
            spent += sum(BUYBACK_BASE
                         + _gold_earned_at(gold_t, bt) / BUYBACK_NETWORTH_DIV
                         for bt in used)
            unspent = max(0.0, earned - spent)
            unspent_diff += s * unspent

            on_cooldown = any(t - bt < BUYBACK_COOLDOWN_S for bt in used)
            cost_now = BUYBACK_BASE + earned / BUYBACK_NETWORTH_DIV
            if not on_cooldown and unspent >= cost_now:
                avail_diff += s
        unspent_series.append(unspent_diff)
        avail_series.append(avail_diff)
    return {"unspent_gold_diff": unspent_series,
            "buyback_availability": avail_series}


def neutral_level_features(m: dict, minutes: list[int]
                           ) -> dict[str, list[float]]:
    """F6: сумма тиров нейтралок и разница уровней.

    Времени выпадения нейтралки в JSON нет — тир приписывается по окну
    времени, а к минуте t учитываются тиры, доступные к этому моменту
    (приближение: команда с большим числом живых героев фармит нейтралки
    быстрее, но этой детали в данных нет).
    Уровни: xp_t поминутно есть у каждого игрока — уровень восстанавливаем
    по таблице опыта Dota.
    """
    XP_TABLE = [0, 0, 230, 600, 1080, 1660, 2260, 2980, 3730, 4620, 5550,
                6520, 7530, 8580, 9805, 11055, 12330, 13630, 14955, 16455,
                18045, 19645, 21495, 23595, 25945, 28545, 32045, 36545,
                42045, 48545, 56045]

    def level_of(xp: int) -> int:
        lvl = 1
        for i, need in enumerate(XP_TABLE):
            if xp >= need:
                lvl = max(1, i)
        return min(lvl, 30)

    levels: list[float] = []
    xp_series = [(team_of(p.get("player_slot", 0)), p.get("xp_t") or [])
                 for p in m.get("players") or []]
    for t in minutes:
        idx = t // 60
        acc = 0.0
        for team, xp in xp_series:
            if idx < len(xp):
                acc += _sign(team) * level_of(int(xp[idx]))
        levels.append(acc)

    neutral_now = 0.0
    for p in m.get("players") or []:
        if p.get("item_neutral"):
            neutral_now += _sign(team_of(p.get("player_slot", 0)))
    tier_series = []
    for t in minutes:
        tier = 0
        for limit, tv in _NEUTRAL_TIER_BY_TIME:
            if t >= limit:
                tier = tv
        tier_series.append(neutral_now * tier)
    return {"levels_diff": levels, "neutral_tier_diff": tier_series}


def all_minute_features(m: dict, minutes: list[int]) -> dict[str, list[float]]:
    """Все поминутные фичи трека F одним вызовом."""
    out: dict[str, list[float]] = {}
    for fn in (objective_features, vision_features, vision_coverage,
               item_features, economy_reserve_features,
               neutral_level_features):
        try:
            out.update(fn(m, minutes))
        except Exception:  # noqa: BLE001 — сбой одной группы не рушит остальные
            continue
    return out
