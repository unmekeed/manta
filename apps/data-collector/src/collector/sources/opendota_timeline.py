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
import os
import time
from dataclasses import dataclass, field
from typing import Iterable

import requests

from ..signals import all_minute_features
from . import Shard, SourceSplit, with_api_key

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
    # Средний ранг матча в шкале rank_tier (тир×10 + звезда); 0 —
    # неизвестен. Нужен, чтобы «Premium» означало одну популяцию у всех
    # источников, а не только у того, который умеет фильтровать.
    avg_rank: int = 0
    # Готовая строка MatchDraft. Нужна источникам, у которых нет
    # JSON-ответа формы OpenDota: STRATZ отдаёт составы прямо в своём
    # ответе, и подделывать ради них «сырой JSON» было бы хуже — раннер
    # начал бы считать по нему фичи трека F, которых там нет.
    draft: dict | None = None
    raw: dict = field(default_factory=dict)  # исходный JSON матча (трек F)


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

    # Трек F: объективы, предметы, вижн, нейтралки — из того же JSON.
    minutes = [i * 60 for i in range(1, n)]
    extra = all_minute_features(m, minutes)

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
            **{k: v[i - 1] for k, v in extra.items() if len(v) >= i},
        })
    return rows


def avg_rank(m: dict) -> int:
    """Средний rank_tier матча (тир×10 + звезда); 0 — неизвестен.

    Ранг известен не у всех: часть игроков скрывает профиль. Меньше
    пяти известных рангов — среднее считать не по чему, и это честнее
    отдать нулём, чем усреднить по одному игроку.
    """
    ranks = [int(p["rank_tier"]) for p in (m.get("players") or [])
             if p.get("rank_tier")]
    return int(sum(ranks) / len(ranks)) if len(ranks) >= 5 else 0


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
        if avg_rank(m) == 0:
            return False, "ranks-unknown"
        if avg_rank(m) < min_rank:
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
                 shard: Shard | None = None,
                 split: SourceSplit | None = None,
                 league_ttl_s: float = 3600.0) -> None:
        assert mode in ("public", "pro", "league")
        self._mode = mode
        self.name = {"public": "opendota_timeline",
                     "pro": "opendota_timeline_pro",
                     "league": "opendota_league"}[mode]
        self._candidates_path = ("parsedMatches" if mode == "public"
                                 else "proMatches")
        self._tier = "Premium" if mode == "public" else "Professional"
        # Кэш списка активных лиг: он меняется медленно (турниры живут
        # неделями), а вызов стоит столько же, сколько страница листинга.
        self._league_ttl = league_ttl_s
        self._leagues: list[int] = []
        self._leagues_at = -league_ttl_s
        self._base = base_url.rstrip("/")
        self._limit = limit_per_cycle
        self._min_rank = min_rank
        self._min_duration_s = min_duration_s
        self._min_patch = min_patch      # None → определить при первом цикле
        self._timeout = timeout
        self._delay = api_delay_s
        self._api_key = api_key
        self._shard = shard or Shard()
        # Разделение кандидатов со STRATZ-источником: оба читают один и тот
        # же листинг с вершины и без него дрались бы за одни матчи.
        self._split = split or SourceSplit()
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
        """Нижняя граница патча для фильтра.

        НЕ самый свежий патч, а самый свежий минус PATCH_LAG (дефолт 1).
        В день выхода патча матчей на нём почти нет, и планка «строго
        текущий» обнуляет приток на сутки-двое — ровно это случилось
        2026-07-31 с выходом 7.41. Отсекать старую мету полностью и не
        нужно: при обучении она уже даунвешена по возрасту патча (A9),
        так что жёсткий отсев был бы двойным наказанием.
        """
        patches = self._get("constants/patch").json()
        latest = max(p["id"] for p in patches)
        lag = int(os.getenv("PATCH_LAG", "1"))
        floor = max(latest - max(lag, 0), 0)
        logger.info("актуальный патч: id=%d, принимаю от id=%d (lag=%d)",
                    latest, floor, lag)
        return floor

    def _listing_candidates(self) -> Iterable[int]:
        """Кандидаты из окна свежих матчей (/parsedMatches | /proMatches).

        Отдаёт id по убыванию, вглубь до десяти страниц.
        """
        cursor: int | None = None
        for _ in range(10):
            params = {}
            if cursor:
                params["less_than_match_id"] = cursor
            batch = self._get(self._candidates_path, **params).json()
            if not batch:
                return
            for entry in batch:
                mid = entry.get("match_id")
                if mid is None:
                    continue
                cursor = int(mid)
                yield cursor

    def _active_leagues(self) -> list[int]:
        """id лиг, матчи которых сейчас идут (по свежему /proMatches)."""
        now = time.monotonic()
        if self._leagues and now - self._leagues_at < self._league_ttl:
            return self._leagues
        try:
            batch = self._get("proMatches").json() or []
        except requests.RequestException:
            logger.warning("список лиг не обновлён — работаю на прежнем",
                           exc_info=True)
            return self._leagues
        seen: list[int] = []
        for entry in batch:
            lid = entry.get("leagueid")
            if lid and int(lid) not in seen:
                seen.append(int(lid))
        if seen:
            self._leagues, self._leagues_at = seen, now
            logger.info("активных лиг: %d %s", len(seen), seen[:8])
        return self._leagues

    def _league_candidates(self) -> Iterable[int]:
        """Кандидаты ИЗ ЛИГ — вглубь, а не вширь.

        Окно /proMatches — около тысячи последних матчей на весь мир, и
        оно выбирается за сутки: цикл начинает давать «собрано 0, все
        дубликаты», а про-эталон перестаёт расти (наблюдалось
        2026-08-04). При этом каждая лига — сотни матчей, которые в это
        окно давно не попадают, и стоят они по одному дешёвому вызову
        на лигу.

        Порядок лиг сохраняется как в /proMatches, то есть самые
        активные турниры идут первыми; дедуп по CollectedMatches сам
        отсеет уже собранное, а бюджет цикла ограничит глубину.
        """
        for lid in self._active_leagues():
            try:
                batch = self._get(f"leagues/{lid}/matches").json() or []
            except requests.RequestException as e:
                logger.warning("лига %s: %s — пропуск", lid, e)
                continue
            for entry in batch:
                mid = entry.get("match_id")
                if mid is not None:
                    yield int(mid)

    def _candidates(self) -> Iterable[int]:
        if self._mode == "league":
            return self._league_candidates()
        return self._listing_candidates()

    def fetch_new(self, after_cursor: str | None = None,
                  skip=None) -> Iterable[TimelineMatch]:
        """Свежие распаршенные матчи.

        after_cursor игнорируется: листинг отдаёт id по убыванию, и
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
        yielded = 0
        details = 0
        for mid in self._candidates():
            if (not self._shard.accepts(mid)
                    or not self._split.accepts(mid)
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
                                   pro=(self._mode != "public"))
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
                                patch=int(m.get("patch") or 0),
                                avg_rank=avg_rank(m),
                                raw=m)
            if yielded >= self._limit:
                return
