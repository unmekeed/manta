# Manta — сводка для нового чата (2026-07-20, после спринта 48)

Manta — платформа аналитики Dota 2: сбор матчей → парсинг реплеев →
фичи → ML-модели (Win Probability, Death-Risk) → отчёты с разбором
ошибок → веб-UI. Монорепо `unmekeed/manta`, ветка `main`, всё в
коммитах по спринтам (1–48). Спецификация — `docs/specification/`,
роадмап — `docs/ROADMAP.md`, инциденты — `docs/runbooks.md`.

**Ключевой операционный факт**: облачный сбор ОСТАНОВЛЕН (решение
владельца, 2026-07-19). Локалка (Windows + WSL2 + Docker Desktop,
`~/manta`) — единственный источник истины: сбор, обучение,
Telegram-уведомления. Облачная песочница используется только для
разработки кода; её docker-volumes эфемерны и уже откатывались.

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
frontend (React+TS+Vite :4173 preview / :3000 в compose) — страницы:
   список матчей, матч (WP beta-бейдж, SHAP-чипы, карта смертей,
   риск-бейджи), драфт-симулятор
Similarity (:50052) / Draft (:50053) / Coach (:50054) /
   Feature Store (:50055, Redis) — gRPC-сервисы поверх витрин
```

Инфраструктура (docker compose, `deployments/docker-compose.yml`):
Postgres 16 (:5432, БД manta + mlflow), ClickHouse 24.8 (:8123/:9000),
Kafka 3.8 (:9092), MinIO (:9500/:9501, бакеты replays+models), Redis
(:6379), MLflow (:9600, backend в postgres). Креды dev: dota /
dota_dev_password. Метрики Prometheus: 9101–9114 (список в README);
живой дашборд — `make dashboard` (:9107).

## Данные

- Postgres: collectedmatches (дедуп сбора), collectorcursor,
  matchreports (готовые отчёты jsonb), eventoutbox и др.
- ClickHouse (manta): ReplayEvents (TTL 14 дней!), EconomyTimeline,
  PositionSnapshots (player_id НЕ заполняется — сущность = hero),
  PlayerMatchFeatures (ростер: hero→player_id/team), MatchTimelineFeatures
  (витрина WP: ReplacingMergeTree, FINAL при чтении).
- Матчи двух путей: реплейные (полные: позиции/SI/laning) и JSON
  (только таймлайн, NaN в позиционных фичах). Tier: Premium
  (высокий ранг) / Professional (про-эталон гейта, в train не входит).
- Состояние на 2026-07-20: витрина ~4115+ матчей (локалка), реплейных
  с позициями ~1500. Реестр моделей: S3 (MinIO) по умолчанию,
  REGISTRY_BACKEND=mlflow — опция; авточистка REGISTRY_KEEP_LAST=10 +
  все продвигавшиеся.

## Модели

- **Win Probability** v0.7.0: LightGBM + OOF-изотоника, 10 фич
  (networth/xp/kills diff+total, position_advance, alive/towers/rax
  diff, networth_rel, game_time). Гейт: про-эталон/fresh-holdout на
  общих данных + бутстрап-σ. Метрики: OOF-Brier ~0.137–0.144, про-эталон
  0.15–0.16, фазовые Brier early ~0.22 / mid ~0.13 / late ~0.09.
  Релизные критерии B1+B2 пройдены; beta-бейдж на фронте.
- **Death-Risk** v0.1.0 (спринт 48): P(смерть героя в 30с) по
  позиционной обстановке (9 фич: глубина, дистанции/счётчики живых
  врагов/союзников, время). Обучение: training/risk.py, 3.65M сэмплов /
  1482 матча, GroupSplit по матчам, AUC 0.792, PR-AUC 0.302 (×3.3),
  Brier 0.072. Заменяет эвристический SI в отчётах (si_model=true,
  порог «рискованно» 0.3; фолбэк на эвристику с порогом 0.6).
  ВАЖНО: на локалке модель нужно обучить локально:
  `make ml-train-risk RISK_ARGS="--push"` — облачная не переносилась.

## Операционка

- `make recover` (scripts/dev-recover.sh) — идемпотентный подъём всего
  (dockerd → инфра → 13 сервисов); секреты из env-файла
  `MANTA_TRAIN_ENV` (Telegram token/chat, OPENDOTA_API_KEY — вне git).
- `make dataset-export` / `dataset-import IN=…tar` — перенос датасета
  между машинами, идемпотентно (E2).
- OpenDota: БЕЗ API-ключа (сайт ключей лежал). Реальный анонимный
  дневной лимит НИЖЕ заявленного (remaining-day уходил в -900);
  бюджетные дефолты recover ~1100 вызовов/сутки (timeline 10/30мин,
  pro-timeline 5/час, реплей-пути 1/час, кэш отвергнутых кандидатов,
  TIMELINE_DETAIL_BUDGET). При 429 коллектор сам ждёт до 00:00 UTC.
  Если 429 регулярны — резать лимиты дальше или добыть ключ.
- Коллекторы переживают рестарт Postgres/Docker Desktop (auto-reconnect,
  спринт 47). Kafka (rdkafka) переподключается сам.
- Известные грабли — docs/runbooks.md: «витрина не растёт» (квота /
  зомби-процессы после рестарта докера / CH разгребает мерджи /
  непрогнанная миграция), «гейт всё отклоняет», «PSI-алярм».
- CI: go vet/test/build, pytest всех 8 python-сервисов, cmake+ctest C++,
  валидация схем. proto/services.proto — источник истины; make proto-gen
  генерит python-стабы всем + Go-стабы (proto/gen/go, модуль с replace).

## Что осталось (из docs/ROADMAP.md)

1. **C5, Laning-модель** — вторая половина: модель исхода/оценки
   лейнинга поверх реплейных фич (laning_score сейчас — честная
   эвристика «исход линии против оппонентов»). Error-часть (Death-Risk)
   готова — можно взять её каркас (training/risk.py) за образец.
2. **A9** — даунвейт матчей старого патча (вес ×0.3–0.5) после
   баланс-патча; PSI-триггер уже ловит смену меты. Витрина не хранит
   patch — потребуется колонка/вывод из match_id.
3. **A10** — фичи Roshan/aegis/buyback, hero-фичи, Optuna-тюнинг —
   порог 5000+ матчей (близко).
4. **B4** — публичный релиз WP: технические критерии выполнены,
   решение за владельцем.
5. **D5** — нагрузочные тесты NFR-PERF/SCAL (гейт Фазы 4).
6. **D6** — security review: SAST/SCA, секреты, GDPR (гейт Фазы 4).
7. **E1** — автозапуск стека на локалке: Планировщик задач Windows →
   автозапуск Docker Desktop + `wsl -d Ubuntu -- make -C ~/manta
   recover` (или systemd-юнит в WSL). Делается на машине владельца.
8. Мелочь: gateway/frontend не входят в dev-recover (поднимаются
   руками при необходимости); LLM-слой Coach включается env-ключом
   ANTHROPIC_API_KEY (каркас готов, спринт 38).

## Договорённости с владельцем

- Общение на русском; спринтовый режим «продолжай некст спринт»;
  каждый спринт: реализация + тесты + живая сквозная проверка +
  коммит с подробным сообщением + push в main + обновление ROADMAP.
- Секреты (Telegram, API-ключи) в чат не постятся — только env-файл.
- Облачный сбор не включать; тяжёлые проверки в облаке — поднимать
  контейнеры точечно и останавливать после.
