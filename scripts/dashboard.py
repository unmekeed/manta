#!/usr/bin/env python3
"""Локальный дашборд наблюдаемости Manta без Docker/Grafana (Гл. 11.2).

Один процесс на стандартной библиотеке: серверно опрашивает /metrics всех
сервисов конвейера (обходит CORS браузера), парсит формат Prometheus, держит
короткую историю в памяти для спарклайнов и отдаёт авто-обновляющуюся
страницу. Ставить нечего — только python3.

    python3 scripts/dashboard.py            # http://localhost:9107
    DASHBOARD_PORT=9200 python3 scripts/dashboard.py

Порты сервисов берутся из окружения (METRICS_PORT-схема dev-recover), иначе —
дефолты 9101..9106. ClickHouse-витрина опрашивается напрямую (число матчей —
источник истины, а не только gauge авто-обучения).
"""
from __future__ import annotations

import json
import os
import time
import urllib.request
from collections import defaultdict, deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# -- Конфигурация целей --------------------------------------------------------
# port из окружения, если сервис поднят с нестандартным METRICS_PORT.
SERVICES = [
    ("parser-svc",        int(os.getenv("PARSER_METRICS_PORT",    "9101"))),
    ("feature-extractor", int(os.getenv("EXTRACTOR_METRICS_PORT", "9102"))),
    ("report-generator",  int(os.getenv("REPORT_METRICS_PORT",    "9103"))),
    ("ml-service",        int(os.getenv("ML_METRICS_PORT",        "9104"))),
    ("data-collector",    int(os.getenv("COLLECTOR_METRICS_PORT", "9105"))),
    ("ml-autotrain",      int(os.getenv("AUTOTRAIN_METRICS_PORT", "9106"))),
]
CH_URL = os.getenv("CLICKHOUSE_URL", "http://localhost:8123")
CH_DB = os.getenv("CLICKHOUSE_DB", "manta")
CH_USER = os.getenv("CLICKHOUSE_USER", "dota")
CH_PASSWORD = os.getenv("CLICKHOUSE_PASSWORD", "dota_dev_password")

HISTORY_MAX = 120          # точек в спарклайне (при 5s опросе — 10 минут)
SCRAPE_TTL_S = 4.0         # не чаще раза в TTL реально ходим к сервисам

_history: dict[str, deque] = defaultdict(lambda: deque(maxlen=HISTORY_MAX))
_cache: dict = {"ts": 0.0, "payload": None}


# -- Разбор формата Prometheus -------------------------------------------------
def _parse_prom(text: str) -> dict[tuple[str, tuple], float]:
    """Текст /metrics → {(name, (('label','val'),...)): value}."""
    out: dict[tuple[str, tuple], float] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            left, val = line.rsplit(" ", 1)
            value = float(val)
        except ValueError:
            continue
        if "{" in left:
            name, rest = left.split("{", 1)
            labels = rest.rstrip("}")
            pairs = []
            for kv in labels.split(","):
                if "=" in kv:
                    k, v = kv.split("=", 1)
                    pairs.append((k.strip(), v.strip().strip('"')))
            out[(name.strip(), tuple(sorted(pairs)))] = value
        else:
            out[(left.strip(), ())] = value
    return out


def _sum(metrics: dict, name: str) -> float:
    """Сумма метрики по всем наборам меток (счётчики с лейблами)."""
    return sum(v for (n, _), v in metrics.items() if n == name)


def _pick(metrics: dict, name: str, **labels) -> float | None:
    key = (name, tuple(sorted(labels.items())))
    return metrics.get(key)


def _scrape(port: int) -> dict | None:
    try:
        with urllib.request.urlopen(f"http://localhost:{port}/metrics",
                                    timeout=2) as r:
            return _parse_prom(r.read().decode("utf-8", "replace"))
    except Exception:
        return None


def _clickhouse_matches() -> int | None:
    q = (f"SELECT count(DISTINCT match_id) FROM {CH_DB}."
         "MatchTimelineFeatures FINAL")
    try:
        req = urllib.request.Request(
            f"{CH_URL}/?database={CH_DB}", data=q.encode(),
            headers={"X-ClickHouse-User": CH_USER,
                     "X-ClickHouse-Key": CH_PASSWORD})
        with urllib.request.urlopen(req, timeout=5) as r:
            return int(r.read().decode().strip())
    except Exception:
        return None


