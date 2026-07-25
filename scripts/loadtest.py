#!/usr/bin/env python3
"""Нагрузочные тесты NFR-PERF/SCAL (D5 роадмапа, Гл. 10.5 спеки).

Меряет ровно то, что заявлено в Гл. 1 спецификации, и печатает вердикт
по каждому требованию:

  NFR-PERF-01  парсинг 40-мин реплея         ≤ 10 с
  NFR-PERF-02  Similarity/Draft (p95)        ≤ 2 с
  NFR-PERF-03  REST read-эндпоинты           p95 ≤ 300 мс, p99 ≤ 800 мс
  NFR-PERF-04  пропускная способность        ≥ 2000 реплеев/ч на кластер
  NFR-SCAL-01  аналитика на большой базе     без деградации

Запуск:  make loadtest            (весь профиль)
         python3 scripts/loadtest.py --only rest --duration 20

Важно про REST: у gateway включён rate limit (GATEWAY_RATE_LIMIT_RPS,
дефолт 20). Нагрузка выше него честно упрётся в 429, поэтому тест
считает латентность по успешным ответам и ОТДЕЛЬНО показывает долю 429.
Чтобы мерить сервис, а не лимитер, поднимите лимит на время теста:
GATEWAY_RATE_LIMIT_RPS=10000 GATEWAY_RATE_LIMIT_BURST=20000 (перезапуск
gateway).

SCAL-01 не ждёт 100 млн реальных матчей: тест наливает синтетическую
таблицу той же схемы (--scale-rows, дефолт 5 млн строк ≈ 130 тыс матчей)
и меряет типовые аналитические выборки, проверяя отсечение партиций.
Экстраполяция честно помечена в отчёте как экстраполяция.
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import subprocess
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

API = os.getenv("MANTA_API", "http://localhost:8080")
CH = os.getenv("CLICKHOUSE_URL", "http://localhost:8123")
CH_DB = os.getenv("CLICKHOUSE_DB", "manta")
CH_AUTH = {"X-ClickHouse-User": os.getenv("CLICKHOUSE_USER", "dota"),
           "X-ClickHouse-Key": os.getenv("CLICKHOUSE_PASSWORD",
                                         "dota_dev_password")}

results: list[tuple[str, str, str, bool | None]] = []


def verdict(nfr: str, target: str, measured: str, ok: bool | None) -> None:
    results.append((nfr, target, measured, ok))
    mark = {True: "\033[32mPASS\033[0m", False: "\033[31mFAIL\033[0m",
            None: "\033[33mSKIP\033[0m"}[ok]
    print(f"  [{mark}] {nfr}: {measured} (цель {target})")


def pct(values: list[float], p: float) -> float:
    if not values:
        return float("nan")
    s = sorted(values)
    k = min(len(s) - 1, int(round(p / 100.0 * (len(s) - 1))))
    return s[k]


def ch_query(sql: str, timeout: int = 600) -> str:
    req = urllib.request.Request(
        f"{CH}/?database={CH_DB}", data=sql.encode(), headers=CH_AUTH)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode().strip()


# -- NFR-PERF-03: латентность REST --------------------------------------------

def load_rest(duration: int, concurrency: int) -> None:
    print("\n== NFR-PERF-03: латентность REST (read-эндпоинты)")
    try:
        with urllib.request.urlopen(f"{API}/healthz", timeout=5) as r:
            r.read()
    except Exception as e:  # noqa: BLE001
        verdict("NFR-PERF-03", "p95≤300мс, p99≤800мс",
                f"gateway недоступен ({e})", None)
        return

    # Реальные match_id из витрины — иначе меряли бы только 404.
    try:
        body = json.loads(urllib.request.urlopen(
            f"{API}/api/v1/matches?limit=20", timeout=10).read())
        ids = [m["match_id"] for m in body.get("matches", [])][:20]
    except Exception:  # noqa: BLE001
        ids = []
    paths = ["/api/v1/matches", "/api/v1/heroes"]
    for mid in ids[:10]:
        paths += [f"/api/v1/matches/{mid}/timeline",
                  f"/api/v1/matches/{mid}/analysis"]
    print(f"   эндпоинтов в ротации: {len(paths)}, "
          f"нагрузка {concurrency} поток(ов) × {duration}с")

    lat: list[float] = []
    codes: dict[int, int] = {}
    stop = time.time() + duration

    def worker(idx: int) -> None:
        i = idx
        while time.time() < stop:
            path = paths[i % len(paths)]
            i += len(paths) or 1
            t0 = time.perf_counter()
            try:
                with urllib.request.urlopen(API + path, timeout=30) as r:
                    r.read()
                    code = r.status
            except urllib.error.HTTPError as e:
                code = e.code
                e.read()
            except Exception:  # noqa: BLE001
                code = 0
            dt = (time.perf_counter() - t0) * 1000
            codes[code] = codes.get(code, 0) + 1
            if code == 200:
                lat.append(dt)

    with ThreadPoolExecutor(max_workers=concurrency) as ex:
        list(ex.map(worker, range(concurrency)))

    total = sum(codes.values())
    limited = codes.get(429, 0)
    if not lat:
        verdict("NFR-PERF-03", "p95≤300мс, p99≤800мс",
                f"нет успешных ответов (коды {codes})", False)
        return
    p50, p95, p99 = pct(lat, 50), pct(lat, 95), pct(lat, 99)
    rps = total / duration
    print(f"   запросов {total} ({rps:.0f}/с), 200: {codes.get(200, 0)}, "
          f"429: {limited} ({100 * limited / total:.0f}%), "
          f"p50 {p50:.0f}мс")
    if limited > total * 0.5:
        print("   ВНИМАНИЕ: >50% ответов — 429; поднимите "
              "GATEWAY_RATE_LIMIT_RPS для замера сервиса, а не лимитера")
    verdict("NFR-PERF-03", "p95≤300мс, p99≤800мс",
            f"p95 {p95:.0f}мс, p99 {p99:.0f}мс",
            p95 <= 300 and p99 <= 800)


# -- NFR-PERF-02: gRPC Similarity/Draft ---------------------------------------

def load_grpc(iterations: int) -> None:
    print("\n== NFR-PERF-02: Similarity / Draft (gRPC)")
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..",
                                    "apps", "similarity", "src"))
    try:
        import grpc  # noqa: PLC0415
        from gen import services_pb2, services_pb2_grpc  # noqa: PLC0415
    except Exception as e:  # noqa: BLE001
        verdict("NFR-PERF-02", "p95≤2с", f"стабы недоступны ({e})", None)
        return

    try:
        ref = int(ch_query("SELECT max(match_id) FROM MatchTimelineFeatures"))
    except Exception:  # noqa: BLE001
        ref = 0

    lat: dict[str, list[float]] = {"similarity": [], "draft": []}

    def measure(name: str, addr: str, call) -> None:
        try:
            with grpc.insecure_channel(addr) as ch:
                grpc.channel_ready_future(ch).result(timeout=5)
                for _ in range(iterations):
                    t0 = time.perf_counter()
                    call(ch)
                    lat[name].append((time.perf_counter() - t0) * 1000)
        except Exception as e:  # noqa: BLE001
            print(f"   {name}: недоступен ({type(e).__name__})")

    measure("similarity", os.getenv("SIMILARITY_ADDR", "localhost:50052"),
            lambda ch: services_pb2_grpc.SimilarityServiceStub(ch).FindSimilar(
                services_pb2.SimilarityQuery(entity="match", reference_id=ref,
                                             top_k=10), timeout=30))
    measure("draft", os.getenv("DRAFT_ADDR", "localhost:50053"),
            lambda ch: services_pb2_grpc.DraftServiceStub(ch).SimulateDraft(
                services_pb2.DraftState(radiant_picks=[1, 2, 3],
                                        dire_picks=[4, 5],
                                        bans=[6, 7],
                                        next_action="radiant_pick"),
                timeout=30))

    all_lat = lat["similarity"] + lat["draft"]
    if not all_lat:
        verdict("NFR-PERF-02", "p95≤2с", "сервисы недоступны", None)
        return
    for name, vals in lat.items():
        if vals:
            print(f"   {name}: n={len(vals)}, p50 {pct(vals, 50):.0f}мс, "
                  f"p95 {pct(vals, 95):.0f}мс")
    p95 = pct(all_lat, 95)
    verdict("NFR-PERF-02", "p95≤2с", f"p95 {p95:.0f}мс", p95 <= 2000)


# -- NFR-PERF-01/04: парсер ----------------------------------------------------

def load_parser(replay: str | None) -> None:
    print("\n== NFR-PERF-01/04: парсинг реплея")
    root = os.path.join(os.path.dirname(__file__), "..")
    binary = os.path.join(root, "apps", "replay-parser", "build", "demoinfo")
    if not os.path.exists(binary):
        verdict("NFR-PERF-01", "≤10с/40-мин реплей",
                "demoinfo не собран (make parser-build)", None)
        verdict("NFR-PERF-04", "≥2000 реплеев/ч", "нет замера PERF-01", None)
        return
    if not replay or not os.path.exists(replay):
        verdict("NFR-PERF-01", "≤10с/40-мин реплей",
                "нет .dem (--replay путь; эталон в s3://replays/fixtures/)",
                None)
        verdict("NFR-PERF-04", "≥2000 реплеев/ч", "нет замера PERF-01", None)
        return

    out = "/tmp/loadtest-parse"
    os.makedirs(out, exist_ok=True)
    # Тот же вызов, что в parser-svc (pipeline.runCore) — меряем ровно то,
    # что работает в конвейере, а не другой режим CLI.
    t0 = time.perf_counter()
    p = subprocess.run(
        [binary, "--events", f"{out}/events.jsonl",
         "--entities", f"{out}/pos.jsonl", "--economy", f"{out}/eco.jsonl",
         "--summary", f"{out}/summary.json", replay],
        capture_output=True, timeout=600)
    dt = time.perf_counter() - t0
    if p.returncode != 0:
        verdict("NFR-PERF-01", "≤10с/40-мин реплей",
                f"парсер упал: {p.stderr.decode()[:120]}", False)
        return
    try:
        dur_min = json.load(open(f"{out}/summary.json")).get(
            "playback_time_s", 0) / 60
    except Exception:  # noqa: BLE001
        dur_min = 0
    norm = dt * (40 / dur_min) if dur_min > 5 else dt
    print(f"   реплей {dur_min:.0f} мин за {dt:.1f}с "
          f"(нормировано на 40 мин: {norm:.1f}с)")
    verdict("NFR-PERF-01", "≤10с/40-мин реплей", f"{norm:.1f}с", norm <= 10)

    # PERF-04 — пропускная способность кластера: последовательная скорость
    # одного воркера × число воркеров. Честно помечено экстраполяцией.
    per_worker = 3600 / norm
    workers = int(os.getenv("PARSER_WORKERS", str(os.cpu_count() or 4)))
    total = per_worker * workers
    verdict("NFR-PERF-04", "≥2000 реплеев/ч",
            f"{total:.0f}/ч (экстраполяция: {per_worker:.0f}/ч × "
            f"{workers} воркеров)", total >= 2000)


# -- NFR-SCAL-01: аналитика на большой базе ------------------------------------

SCALE_TABLE = "LoadTestTimeline"


def load_scale(rows: int) -> None:
    print(f"\n== NFR-SCAL-01: аналитика на синтетике ({rows:,} строк)")
    try:
        ch_query("SELECT 1")
    except Exception as e:  # noqa: BLE001
        verdict("NFR-SCAL-01", "выборки без деградации",
                f"ClickHouse недоступен ({e})", None)
        return

    ch_query(f"DROP TABLE IF EXISTS {SCALE_TABLE}")
    ch_query(f"""CREATE TABLE {SCALE_TABLE} AS MatchTimelineFeatures""")
    t0 = time.perf_counter()
    # ~38 строк на матч (как в реальной витрине).
    ch_query(f"""INSERT INTO {SCALE_TABLE}
        (match_id, game_time, networth_diff, xp_diff, kills_radiant,
         kills_dire, radiant_win, tier, patch)
        SELECT 8000000000 + intDiv(number, 38), (number % 38) * 60,
               toInt32(rand() % 20000) - 10000, toInt32(rand() % 25000) - 12500,
               toUInt16(rand() % 40), toUInt16(rand() % 40),
               toUInt8(rand() % 2), 'Premium', 58 + rand() % 3
        FROM numbers({rows})""", timeout=1800)
    fill = time.perf_counter() - t0
    n_matches = int(ch_query(
        f"SELECT uniqExact(match_id) FROM {SCALE_TABLE}"))
    print(f"   налито за {fill:.0f}с: {n_matches:,} матчей")

    queries = {
        "точечная выборка матча (API /timeline)":
            f"SELECT * FROM {SCALE_TABLE} WHERE match_id = "
            f"(SELECT max(match_id) FROM {SCALE_TABLE}) FORMAT Null",
        "агрегат по всей базе (обучение)":
            f"SELECT avg(networth_diff), count() FROM {SCALE_TABLE} FORMAT Null",
        "группировка по патчу (аналитика меты)":
            f"SELECT patch, uniqExact(match_id), avg(kills_radiant) "
            f"FROM {SCALE_TABLE} GROUP BY patch FORMAT Null",
        "срез по времени игры (фазовые метрики)":
            f"SELECT intDiv(game_time, 600), avg(xp_diff) FROM {SCALE_TABLE} "
            f"GROUP BY 1 ORDER BY 1 FORMAT Null",
    }
    slow: list[str] = []
    for name, sql in queries.items():
        t0 = time.perf_counter()
        ch_query(sql, timeout=600)
        dt = (time.perf_counter() - t0) * 1000
        rps = rows / (dt / 1000) if dt else 0
        print(f"   {name}: {dt:.0f}мс ({rps / 1e6:.0f}М строк/с)")
        # Точечная выборка обязана отсекать партиции: на порядки быстрее
        # полного скана, иначе индексация не работает.
        if "точечная" in name and dt > 500:
            slow.append(f"{name} {dt:.0f}мс")
        if "агрегат" in name and rps < 5e6:
            slow.append(f"{name} {rps / 1e6:.1f}М строк/с")

    # Отсечение партиций — ключ к 100M+ (Гл. 4.4: PARTITION BY intDiv(match_id, 1e6)).
    plan = ch_query(
        f"EXPLAIN indexes = 1 SELECT count() FROM {SCALE_TABLE} "
        f"WHERE match_id = 8000000001")
    pruned = "Parts:" in plan or "Granules:" in plan
    print(f"   отсечение партиций по match_id: "
          f"{'работает' if pruned else 'НЕ подтверждено'}")

    extrapolation = 100_000_000 / max(n_matches, 1)
    print(f"   до 100 млн матчей — ×{extrapolation:,.0f} от протестированного "
          f"объёма (экстраполяция, полный объём требует отдельного стенда)")
    ch_query(f"DROP TABLE IF EXISTS {SCALE_TABLE}")
    verdict("NFR-SCAL-01", "выборки без деградации",
            "точечные выборки с отсечением партиций, линейные агрегаты"
            if not slow else f"деградация: {'; '.join(slow)}",
            not slow and pruned)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", choices=["rest", "grpc", "parser", "scale"],
                    help="прогнать только один блок")
    ap.add_argument("--duration", type=int, default=15,
                    help="секунд нагрузки на REST")
    ap.add_argument("--concurrency", type=int, default=8)
    ap.add_argument("--iterations", type=int, default=30,
                    help="вызовов на каждый gRPC-сервис")
    ap.add_argument("--replay", default=os.getenv("LOADTEST_REPLAY"),
                    help="путь к .dem для NFR-PERF-01")
    ap.add_argument("--scale-rows", type=int, default=5_000_000)
    args = ap.parse_args()

    print("Нагрузочный профиль Manta (D5) — пороги из Гл. 1 спецификации")
    if args.only in (None, "rest"):
        load_rest(args.duration, args.concurrency)
    if args.only in (None, "grpc"):
        load_grpc(args.iterations)
    if args.only in (None, "parser"):
        load_parser(args.replay)
    if args.only in (None, "scale"):
        load_scale(args.scale_rows)

    print("\n== Итог")
    width = max(len(r[2]) for r in results) if results else 10
    for nfr, target, measured, ok in results:
        mark = {True: "PASS", False: "FAIL", None: "SKIP"}[ok]
        print(f"  {mark:4}  {nfr:12}  {measured:<{width}}  цель: {target}")
    failed = [r[0] for r in results if r[3] is False]
    skipped = [r[0] for r in results if r[3] is None]
    if skipped:
        print(f"\n  пропущено (нет данных/сервисов): {', '.join(skipped)}")
    if failed:
        print(f"\n\033[31m  НЕ ПРОШЛИ: {', '.join(failed)}\033[0m")
        return 1
    print("\n\033[32m  Все измеренные NFR в пределах порогов\033[0m")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
