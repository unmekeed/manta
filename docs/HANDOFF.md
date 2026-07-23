# Manta — сводка для нового чата (обновлено 2026-07-23, после спринта 49)

Manta — платформа аналитики Dota 2: сбор матчей → парсинг реплеев →
фичи → ML-модели (Win Probability, Death-Risk) → отчёты с разбором
ошибок → веб-UI. Монорепо `unmekeed/manta`, ветка `main`, всё в
коммитах по спринтам (1–49). Спецификация — `docs/specification/`,
роадмап — `docs/ROADMAP.md`, инциденты — `docs/runbooks.md`.

**Ключевой операционный факт**: облачный сбор ОСТАНОВЛЕН (решение
владельца, 2026-07-19). Локалка (Windows + WSL2 + Docker Desktop,
`~/manta`) — единственный источник истины: сбор, обучение,
Telegram-уведомления. Облачная песочница используется только для
разработки кода; её docker-volumes эфемерны и уже откатывались,
а git-чекаут в ней после перезапуска среды может оказаться СТАРЫМ —
первым делом всегда `git fetch origin main && git merge --ff-only`.

---

## Архитектура (все сервисы — процессы на хосте поверх docker-инфры)

```
OpenDota API ──► data-collector (4 процесса-источника, Python)
   │  реплей-путь: .dem → MinIO → Kafka(match.downloaded)
   │  JSON-путь:   таймлайны → ClickHouse напрямую (без реплея)
   ▼
parser-svc (Go + C++ ядро demoinfo) — Kafka → разбор .dem →
   ReplayEvents / EconomyTimeline / PositionSnapshots (ClickHouse)
   → Kafka(replay.parsed); .dem после парсинга удаляется
   ▼
feature-extractor (Python) — сырые таблицы → витрины
   PlayerMatchFeatures / MatchTimelineFeatures → Kafka(features.calculated);
   при FEATURE_STORE_ADDR пушит последний срез в Feature Store
   ▼
ml-service (gRPC :50051) — Predict/PredictStream; модели из реестра;
   model_name маршрутизируется: win_probability | death_risk
report-generator — Kafka-петля + CLI --match: WP-кривая через
   PredictStream (SHAP-вклады), атрибуция ошибок ΔWP, модельный SI,
   позиции смертей → MatchReports (Postgres, jsonb)
auto-train (training.auto) — переобучение WP по объёму (+20 матчей)
   или PSI-дрейфу; честный гейт на общем holdout; Telegram-уведомления;
   алерт «витрина не растёт» (DATASET_STALL_ALERT_H=12)
api-gateway (Go :8080) — REST: /matches, /timeline, /analysis,
   /heroes, /draft/simulate (gRPC-прокси к Draft)
frontend (React+TS+Vite) — список матчей, матч (WP beta-бейдж,
   SHAP-чипы, карта смертей, риск-бейджи), драфт-симулятор
Similarity (:50052) / Draft (:50053) / Coach (:50054) /
   Feature Store (:50055, Redis) — gRPC-сервисы поверх витрин
```

Инфраструктура (docker compose, `deployments/docker-compose.yml`):
Postgres 16 (:5432, БД manta + mlflow), ClickHouse 24.8 (:8123/:9000),
Kafka 3.8 (:9092, **AUTO_CREATE_TOPICS=false — топики только через
`make topics`!**), MinIO (:9500/:9501, бакеты replays+models), Redis
(:6379), MLflow (:9600, backend в postgres). Креды dev: dota /
dota_dev_password. Метрики Prometheus: 9101–9114; живой дашборд —
`make dashboard` (:9107).

## Данные

- Postgres: collectedmatches (дедуп сбора), collectorcursor,
  matchreports (готовые отчёты jsonb), eventoutbox и др.
- ClickHouse (manta): ReplayEvents (**TTL 14 дней**), EconomyTimeline,
  PositionSnapshots (**player_id НЕ заполняется — сущность = hero,
  ростер и команды брать из PlayerMatchFeatures**), PlayerMatchFeatures,
  MatchTimelineFeatures (витрина WP: ReplacingMergeTree, читать FINAL).
- Матчи двух путей: реплейные (полные: позиции/SI/laning) и JSON
  (только таймлайн, NaN в позиционных фичах). Tier: Premium
  (высокий ранг) / Professional (про-эталон гейта, в train не входит).
- Реестр моделей: S3 (MinIO) по умолчанию, REGISTRY_BACKEND=mlflow —
  опция; авточистка REGISTRY_KEEP_LAST=10 + все продвигавшиеся.
- **Параллельный сбор на 2+ машинах** (разные IP = разные квоты
  ~3000/сутки): COLLECTOR_SHARD_COUNT/COLLECTOR_SHARD_ID в env-файле
  (одинаковый COUNT, разный ID) — шардирование match_id % N, пересечений
  нет, dataset-import конфликт-фри. Реализовано в sources/Shard.
