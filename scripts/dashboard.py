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

import ipaddress
import json
import os
import re
import shlex
import subprocess
import threading
import time
import urllib.request
from collections import defaultdict, deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

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



# -- Панель управления (спринт 74) ---------------------------------------------
#
# Дашборд живёт на хосте и потому МОЖЕТ запускать процессы конвейера — это
# единственное место, где такая кнопка вообще реализуема без отдельного
# демона. Отсюда же весь риск: страница, умеющая выполнять команды, — это
# удалённое выполнение кода, и защищать её надо соответственно.
#
# Три меры, каждая закрывает свой вектор:
#
# 1. БИНД НА LOOPBACK (DASHBOARD_BIND, дефолт 127.0.0.1). Раньше сервер
#    слушал 0.0.0.0; пока он только показывал метрики, это было терпимо, но
#    с кнопкой «стоп» любой в локальной сети остановил бы сбор. Если бинд
#    всё же не петлевой, действия ОТКЛЮЧАЮТСЯ — панель остаётся смотровой,
#    пока владелец явно не выставит DASHBOARD_ALLOW_REMOTE_ACTIONS=1.
#
# 2. ЗАГОЛОВОК X-Manta-Action. Без него любая посещённая владельцем
#    страница могла бы отправить форму POST на localhost:9107 и остановить
#    коллекторы (классический CSRF: браузер сам приложит запрос к
#    локальному порту). Нестандартный заголовок заставляет браузер сделать
#    CORS-preflight, который мы не разрешаем, — кросс-доменный запрос
#    просто не уходит.
#
# 3. БЕЛЫЙ СПИСОК ДЕЙСТВИЙ. Никакого поля «введите команду»: клиент
#    присылает только ключ из ACTIONS, аргументы зашиты в коде.
ROOT = Path(__file__).resolve().parent.parent
ACTION_HEADER = "X-Manta-Action"

ACTIONS = {
    "recover":  {"label": "Поднять всё", "argv": ["make", "recover"],
                 "danger": False,
                 "hint": "идемпотентно: живые процессы не трогает"},
    "doctor":   {"label": "Проверить (doctor)", "argv": ["make", "doctor"],
                 "danger": False,
                 "hint": "health-check по данным, ничего не меняет"},
    "stop":     {"label": "Остановить сервисы", "argv": ["make", "stop"],
                 "danger": True,
                 "hint": "хостовые процессы; контейнеры и данные не трогает"},
    "train":    {"label": "Переобучить WP", "argv": ["make", "ml-train"],
                 "danger": False,
                 "hint": "обучение + гейт; продвижение решает гейт"},
    "ablation": {"label": "Ablation фич", "argv": ["make", "ml-ablation"],
                 "danger": False,
                 "hint": "переобучение без каждой группы фич, долго"},
    "status":   {"label": "Статус обучения", "argv": ["make", "ml-status"],
                 "danger": False, "hint": "версия в проде, разрыв датасета"},
}

LOG_MAX = 4000        # строк вывода в памяти на задание

# doctor и recover раскрашивают вывод; в <pre> escape-последовательности
# превращаются в мусор вида "[31mFAIL[0m". Цвет тут не нужен — статус
# задания панель показывает своими средствами.
ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


