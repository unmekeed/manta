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
lobbyType, startDateTime. Этого хватает на ядро MatchTimelineFeatures.

Что добавлено в спринте 100. Вызов к STRATZ уже оплачен, и лишние поля
в том же запросе не стоят ни одного пункта квоты — растёт только размер
ответа. Взяты два, каждое закрывает измеримую дыру:

  players.stats.networthPerMinute → networth_total, а с ним networth_rel
      (radiantNetworthLeads — РАЗНОСТЬ, суммы из неё не получить, и
      колонка была NaN у 46% датасета);
  players.heroId + playerSlot     → MatchDraft для матчей STRATZ
      (драфт был только у 1224 матчей из 4129, отсюда и нулевое
      покрытие draft_prior).

Чего STRATZ по-прежнему не даёт (пишется NaN, как и у JSON-источника
OpenDota): position_advance и alive_diff — они существуют только в
реплее; фичи трека F (вижн, руны, нейтралки) — их собирает
all_minute_features из JSON OpenDota, у STRATZ структура иная. NaN здесь
честнее нуля: LightGBM обрабатывает пропуск нативно, а 0 был бы ложным
сигналом.

Не взято намеренно: leagueId, lane, position, role. Поля есть и выглядят
полезными, но каждое раздувает ответ на всех матчах, а неверно понятая
семантика молча портит фичу во всём датасете — так уже вышло с
gameVersionId. Добавлять их стоит тогда, когда под них есть потребитель.

Патчи. Колонка patch витрины нужна для даунвейта старых патчей (A9), и
заполняется она по ДАТЕ НАЧАЛА матча против constants/patch OpenDota —
ровно так, как это делает сам OpenDota для своих матчей. Версия STRATZ
(gameVersionId / constants.gameVersions) для этого НЕ годится, и это не
вопрос вкуса:

    список версий STRATZ на 2026-08-03 заканчивается на 7.40b, хотя
    игра уже четыре месяца на 7.41.

Матчам на неизвестной ему версии STRATZ проставляет последнюю известную,
то есть «новейшая версия» у него — корзина «7.40b и всё, что новее».
Перевод по имени давал таким матчам patch=59 (7.40) при актуальном 60, а
`patch_weights` умножает вес такой строки на 0.4 — систематический
даунвейт трети датасета за несуществующее устаревание. Ошибиться в
сторону 0 («неизвестен», без штрафа) не страшно; ошибиться в сторону
чужого номера — тихая порча взвешивания.

