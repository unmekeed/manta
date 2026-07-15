# Feature Extractor

Расчёт признаков матча из сырых таблиц ClickHouse (Гл. 6 спецификации,
спринт 9). Слушает `replay.parsed`, пишет витрины фич, публикует
`features.calculated`.

## Конвейер

```
replay.parsed (match_id, winner, players[])
  → SELECT EconomyTimeline, ReplayEvents(KILL) из ClickHouse
  → features.py (чистые функции, point-in-time окна)
  → INSERT PlayerMatchFeatures, MatchTimelineFeatures
  → features.calculated
```

## Витрины (миграция 003)

- **PlayerMatchFeatures** — по игроку за матч: GPM/XPM, LH/DN на 5-й и
  10-й минутах, net worth на 10/20-й минутах и финальный, доля в net
  worth команды, герой/ник/команда, `won` (метка исхода).
- **MatchTimelineFeatures** — поминутно: net worth diff и XP diff
  (Radiant − Dire), накопительные убийства по командам, `radiant_win`
  (target Win Probability, Гл. 6.2.2).

Обе — `ReplacingMergeTree(computed_at)`: повторная обработка матча
(at-least-once, новая версия фич) замещает строки; читать с `FINAL`.

Point-in-time корректность: «минута N» отсчитывается от первого сэмпла
с ненулевой экономикой (конец пик-фазы), окна берут последний сэмпл
`game_time <= t` — утечки будущего в обучающие фичи нет.

## Запуск

```bash
pip install -r requirements.txt
PYTHONPATH=src python -m extractor       # env: KAFKA_BROKERS, CLICKHOUSE_URL, ...
pytest tests/                            # юнит-тесты чистой логики
```

События старой схемы (без `players[]` в payload) пропускаются с
предупреждением — фичи без ростера не считаются.