class Job:
    """Одно фоновое действие. Слот РОВНО ОДИН: одновременный recover и stop
    гарантированно закончились бы гонкой за одни и те же процессы."""

    def __init__(self):
        self._lock = threading.Lock()
        self.name = ""
        self.started = 0.0
        self.finished = 0.0
        self.code = None
        self.lines: deque[str] = deque(maxlen=LOG_MAX)
        self._proc = None

    def running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def start(self, name: str) -> tuple[bool, str]:
        with self._lock:
            if self.running():
                return False, f"уже выполняется: {self.name}"
            spec = ACTIONS.get(name)
            if spec is None:
                return False, "неизвестное действие"
            self.name, self.started, self.finished, self.code = (
                name, time.time(), 0.0, None)
            self.lines.clear()
            self.lines.append(f"$ {shlex.join(spec['argv'])}")
            # Дашборд не должен умереть вместе с действием и наоборот:
            # отдельная группа процессов, вывод — в трубу.
            self._proc = subprocess.Popen(
                spec["argv"], cwd=str(ROOT), stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, text=True, bufsize=1,
                start_new_session=True)
            threading.Thread(target=self._pump, daemon=True).start()
            return True, "запущено"

    def _pump(self) -> None:
        proc = self._proc
        try:
            for line in proc.stdout:
                self.lines.append(ANSI_RE.sub("", line.rstrip("\n")))
        except Exception as e:  # noqa: BLE001 — чтение лога не критично
            self.lines.append(f"[ошибка чтения вывода: {e}]")
        finally:
            self.code = proc.wait()
            self.finished = time.time()
            self.lines.append(f"[завершено, код {self.code}]")

    def snapshot(self) -> dict:
        return {
            "name": self.name,
            "label": ACTIONS.get(self.name, {}).get("label", self.name),
            "running": self.running(),
            "code": self.code,
            "started": self.started,
            "elapsed": round((self.finished or time.time()) - self.started, 1)
                       if self.started else 0,
            "lines": list(self.lines),
        }


JOB = Job()


def _is_loopback(host: str) -> bool:
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return host in ("localhost",)


def actions_enabled() -> bool:
    """Действия доступны только когда панель не торчит в сеть."""
    bind = os.getenv("DASHBOARD_BIND", "127.0.0.1")
    if _is_loopback(bind):
        return True
    return os.getenv("DASHBOARD_ALLOW_REMOTE_ACTIONS", "") == "1"