Дата от такой рассинхронизации защищена: она не зависит от того, успел
ли поставщик завести версию в справочник.
"""
from __future__ import annotations

import logging
import math
import os
import time
from datetime import datetime
from typing import Iterable

import requests

from . import Shard, SourceSplit, with_api_key
from .. import budget
from ..signals import HERO_BY_ID, team_of
from .opendota_timeline import TimelineMatch

logger = logging.getLogger("collector.stratz")

API_URL = "https://api.stratz.com/graphql"
# STRATZ отклоняет запросы без опознавательного User-Agent.
UA = {"User-Agent": "STRATZ_API"}

RADIANT = 2
RANKED_LOBBIES = {0, 7}
STANDARD_MODES = {1, 2, 3, 4, 5, 16, 22}

# STRATZ отдаёт lobbyType/gameMode ЭНУМАМИ-строками ("UNRANKED"), а не
# числами, как OpenDota. Порядок значений в обоих энумах совпадает с
# нумерацией Valve, поэтому имена переводятся в те же id, по которым
# фильтруют остальные источники.
LOBBY_NAMES = {
    "UNRANKED": 0, "PRACTICE": 1, "TOURNAMENT": 2, "TUTORIAL": 3,
    "COOP_BOTS": 4, "RANKED_TEAM_MMR": 5, "RANKED_SOLO_MMR": 6,
    "RANKED": 7, "SOLO_MID": 8, "BATTLE_CUP": 9, "EVENT": 12,
}
GAME_MODE_NAMES = {
    "NONE": 0, "ALL_PICK": 1, "CAPTAINS_MODE": 2, "RANDOM_DRAFT": 3,
    "SINGLE_DRAFT": 4, "ALL_RANDOM": 5, "INTRO": 6, "THE_DIRETIDE": 7,
    "REVERSE_CAPTAINS_MODE": 8, "THE_GREEVILING": 9, "TUTORIAL": 10,
    "MID_ONLY": 11, "LEAST_PLAYED": 12, "LIMITED_HEROES": 13,
    "COMPENDIUM_MATCHMAKING": 14, "CUSTOM": 15, "CAPTAINS_DRAFT": 16,
    "BALANCED_DRAFT": 17, "ABILITY_DRAFT": 18, "EVENT": 19,
    "ALL_RANDOM_DEATH_MATCH": 20, "SOLO_MID": 21, "ALL_PICK_RANKED": 22,
    "TURBO": 23, "MUTATION": 24,
}


# Перечитывание справочника патчей после неудачи — не чаще раза в 10 минут.
PATCH_MAP_RETRY_S = float(os.getenv("PATCH_MAP_RETRY_S", "600"))


def patch_at(started_at: int, patches: list[tuple[int, int]]) -> int:
    """id патча OpenDota, действовавшего в момент started_at (epoch).

    patches — пары (дата начала патча, id), по возрастанию даты. Матч
    относится к последнему патчу, вышедшему НЕ ПОЗЖЕ его начала. Ноль
    означает «неизвестен»: матч старше первого известного патча либо
    справочник не прочитан.
    """
    if not started_at or not patches:
        return 0
    out = 0
    for ts, pid in patches:
        if started_at >= ts:
            out = pid
        else:
            break
    return out


def parse_patch_dates(od_patches: list) -> list[tuple[int, int]]:
    """constants/patch OpenDota → [(epoch, id)] по возрастанию даты."""
    out = []
    for p in od_patches or []:
        raw, pid = p.get("date"), p.get("id")
        if not raw or pid is None:
            continue
        try:
            # '2026-03-24T00:50:59.580Z' — fromisoformat принимает 'Z'
            # только с 3.11, а машины кластера бывают старее.
            ts = datetime.fromisoformat(
                str(raw).replace("Z", "+00:00")).timestamp()
        except ValueError:
            continue
        out.append((int(ts), int(pid)))
    out.sort()
    return out


def enum_id(value, names: dict[str, int]) -> int:
    """id значения энума STRATZ: принимает и число, и имя.

    Неизвестное имя даёт -1 (матч не пройдёт фильтр) и пишется в лог:
    молча пропустить незнакомый режим в обучающую выборку хуже, чем
    потерять матч, но и знать о расширении энума нужно.
    """
    if isinstance(value, bool) or value is None:
        return -1
    if isinstance(value, (int, float)):
        return int(value)
    text = str(value).strip()
    if text.lstrip("-").isdigit():
        return int(text)
    key = text.upper()
    if key in names:
        return names[key]
    logger.warning("неизвестное значение энума STRATZ: %r", value)
    return -1

FEATURE_VERSION = "stratz-graphql@1"

# Поля матча. Держим списком, а не одной строкой: при расхождении со схемой
# STRATZ ошибка GraphQL называет конкретное поле, и правка очевидна.
MATCH_FIELDS = """
    id
    didRadiantWin
    durationSeconds
    gameMode
    lobbyType
    startDateTime
    rank
    radiantNetworthLeads
    radiantExperienceLeads
    radiantKills
    direKills
    players {
        playerSlot
        heroId
        stats { networthPerMinute }
    }
