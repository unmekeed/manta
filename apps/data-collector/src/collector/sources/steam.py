"""Поток матчей Valve — Steam Web API (спринт 123).

Зачем нужен, если есть STRATZ и OpenDota. Затем, что Valve — не агрегатор,
а первоисточник, и у него нет квоты в нашем смысле: один вызов
GetMatchHistoryBySequenceNum отдаёт СТО матчей целиком, то есть тысячу
account_id. Это ровно то топливо, которого требует кэш рангов: чтобы
опрашивать самых частых игроков, надо сначала увидеть поток и посчитать,
кто в нём частый.

Чего у Valve НЕТ и не будет (проверено зондами 2026-08-06):
  * ранга — ни одного поля про rank/skill/mmr, ни у матча, ни у игрока;
  * поминутных рядов — только итоги матча (net_worth, gpm, xpm, предметы);
  * replay_salt — он приходит только из Game Coordinator, не из Web API.
Поэтому этот модуль НЕ является источником матчей в смысле Source: он не
отдаёт MatchRef и ничего не скачивает. Он поставляет сырые матчи и
account_id, а решение «брать или не брать» принимается по кэшу рангов.

Отдельная ловушка, стоившая мне одного неверного вывода. Вызов без
start_at_match_seq_num читает от НАЧАЛА записанной истории (2011 год), а
не с конца. Ответ при этом совершенно валидный — сто матчей, статус 1, —
просто это матчи пятнадцатилетней давности с game_mode=0. Отсюда правило:
номер последовательности задаётся ВСЕГДА, а найти его хвост — отдельная
операция (tip_seq).
"""
from __future__ import annotations

import logging
import time

import requests

logger = logging.getLogger("collector.steam")

API_BASE = "https://api.steampowered.com/IDOTA2Match_570"

# account_id игрока со скрытым профилем. Valve подставляет 0xFFFFFFFF
# вместо настоящего id — это не игрок, а заглушка, и одна и та же для
# всех. Не отфильтровать её значит завести в кэше «аккаунт», который
# встречается в каждом втором матче, вечно занимает первое место в
# очереди опроса и не резолвится никогда.
ANONYMOUS_ACCOUNT_ID = 4294967295

# Верхняя граница для поиска хвоста последовательности. Номера растут
# монотонно и медленнее, чем match_id; 2^34 (~17 млрд) с большим запасом
# перекрывает обозримое будущее и ограничивает поиск ~34 итерациями.
SEQ_SEARCH_CEILING = 1 << 34

# Точность поиска хвоста. 10 000 матчей мирового потока — это порядка
# десяти минут; уточнять дальше значит гоняться за движущейся целью.
SEQ_TIP_PRECISION = 10_000


class SteamAPIError(RuntimeError):
    """Valve ответил, но не данными (status != 1 или HTTP-ошибка)."""


class SteamMatchStream:
    """Чтение живого потока матчей по номеру последовательности."""

    def __init__(self, api_key: str, base_url: str = API_BASE,
                 timeout: float = 30.0, api_delay_s: float = 1.0) -> None:
        if not api_key:
            raise ValueError("steam: пустой STEAM_API_KEY")
        self._key = api_key
        self._base = base_url.rstrip("/")
        self._timeout = timeout
        self._api_delay_s = api_delay_s
        self._last_call = 0.0

    # -- транспорт -------------------------------------------------------------

    def _get(self, method: str, **params) -> dict:
        gap = self._api_delay_s - (time.monotonic() - self._last_call)
        if gap > 0:
            time.sleep(gap)
        params["key"] = self._key
        resp = requests.get(f"{self._base}/{method}/v1/",
                            params=params, timeout=self._timeout)
        self._last_call = time.monotonic()
        if resp.status_code != 200:
            raise SteamAPIError(f"{method}: HTTP {resp.status_code}")
        result = (resp.json() or {}).get("result") or {}
        # status=1 — успех. Всё остальное (в том числе 8 «за пределом
        # последовательности») отдаём как ошибку с текстом Valve: гадать
        # по коду мы уже пробовали, дешевле прочитать.
        if result.get("status") != 1:
            raise SteamAPIError(
                f"{method}: status={result.get('status')} "
                f"{result.get('statusDetail', '')}".strip())
        return result

    # -- поток -----------------------------------------------------------------

    def batch(self, seq: int, count: int = 100) -> list[dict]:
        """Сто матчей начиная с номера последовательности seq."""
        result = self._get("GetMatchHistoryBySequenceNum",
                           start_at_match_seq_num=int(seq),
                           matches_requested=int(count))
        return list(result.get("matches") or [])

    def _seq_alive(self, seq: int) -> bool:
        """Есть ли матчи начиная с seq. False и на ошибке «за пределом»."""
        try:
            return bool(self.batch(seq, count=1))
        except SteamAPIError as exc:
            logger.debug("seq %s недоступен: %s", seq, exc)
            return False

    def tip_seq(self) -> int:
        """Найти конец последовательности удвоением и делением пополам.

        Дорого (порядка 40 вызовов), поэтому результат положено сохранять
        в CollectorCursor и дальше двигать инкрементально. Точность —
        SEQ_TIP_PRECISION матчей: искать хвост точнее бессмысленно, за
        время поиска он всё равно уедет вперёд.
        """
        lo = 1
        if not self._seq_alive(lo):
            raise SteamAPIError("поток пуст с самого начала — проверь ключ")
        hi = 2
        while hi < SEQ_SEARCH_CEILING and self._seq_alive(hi):
            lo, hi = hi, hi * 2
        if hi >= SEQ_SEARCH_CEILING:
            raise SteamAPIError("хвост последовательности выше потолка поиска")
        while hi - lo > SEQ_TIP_PRECISION:
            mid = (lo + hi) // 2
            if self._seq_alive(mid):
                lo = mid
            else:
                hi = mid
        return lo


def match_accounts(match: dict) -> list[int]:
    """account_id всех игроков матча, без анонимов и дублей.

    Дубли реальны: у Valve встречаются матчи, где один и тот же слот
    продублирован, а UPSERT кэша по такому списку падает с «ON CONFLICT
    cannot affect row a second time». Чистим здесь, у самого источника,
    а не в кэше — иначе каждый будущий поставщик аккаунтов будет обязан
    помнить об этом сам.
    """
    seen: dict[int, None] = {}
    for p in match.get("players") or []:
        raw = p.get("account_id")
        if raw is None:
            continue
        try:
            aid = int(raw)
        except (TypeError, ValueError):
            continue
        if aid <= 0 or aid == ANONYMOUS_ACCOUNT_ID:
            continue
        seen.setdefault(aid, None)
    return list(seen)
