"""Источник таймлайнов STRATZ (GraphQL) — обход потолка квоты OpenDota.

Зачем. Дефицит проекта — не список матчей, а ДЕТАЛИ матча: `/matches/{id}`
у OpenDota стоит один вызов на матч и упирается в ~2000 вызовов/сутки с IP.
Листинг же дёшев: одна страница /parsedMatches или /proMatches отдаёт до
100 кандидатов за вызов. STRATZ решает ровно узкое место: кандидаты
по-прежнему берутся дешёвым листингом OpenDota, а поминутные ряды —
из STRATZ (Default-токен: 20/сек, 250/мин, 2000/час, 10000/сутки).
Суточный потолок деталей растёт примерно в пять раз.

Что отдаёт STRATZ. Поля MatchType, на которых держится витрина:
radiantNetworthLeads / radiantExperienceLeads (поминутные ряды),
radiantKills / direKills, didRadiantWin, durationSeconds, gameMode,
lobbyType, gameVersionId. Этого хватает на ядро MatchTimelineFeatures.

Чего STRATZ не даёт (пишется NaN, как и у JSON-источника OpenDota):
position_advance и alive_diff — они существуют только в реплее; фичи
трека F (вижн, предметы, руны, нейтралки) — их собирает all_minute_features
из JSON OpenDota, у STRATZ структура иная. NaN здесь честнее нуля:
LightGBM обрабатывает пропуск нативно, а 0 был бы ложным сигналом.

Патчи. gameVersionId у STRATZ — СВОЙ идентификатор, он не совпадает с
patch id OpenDota, а колонка patch витрины используется для даунвейта
старых патчей (A9). Поэтому id переводится по ИМЕНИ версии ("7.39"):
constants.gameVersions у STRATZ против constants/patch у OpenDota. Если
перевод не удался — patch=0 («неизвестен»), а не чужой id: испортить
взвешивание молча хуже, чем потерять признак.
"""
from __future__ import annotations

import logging
import math
import time
from typing import Iterable

import requests

from . import Shard, SourceSplit, with_api_key
from .opendota_timeline import TimelineMatch

logger = logging.getLogger("collector.stratz")

API_URL = "https://api.stratz.com/graphql"
# STRATZ отклоняет запросы без опознавательного User-Agent.
UA = {"User-Agent": "STRATZ_API"}

RANKED_LOBBIES = {0, 7}
STANDARD_MODES = {1, 2, 3, 4, 5, 16, 22}

FEATURE_VERSION = "stratz-graphql@1"

# Поля матча. Держим списком, а не одной строкой: при расхождении со схемой
# STRATZ ошибка GraphQL называет конкретное поле, и правка очевидна.
MATCH_FIELDS = """
    id
    didRadiantWin
    durationSeconds
    gameMode
    lobbyType
    gameVersionId
    radiantNetworthLeads
    radiantExperienceLeads
    radiantKills
    direKills
"""

MATCH_QUERY = "query($id: Long!) { match(id: $id) { %s } }" % MATCH_FIELDS
VERSIONS_QUERY = "{ constants { gameVersions { id name } } }"


class StratzError(RuntimeError):
    """Ошибка GraphQL-слоя STRATZ (в ответе есть errors, данных нет)."""


# Матч с более чем таким числом убийств у одной стороны практически
# невозможен: верный признак, что ряд УЖЕ был накопительным и мы накопили
# его второй раз (квадратичное искажение).
KILLS_SANITY_MAX = 250


def cumulative(per_minute: list, already_cumulative: bool = False) -> list[int]:
    """Накопительный ряд убийств из поминутного.

    radiantKills/direKills STRATZ отдаёт числом убийств ЗА минуту, а витрине
    нужны kills_radiant/kills_dire накопительно (так их считают и
    feature-extractor, и JSON-источник OpenDota).

    Определить вид ряда по самим данным НЕЛЬЗЯ: [0,0,1,1] одинаково
    правдоподобен и как поминутный, и как накопительный. Поэтому вид
    задаётся явно (STRATZ_KILLS_CUMULATIVE), а не угадывается — ошибка
    угадывания молча испортила бы фичу во всём датасете. Значение по
    умолчанию соответствует поминутной семантике STRATZ; страховка от
    ошибки — проверка на KILLS_SANITY_MAX в timeline_rows.
    """
    vals = [int(v or 0) for v in per_minute]
    if already_cumulative or not vals:
        return vals
    out, acc = [], 0
    for v in vals:
        acc += v
        out.append(acc)
    return out


