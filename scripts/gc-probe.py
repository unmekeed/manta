#!/usr/bin/env python3
"""Пробный замер Game Coordinator: сколько солей реплеев отдаёт один аккаунт.

ЗАЧЕМ. Соль реплея (`replay_salt`) не отдаёт ни один эндпоинт Steam Web
API — её знает только Game Coordinator. Поэтому сейчас за каждый глубокий
матч мы платим один вызов OpenDota, а их 2000 в сутки на IP и купить
больше нельзя. GC убрал бы эту зависимость целиком.

Прежде чем строить бота, надо ответить на единственный вопрос, от
которого зависит смысл затеи: СКОЛЬКО ОДИН АККАУНТ ОТДАЁТ В СУТКИ. От
ответа зависит не количество аккаунтов, а вывод по проекту — одна учётка
или ферма из двадцати.

Докстринги библиотеки `ValvePython/dota2` заявляют:

    request_match_details  — 100 запросов/сутки
    request_matches        — 50 запросов/сутки (но отдаёт СПИСОК матчей)

Верить им нельзя: это комментарии, написанные годы назад, а Valve с тех
пор не раз меняла GC. Отсюда три режима замера — по одному на каждый
путь получения соли.

ПОЧЕМУ ОТДЕЛЬНОЕ ОКРУЖЕНИЕ. `dota2` тянет protobuf 3.20 и gevent.
Коллекторы живут на своём protobuf и своём event loop; ставить это в один
интерпретатор — гарантированный конфликт. Venv создаётся вне репозитория
(`make gc-venv`), в git не попадает ничего.

ВХОД. Пароль этот скрипт не видит вовсе — он входит по refresh-токену
из ~/.manta-gc/refresh-token. Так вышло не из красоты: steam==1.4.4
логинится устаревшим сообщением (пароль открытым полем в ClientLogon),
Valve этот путь отключила, и на верный пароль приходит InvalidPassword.
Нового механизма в библиотеке нет и не будет — 1.4.4 последняя на PyPI.

Поэтому пароль на токен меняет Node, у которого библиотека
поддерживаемая, а замер остаётся питоновским:

    make gc-node      # один раз
    make gc-token     # редко: токен живёт месяцами
    make gc-probe ARGS=login

Токен равносилен паролю: лежит с правами 0600 вне репозитория, не
печатается и не попадает в сообщения об ошибках.

ЧТО ЗАМЕР НЕ ДЕЛАЕТ. Ничего не пишет в базы, не трогает очередь
кандидатов (только SELECT — иначе отобрал бы кандидатов у живого
коллектора) и не расходует квоту OpenDota вообще.

Запуск:
    make gc-venv                    # один раз
    make gc-probe ARGS=login        # пускает ли аккаунт в GC
    make gc-probe ARGS="details --limit 200"
    make gc-probe ARGS=bulk
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

CREDENTIAL_DIR = Path(os.getenv("GC_STATE_DIR",
                                Path.home() / ".manta-gc")).expanduser()

# Пауза между запросами. Секунда — заведомо мягкий темп: замер ищет
# СУТОЧНЫЙ потолок, а не то, как быстро можно получить 429.
DEFAULT_DELAY_S = 1.0

# Сколько отказов подряд считать концом. Один отказ бывает случайным
# (матч слишком старый, GC моргнул), три подряд — это стена.
STOP_AFTER_FAILURES = 3

# Сколько ждать ответа на массовый запрос. Минута — с большим запасом:
# GC отвечает за секунды, и если он молчит минуту, он молчит совсем.
BULK_TIMEOUT_S = 60


def die(msg: str) -> None:
    print(f"ОШИБКА: {msg}", file=sys.stderr)
    raise SystemExit(2)


# -- источник match_id ------------------------------------------------------------

def match_ids_from_queue(limit: int) -> list[int]:
    """Свежие кандидаты из очереди — ТОЛЬКО ЧТЕНИЕ.

    Брать через CandidateQueue.take() нельзя: он помечает кандидатов
    `taken`, и замер увёл бы их у работающего коллектора.
    """
    try:
        import psycopg
    except ImportError:
        # Молчать здесь нельзя. Пустой список неотличим от пустой
        # очереди, и замер честно сообщал «очередь пуста» в тот момент,
        # когда очередь он попросту не умел прочитать: psycopg живёт в
        # окружении коллекторов, а у замера своё, где его нет.
        print("psycopg в окружении замера нет — очередь прочитать нечем.")
        print("  проще:   make gc-probe ARGS='details --from-public 100'")
        print("  либо:    ~/.manta-gc-venv/bin/pip install 'psycopg[binary]'")
        return []
    dsn = os.getenv("POSTGRES_DSN",
                    "postgresql://dota:dota_dev_password@localhost:5432/manta")
    try:
        with psycopg.connect(dsn, autocommit=True) as db, db.cursor() as cur:
            cur.execute(
                "SELECT match_id FROM ReplayCandidates "
                " WHERE state = 'new' ORDER BY found_at DESC LIMIT %s",
                (limit,))
            return [int(r[0]) for r in cur.fetchall()]
    except Exception as exc:  # noqa: BLE001 — БД не обязательна для замера
        print(f"очередь недоступна ({exc}); нужен --match-ids или --from-public")
        return []


def match_ids_from_public(limit: int) -> list[int]:
    """Свежие match_id из публичной ленты OpenDota — один вызов на сотню.

    Зачем, если есть очередь кандидатов. Затем, что замеру всё равно,
    ЧЬИ это матчи: он меряет суточный потолок аккаунта в Game
    Coordinator, а не качество отбора. Требовать для этого поднятую
    Postgres с непустой очередью значит связывать замер с состоянием
    машины, на которой он запущен, — а на второй машине очередь пуста, и
    замер из-за этого не стартовал вовсе.

    Стоит один вызов из суточной квоты 2000. Это дешевле, чем поднимать
    ради замера весь стек сбора.
    """
    # requests, а не urllib: он и так есть в этом окружении (его тянет
    # steam), и им же ходят коллекторы — незачем иметь два разных
    # сетевых поведения для одного и того же API.
    #
    # User-Agent обязателен. По умолчанию клиент представляется как
    # «Python-urllib/3.x», и OpenDota отвечает 403 — тот же запрос через
    # curl проходит. Ошибка выглядела бы как «лента недоступна», то есть
    # уводила бы в сторону сети.
    import requests

    url = "https://api.opendota.com/api/publicMatches"
    headers = {"User-Agent": "manta-gc-probe"}
    # Две попытки и щедрый таймаут: на живой машине ответ не пришёл за 30
    # секунд, хотя коллекторы к тому же API ходят успешно. Лента отдаёт
    # сотню матчей целиком и бывает медленной; одна неудача — не повод
    # объявлять её недоступной.
    last = None
    for attempt in (1, 2):
        try:
            resp = requests.get(url, headers=headers, timeout=60)
            resp.raise_for_status()
            rows = resp.json()
            break
        except Exception as exc:  # noqa: BLE001 — сеть: скажем честно
            last = exc
            print(f"лента OpenDota не ответила (попытка {attempt}/2): {exc}")
    else:
        print(f"публичная лента недоступна ({last})")
        print("  запасной источник у Valve, мимо OpenDota:")
        print("    make gc-probe ARGS='details --from-steam 100'")
        return []

    ids = [int(r["match_id"]) for r in rows if r.get("match_id")]
    print(f"из публичной ленты OpenDota: {len(ids)} матчей (1 вызов квоты)")
    return ids[:limit]


def match_ids_from_steam(limit: int) -> list[int]:
    """Свежие match_id у самой Valve — мимо OpenDota и мимо её квоты.

    Нужен как запасной путь: на живой машине лента OpenDota не ответила,
    а замер от неё зависеть не должен. Valve отдаёт сто матчей за вызов
    и своей квоты в нашем смысле не имеет.

    Ключ берётся из того же ~/manta-train.env, что и всё остальное.
    """
    import requests

    key = os.getenv("STEAM_API_KEY")
    if not key:
        print("нет STEAM_API_KEY в ~/manta-train.env — источник недоступен")
        return []
    url = ("https://api.steampowered.com/IDOTA2Match_570"
           "/GetMatchHistory/v1/")

    # Постранично. За вызов Valve отдаёт сотню, а замеру нужно больше:
    # первый живой прогон обработал ровно сто матчей БЕЗ единого отказа,
    # то есть упёрся не в потолок аккаунта, а в конец списка. Без
    # страниц суточный потолок измерить нечем.
    #
    # Страница задаётся start_at_match_id: следующий запрос начинается с
    # матча ПЕРЕД последним полученным. Номера убывают, поэтому минус.
    ids: list[int] = []
    start_at = None
    while len(ids) < limit:
        params = {"key": key, "matches_requested": 100}
        if start_at is not None:
            params["start_at_match_id"] = start_at
        try:
            resp = requests.get(url, params=params, timeout=60)
            resp.raise_for_status()
            result = (resp.json() or {}).get("result") or {}
        except Exception as exc:  # noqa: BLE001
            print(f"Valve не ответила ({exc})")
            break
        if result.get("status") != 1:
            print(f"Valve вернула status={result.get('status')} "
                  f"{result.get('statusDetail', '')}".strip())
            break
        page = [int(m["match_id"]) for m in (result.get("matches") or [])
                if m.get("match_id")]
        if not page:
            break
        ids.extend(page)
        start_at = min(page) - 1
    print(f"от Valve: {len(ids)} матчей (квота OpenDota не тронута)")
    return ids[:limit]


# -- проверка соли ----------------------------------------------------------------

def salt_is_real(url: str, timeout: float = 20.0) -> bool:
    """Соль верна, если CDN Valve отдаёт по собранному URL первый байт.

    Проверка сквозная и бесплатная: качать 58 МиБ ради подтверждения не
    нужно, достаточно Range-запроса. Ошибка в соли даёт 403/404 — то
    есть отличить «GC ответил» от «GC ответил ПРАВИЛЬНО» можно только
    так, и без этой проверки замер ничего не доказывает.
    """
    import urllib.error
    import urllib.request

    req = urllib.request.Request(url, headers={"Range": "bytes=0-0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status in (200, 206)
    except urllib.error.HTTPError as exc:
        return exc.code in (200, 206)
    except Exception:  # noqa: BLE001 — сеть/таймаут: соль не подтверждена
        return False


# -- что ещё лежит в ответе, кроме соли -------------------------------------------

# Поля игрока, которые НЕСУТ ЗНАЧЕНИЕ РАНГА. Только по ним выносится
# вердикт: достаётся ли ранг из GC.
#
# Список пришлось сузить после живого прогона. Сначала сюда попал и
# `mmr_type`, и на первом же матче он оказался единственным ненулевым —
# отчего замер объявил «ранг из GC ДОСТАЁТСЯ», хотя все настоящие
# ранговые поля были нулями. `mmr_type` это не ранг, а тип матчмейкинга
# (соло/пати): он говорит, КАКОЙ рейтинг считался, и ничего не говорит о
# его величине. Правило на будущее: в вердикт идут поля со ЗНАЧЕНИЕМ, а
# не поля, которые просто лежат рядом по смыслу.
RANK_FIELDS = ("previous_rank", "search_rank")

# Соседние поля: сами по себе ранга не дают, но помогают понять, что
# вообще приехало. Показываются, в вердикте НЕ участвуют.
RANK_CONTEXT_FIELDS = ("rank_change", "rank_tier_updated", "mmr_type")


def rank_report(match) -> dict[str, int]:
    """Сколько игроков матча имеют НЕнулевое значение в ранговых полях.

    Зачем это в замере соли. Сейчас ранг каждого игрока приезжает
    бесплатно вместе с солью из /matches/{id} OpenDota, и на нём стоит
    измерение точности отбора (`_observe` в sources/candidates.py):
    правило берёт матч по двум известным рангам из десяти, и только факт
    показывает, не набираем ли мы мусор. Если соль начнёт приходить из
    GC, этот источник истины исчезнет — если только его не отдаёт сам GC.

    Спрашивать это отдельным замером незачем: ответы уже получены, поля
    уже в них, стоит проверка ноль запросов. А ответ меняет проект: при
    заполненных рангах GC заменяет OpenDota полностью, при пустых —
    придётся гнать выборку матчей через OpenDota ради одной лишь истины.
    """
    out = {name: 0 for name in RANK_FIELDS + RANK_CONTEXT_FIELDS}
    out["average_skill"] = int(getattr(match, "average_skill", 0) or 0)
    for p in getattr(match, "players", []):
        for name in RANK_FIELDS + RANK_CONTEXT_FIELDS:
            if int(getattr(p, name, 0) or 0):
                out[name] += 1
    return out


def rank_is_available(rep: dict[str, int]) -> bool:
    """Достаётся ли ранг. Только по полям со ЗНАЧЕНИЕМ ранга.

    Соседние поля (`mmr_type` и прочие) в решении не участвуют: на живом
    прогоне ненулевым оказался ровно `mmr_type`, и вердикт по «любому
    ненулю» соврал в самую дорогую сторону — объявил, что OpenDota для
    истины по рангам больше не нужна.
    """
    return any(rep.get(name) for name in RANK_FIELDS)


def print_rank_report(match) -> None:
    rep = rank_report(match)
    print("\n  ранг в ответе GC (игроков из 10 с ненулём):")
    for name in RANK_FIELDS:
        print(f"    {name:20} {rep[name]}")
    print("  рядом лежащее (в вердикте НЕ участвует):")
    for name in RANK_CONTEXT_FIELDS:
        print(f"    {name:20} {rep[name]}")
    print(f"    average_skill (на матч) {rep['average_skill']}"
          "   # 0 — не задан, 1/2/3 — normal/high/very high")
    if rank_is_available(rep):
        print("    => ранг из GC ДОСТАЁТСЯ: OpenDota не нужен и ради истины")
    else:
        print("    => РАНГОВ В ОТВЕТЕ НЕТ: истину по рангам придётся брать")
        print("       из OpenDota выборочно, иначе точность отбора ослепнет")


# -- клиент -----------------------------------------------------------------------

def connect():
    """Логин в Steam по refresh-токену и подключение к GC Dota 2.

    Возвращает (steam_client, dota_client).

    Вход идёт ТОКЕНОМ, а не паролем, и это вынужденно. steam==1.4.4
    логинится устаревшим сообщением — пароль открытым полем в
    ClientLogon, — Valve этот путь отключила, и на верный пароль
    приходит InvalidPassword. Нового механизма в библиотеке нет и не
    будет: 1.4.4 — последняя версия на PyPI, в master та же строка.

    Поэтому пароль на токен меняет Node (`make gc-token`), у которого
    библиотека поддерживаемая, а сюда приезжает готовый токен. Всё
    остальное — Game Coordinator, режимы замера, проверка соли на CDN —
    осталось питоновским: переписывать работающий код ради чужой
    сломанной аутентификации незачем.
    """
    try:
        from dota2.client import Dota2Client
        from steam.client import SteamClient
    except ImportError:
        die("нет библиотек steam/dota2 — сначала `make gc-venv`")

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from gc_token import (TokenError, check_expiry,  # noqa: E402
                          login_with_token, read_token)

    try:
        token = read_token()
        print(check_expiry(token))
    except TokenError as exc:
        die(str(exc))

    CREDENTIAL_DIR.mkdir(parents=True, exist_ok=True)
    CREDENTIAL_DIR.chmod(0o700)

    steam = SteamClient()
    steam.set_credential_location(str(CREDENTIAL_DIR))
    dota = Dota2Client(steam)

    # Библиотека печатает «Failed to parse: <номер>» на каждое сообщение
    # GC, которого нет в её протоколах: dota2 не обновлялась с 2022 года,
    # а Valve с тех пор добавила своих. Это НЕ ошибки замера — приходят
    # уведомления, которых мы не просили и которые нам не нужны. Соль
    # лежит в CMsgDOTAMatch, и его библиотека разбирает.
    print("вход по токену …")
    print("(строки «Failed to parse: NNNNN» ниже — шум устаревших")
    print(" протоколов библиотеки, на замер они не влияют)")
    result = login_with_token(steam, token)
    if result != 1:                       # EResult.OK
        hint = ""
        if int(result) == 5:              # InvalidPassword
            hint = ("\nПри входе ТОКЕНОМ это значит, что токен отозван или "
                    "просрочен, а не что пароль неверен.\n"
                    "Обновить: make gc-token")
        die(f"Steam отказал: {result!r}{hint}")
    print("Steam: вошли")

    dota.launch()
    dota.wait_event("ready", timeout=30)
    print("GC: сессия поднята")
    return steam, dota


# -- режимы -----------------------------------------------------------------------

def probe_login() -> int:
    """Главный вопрос режима: пускает ли limited-аккаунт в GC вообще.

    Ограничение «limited» (до 5 долларов пополнения) точно закрывает
    Web API-ключ. Закрывает ли оно GC — нигде не сказано, а из России
    пополнить Steam отдельная история, так что проверить надо ДО того,
    как на это рассчитывать.
    """
    steam, dota = connect()
    print("\nВЕРДИКТ: аккаунт в Game Coordinator пускают.")
    dota.exit()
    steam.logout()
    return 0


def detail_failure_reason(resp) -> str | None:
    """Причина отказа по ответу GC, либо None если это УСПЕХ.

    Здесь была ошибка, стоившая целого прогона. Успехом считалось
    `result == 0`, а на деле поле `result` — это EResult, и успех у него
    ЕДИНИЦА (`EResult.OK == 1`), тогда как ноль означает `Invalid`.
    Замер поэтому записывал каждый нормальный ответ GC в отказы,
    останавливался после трёх подряд и объявлял «аккаунт отдал 0 солей,
    нужно 2000 аккаунтов» — притом что GC отвечал исправно.

    Сама библиотека делает ровно так (dota2/features/match.py:54):

        eresult = EResult(message.result)
        match = message.match if eresult == EResult.OK else None

    Вывод на будущее: числовой код из чужого протокола нельзя
    истолковывать на глаз. Сравнение идёт с ИМЕНОВАННОЙ константой, а
    имя берётся из той же библиотеки, что и ответ.
    """
    from steam.enums import EResult

    if resp is None:
        return "молчание GC"
    result = EResult(resp.result)
    if result != EResult.OK:
        return f"{result!r}"
    return None


def probe_details(match_ids: list[int], delay_s: float, limit: int) -> int:
    """Сколько одиночных запросов деталей отдаёт аккаунт до отказа.

    Библиотека заявляет 100/сутки. Если это правда, GC-бот под 2000
    матчей требует двадцати аккаунтов, и затея меняет масштаб.
    """
    from dota2.utils import replay_url_from_match

    steam, dota = connect()
    ok = failed = verified = attempted = 0
    streak = 0
    started = time.time()
    reasons: dict[str, int] = {}

    print(f"\nзапрашиваю детали по одному, пауза {delay_s} с, "
          f"потолок {limit}\n")
    try:
        for n, match_id in enumerate(match_ids[:limit], start=1):
            attempted = n
            jobid = dota.request_match_details(match_id)
            resp = dota.wait_msg(jobid, timeout=30)
            reason = detail_failure_reason(resp)
            if reason:
                failed += 1
                streak += 1
                reasons[reason] = reasons.get(reason, 0) + 1
            else:
                url = replay_url_from_match(resp.match)
                if url is None:
                    failed += 1
                    streak += 1
                    reasons["нет соли в ответе"] = (
                        reasons.get("нет соли в ответе", 0) + 1)
                else:
                    ok += 1
                    streak = 0
                    # По первому же успешному матчу смотрим, отдаёт ли GC
                    # ранги. Один раз: это свойство протокола, а не матча.
                    if ok == 1:
                        print_rank_report(resp.match)
                    # Первые пять проверяем на CDN: дальше смысла нет,
                    # соль либо работает, либо нет — это свойство пути,
                    # а не отдельного матча.
                    if verified < 5 and salt_is_real(url):
                        verified += 1
            if n % 25 == 0 or streak:
                print(f"  {n:>4}: получено {ok}, отказов {failed}"
                      f"{', подряд ' + str(streak) if streak else ''}")
            if streak >= STOP_AFTER_FAILURES:
                print(f"\n{STOP_AFTER_FAILURES} отказа подряд — стена.")
                break
            time.sleep(delay_s)
    finally:
        dota.exit()
        steam.logout()

    minutes = (time.time() - started) / 60
    print("\n=== ИТОГ: одиночные детали ===")
    print(f"  получено солей:   {ok}")
    print(f"  отказов:          {failed}  {reasons or ''}")
    print(f"  соль проверена на CDN: {verified} из первых 5")
    print(f"  заняло:           {minutes:.1f} мин")
    if ok == 0:
        # Экстраполировать здесь нечего, и делать этого нельзя. Прежняя
        # версия делила 2000 на max(ok, 1) и печатала «нужно ~2000
        # аккаунтов» — число, которое выглядит замером, а на деле
        # означает лишь «ноль в знаменателе». Ноль солей это не потолок
        # аккаунта, а неработающий путь, и разбираться надо с причинами.
        print("\n  ВЕРДИКТ: соли не получено ни одной — считать нечего.")
        print("  Смотри причины отказов выше: это НЕ суточный потолок,")
        print("  а неработающий путь. Дальше — по имени ошибки:")
        print("    AccessDenied / limited     — ограничения аккаунта;")
        print("    Fail, Busy, Timeout        — повторить позже;")
        print("    молчание GC                — эндпоинт, похоже, снят;")
        print("    нет соли в ответе          — матчи слишком свежие или")
        print("                                 их реплеев уже нет.")
    elif streak < STOP_AFTER_FAILURES:
        # Замер кончился НЕ отказом GC, а концом входных данных: сколько
        # match_id дали, столько и обработали. Это не потолок аккаунта, и
        # называть его потолком нельзя.
        #
        # Прежняя версия сравнивала ok с --limit (по умолчанию 200), а
        # список был на 100 — и объявила «аккаунт отдал 100 солей до
        # отказа, нужно ~20 аккаунтов». Отказов при этом было ноль.
        # Третий случай подряд, когда вердикт истолковывал СВОЮ
        # остановку как ответ внешней системы.
        print(f"\n  ПОТОЛОК НЕ ДОСТИГНУТ: отказов ноль, кончились матчи "
              f"({attempted} шт).")
        print("  Сколько аккаунт отдаёт в сутки — пока НЕ измерено.")
        print("  Дальше: make gc-probe ARGS='details --from-steam 500 "
              "--limit 500'")
        print("  Если суточный лимит уже выбран, следующий прогон упрётся")
        print("  в стену сразу — это тоже ответ, и тоже полезный.")
    else:
        print(f"\n  ВЕРДИКТ: аккаунт отдал {ok} солей и упёрся в стену "
              f"({attempted} попыток).")
        print(f"  Под 2000 матчей в сутки нужно ~{-(-2000 // ok)} аккаунтов.")
    return 0


def probe_bulk(matches_requested: int, hero_id: int | None) -> int:
    """Отдаёт ли GC соли ПАЧКОЙ.

    Здесь вся экономика затеи. `CMsgDOTARequestMatchesResponse` несёт
    список `CMsgDOTAMatch`, а в каждом есть `replay_salt`. Если этот путь
    жив и отдаёт по сотне матчей за вызов, то даже 50 вызовов в сутки —
    это пять тысяч солей с ОДНОГО аккаунта, и никакая ферма не нужна.

    Библиотека предупреждает: «часть аргументов не работает, спрашивайте
    Valve». Поиск по истории матчей Valve в своё время сильно урезала, и
    вполне возможно, что путь мёртв. Это и проверяем.
    """
    from dota2.enums import EDOTAGCMsg
    from dota2.utils import replay_url_from_match

    steam, dota = connect()
    try:
        kwargs: dict = {"matches_requested": matches_requested}
        if hero_id:
            kwargs["hero_id"] = hero_id
        print(f"\nмассовый запрос: {kwargs}")
        jobid = dota.send_job(EDOTAGCMsg.EMsgGCRequestMatches, kwargs)
        # Молчание — это ОТВЕТ, и ждать его надо до конца. Без этой
        # строки замер выглядел зависшим: запрос ушёл, GC не отвечает, на
        # экране ничего. Прогон прервали с клавиатуры, так и не узнав,
        # молчит ли GC или просто думает.
        print(f"жду ответа GC до {BULK_TIMEOUT_S} с. Тишина здесь — это не "
              "зависание,\nа вероятный ответ «путь закрыт»: дождись конца.")
        resp = dota.wait_msg(jobid, timeout=BULK_TIMEOUT_S)

        if resp is None:
            print(f"\nGC не ответил за {BULK_TIMEOUT_S} с.")
            print("\nВЕРДИКТ: массовый путь, похоже, закрыт — Valve в своё")
            print("время урезала поиск по истории матчей. Остаётся путь")
            print("одиночных деталей: make gc-probe ARGS='details "
                  "--from-public 100'")
            return 0

        matches = list(getattr(resp, "matches", []))
        with_salt = [m for m in matches if replay_url_from_match(m)]
        print("\n=== ИТОГ: массовый запрос ===")
        print(f"  матчей в ответе:  {len(matches)}")
        print(f"  из них с солью:   {len(with_salt)}")
        print(f"  всего результатов:{getattr(resp, 'total_results', '—')}")
        print(f"  осталось:         {getattr(resp, 'results_remaining', '—')}")

        if with_salt:
            print_rank_report(with_salt[0])
            url = replay_url_from_match(with_salt[0])
            good = salt_is_real(url)
            print(f"  соль первого матча подтверждена CDN: "
                  f"{'да' if good else 'НЕТ'}")
            print(f"\n  ВЕРДИКТ: путь жив, {len(with_salt)} солей за один "
                  f"вызов. При заявленных 50 вызовах в сутки это "
                  f"~{len(with_salt) * 50} солей с одного аккаунта.")
        else:
            print("\n  ВЕРДИКТ: соли в массовом ответе нет — "
                  "остаётся только путь одиночных деталей.")
    finally:
        dota.exit()
        steam.logout()
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("mode", choices=("login", "details", "bulk"))
    ap.add_argument("--limit", type=int, default=200,
                    help="сколько одиночных запросов пробовать (details)")
    ap.add_argument("--delay", type=float, default=DEFAULT_DELAY_S)
    ap.add_argument("--match-ids", help="через запятую, вместо очереди")
    ap.add_argument("--from-public", type=int, metavar="N",
                    help="взять N свежих match_id из публичной ленты "
                         "OpenDota вместо очереди (1 вызов квоты)")
    ap.add_argument("--from-steam", type=int, metavar="N",
                    help="то же, но у самой Valve: мимо OpenDota и её "
                         "квоты (нужен STEAM_API_KEY)")
    ap.add_argument("--matches-requested", type=int, default=100,
                    help="сколько матчей просить в массовом запросе (bulk)")
    ap.add_argument("--hero-id", type=int,
                    help="фильтр массового запроса по герою (bulk)")
    args = ap.parse_args()

    if args.mode == "login":
        return probe_login()
    if args.mode == "bulk":
        return probe_bulk(args.matches_requested, args.hero_id)

    if args.match_ids:
        ids = [int(x) for x in args.match_ids.split(",") if x.strip()]
    elif args.from_public:
        ids = match_ids_from_public(args.from_public)
    elif args.from_steam:
        ids = match_ids_from_steam(args.from_steam)
    else:
        ids = match_ids_from_queue(args.limit)
    if not ids:
        die("нет match_id. Варианты: --from-steam 100 (у Valve, квоту "
            "OpenDota не тратит), --from-public 100 (лента OpenDota, "
            "1 вызов квоты), --match-ids через запятую, либо поднять "
            "Postgres с непустой очередью кандидатов")
    print(f"матчей для замера: {len(ids)}")
    return probe_details(ids, args.delay, args.limit)


if __name__ == "__main__":
    raise SystemExit(main())