"""

MATCH_QUERY = "query($id: Long!) { match(id: $id) { %s } }" % MATCH_FIELDS

# НЕ ПЫТАТЬСЯ СНОВА: корневой `matches(ids: [Long]!)` в схеме есть, тип
# аргумента верный, но на Default-токене он отвечает
# `{"message": "User is not an admin."}` — эндпоинт админский (проверено
# 2026-08-06, спринт 122). Пакетная выборка была бы прямым множителем
# пропускной способности (квота STRATZ считается по HTTP-запросам, а не
# по матчам), и именно поэтому соблазн вернуться к ней велик. Нельзя:
# схема разрешает, авторизация — нет.


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
    totals = networth_totals(m, n)
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
            "networth_total": totals[i] if i < len(totals) else math.nan,
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


def stratz_rank(m: dict) -> int:
    """Средний ранг матча у STRATZ в шкале rank_tier; 0 — неизвестен.

    Полей-кандидатов в схеме четыре, и они не равноценны: `averageRank`
    на живых матчах приходит пустым, `bracket` — это всего лишь
    rank // 10 (тир без звезды, потеря точности), а `rank` и
    `actualRank` совпадают и дают полную шкалу. Берём `rank`,
    `actualRank` — запасной.
    """
    for key in ("rank", "actualRank"):
        val = m.get(key)
        if val is None:
            continue
        try:
            n = int(val)
        except (TypeError, ValueError):
            continue
        if n > 0:
            return n
    return 0


def networth_totals(m: dict, minutes: int) -> list[float]:
    """Суммарный нетворс обеих команд по минутам.

    radiantNetworthLeads — РАЗНОСТЬ, а не сумма, поэтому networth_total из
    неё не получить: 5000 преимущества при 20 тысячах общего золота и при
    100 тысячах означают совершенно разное, и ровно эту поправку даёт
    networth_rel. До спринта 100 колонка была NaN у всех матчей STRATZ,
    то есть networth_rel отсутствовал у 46% датасета.

    Пустой список (колонка останется NaN), если поминутного нетворса нет
    ни у кого: у старых ответов STRATZ поля players могло не быть.
    """
    series = []
    for p in m.get("players") or []:
        row = ((p.get("stats") or {}).get("networthPerMinute")) or []
        if row:
            series.append(row)
    if not series:
        return []
    out = []
    for i in range(minutes):
        total = 0.0
        for row in series:
            if i < len(row):
                try:
                    total += float(row[i] or 0)
                except (TypeError, ValueError):
                    pass
        out.append(total)
    return out


def draft_row(m: dict) -> dict | None:
    """Строка MatchDraft из составов STRATZ.

    До спринта 100 драфт был только у матчей OpenDota — 1224 из 4129, и
    это одна из причин нулевого покрытия draft_prior. Составы у STRATZ
    лежат прямо в ответе, который мы и так запрашиваем.

    Баны и порядок пика не берём: их в запросе нет, а добавлять поле ради
    графы, которой draft_prior не пользуется, значит раздувать ответ на
    всех матчах. bans=[] и first_pick_team=0 честнее выдумки.
    """
    radiant, dire = [], []
    for p in m.get("players") or []:
        npc = HERO_BY_ID.get(int(p.get("heroId") or 0))
        if not npc:
            return None            # неизвестный герой — матч мимо
        slot = p.get("playerSlot")
        if slot is None:
            return None
        (radiant if team_of(slot) == RADIANT else dire).append(npc)
    if len(radiant) != 5 or len(dire) != 5:
        return None
    return {
        "match_id": int(m["id"]),
        "patch": 0,                # проставит раннер из TimelineMatch.patch
        "radiant_win": 1 if m.get("didRadiantWin") else 0,
        "radiant_heroes": radiant,
        "dire_heroes": dire,
        "bans": [],
        "first_pick_team": 0,
        "source": "stratz",
    }


def match_passes(m: dict, min_duration_s: int, min_patch: int | None,
                 pro: bool = False, patch: int = 0,
                 min_rank: int = 0) -> tuple[bool, str]:
    """Фильтр качества — та же популяция, что у JSON-источника OpenDota.

    РАНГ. Раньше здесь стояло «ранг не проверяется, кандидаты и так из
    /parsedMatches после рангового фильтра». Это было неверно: OpenDota
    фильтрует по рангу не листинг, а ДЕТАЛИ матча (см. match_passes в
    opendota_timeline), и STRATZ, беря тот же листинг, забирал матчи
    любого ранга под тем же ярлыком tier='Premium'. Замер 2026-08-04:
    из четырёх свежесобранных матчей два оказались rank 44 (Legend 4) и
    64 (Ancient 4) — их порог 80 отсёк бы. Половина «высокоранговой»
    выборки была не высокоранговой, и модель училась на популяции, не
    похожей на про-эталон, по которому её судят.

    Шкала `rank` у STRATZ совпадает с rank_tier OpenDota (тир×10 +
    звезда), проверено интроспекцией и живыми матчами. min_rank=0
    отключает проверку.

    patch — УЖЕ переведённый id OpenDota (см. patch_at). Раньше здесь
    стоял сырой gameVersionId STRATZ, то есть номер из чужой нумерации
    сравнивался с порогом в нумерации OpenDota; фильтр молча отсекал бы
    не то, что задумано. Сейчас порог не задан ни на одной машине, но
    сравнение приведено к одной шкале, пока это не выстрелило.
    """
    if not pro:
        if enum_id(m.get("lobbyType"), LOBBY_NAMES) not in RANKED_LOBBIES:
            return False, "lobby"
        if enum_id(m.get("gameMode"), GAME_MODE_NAMES) not in STANDARD_MODES:
            return False, "mode"
    if int(m.get("durationSeconds") or 0) < min_duration_s:
        return False, "short"
    if min_patch is not None and patch < min_patch:
        return False, "old-patch"
    if not pro and min_rank:
        rank = stratz_rank(m)
        if rank == 0:
            # Неизвестный ранг отбрасываем так же, как OpenDota
            # ("ranks-unknown"): взять матч «на всякий случай» значило бы
            # вернуть ровно ту смесь популяций, ради устранения которой
            # фильтр и вводится. Счётчик причины виден в логе цикла.
            return False, "rank-unknown"
        if rank < min_rank:
            return False, "low-rank"
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
                 min_rank: int = 0,
                 timeout: float = 30.0, api_delay_s: float = 0.6,
                 mode: str = "public",
                 opendota_base: str = "https://api.opendota.com/api",
                 opendota_key: str | None = None,
                 api_url: str = API_URL, kills_cumulative: bool = False,
                 shard: Shard | None = None,
                 split: SourceSplit | None = None,
                 retry_attempts: int = 3,
                 detail_budget: int | None = None,
                 skip_freshest: int = 0,
                 id_lag: int = 0,
                 id_lag_min: int = 30_000,
                 id_lag_max: int = 400_000,
                 quota_floor: int = 500,
                 batch_size: int = 1) -> None:
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
        self._min_rank = min_rank
        self._timeout = timeout
        # 250 запросов/мин у Default-токена. Пауза 0.6с даёт ~100/мин на
        # источник — и это важно: public и pro работают ОДНОВРЕМЕННО и
        # делят один токен, поэтому считать надо суммарный темп. При
        # прежних 0.35с (~170/мин каждый) двое вместе давали ~340/мин и
        # ловили 429 (инцидент 2026-08-01).
        self._delay = api_delay_s
        self._api_url = api_url
        self._kills_cumulative = kills_cumulative
        self._opendota_base = opendota_base.rstrip("/")
        self._opendota_key = opendota_key
        self._shard = shard or Shard()
        # Своя доля кандидатов: JSON-источник OpenDota читает тот же
        # листинг с вершины, и без разделения оба брали бы одни матчи.
        self._split = split or SourceSplit()
        # Отказы ПОСТОЯННЫЕ: режим, лобби, длительность, патч, неразбор.
        # Такой матч не станет пригодным никогда, и повторный запрос —
        # чистая трата квоты.
        self._rejected: set[int] = set()
        # Отказы ВРЕМЕННЫЕ: у STRATZ ещё нет матча или его таймлайна.
        # Хранится число попыток; после retry_attempts матч переезжает
        # в постоянные. Подробности — в _defer().
        self._pending: dict[int, int] = {}
        self._retry_attempts = retry_attempts
        # Потолок detail-вызовов за цикл. Ограничение по limit_per_cycle
        # считает только УСПЕХИ, а вызов тратится и на промах: когда
        # промахов много, цикл мог перебрать все 1000 кандидатов.
        self._detail_budget = detail_budget or 4 * limit_per_cycle
        # Сколько самых свежих кандидатов пропустить (см. _candidates).
        self._skip_freshest = skip_freshest
        # Отступ от вершины листинга в ЕДИНИЦАХ match_id (спринт 114).
        # Замер 2026-08-05: id растут со скоростью ~789 в минуту, а 100
        # записей /parsedMatches укладываются в ~30 000 id ≈ 38 минут.
        # Отступ «в записях» поэтому означает то полчаса, то три — он
        # зависит от того, сколько матчей OpenDota успел распарсить, а не
        # от того, сколько времени было у STRATZ. Отступ в id — это
        # отступ во времени, и он устойчив.
        self._id_lag = max(int(id_lag), 0)
        self._id_lag_min = max(int(id_lag_min), 0)
        self._id_lag_max = max(int(id_lag_max), self._id_lag_min)
        # Состояние проверки гипотезы «промахи лечатся глубиной отступа».
        self._lag_probe: tuple[int, float] | None = None
        self._lag_frozen = False
        # Остаток суточной квоты по последнему ответу; None — ещё не
        # спрашивали. Обновляется в _gql из заголовка.
        self._quota_left: int | None = None
        self._quota_floor = max(int(quota_floor), 0)
        # Матчей на запрос. ВСЕГДА 1: пакетный `matches(ids:)` доступен
        # только админским токенам (спринт 122). Настройка оставлена,
        # чтобы при появлении подходящего токена включить пакет одной
        # переменной, а не переписывать цикл заново.
        self._batch_size = max(int(batch_size), 1)
        # Справочник патчей OpenDota [(дата, id)]; читается лениво.
        self._patches: list[tuple[int, int]] = []
        self._patch_map_at = -PATCH_MAP_RETRY_S

    # -- транспорт ------------------------------------------------------------

    def _gql(self, query: str, variables: dict | None = None) -> dict:
        time.sleep(self._delay)
        resp = requests.post(
            self._api_url,
            json={"query": query, "variables": variables or {}},
            headers={"Authorization": f"Bearer {self._token}", **UA},
            timeout=self._timeout)
        # Остаток суточной квоты — из ЗАГОЛОВКА ОТВЕТА, а не из своего
        # счётчика (спринт 120). Свой счётчик врёт при рестарте процесса
        # и не знает про вторую машину с тем же токеном: лимит STRATZ
        # привязан к ТОКЕНУ, и один токен на двоих означает общую квоту.
        # Пока бюджет цикла был 100, до потолка мы не доходили и врать
        # счётчику было негде; с открытым дросселем это уже вопрос
        # нескольких часов.
        left = resp.headers.get("x-ratelimit-remaining-day")
        if left is not None:
            try:
                self._quota_left = int(left)
            except ValueError:
                pass
        resp.raise_for_status()
        body = resp.json()
        if body.get("errors"):
            # Схема STRATZ меняется — важно, чтобы в логе было ИМЯ поля,
            # а не «что-то пошло не так».
            raise StratzError(str(body["errors"])[:500])
        return body.get("data") or {}

    def _opendota(self, path: str, **params) -> list:
        budget.spend()
        resp = requests.get(f"{self._opendota_base}/{path}",
                            params=with_api_key(params, self._opendota_key),
                            timeout=self._timeout,
                            headers={"User-Agent": "manta-collector/1.0"})
        resp.raise_for_status()
        return resp.json()

    # -- патчи ----------------------------------------------------------------

    def _load_patch_dates(self) -> list[tuple[int, int]]:
        """Справочник патчей OpenDota как [(дата начала, id)].

        Один вызов дешёвого constants-эндпоинта. Сбой не должен ронять
        сбор: пустой справочник означает patch=0, витрина при этом
        наполняется.
        """
        try:
            dates = parse_patch_dates(self._opendota("constants/patch"))
        except Exception:  # noqa: BLE001
            logger.warning("справочник патчей не прочитан — patch=0 у матчей",
                           exc_info=True)
            return []
        if dates:
            logger.info("справочник патчей OpenDota: %d записей, последний "
                        "id=%d", len(dates), dates[-1][1])
        else:
            # Пустой справочник — ТИХАЯ потеря колонки patch у всех матчей
            # источника, а с ней и даунвейта старых патчей (A9). Молчать
            # нельзя: в витрине это выглядит как обычный ноль.
            logger.warning("справочник патчей ПУСТ — patch=0 у всех матчей "
                           "STRATZ")
        return dates

    def _patch_of(self, m: dict) -> int:
        if not self._patches:
            # ПОВТОРЯЕМ чтение, а не кэшируем неудачу навсегда: прежняя
            # версия писала пустой результат в кэш при первом же сбое
            # (OpenDota моргнул) и процесс жил неделями, проставляя
            # patch=0 каждому матчу. Чтобы повтор не превратился в лишний
            # вызов на КАЖДЫЙ матч, он не чаще PATCH_MAP_RETRY_S.
            now = time.monotonic()
            if now - self._patch_map_at >= PATCH_MAP_RETRY_S:
                self._patch_map_at = now
                self._patches = self._load_patch_dates()
        started = m.get("startDateTime")
        return patch_at(int(started or 0), self._patches)

    # -- цикл -----------------------------------------------------------------

    def _consume(self, mid: int, m: dict, stats: dict, defer) -> object:
        """Разобрать один матч ответа: TimelineMatch либо None.

        Вынесено из цикла при переходе на пакетную выборку (спринт 121) —
        логика не менялась. Разбор ОДНОГО матча не имеет права ронять
        цикл: STRATZ меняет формат полей (2026-07-31 lobbyType приехал
        строкой-энумом вместо числа), и один неожиданный тип обнулял весь
        проход, а с ним и половину суточного притока. С пакетом цена
        такой ошибки выросла бы ещё: рухнул бы разбор всех матчей пакета.
        """
        try:
            # Патч считается ДО фильтра: min_patch сравнивается с ним,
            # а не с чужой нумерацией версий STRATZ.
            patch = self._patch_of(m)
            ok, why = match_passes(m, self._min_duration_s, self._min_patch,
                                   pro=(self._mode == "pro"), patch=patch,
                                   min_rank=self._min_rank)
            if not ok:
                if why == "no-timeline":
                    # Матч у STRATZ есть, а поминутных рядов ещё нет — он
                    # в очереди на парсинг. Причина временная.
                    defer(mid, "нет рядов")
                    return None
                stats["фильтр"] += 1
                stats[f"  · {why}"] = stats.get(f"  · {why}", 0) + 1
                # ЗАМЕР ПРЕДЛОЖЕНИЯ ПО РАНГАМ (спринт 120). «low-rank: 17»
                # говорит, что матч не дотянул, но не говорит НАСКОЛЬКО.
                # Отсеянные на 75–79 значат, что планка режет соседнюю
                # скобку; отсеянные на 40–50 — что Immortal-матчей мало и
                # квота не поможет.
                if why == "low-rank":
                    r = stratz_rank(m)
                    band = f"{r // 10 * 10}-{r // 10 * 10 + 9}"
                    key = f"    ранг {band}"
                    stats[key] = stats.get(key, 0) + 1
                self._rejected.add(mid)
                return None
            rows = timeline_rows(m, self._kills_cumulative)
        except (ValueError, TypeError, KeyError) as e:
            logger.warning("матч %d: не разобрать ответ STRATZ (%s) — пропуск",
                           mid, e)
            self._rejected.add(mid)
            return None
        if not rows:
            defer(mid, "нет рядов")
            return None
        return TimelineMatch(match_id=mid, tier=self._tier, rows=rows,
                             source_cursor=str(mid), patch=patch,
                             avg_rank=stratz_rank(m),
                             draft=draft_row(m), raw={})

    def _adapt_lag(self, calls: int, misses: int) -> None:
        """Подстроить отступ по доле промахов прошедшего цикла.

        Константу здесь угадывали дважды и оба раза мимо: сначала отступа
        не было вовсе, потом поставили 150 записей — и 87–93 вызова из
        ста всё равно уходили в «ждут парсинга». Третьей догадки не будет:
        источник сам знает, сколько раз промахнулся, и двигает отступ.

        Асимметрия намеренная. Растём БЫСТРО (×1.5): каждый цикл с
        промахами — это сожжённая квота и несобранные матчи. Убываем
        МЕДЛЕННО (×0.9): слишком глубокий отступ не теряет матчи (листинг
        — движущееся окно, всё опустится к следующему циклу), он лишь
        добавляет дубликатов. То есть ошибка вверх дешевле ошибки вниз, и
        шаги это отражают.

        Порог «мало промахов» — 10%, а не 0: STRATZ парсит матчи не
        мгновенно и не строго по порядку, поэтому единичные промахи
        неустранимы, и гнаться за нулём значит уползать всё глубже.
        """
        if calls <= 0:
            # Ни одного вызова — учиться не на чем, но и оставлять отступ
            # как есть нельзя: если он вырос до недостижимого, источник
            # молчит, а «нет промахов» выглядит как здоровье. Сжимаем.
            self._id_lag = max(self._id_lag_min, int(self._id_lag * 0.5))
            return
        rate = misses / calls
        before = self._id_lag

        # ПРОВЕРКА ГИПОТЕЗЫ (спринт 116). Регулятор, который умеет только
        # наращивать, всегда доезжает до потолка — даже когда его рычаг ни
        # на что не влияет. Замер 2026-08-05: отступ прошёл 90 000 →
        # 400 000 (2 часа → 8.4 часа игрового потока), а промахи остались
        # 75–88%. Глубина не лечила НИЧЕГО, и без этой проверки источник
        # так и работал бы на потолке, собирая матчи восьмичасовой
        # давности без всякой пользы.
        if self._lag_probe is not None:
            probe_lag, probe_rate = self._lag_probe
            self._lag_probe = None
            if self._id_lag > probe_lag and rate > probe_rate - 0.05:
                # Подняли отступ — промахи не упали. Рычаг не работает:
                # возвращаемся и больше не растём. Уменьшаться при этом
                # по-прежнему можно: вдруг причина исчезнет сама.
                self._id_lag = probe_lag
                self._lag_frozen = True
                logger.info("отступ STRATZ заморожен на %d: подъём с %d не "
                            "снизил промахи (%.0f%% против %.0f%%) — причина "
                            "НЕ в свежести кандидатов",
                            probe_lag, probe_lag, rate * 100, probe_rate * 100)
                return

        if rate > 0.5 and not self._lag_frozen:
            self._lag_probe = (self._id_lag, rate)
            self._id_lag = int(self._id_lag * 1.5) or self._id_lag_min
        elif rate < 0.1:
            self._id_lag = int(self._id_lag * 0.9)
        self._id_lag = max(self._id_lag_min, min(self._id_lag, self._id_lag_max))
        if self._id_lag != before:
            logger.info("отступ STRATZ: %d -> %d (промахов %.0f%% из %d "
                        "вызовов, ~%d мин игрового потока)",
                        before, self._id_lag, rate * 100, calls,
                        self._id_lag // 789)

    def _max_pages(self) -> int:
        """Сколько страниц листинга листать под текущий бюджет вызовов."""
        return min(max(self._detail_budget // 10, 20), 60)

    def _candidates(self, max_pages: int | None = None) -> Iterable[int]:
        """match_id из листинга OpenDota, с пагинацией вглубь.

        ОТСТУП ОТ ВЕРШИНЫ (спринт 95). Листинг отдаёт матчи, которые
        распарсил OpenDota, а детали мы просим у STRATZ — и он отстаёт.
        Матч с вершины листинга у STRATZ обычно ещё без поминутных
        рядов, вызов уходит впустую, и матч откладывается до следующего
        цикла. В логе это выглядело так:

            собрано 1 из 421 кандидатов, вызовов 100
            (… ждут парсинга: 87, бюджет вызовов: 1)

        Восемьдесят семь вызовов из ста — по матчам, которых у STRATZ
        ещё нет. Бюджет цикла выгорал на них, и до готовых кандидатов
        очередь просто не доходила: один собранный матч за цикл при
        лимите 25 и целой квоте (13834 из 15000 на сутки).
        Пропуская N самых свежих, мы попадаем в матчи, которые STRATZ
        успел обработать. Ничего не теряется: листинг — движущееся
        окно, и пропущенный сейчас матч опустится ниже отступа к
        следующему циклу.

        Одной страницы (100 записей) не хватает: шард машины делит их
        пополам, SourceSplit со STRATZ — ещё пополам, и после дедупа от
        сотни остаются единицы. Именно поэтому цикл собирал 4–13 матчей
        при лимите 40, упираясь не в квоту STRATZ, а в поставку
        кандидатов. Листинг дёшев (один вызов на 100 записей), так что
        уходим вглубь, пока не наберём достаточно или не кончатся
        страницы.
        """
        # Страниц столько, сколько нужно бюджету вызовов (спринт 120).
        # Страница даёт 100 id, из них шард и SourceSplit оставляют
        # четверть, а дедуп срезает ещё. Фиксированные 20 страниц были
        # рассчитаны на бюджет 100; с бюджетом 300 источник упирался бы
        # не в квоту, а в НЕХВАТКУ КАНДИДАТОВ — и выглядело бы это как
        # «STRATZ отдаёт мало», хотя дело в нашем листинге.
        # Листинг дёшев: один вызов на 100 записей, и он же общий для
        # всех коллекторов машины, поэтому глубину берём с запасом, но
        # не безграничную.
        if max_pages is None:
            max_pages = self._max_pages()
        cursor: int | None = None
        seen: set[int] = set()
        skipped = 0
        head: int | None = None      # самый свежий id листинга этого цикла
        deep: list[int] = []         # отсеянные отступом, от свежих к старым
        yielded_any = False
        for _ in range(max_pages):
            params = {}
            if cursor:
                params["less_than_match_id"] = cursor
            batch = self._opendota(self._candidates_path, **params)
            if not batch:
                return
            fresh = 0
            for entry in batch:
                mid = entry.get("match_id")
                if mid is None:
                    continue
                cursor = int(mid)
                if cursor in seen:
                    continue          # страница повторяет уже выданное
                seen.add(cursor)
                fresh += 1
                if head is None:
                    head = cursor
                if skipped < self._skip_freshest:
                    # Самые свежие кандидаты пропускаются НАМЕРЕННО.
                    skipped += 1
                    continue
                # Отступ в единицах match_id: то же самое, что отступ во
                # времени, но не зависит от того, с какой скоростью
                # OpenDota наполняет листинг.
                if self._id_lag and head - cursor < self._id_lag:
                    skipped += 1
                    deep.append(cursor)
                    continue
                yielded_any = True
                yield cursor
            # Страница не дала ни одного нового id — значит API не понял
            # less_than_match_id и отдаёт одно и то же. Дальше листать
            # бессмысленно: без этой проверки цикл жёг бы вызовы, гоняя
            # одни и те же кандидаты по кругу.
            if fresh == 0:
                break
        # СТРАХОВКА ОТ САМОУДУШЕНИЯ. Отступ отсчитывается от вершины
        # листинга, поэтому при большом значении до кандидатов надо
        # пролистать несколько страниц. Если листинг оказался короче
        # отступа (мало распаршенных матчей, лимит страниц, ночной
        # провал), цикл не сделал бы НИ ОДНОГО вызова — а подстройка,
        # которой не на чем учиться, оставила бы отступ прежним. Источник
        # замолчал бы навсегда, ровно как душил себя кэшем отказов до
        # спринта 87.
        # Поэтому: не отдали ничего — отдаём самые глубокие из отсеянных.
        if not yielded_any and deep:
            logger.info("отступ %d съел весь листинг (%d кандидатов) — "
                        "беру самые глубокие", self._id_lag, len(deep))
            for mid in deep[-max(len(deep) // 4, 1):]:
                yield mid

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
        if len(self._pending) > 50_000:
            self._pending.clear()
        yielded = 0
        calls = 0
        # Счётчики причин отсева: без них в логе виден только итог
        # «собрано N», и непонятно, упёрлись мы в лимит, в дедуп или в
        # фильтр качества — а лечатся эти три случая по-разному.
        stats = {"кандидатов": 0, "чужой шард": 0, "не моя доля": 0,
                 "дубликат": 0, "кэш отказов": 0, "фильтр": 0,
                 "нет данных": 0, "ждут парсинга": 0, "бюджет вызовов": 0,
                 "квота на исходе": 0}

        def report() -> None:
            logger.info("цикл STRATZ: собрано %d из %d кандидатов, "
                        "вызовов %d, отступ %d (%s)",
                        yielded, stats["кандидатов"], calls, self._id_lag,
                        ", ".join(f"{k}: {v}" for k, v in stats.items()
                                  if k != "кандидатов" and v))
            # Подстройка ПОСЛЕ печати: в логе остаётся отступ, с которым
            # цикл реально работал, а не тот, что применится к следующему.
            # Иначе разбор постфактум («почему промахов было 87») уводил
            # бы к неверному числу.
            self._adapt_lag(calls, stats["ждут парсинга"] + stats["нет данных"])

        def defer(mid: int, kind: str = "нет матча") -> None:
            """Матч без данных — причина ВРЕМЕННАЯ, не повод хоронить.

            STRATZ парсит матч с задержкой, а кандидаты берутся с вершины
            листинга OpenDota, то есть самые свежие. Прежняя версия
            отправляла такой матч в постоянный кэш отказов навсегда — и
            он не пробовался снова, хотя данные появлялись через
            минуты. Источник душил сам себя: за три цикла подряд
            2026-08-03 «кэш отказов» рос 158→176→202 при падении «нет
            данных» 72→56→36, и сбор давал 3, 3, 5 матчей вместо 25 —
            при израсходованных 8% суточной квоты STRATZ.

            Постоянные причины (режим, лобби, длительность, патч,
            неразбор ответа) по-прежнему уходят в _rejected сразу.
            """
            n = self._pending.get(mid, 0) + 1
            self._pending[mid] = n
            if n >= self._retry_attempts:
                del self._pending[mid]
                self._rejected.add(mid)
                stats["нет данных"] += 1
            else:
                stats["ждут парсинга"] += 1
            # Разделение видов — спринт 116. Прежде оба случая падали в
            # один счётчик, и по нему нельзя было отличить «STRATZ не
            # знает матч» от «знает, но поминутных рядов нет». Разница
            # решающая: первое отступом НЕ лечится вовсе (матча у STRATZ
            # просто не будет), второе — вопрос времени.
            stats[f"  ~ {kind}"] = stats.get(f"  ~ {kind}", 0) + 1

        def accepted():
            """Кандидаты, прошедшие ДЕШЁВЫЕ проверки — до похода в сеть."""
            for mid in self._candidates():
                stats["кандидатов"] += 1
                if not self._shard.accepts(mid):
                    stats["чужой шард"] += 1
                    continue
                if not self._split.accepts(mid):
                    stats["не моя доля"] += 1
                    continue
                if skip(mid):
                    stats["дубликат"] += 1
                    continue
                if mid in self._rejected:
                    stats["кэш отказов"] += 1
                    continue
                yield mid

        def take(ids: list[int]):
            """Забрать матчи списка и отдать пригодные.

            Запрос НА КАЖДЫЙ id: пакетный `matches(ids:)` доступен только
            админским токенам (спринт 122). Проход именно по списку, а не
            по первому элементу — промежуточная версия брала `ids[0]`, а
            перебирала все, и остальные молча уходили в «нет матча»: при
            пакете в десять терялось девять матчей из десяти, а в логе
            это выглядело как «STRATZ их не знает».

            Возвращает через `return` False, если цикл пора закончить
            (лимит, бюджет, квота, ошибка схемы) — вызывающий обязан это
            проверить.
            """
            nonlocal yielded, calls
            for mid in ids:
                if yielded >= self._limit:
                    return False
                # Предохранитель по остатку СУТОЧНОЙ квоты (спринт 120).
                # Проверка ДО запроса: заголовок отдаёт остаток на момент
                # ОТВЕТА, и у самой границы легко проскочить.
                if (self._quota_left is not None
                        and self._quota_left <= self._quota_floor):
                    stats["квота на исходе"] += 1
                    return False
                if calls >= self._detail_budget:
                    # Считаем ЗАПРОСЫ: именно они списываются с квоты, и
                    # промах стоит столько же, сколько попадание.
                    stats["бюджет вызовов"] += 1
                    return False
                calls += 1
                try:
                    data = self._gql(MATCH_QUERY, {"id": mid})
                except StratzError as e:
                    # Ошибка схемы повторится на каждом запросе — цикл
                    # обрывается, чтобы не сжечь остаток лимита.
                    logger.error("STRATZ GraphQL: %s — обрываю цикл", e)
                    return False
                except requests.HTTPError as e:
                    if (e.response is not None
                            and e.response.status_code in (429, 401, 403)):
                        raise      # квота/токен — обрабатывается в __main__
                    logger.warning("матч %d: %s — пропуск", mid, e)
                    continue
                except requests.RequestException as e:
                    logger.warning("матч %d: %s — пропуск", mid, e)
                    continue

                # Сверяем id ОТВЕТА с запрошенным. Перепутать сейчас
                # неоткуда, но проверка стоит копейки, а цена ошибки —
                # ряды одного матча под id другого: правдоподобно (ряды
                # настоящие, id настоящий) и почти незаметно.
                m = data.get("match")
                try:
                    if m is not None and int(m["id"]) != mid:
                        m = None
                except (TypeError, ValueError, KeyError):
                    m = None
                if not m:
                    defer(mid, "нет матча")
                    continue
                out = self._consume(mid, m, stats, defer)
                if out is not None:
                    yielded += 1
                    yield out
            return True

        # try/finally, а не report() перед каждым return: выходов из
        # цикла пять (лимит, бюджет, исчерпание кандидатов, ошибка
        # схемы, 429), и на двух из них отчёт терялся вместе с
        # подстройкой отступа. Ровно эту дыру спринт 104 закрыл у
        # источника OpenDota, а здесь она осталась.
        try:
            # ПАКЕТНАЯ ВЫБОРКА (спринт 121). Квота STRATZ считается по
            # HTTP-ЗАПРОСАМ, а `matches(ids: [...])` отдаёт до нескольких
            # десятков матчей за один запрос. Раньше на каждый матч
            # уходил свой вызов, и потолок был жёстким: 15 000 запросов в
            # сутки при выходе 4–5% — это около 700 матчей.
            #
            # С пакетом в N раз больше выборок на ту же квоту, и — что
            # важнее — промахи «STRATZ не знает матч» перестают чего-либо
            # стоить: пакет просто вернёт меньше элементов, а запрос
            # потрачен один. Именно промахи (80% вызовов) и делали
            # источник дорогим.
            batch: list[int] = []
            for mid in accepted():
                batch.append(mid)
                if len(batch) >= self._batch_size:
                    if not (yield from take(batch)):
                        return
                    batch = []
            if batch:
                yield from take(batch)
        finally:
            report()
