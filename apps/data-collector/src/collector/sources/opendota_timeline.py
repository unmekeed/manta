"""JSON-источник таймлайнов OpenDota — сбор БЕЗ скачивания реплеев.

Ключевой факт (Гл. 2.5, ускорение сбора ×10+): для распаршенных OpenDota
матчей /matches/{id} уже содержит поминутную экономику (radiant_gold_adv,
radiant_xp_adv) и kills_log игроков — это ровно MatchTimelineFeatures минус
position_advance. Один матч = один API-вызов вместо реплея на 50–110 МиБ.

Свежие паблики почти не распаршены (проверено вживую: ~1/6), поэтому
кандидатов даёт /parsedMatches (гарантированно с экономикой), а фильтр
качества тот же, что у реплей-источника: ranked/All Pick, ≥ 15 минут,
актуальный патч, средний rank_tier ≥ 80 (Immortal-скобка → tier=Premium —
та же обучающая популяция, что у реплей-пути).

position_advance у JSON-матчей отсутствует и пишется как NaN — НЕ 0:
ноль означал бы «бой ровно в центре карты» (ложный сигнал), а NaN LightGBM
обрабатывает нативно как пропуск. Реплей-путь продолжает работать
параллельно (позиции нужны Laning/Error-моделям); дедуп общий по
CollectedMatches.
"""
from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass, field
from typing import Iterable

import requests

from . import Shard, with_api_key

logger = logging.getLogger("collector.opendota_timeline")

# Форматы качества данных — как у реплей-источника (opendota_public).
RANKED_LOBBIES = {0, 7}          # normal | ranked
STANDARD_MODES = {1, 2, 3, 4, 5, 16, 22}   # без Turbo(23) и событийных
UA = {"User-Agent": "manta-collector/1.0"}


@dataclass
class TimelineMatch:
    """Готовые строки витрины одного матча (контракт для раннера)."""

    match_id: int
    tier: str
    rows: list[dict] = field(default_factory=list)   # схема MTF
    source_cursor: str = ""
    patch: int = 0            # id патча OpenDota; 0 — неизвестен (A9)


def timeline_rows(m: dict) -> list[dict]:
    """Поминутные строки MatchTimelineFeatures из JSON распаршенного матча.

    Сетка минут — как у feature-extractor: game_time = 60, 120, …
    (индекс i массива gold_adv соответствует минуте i; нулевую пропускаем).
    kills_* — накопительные по kills_log игроков каждой стороны.
    """
    gold = m.get("radiant_gold_adv") or []
    xp = m.get("radiant_xp_adv") or []
    n = min(len(gold), len(xp))
    if n < 2:
        return []
    radiant_win = 1 if m.get("radiant_win") else 0

    # Времена убийств по сторонам (player_slot < 128 → Radiant).
    r_kills, d_kills = [], []
    for p in m.get("players") or []:
        dst = r_kills if int(p.get("player_slot", 0)) < 128 else d_kills
        dst.extend(int(k["time"]) for k in (p.get("kills_log") or [])
                   if "time" in k)
    r_kills.sort()
    d_kills.sort()

    def _cum(sorted_times: list[int], t: int) -> int:
        # число убийств к моменту t (массивы короткие — линейно достаточно)
        c = 0
        for kt in sorted_times:
            if kt > t:
                break
            c += 1
        return c

    # Снесённые здания из objectives: снесённое goodguys (Radiant) здание —
    # очко Dire, badguys — очко Radiant (знак согласован с networth_diff).
    tower_events, rax_events = [], []
    for obj in m.get("objectives") or []:
        key = str(obj.get("key", ""))
        if obj.get("type") != "building_kill" or "time" not in obj:
            continue
        delta = 1 if "badguys" in key else -1
        if "tower" in key:
            tower_events.append((int(obj["time"]), delta))
        elif "_rax_" in key:
            rax_events.append((int(obj["time"]), delta))
    tower_events.sort()
    rax_events.sort()

    def _cum_delta(events: list[tuple[int, int]], t: int) -> int:
        return sum(d for et, d in events if et <= t)

    # Суммарное золото обеих команд по минутам: players[].gold_t —
    # накопительное золото игрока (прокси net worth). Если массивы короче
    # таймлайна или отсутствуют — NaN (фича networth_rel честно выпадает).
    gold_t = [p.get("gold_t") or [] for p in m.get("players") or []]

    def _total_at(i: int) -> float:
        vals = [gt[i] for gt in gold_t if len(gt) > i]
        return float(sum(vals)) if len(vals) == len(gold_t) and vals else math.nan

    rows = []
    for i in range(1, n):
        t = i * 60
        rows.append({
            "match_id": int(m["match_id"]),
            "game_time": t,
            "networth_diff": int(gold[i]),
            "networth_total": _total_at(i),
            "xp_diff": int(xp[i]),
            "kills_radiant": _cum(r_kills, t),
            "kills_dire": _cum(d_kills, t),
            "position_advance": math.nan,   # позиций в JSON нет — пропуск
            "alive_diff": math.nan,         # живые герои — только из реплея
            "towers_diff": float(_cum_delta(tower_events, t)),
            "rax_diff": float(_cum_delta(rax_events, t)),
            "radiant_win": radiant_win,
        })
    return rows