def timeline_rows(m: dict, kills_cumulative: bool = False) -> list[dict]:
    """Поминутные строки MatchTimelineFeatures из ответа STRATZ.

    Сетка минут та же, что у остальных источников: game_time = 60, 120, …
    Индекс i массива соответствует минуте i, нулевая минута пропускается.
    patch сюда не передаётся: его проставляет раннер из TimelineMatch.patch.
    """
    gold = m.get("radiantNetworthLeads") or []
    xp = m.get("radiantExperienceLeads") or []
    n = min(len(gold), len(xp))
    if n < 2:
        return []
    radiant_win = 1 if m.get("didRadiantWin") else 0
    r_kills = cumulative(m.get("radiantKills") or [], kills_cumulative)
    d_kills = cumulative(m.get("direKills") or [], kills_cumulative)
    worst = max((r_kills or [0])[-1], (d_kills or [0])[-1])
    if worst > KILLS_SANITY_MAX:
        # Ряд, похоже, приходит уже накопительным, и мы накопили его дважды.
        # Сообщаем громко: тихо испорченная фича хуже пропуска.
        logger.warning(
            "матч %s: %d убийств у стороны — ряд kills, похоже, уже "
            "накопительный; выставить STRATZ_KILLS_CUMULATIVE=1",
            m.get("id"), worst)

    # kills_* — UInt16 в витрине, NaN туда не записать. Пропуск здесь и не
    # нужен: отсутствие точки в ряду означает «убийств к этой минуте не
    # зафиксировано», и ноль — верное её прочтение (в отличие от
    # position_advance, где ноль означал бы «бой в центре карты»).
    def _kills(series: list[int], i: int) -> int:
        return series[i] if len(series) > i else 0

    rows = []
    for i in range(1, n):
        rows.append({
            "match_id": int(m["id"]),
            "game_time": i * 60,
            "networth_diff": int(gold[i] or 0),
            "networth_total": math.nan,   # суммарного нетворса по минутам нет
            "xp_diff": int(xp[i] or 0),
            "kills_radiant": _kills(r_kills, i),
            "kills_dire": _kills(d_kills, i),
            "position_advance": math.nan,  # только из реплея
            "alive_diff": math.nan,        # только из реплея
            "towers_diff": math.nan,       # у STRATZ иная модель событий
            "rax_diff": math.nan,
            "radiant_win": radiant_win,
        })
    return rows


def match_passes(m: dict, min_duration_s: int, min_patch: int | None,
                 pro: bool = False) -> tuple[bool, str]:
    """Фильтр качества — та же популяция, что у JSON-источника OpenDota.

    Ранг не проверяется: у STRATZ он лежит в другом поле и не всегда
    заполнен, а кандидатов мы берём из /parsedMatches (public) уже после
    рангового фильтра либо из /proMatches (pro), где ранг не применим.
    """
    if not pro:
        if int(m.get("lobbyType", -1)) not in RANKED_LOBBIES:
            return False, "lobby"
        if int(m.get("gameMode", -1)) not in STANDARD_MODES:
            return False, "mode"
    if int(m.get("durationSeconds") or 0) < min_duration_s:
        return False, "short"
    if min_patch is not None and int(m.get("gameVersionId") or 0) < min_patch:
        return False, "old-patch"
    if not (m.get("radiantNetworthLeads") and m.get("radiantExperienceLeads")):
        return False, "no-timeline"
    return True, "ok"