def _record(key: str, value):
    if value is not None:
        _history[key].append(round(float(value), 6))


def collect() -> dict:
    """Опросить все цели; вернуть снапшот + историю для спарклайнов."""
    now = time.time()
    if _cache["payload"] and now - _cache["ts"] < SCRAPE_TTL_S:
        return _cache["payload"]

    scraped = {name: _scrape(port) for name, port in SERVICES}
    services = [{"name": name, "port": port, "up": scraped[name] is not None}
                for name, port in SERVICES]

    def m(name):  # метрики сервиса или пустой dict
        return scraped.get(name) or {}

    ext, rep = m("feature-extractor"), m("report-generator")
    par, mls, auto = m("parser-svc"), m("ml-service"), m("ml-autotrain")

    ch_matches = _clickhouse_matches()

    # KPI-плитки: (ключ, значение, «меньше-лучше»)
    tiles = {
        "dataset":     ch_matches if ch_matches is not None
                       else _pick(auto, "training_dataset_matches"),
        "prod":        _pick(auto, "training_production_matches"),
        "brier_bm":    _pick(auto, "wp_brier_benchmark_pro"),
        "brier_valid": _pick(auto, "wp_brier_valid"),
        "promoted":    _pick(auto, "retrains_total", outcome="promoted"),
        "rejected":    _pick(auto, "retrains_total", outcome="rejected"),
        "psi_max":     _pick(auto, "wp_psi_max"),
        "collected":   _sum(m("data-collector"), "matches_collected_total"),
        "parsed":      _sum(par, "replays_parsed_total"),
        "dlq":         _sum(par, "replays_dlq_total"),
        "features":    _sum(ext, "features_calculated_total"),
        "feat_failed": _sum(ext, "features_failed_total"),
        "reports":     _sum(rep, "reports_generated_total"),
        "predictions": _sum(mls, "ml_predictions_total"),
    }

    for k, v in tiles.items():
        _record(k, v)

    payload = {
        "ts": now,
        "services": services,
        "tiles": tiles,
        "history": {k: list(v) for k, v in _history.items()},
    }
    _cache.update(ts=now, payload=payload)
    return payload


# -- HTTP ----------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # тихий лог
        pass

    def do_GET(self):
        if self.path.startswith("/api/metrics"):
            body = json.dumps(collect()).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path in ("/", "/index.html"):
            body = PAGE.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()