- `make dataset-export` / `dataset-import IN=…tar` — перенос между
  машинами, идемпотентно. **Слепок НЕ включает ReplayEvents** (TTL,
  объём) — после импорта позиции есть, а combat-лога нет; Death-Risk
  на новой машине требует локально спарсенных реплеев.

## Модели

- **Win Probability** v0.7.x: LightGBM + OOF-изотоника, 10 фич.
  Гейт: про-эталон/fresh-holdout на общих данных + бутстрап-σ.
  OOF-Brier ~0.137–0.144, про-эталон 0.15–0.16, фазовые Brier
  early ~0.22 / mid ~0.13 / late ~0.09. Критерии релиза B1+B2 пройдены.
- **Death-Risk** v0.1.0 (спринт 48): P(смерть героя в 30с) по 9
  позиционным фичам. training/risk.py; GroupSplit ПО МАТЧАМ (row-split
  = утечка); дедуп снапшотов по тику (иллюзии пересэмплируют драки).
  Облачное обучение: 1482 матча / 3.65M сэмплов, AUC 0.792, PR-AUC
  0.302 (×3.3 к базе 9.1%), Brier 0.072. Сервится тем же классом, что
  WP (одинаковый формат артефакта); Predict(model_name="death_risk").
  В отчётах заменяет эвристический SI (поле si_model=true, порог
  «рискованно» 0.3; фолбэк на эвристику, порог 0.6).
  **На локалке обучить свою**: `make ml-train-risk RISK_ARGS="--push"`
  (нужны ReplayEvents — см. хронику инцидентов №6).

---

## Хроника инцидентов и уроки (все случились реально)

1. **Эфемерность облачной среды** — контейнер перезапускается, dockerd
   и фоновые процессы гибнут, docker-volumes могут ОТКАТИТЬСЯ на
   старый снапшот, git-чекаут — на старый коммит. Решение:
   `make recover` идемпотентен; в облаке начинать с git fetch/merge.
   Нюанс recover: nohup-дети держат родительский bash (скрипт «висит»
   в do_wait) — это косметика, процессы уже запущены.
2. **ClickHouse после нечистого стопа**: стартует минуты (разгребает
   tmp-мерджи), в контейнере ulimit 4096 → «Too many open files» и
   зацикленный мердж system.metric_log. Ждать «Ready for connections»;
   при зацикливании — docker restart clickhouse.
3. **Квота OpenDota (анонимная)**: реальный дневной лимит НИЖЕ
   заявленного (remaining-day уходил в −930); сброс 00:00 UTC. Симптом:
   рост матчей только в окно ~4–7 утра МСК. Решения по слоям: бюджетные
   дефолты recover (~1100 вызовов/сутки), кэш отвергнутых кандидатов,
   TIMELINE_DETAIL_BUDGET, при 429 — сон до 00:00 UTC (не interval!),
   отдельный лог с remaining-day + метрика opendota_rate_limited_total.
   OPENDOTA_API_KEY поддержан кодом (сайт ключей периодически лежит).
4. **«Telegram-уведомления прекратились»** = чаще всего датасет
   перестал расти (auto-train шлёт только при переобучении). Смотреть
   не на бота, а на рост витрины по часам. Облако слать не может
   вообще (нет MANTA_TRAIN_ENV) — источник сообщений не путать.
5. **Рестарт Docker Desktop**: контейнеры пересоздаются, а хост-процессы
   остаются жить со СТАРЫМИ соединениями — pgrep «OK», но каждый цикл
   падает psycopg.OperationalError. Решено в коде (спринт 47:
   _ensure_db ping+reconnect в обоих раннерах). rdkafka реконнектится
   сам. На старом коде: pkill коллекторов + make recover.
6. **Потеря Kafka-топиков** (volume пересоздался; AUTO_CREATE=false):
   продюсер МОЛЧА теряет сообщения (ошибка приходит асинхронно, flush
   не спасает; rdkafka пишет «Terminating with 1 message in queue» при
   --once), консьюмер-группы не создаются, реплейный путь мёртв
   НЕДЕЛЯМИ — незаметно, потому что JSON-путь продолжает наполнять
   витрину. Итог на локалке: ReplayEvents никогда не наполнялась
   (PositionSnapshots была — из dataset-import). Диагностика:
   `kafka-topics.sh --list` (только __consumer_offsets = беда),
   `kafka-consumer-groups.sh --describe --group replay-parser`
   (group id — replay-parser, НЕ parser-svc); пустая таблица vs
   вычищенная TTL: system.parts + min(ingested_at)=1970 → parts
   никогда не было. Лечение: make topics; перезапуск parser-svc и
   extractor; системное решение — спринт 49 (ниже).