class StratzTimelineSource:
    """Кандидаты — дешёвый листинг OpenDota, детали — GraphQL STRATZ.

    mode="public": /parsedMatches → tier=Premium.
    mode="pro":    /proMatches    → tier=Professional (эталон гейта).
    """

    # Читается раннером: строки STRATZ отличимы в витрине от opendota-json,
    # у которого заполнен трек F.
    feature_version = FEATURE_VERSION

    def __init__(self, token: str, limit_per_cycle: int = 40,
                 min_duration_s: int = 900, min_patch: int | None = None,
                 timeout: float = 30.0, api_delay_s: float = 0.35,
                 mode: str = "public",
                 opendota_base: str = "https://api.opendota.com/api",
                 opendota_key: str | None = None,
                 api_url: str = API_URL, kills_cumulative: bool = False,
                 shard: Shard | None = None,
                 split: SourceSplit | None = None) -> None:
        assert mode in ("public", "pro")
        if not token:
            raise ValueError(
                "STRATZ_API_TOKEN не задан — источник stratz работать не может")
        self._token = token
        self._mode = mode
        self.name = "stratz_timeline" if mode == "public" else "stratz_timeline_pro"
        self._candidates_path = ("parsedMatches" if mode == "public"
                                 else "proMatches")
        self._tier = "Premium" if mode == "public" else "Professional"
        self._limit = limit_per_cycle
        self._min_duration_s = min_duration_s
        self._min_patch = min_patch
        self._timeout = timeout
        # 250 запросов/мин у Default-токена → 0.35с между вызовами держит
        # ~170/мин с запасом на всплески.
        self._delay = api_delay_s
        self._api_url = api_url
        self._kills_cumulative = kills_cumulative
        self._opendota_base = opendota_base.rstrip("/")
        self._opendota_key = opendota_key
        self._shard = shard or Shard()
        # Своя доля кандидатов: JSON-источник OpenDota читает тот же
        # листинг с вершины, и без разделения оба брали бы одни матчи.
        self._split = split or SourceSplit()
        self._rejected: set[int] = set()
        # gameVersionId STRATZ -> patch id OpenDota; строится лениво.
        self._patch_map: dict[int, int] | None = None

    # -- транспорт ------------------------------------------------------------

    def _gql(self, query: str, variables: dict | None = None) -> dict:
        time.sleep(self._delay)
        resp = requests.post(
            self._api_url,
            json={"query": query, "variables": variables or {}},
            headers={"Authorization": f"Bearer {self._token}", **UA},
            timeout=self._timeout)
        resp.raise_for_status()
        body = resp.json()
        if body.get("errors"):
            # Схема STRATZ меняется — важно, чтобы в логе было ИМЯ поля,
            # а не «что-то пошло не так».
            raise StratzError(str(body["errors"])[:500])
        return body.get("data") or {}

    def _opendota(self, path: str, **params) -> list:
        resp = requests.get(f"{self._opendota_base}/{path}",
                            params=with_api_key(params, self._opendota_key),
                            timeout=self._timeout,
                            headers={"User-Agent": "manta-collector/1.0"})
        resp.raise_for_status()
        return resp.json()

    # -- патчи ----------------------------------------------------------------

    def _build_patch_map(self) -> dict[int, int]:
        """gameVersionId STRATZ → patch id OpenDota по совпадению имени.

        Обе стороны называют версии одинаково ("7.39"), а числовые id у них
        независимы. Сбой любой из сторон не должен ронять сбор: пустая карта
        означает patch=0, витрина при этом наполняется.
        """
        try:
            versions = (self._gql(VERSIONS_QUERY).get("constants") or {}
                        ).get("gameVersions") or []
            od = self._opendota("constants/patch")
        except Exception:  # noqa: BLE001
            logger.warning("карта патчей не построена — patch=0 у матчей",
                           exc_info=True)
            return {}
        by_name = {str(p["name"]).strip(): int(p["id"]) for p in od
                   if p.get("name") and p.get("id") is not None}
        out = {}
        for v in versions:
            od_id = by_name.get(str(v.get("name", "")).strip())
            if od_id is not None and v.get("id") is not None:
                out[int(v["id"])] = od_id
        logger.info("карта патчей STRATZ→OpenDota: %d версий", len(out))
        return out

    def _patch_of(self, m: dict) -> int:
        if self._patch_map is None:
            self._patch_map = self._build_patch_map()
        gv = m.get("gameVersionId")
        if gv is None:
            return 0
        return self._patch_map.get(int(gv), 0)

    # -- цикл -----------------------------------------------------------------

    def _candidates(self) -> Iterable[int]:
        """match_id с вершины листинга OpenDota (один дешёвый вызов)."""
        for entry in self._opendota(self._candidates_path):
            mid = entry.get("match_id")
            if mid is not None:
                yield int(mid)

    def fetch_new(self, after_cursor: str | None = None,
                  skip=None) -> Iterable[TimelineMatch]:
        """Свежие матчи из листинга, детали — из STRATZ.

        after_cursor игнорируется по той же причине, что и у JSON-источника
        OpenDota: листинг отдаёт id по убыванию, и возобновление «с прошлой
        позиции» уводило бы в прошлое от свежих матчей. Дедуп — предикат
        skip(match_id) по общей CollectedMatches.
        """
        skip = skip or (lambda _mid: False)
        if len(self._rejected) > 50_000:   # id монотонны, старые не вернутся
            self._rejected.clear()
        yielded = 0
        for mid in self._candidates():
            if yielded >= self._limit:
                return
            if (not self._shard.accepts(mid)
                    or not self._split.accepts(mid)
                    or skip(mid) or mid in self._rejected):
                continue
            try:
                data = self._gql(MATCH_QUERY, {"id": mid})
            except StratzError as e:
                # Ошибка схемы повторится на каждом матче — цикл обрывается,
                # чтобы не сжечь остаток лимита на заведомо битом запросе.
                logger.error("STRATZ GraphQL: %s — обрываю цикл", e)
                return
            except requests.HTTPError as e:
                if (e.response is not None
                        and e.response.status_code in (429, 401, 403)):
                    raise      # квота/токен — обрабатывается в __main__
                logger.warning("матч %d: %s — пропуск", mid, e)
                continue
            except requests.RequestException as e:
                logger.warning("матч %d: %s — пропуск", mid, e)
                continue
            m = data.get("match")
            if not m:
                self._rejected.add(mid)
                continue
            ok, why = match_passes(m, self._min_duration_s, self._min_patch,
                                   pro=(self._mode == "pro"))
            if not ok:
                logger.debug("матч %d отфильтрован: %s", mid, why)
                self._rejected.add(mid)
                continue
            patch = self._patch_of(m)
            rows = timeline_rows(m, self._kills_cumulative)
            if not rows:
                self._rejected.add(mid)
                continue
            yielded += 1
            yield TimelineMatch(match_id=mid, tier=self._tier, rows=rows,
                                source_cursor=str(mid), patch=patch, raw={})