def match_passes(m: dict, min_rank: int, min_duration_s: int,
                 min_patch: int | None, pro: bool = False) -> tuple[bool, str]:
    """Фильтр качества.

    public: та же популяция, что у реплей-источника (ranked/All Pick,
    средний rank_tier). pro: лиговые матчи играются в лобби турнира и
    Captains Mode, а rank_tier у про-игроков обычно скрыт — проверяются
    только длительность, патч и наличие таймлайна.
    """
    if not pro:
        if int(m.get("lobby_type", -1)) not in RANKED_LOBBIES:
            return False, "lobby"
        if int(m.get("game_mode", -1)) not in STANDARD_MODES:
            return False, "mode"
    if int(m.get("duration") or 0) < min_duration_s:
        return False, "short"
    if min_patch is not None and int(m.get("patch") or 0) < min_patch:
        return False, "old-patch"
    if not pro:
        ranks = [p["rank_tier"] for p in (m.get("players") or [])
                 if p.get("rank_tier")]
        if len(ranks) < 5:
            return False, "ranks-unknown"
        if sum(ranks) / len(ranks) < min_rank:
            return False, "low-rank"
    if not (m.get("radiant_gold_adv") and m.get("radiant_xp_adv")):
        return False, "no-timeline"
    return True, "ok"


class OpenDotaTimelineSource:
    """mode="public": /parsedMatches, фильтр по рангу → tier=Premium.
    mode="pro": /proMatches (лиги распаршены всегда) → tier=Professional —
    эталонная выборка для гейта, в train не попадает никогда."""

    def __init__(self, base_url: str = "https://api.opendota.com/api",
                 limit_per_cycle: int = 30, min_rank: int = 80,
                 min_duration_s: int = 900, min_patch: int | None = None,
                 timeout: float = 30.0, api_delay_s: float = 1.1,
                 mode: str = "public", api_key: str | None = None,
                 detail_budget: int | None = None,
                 shard: Shard | None = None) -> None:
        assert mode in ("public", "pro")
        self._mode = mode
        self.name = ("opendota_timeline" if mode == "public"
                     else "opendota_timeline_pro")
        self._candidates_path = ("parsedMatches" if mode == "public"
                                 else "proMatches")
        self._tier = "Premium" if mode == "public" else "Professional"
        self._base = base_url.rstrip("/")
        self._limit = limit_per_cycle
        self._min_rank = min_rank
        self._min_duration_s = min_duration_s
        self._min_patch = min_patch      # None → определить при первом цикле
        self._timeout = timeout
        self._delay = api_delay_s
        self._api_key = api_key
        self._shard = shard or Shard()
        # Бюджет анонимного тарифа: /matches/{id} — самый дорогой вызов
        # цикла, ограничиваем их число сверху (yielded + отфильтрованные).
        self._detail_budget = detail_budget or 2 * limit_per_cycle
        # Отвергнутые фильтром кандидаты (low-rank, старый патч, без
        # таймлайна): вердикт не меняется, а без кэша каждый цикл заново
        # платил бы detail-вызов за те же match_id с вершины /parsedMatches.
        self._rejected: set[int] = set()

    def _get(self, path: str, **params) -> requests.Response:
        time.sleep(self._delay)          # бюджет free tier: 60 вызовов/мин
        resp = requests.get(f"{self._base}/{path}",
                            params=with_api_key(params, self._api_key),
                            timeout=self._timeout, headers=UA)
        resp.raise_for_status()
        return resp

    def _latest_patch(self) -> int:
        patches = self._get("constants/patch").json()
        latest = max(p["id"] for p in patches)
        logger.info("актуальный патч: id=%d", latest)
        return latest

    def fetch_new(self, after_cursor: str | None = None,
                  skip=None) -> Iterable[TimelineMatch]:
        """Свежие распаршенные матчи: всегда от вершины /parsedMatches вниз.

        after_cursor игнорируется: /parsedMatches отдаёт id по убыванию, и
        «возобновление с прошлой позиции» уводило бы в прошлое от свежих
        матчей. Вместо курсора — предикат skip(match_id) (дедуп по
        CollectedMatches): уже собранные отсекаются ДО дорогого вызова
        /matches/{id}, свежие набираются до limit_per_cycle.
        """
        if self._min_patch is None:
            self._min_patch = self._latest_patch()
        skip = skip or (lambda _mid: False)
        if len(self._rejected) > 50_000:   # id монотонны, старые не вернутся
            self._rejected.clear()
        cursor: int | None = None
        yielded = 0
        pages = 0
        details = 0
        while yielded < self._limit and pages < 10:
            params = {}
            if cursor:
                params["less_than_match_id"] = cursor
            batch = self._get(self._candidates_path, **params).json()
            if not batch:
                return
            pages += 1
            for entry in batch:
                mid = int(entry["match_id"])
                cursor = mid
                if (not self._shard.accepts(mid)
                        or skip(mid) or mid in self._rejected):
                    continue
                if details >= self._detail_budget:
                    logger.info("бюджет detail-вызовов цикла исчерпан "
                                "(%d), собрано %d", details, yielded)
                    return
                details += 1
                try:
                    m = self._get(f"matches/{mid}").json()
                except requests.HTTPError as e:
                    if (e.response is not None
                            and e.response.status_code == 429):
                        # Квота исчерпана — остальные кандидаты дадут те же
                        # 429; обрываем цикл, не сжигая остаток лимита.
                        raise
                    logger.warning("матч %d: %s — пропуск", mid, e)
                    continue
                except requests.RequestException as e:
                    logger.warning("матч %d: %s — пропуск", mid, e)
                    continue
                ok, why = match_passes(m, self._min_rank,
                                       self._min_duration_s, self._min_patch,
                                       pro=(self._mode == "pro"))
                if not ok:
                    logger.debug("матч %d отфильтрован: %s", mid, why)
                    self._rejected.add(mid)
                    continue
                rows = timeline_rows(m)
                if not rows:
                    self._rejected.add(mid)
                    continue
                yielded += 1
                yield TimelineMatch(match_id=mid, tier=self._tier, rows=rows,
                                    source_cursor=str(mid),
                                    patch=int(m.get("patch") or 0))
                if yielded >= self._limit:
                    return