# -- HTTP ----------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # тихий лог
        pass

    def _json(self, code: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        if not self.path.startswith("/api/action/"):
            self.send_response(404)
            self.end_headers()
            return
        # CSRF: браузер не отправит нестандартный заголовок кросс-доменно
        # без preflight, а preflight мы не разрешаем. Без этой проверки
        # любая открытая владельцем страница могла бы остановить сбор.
        if self.headers.get(ACTION_HEADER) != "1":
            self._json(403, {"error": f"нужен заголовок {ACTION_HEADER}: 1"})
            return
        if not actions_enabled():
            self._json(403, {"error": "панель слушает не loopback — действия "
                                      "выключены (DASHBOARD_ALLOW_REMOTE_ACTIONS=1 "
                                      "включает осознанно)"})
            return
        name = self.path.rsplit("/", 1)[-1]
        ok, msg = JOB.start(name)
        self._json(200 if ok else 409, {"ok": ok, "message": msg})

    def do_GET(self):
        if self.path.startswith("/api/job"):
            self._json(200, {"actions_enabled": actions_enabled(),
                             "job": JOB.snapshot()})
        elif self.path.startswith("/api/metrics"):
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
  .panel { margin: 18px 0; padding: 14px; background: var(--surface-1);
           border: 1px solid var(--border); border-radius: 10px; }
  .panel h2 { margin: 0 0 10px; font-size: 15px; color: var(--text-2);
              font-weight: 600; }
  .controls { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; }
  button.act { background: var(--surface-2); color: var(--text-1);
               border: 1px solid var(--border); border-radius: 7px;
               padding: 7px 13px; font-size: 13px; cursor: pointer; }
  button.act:hover:not(:disabled) { border-color: var(--accent); }
  button.act:disabled { opacity: .45; cursor: not-allowed; }
  button.act.danger { border-color: #5a3230; color: #ffb4b0; }
  .note { color: var(--warning); font-size: 12px; }
  .job-head { margin-top: 12px; font-size: 13px; color: var(--text-2); }
  .job-head.running { color: var(--warning); }
  .job-head.ok { color: var(--good); }
  .job-head.fail { color: var(--critical); }
  .job-log { margin: 8px 0 0; max-height: 260px; overflow: auto;
             background: var(--surface-0); border: 1px solid var(--border);
             border-radius: 7px; padding: 10px; font-size: 12px;
             line-height: 1.45; white-space: pre-wrap; color: var(--text-2); }
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

<section class="panel">
  <h2>Управление</h2>
  <div id="controls" class="controls"></div>
  <div id="job-head" class="job-head">действий пока не было</div>
  <pre id="job-log" class="job-log"></pre>
</section>
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

// -- Панель управления --------------------------------------------------------
const ACTIONS = [
  {key:"recover",  label:"Поднять всё",        hint:"идемпотентно"},
  {key:"doctor",   label:"Проверить",          hint:"health-check по данным"},
  {key:"status",   label:"Статус обучения",    hint:"версия в проде"},
  {key:"train",    label:"Переобучить WP",     hint:"обучение + гейт"},
  {key:"ablation", label:"Ablation фич",       hint:"долго"},
  {key:"stop",     label:"Остановить сервисы", hint:"контейнеры не трогает",
   danger:true},
];

function renderControls(state) {
  const box = document.getElementById("controls");
  const job = state.job;
  const busy = job.running;
  box.innerHTML = ACTIONS.map(a =>
    `<button class="act${a.danger ? " danger" : ""}" data-k="${a.key}"` +
    `${busy || !state.actions_enabled ? " disabled" : ""}` +
    ` title="${a.hint}">${a.label}</button>`).join("");
  for (const b of box.querySelectorAll("button")) {
    b.onclick = () => runAction(b.dataset.k);
  }
  if (!state.actions_enabled) {
    box.insertAdjacentHTML("beforeend",
      '<span class="note">действия выключены: панель слушает не loopback</span>');
  }
  const head = document.getElementById("job-head");
  const log = document.getElementById("job-log");
  if (!job.name) { head.textContent = "действий пока не было"; return; }
  const st = job.running ? "выполняется"
           : (job.code === 0 ? "успех" : `код ${job.code}`);
  head.textContent = `${job.label} — ${st}, ${job.elapsed}с`;
  head.className = job.running ? "running"
                 : (job.code === 0 ? "ok" : "fail");
  const atBottom = log.scrollTop + log.clientHeight >= log.scrollHeight - 40;
  log.textContent = job.lines.join("\n");
  if (atBottom) log.scrollTop = log.scrollHeight;   // не мешаем читать выше
}

async function runAction(key) {
  const spec = ACTIONS.find(a => a.key === key);
  // Подтверждение только на разрушительное: на каждый чих спрашивать —
  // приучить нажимать «да» не глядя.
  if (spec.danger && !confirm(
        `${spec.label}?\n\nОстановит хостовые процессы Manta. ` +
        `Контейнеры, тома и данные не затрагиваются.`)) return;
  const r = await fetch("/api/action/" + key,
                        {method:"POST", headers:{"X-Manta-Action":"1"}});
  const j = await r.json();
  if (!r.ok) alert(j.error || j.message || "не удалось запустить");
  pollJob();
}

async function pollJob() {
  try {
    const r = await fetch("/api/job");
    renderControls(await r.json());
  } catch (e) { /* панель перезапускается — молча ждём */ }
}

pollJob();
setInterval(pollJob, 1500);
</script>
</body>
</html>
"""


def main() -> int:
    port = int(os.getenv("DASHBOARD_PORT", "9107"))
    # Дефолт — loopback (спринт 74): страница умеет запускать процессы,
    # поэтому в сеть она по умолчанию не смотрит. Из Windows WSL2 всё равно
    # доступна по localhost — проброс работает и для 127.0.0.1.
    bind = os.getenv("DASHBOARD_BIND", "127.0.0.1")
    srv = ThreadingHTTPServer((bind, port), Handler)
    print(f"Manta dashboard → http://localhost:{port}  (Ctrl+C для выхода)")
    if not _is_loopback(bind):
        print(f"   ВНИМАНИЕ: слушаю {bind} — панель видна из сети; "
              f"действия {'РАЗРЕШЕНЫ' if actions_enabled() else 'выключены'}")
    print(f"опрашивает: {', '.join(f'{n}:{p}' for n, p in SERVICES)}")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nостановлен")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
