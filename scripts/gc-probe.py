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

БЕЗОПАСНОСТЬ. Логин и пароль читаются ТОЛЬКО из окружения и не попадают
ни в лог, ни в вывод. Кладутся в ~/manta-train.env, который вне git:

    STEAM_BOT_LOGIN=...
    STEAM_BOT_PASSWORD=...

Первый запуск интерактивный: Steam пришлёт код на почту, скрипт его
спросит. Дальше сессия переиспользуется из ~/.manta-gc/ и вопросов
больше не будет.

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
        print(f"очередь недоступна ({exc}); нужен --match-ids")
        return []


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


# -- клиент -----------------------------------------------------------------------

def connect():
    """Логин в Steam и подключение к GC Dota 2.

    Возвращает (steam_client, dota_client). Первый запуск спросит код
    Steam Guard; дальше сессия берётся из CREDENTIAL_DIR.
    """
    try:
        from dota2.client import Dota2Client
        from steam.client import SteamClient
    except ImportError:
        die("нет библиотек steam/dota2 — сначала `make gc-venv`")

    login = os.getenv("STEAM_BOT_LOGIN")
    password = os.getenv("STEAM_BOT_PASSWORD")
    if not login or not password:
        die("нет STEAM_BOT_LOGIN / STEAM_BOT_PASSWORD в ~/manta-train.env")

    CREDENTIAL_DIR.mkdir(parents=True, exist_ok=True)
    CREDENTIAL_DIR.chmod(0o700)

    steam = SteamClient()
    steam.set_credential_location(str(CREDENTIAL_DIR))
    dota = Dota2Client(steam)

    print(f"логин {login[:2]}*** …")     # имя не печатаем целиком
    result = steam.cli_login(username=login, password=password)
    if result != 1:                       # EResult.OK
        die(f"Steam отказал: {result!r}")
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


def probe_details(match_ids: list[int], delay_s: float, limit: int) -> int:
    """Сколько одиночных запросов деталей отдаёт аккаунт до отказа.

    Библиотека заявляет 100/сутки. Если это правда, GC-бот под 2000
    матчей требует двадцати аккаунтов, и затея меняет масштаб.
    """
    from dota2.utils import replay_url_from_match

    steam, dota = connect()
    ok = failed = verified = 0
    streak = 0
    started = time.time()
    reasons: dict[str, int] = {}

    print(f"\nзапрашиваю детали по одному, пауза {delay_s} с, "
          f"потолок {limit}\n")
    try:
        for n, match_id in enumerate(match_ids[:limit], start=1):
            jobid = dota.request_match_details(match_id)
            resp = dota.wait_msg(jobid, timeout=30)
            if resp is None:
                failed += 1
                streak += 1
                reasons["молчание GC"] = reasons.get("молчание GC", 0) + 1
            elif resp.result != 0:
                failed += 1
                streak += 1
                key = f"eresult={resp.result}"
                reasons[key] = reasons.get(key, 0) + 1
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
    if ok >= limit:
        print("\n  потолок НЕ достигнут — повторить с большим --limit")
    else:
        print(f"\n  ВЕРДИКТ: аккаунт отдал {ok} солей до отказа.")
        print(f"  Под 2000 матчей в сутки нужно ~{max(1, -(-2000 // max(ok, 1)))}"
              f" аккаунтов.")
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
        resp = dota.wait_msg(jobid, timeout=60)

        if resp is None:
            print("\nВЕРДИКТ: GC промолчал — путь, похоже, закрыт.")
            return 0

        matches = list(getattr(resp, "matches", []))
        with_salt = [m for m in matches if replay_url_from_match(m)]
        print("\n=== ИТОГ: массовый запрос ===")
        print(f"  матчей в ответе:  {len(matches)}")
        print(f"  из них с солью:   {len(with_salt)}")
        print(f"  всего результатов:{getattr(resp, 'total_results', '—')}")
        print(f"  осталось:         {getattr(resp, 'results_remaining', '—')}")

        if with_salt:
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
    ap.add_argument("--matches-requested", type=int, default=100,
                    help="сколько матчей просить в массовом запросе (bulk)")
    ap.add_argument("--hero-id", type=int,
                    help="фильтр массового запроса по герою (bulk)")
    args = ap.parse_args()

    if args.mode == "login":
        return probe_login()
    if args.mode == "bulk":
        return probe_bulk(args.matches_requested, args.hero_id)

    ids = ([int(x) for x in args.match_ids.split(",") if x.strip()]
           if args.match_ids else match_ids_from_queue(args.limit))
    if not ids:
        die("нет match_id: очередь пуста и --match-ids не задан")
    print(f"матчей для замера: {len(ids)}")
    return probe_details(ids, args.delay, args.limit)


if __name__ == "__main__":
    raise SystemExit(main())
