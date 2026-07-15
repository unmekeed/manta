# Report Generator

Материализация отчётов по матчам (Гл. 3, Гл. 7; спринт 15). Первый
пользовательский продукт поверх модели: WP-кривая и разбор матча
доступны по HTTP через API Gateway.

## Конвейер

```
features.calculated
  → MatchTimelineFeatures / PlayerMatchFeatures (ClickHouse)
  → WP-кривая: gRPC MLService.PredictStream (модель из реестра)
  → builder.py: Timeline + MatchAnalysis (контракты OpenAPI Гл. 7)
  → MatchReports (PostgreSQL, UPSERT — идемпотентно)
  → report.generated
```

Отчёт материализуется при генерации: путь чтения (шлюз) — один SELECT,
без обращений к ClickHouse/ML на запрос.

## API (через api-gateway)

- `GET /api/v1/matches/{matchId}/timeline` — схема Timeline: точки
  `{game_time, radiant_wp, net_worth_diff}` (сырой выход модели);
- `GET /api/v1/matches/{matchId}/analysis` — схема MatchAnalysis:
  итоговая WP, оценки игроков, шаблонный нарратив, `partial: true`.

## Бейзлайны в отчёте (honesty box)

- `laning_score` — прокси из LH/DN@10 (честный Laning Evaluator —
  Гл. 6.2.1, позже); `impact_score` — прокси из доли net worth
  (честный Impact = Σ ΔWP игрока — после Error Detection Engine);
- `narrative` — шаблон (победитель, переломный момент по максимуму
  |ΔWP| на median-3-сглаженной кривой, лучший фарм), не LLM;
- `errors` пуст, `hero_id` = 0 (нет словаря героев) → `partial: true`.

## Запуск

```bash
make report-gen                              # Kafka-петля
PYTHONPATH=src python -m reportgen --match 8892914077   # бэкфилл одного матча
pytest tests/
```

Пример нарратива (эталонный матч TI): «Победу одержали Силы Тьмы
(Dire). Переломный момент на 52-й минуте: вероятность победы сместилась
на 42% в пользу Dire. Лучший фарм у Satanic (kez, 512 GPM).»