PAGE = r"""<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Manta · телеметрия</title>
<style>
  :root {
    color-scheme: dark;
    --surface-0: #131312; --surface-1: #1a1a19; --surface-2: #232322;
    --border: #34332f;
    --text-1: #ffffff; --text-2: #c3c2b7; --text-3: #85847a;
    --accent: #3987e5;
    --good: #0ca30c; --warning: #fab219; --critical: #d03b3b;
  }
  @media (prefers-color-scheme: light) {
    :root:where(:not([data-theme="dark"])) {
      color-scheme: light;
      --surface-0: #f4f3f0; --surface-1: #fcfcfb; --surface-2: #ffffff;
      --border: #e2e1db;
      --text-1: #0b0b0b; --text-2: #52514e; --text-3: #85847a;
      --accent: #2a78d6;
    }
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; background: var(--surface-0); color: var(--text-1);
    font: 14px/1.5 ui-sans-serif, system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
    -webkit-font-smoothing: antialiased;
  }
  header {
    display: flex; align-items: baseline; gap: 16px; flex-wrap: wrap;
    padding: 20px 24px; border-bottom: 1px solid var(--border);
  }
  header h1 { margin: 0; font-size: 18px; font-weight: 650; letter-spacing: -0.01em; }
  header .sub { color: var(--text-3); font-size: 13px; font-variant-numeric: tabular-nums; }
  header .spacer { flex: 1; }
  main { padding: 20px 24px; max-width: 1120px; margin: 0 auto; }
  h2 { font-size: 12px; font-weight: 600; text-transform: uppercase;
       letter-spacing: 0.06em; color: var(--text-3); margin: 26px 0 12px; }

  /* Статус-пиллы сервисов */
  .pills { display: flex; flex-wrap: wrap; gap: 8px; }
  .pill {
    display: inline-flex; align-items: center; gap: 7px;
    padding: 6px 11px; border-radius: 999px; border: 1px solid var(--border);
    background: var(--surface-1); font-size: 13px; font-weight: 500;
  }
  .pill .dot { width: 8px; height: 8px; border-radius: 50%; flex: none; }
  .pill .port { color: var(--text-3); font-variant-numeric: tabular-nums; font-size: 12px; }
  .up   { background: var(--good); }
  .down { background: var(--critical); }
  .pill.is-down { color: var(--text-3); }

  /* KPI-плитки */
  .grid { display: grid; gap: 12px;
          grid-template-columns: repeat(auto-fill, minmax(210px, 1fr)); }
  .tile {
    background: var(--surface-1); border: 1px solid var(--border);
    border-radius: 12px; padding: 14px 15px 12px; position: relative; overflow: hidden;
  }
  .tile .label { color: var(--text-2); font-size: 12.5px; font-weight: 500; }
  .tile .value {
    font-size: 27px; font-weight: 650; letter-spacing: -0.02em;
    margin-top: 4px; font-variant-numeric: tabular-nums;
  }
  .tile .value.dim { color: var(--text-3); font-weight: 500; font-size: 20px; }
  .tile .unit { font-size: 13px; color: var(--text-3); font-weight: 500; margin-left: 3px; }
  .tile svg.spark { display: block; width: 100%; height: 34px; margin-top: 8px; }
  .tile.alert { border-color: color-mix(in oklab, var(--critical) 55%, var(--border)); }
  .tile .foot { font-size: 11.5px; color: var(--text-3); margin-top: 6px;
                font-variant-numeric: tabular-nums; }
  .tile .foot.warn { color: var(--warning); }
  .tile .foot.bad  { color: var(--critical); }
  .disconnected { opacity: 0.55; }
</style>
</head>
<body>
<header>
  <h1>Manta · телеметрия</h1>
  <span class="sub" id="clock">—</span>
  <div class="spacer"></div>
  <span class="sub" id="conn">подключение…</span>
</header>
<main>
  <h2>Сервисы конвейера</h2>
  <div class="pills" id="pills"></div>

  <h2>Обучение Win Probability</h2>
  <div class="grid" id="grid-ml"></div>

  <h2>Поток данных</h2>
  <div class="grid" id="grid-flow"></div>
</main>

<script>
const ACCENT = getComputedStyle(document.documentElement).getPropertyValue('--accent').trim();
const nfmt = (v, d=0) => v == null ? "—" :
  Number(v).toLocaleString("ru-RU", {minimumFractionDigits: d, maximumFractionDigits: d});

// Спарклайн: тонкая линия (2px), маркер на последней точке. Одна серия — без легенды.
function spark(hist, opts={}) {
  const w = 190, h = 34, pad = 3;
  if (!hist || hist.length < 2) return "";
  const xs = hist, n = xs.length;
  let lo = Math.min(...xs), hi = Math.max(...xs);
  if (opts.floorZero && lo > 0) lo = 0;
  if (hi === lo) hi = lo + 1;
  const X = i => pad + (w - 2*pad) * i / (n - 1);
  const Y = v => (h - pad) - (h - 2*pad) * (v - lo) / (hi - lo);
  let d = "";
  xs.forEach((v, i) => { d += (i ? "L" : "M") + X(i).toFixed(1) + " " + Y(v).toFixed(1); });
  const lx = X(n-1).toFixed(1), ly = Y(xs[n-1]).toFixed(1);
  return `<svg class="spark" viewBox="0 0 ${w} ${h}" preserveAspectRatio="none">
    <path d="${d}" fill="none" stroke="${ACCENT}" stroke-width="2"
          stroke-linejoin="round" stroke-linecap="round"/>
    <circle cx="${lx}" cy="${ly}" r="2.6" fill="${ACCENT}"/>
  </svg>`;
}

function tile(t) {
  const val = t.value != null
    ? `<div class="value">${t.value}<span class="unit">${t.unit||""}</span></div>`
    : `<div class="value dim">—</div>`;
  const foot = t.foot ? `<div class="foot ${t.footClass||""}">${t.foot}</div>` : "";
  return `<div class="tile ${t.alert ? "alert" : ""}">
    <div class="label">${t.label}</div>${val}
    ${spark(t.hist, t.sparkOpts)}${foot}</div>`;
}

async function refresh() {
  let data;
  try {
    data = await (await fetch("/api/metrics", {cache: "no-store"})).json();
    document.getElementById("conn").textContent = "обновлено";
    document.body.classList.remove("disconnected");
  } catch (e) {
    document.getElementById("conn").textContent = "нет связи с дашбордом";
    document.body.classList.add("disconnected");
    return;
  }
  const T = data.tiles, H = data.history;
  document.getElementById("clock").textContent =
    new Date(data.ts * 1000).toLocaleTimeString("ru-RU");

  // Статус-пиллы
  document.getElementById("pills").innerHTML = data.services.map(s =>
    `<span class="pill ${s.up ? "" : "is-down"}">
      <span class="dot ${s.up ? "up" : "down"}"></span>${s.name}
      <span class="port">:${s.port}</span></span>`).join("");

  // Плитки обучения
  const brierFoot = v => v == null ? "" :
    (v <= 0.18 ? {t:`цель ≤ 0.18 ✓`, c:""} : {t:`цель ≤ 0.18`, c:"warn"});
  const bbm = brierFoot(T.brier_bm), bva = brierFoot(T.brier_valid);
  document.getElementById("grid-ml").innerHTML = [
    tile({label:"Матчей в витрине", value:nfmt(T.dataset), hist:H.dataset,
          sparkOpts:{floorZero:true}}),
    tile({label:"Матчей в production-модели", value:nfmt(T.prod), hist:H.prod}),
    tile({label:"Brier на про-эталоне", value:T.brier_bm!=null?nfmt(T.brier_bm,4):null,
          hist:H.brier_bm, foot:bbm.t, footClass:bbm.c}),
    tile({label:"Brier на валидации", value:T.brier_valid!=null?nfmt(T.brier_valid,4):null,
          hist:H.brier_valid, foot:bva.t, footClass:bva.c}),
    tile({label:"Продвинуто версий", value:nfmt(T.promoted), hist:H.promoted,
          sparkOpts:{floorZero:true},
          foot: T.rejected ? `отклонено гейтом: ${nfmt(T.rejected)}` : ""}),
    tile({label:"PSI (дрейф фич)", value:T.psi_max!=null?nfmt(T.psi_max,3):null,
          hist:H.psi_max, sparkOpts:{floorZero:true}, alert:T.psi_max>=0.2,
          foot: T.psi_max!=null ? (T.psi_max>=0.2 ? "значимый дрейф — переобучение"
                : "порог 0.2") : "нужна production с референсом"}),
  ].join("");

  // Плитки потока данных
  const dlqAlert = T.dlq > 0;
  document.getElementById("grid-flow").innerHTML = [
    tile({label:"Скачано матчей", value:nfmt(T.collected), hist:H.collected,
          sparkOpts:{floorZero:true}}),
    tile({label:"Распарсено реплеев", value:nfmt(T.parsed), hist:H.parsed,
          sparkOpts:{floorZero:true},
          foot: dlqAlert ? `в DLQ: ${nfmt(T.dlq)}` : "", footClass:"bad"}),
    tile({label:"Посчитано фич", value:nfmt(T.features), hist:H.features,
          sparkOpts:{floorZero:true},
          foot: T.feat_failed ? `сбоев: ${nfmt(T.feat_failed)}` : "", footClass:"warn"}),
    tile({label:"Сгенерировано отчётов", value:nfmt(T.reports), hist:H.reports,
          sparkOpts:{floorZero:true}}),
    tile({label:"Предсказаний (gRPC)", value:nfmt(T.predictions), hist:H.predictions,
          sparkOpts:{floorZero:true}}),
    tile({label:"Реплеев в DLQ", value:nfmt(T.dlq), hist:H.dlq, alert:dlqAlert}),
  ].join("");
}

refresh();
setInterval(refresh, 5000);
</script>
</body>
</html>
"""


def main() -> int:
    port = int(os.getenv("DASHBOARD_PORT", "9107"))
    srv = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print(f"Manta dashboard → http://localhost:{port}  (Ctrl+C для выхода)")
    print(f"опрашивает: {', '.join(f'{n}:{p}' for n, p in SERVICES)}")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nостановлен")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