7. **Непрогнанная миграция** (009 networth_total): similarity падал
   404/UNKNOWN_IDENTIFIER. После каждого git pull — make migrate.
8. **Логи в /tmp гибнут** при рестарте WSL — истории для диагностики
   нет. Решено в спринте 49: MANTA_LOG_DIR по умолчанию ~/manta-logs.
9. **Порт-коллизии**: 9113 заняли и coach, и feature-store (стор
   переехал на 9114); «Address already in use» у ml-service — это
   НАМЕРЕННО (so_reuseport=0), чтобы задвоенный сервер со старой
   моделью не отвечал молча — убить старый процесс.
10. **PositionSnapshots.player_id = 0 всегда** — группировать по hero,
    команды из PlayerMatchFeatures (team 2/3). Иллюзии дают несколько
    строк героя на тик.
11. **Часы WSL2 могут дрейфовать** после сна (в нашем случае не
    подтвердилось, но проверка времени контейнеров vs хоста — полезный
    пункт диагностики; лечение: wsl --shutdown из PowerShell).
12. **Файлы >30 МиБ** через чат не проходят — резать: split -b 25M,
    собирать: cat parts > file (Windows: cmd /c copy /b).

Главный мета-урок: «процесс жив» ≠ «конвейер работает». Проверять
надо ДАННЫЕ (свежесть ingested_at, лаг консьюмер-групп, рост таблиц),
а не pgrep. Отсюда спринт 49.

---

## Спринт 49 (надёжность) — ✅ ВЫПОЛНЕН (2026-07-23)

Системный ответ на инциденты №6/№7/№8; всё проверено вживую:
1. **Recover-гарантии**: dev-recover.sh на каждом запуске идемпотентно
   прогоняет `make topics` (create --if-not-exists), PG-миграции через
   новый `scripts/pg-migrate.sh` (журнал SchemaMigrations — каждый файл
   ровно один раз; на старой базе без журнала baseline 001–004 помечается
   без прогона) и CH-миграции (все и так IF NOT EXISTS). В конце — doctor.
2. **`make doctor`** (`scripts/doctor.sh`) — health-check по ДАННЫМ:
   контейнеры; 7 топиков; группы replay-parser/feature-extractor + лаг;
   свежесть ReplayEvents (max ingested_at), PositionSnapshots
   (system.parts), витрины (max computed_at); квота OpenDota; часы хоста
   vs ClickHouse; журнал PG-миграций и маркер последней CH-миграции
   (009 networth_total — ОБНОВЛЯТЬ при добавлении CH-миграций!).
   OK/WARN/FAIL, exit 1 при FAIL.
3. **Telegram-алерт «реплейный путь стоит»** в training.auto:
   REPLAY_STALL_ALERT_H (6ч), метрика training_replay_path_stalled,
   один алерт на эпизод + сообщение о восстановлении; пустая таблица
   (max=1970) — отдельный текст «путь никогда не писал»; недоступный
   ClickHouse алерта не даёт.
4. **Логи вне /tmp**: MANTA_LOG_DIR по умолчанию `~/manta-logs`
   (переживает рестарт WSL); runbooks обновлены.

## Планы дальше (порядок примерный)

- **Спринт 50 — Laning-модель** (вторая половина C5): обучаемая оценка
  лейнинга поверх реплейных фич; каркас брать с training/risk.py.
- **A9**: даунвейт матчей старого патча (вес ×0.3–0.5) после
  баланс-патча; витрина не хранит patch — нужна колонка/вывод.
- **A10**: Roshan/aegis/buyback/hero-фичи + Optuna (порог 5000+
  матчей — близко).
- **B4**: публичный релиз WP — технические критерии выполнены,
  решение за владельцем.
- **D5/D6**: нагрузочные тесты и security review — гейты Фазы 4.
- **E1**: автозапуск на Windows — Планировщик задач: автозапуск Docker
  Desktop + `wsl -d Ubuntu -- make -C ~/manta recover`; туда же —
  ежедневный `make dataset-export` как бэкап (потеря volume = максимум
  день данных).
- Мелочь: gateway/frontend не входят в recover (поднимать руками);
  LLM-слой Coach включается ANTHROPIC_API_KEY (каркас готов).

## Договорённости с владельцем

- Общение на русском; спринтовый режим «продолжай некст спринт»;
  каждый спринт: реализация + тесты + живая сквозная проверка +
  коммит с подробным сообщением + push в main + обновление ROADMAP
  (и этого файла при существенных изменениях).
- Секреты (Telegram, API-ключи) в чат не постятся — только env-файл
  MANTA_TRAIN_ENV вне git.
- Облачный сбор не включать; тяжёлые проверки в облаке — поднимать
  контейнеры точечно и останавливать после.
- Диагностику на локалке вести блоками готовых команд, которые
  владелец копирует в терминал и присылает вывод.
