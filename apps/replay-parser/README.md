# Replay Parser

Ядро низкоуровневого разбора файлов Source 2 Demo (`.dem`) — Гл. 5 спецификации.

## Состояние: DemoReader (спринт 5) ✅

Реализован внешний слой формата:

- **DemoReader** — mmap-чтение, покадровая итерация (`varint cmd | tick | size | payload`),
  прозрачная распаковка snappy-кадров (флаг `0x40`).
- **pb_lite** — минимальный ридер wire-формата Protobuf для служебных сообщений
  (без кодогенерации; полноценные `.proto` подключаются на этапе EntityDecoder).
- Разбор **CDemoFileHeader** (карта, сервер, билд) и **CDemoFileInfo** (match_id,
  победитель, тайминги, ростер игроков с героями и командами).
- CLI **demoinfo** — сводка по реплею + статистика кадров.

Замер на реальном реплее матча **8892914077** (110.6 МиБ, 74:57 игрового времени,
67 542 кадра, 33 915 сжатых): полный проход с распаковкой — **62 мс (~1.8 ГиБ/с)**.
Эталонный файл хранится в dev-MinIO: `s3://replays/fixtures/8892914077.dem`.

## Сборка и тесты

```bash
apt-get install -y libsnappy-dev cmake g++
cmake -B build -S . -DCMAKE_BUILD_TYPE=Release
cmake --build build -j4
ctest --test-dir build          # unit-тесты (varint, pb-поля, синтетический .dem)
./build/demoinfo replay.dem     # сводка по реальному файлу
```

## Состояние: демукс пакетов и схема сущностей (спринт 6, часть 1) ✅

- **BitReader** — little-endian битовый ридер (read_bits, ubitvar, varint,
  не выровненные байтовые чтения).
- **packet_demux** — внутренний слой `DEM_Packet`/`DEM_SignonPacket`/`DEM_FullPacket`:
  `CDemoPacket.data` → поток сообщений `ubitvar type | varint size | payload`.
- **Схема сущностей**: `CDemoClassInfo` (class_id → имя) и
  `CSVCMsg_FlattenedSerializer` из `CDemoSendTables` (символы, поля с
  bit_count/low/high/encoder, сериализаторы с индексами полей, привязка
  вложенных сериализаторов).
- `demoinfo --deep` — гистограмма внутренних сообщений, имена string tables.

Замер на реплее 8892914077: **958 639 внутренних сообщений за 457 мс**;
схема: 3 229 классов, 3 294 сериализатора, 5 522 символа; найдены все
19 string tables (`CombatLogNames`, `instancebaseline`, `EntityNames`, ...);
сериализатор `CDOTA_Unit_Hero_Puck` содержит 183 поля.

## Состояние: string tables, combat log, декодер сущностей ✅ (спринт 6, части 2/3)

- **string_tables** — декодер `svc_Create/UpdateStringTable` (история ключей,
  user data, snappy); `CombatLogNames` разрешает 544 имени на реальном реплее.
- **combat_log** — `CMsgDOTACombatLogEntry` (msg id 554) с резолвом имён;
  на реплее 8892914077: 131 818 записей, 65 убийств героев с инфликторами.
  `demoinfo --events OUT.jsonl` пишет поток под схему `ReplayEvents`.
- **entities/fieldpath/field_decoder** — полный декодер сущностей
  `svc_PacketEntities`, битово-совместимый с эталоном `dotabuff/manta`:
  - 40 field-path операций (huffman-дерево с точной семантикой
    `container/heap` Go — порядок при равных весах критичен);
  - модель полей Simple/FixedArray/FixedTable/VariableArray/VariableTable
    (классификация по `field_serializer` + набору pointer-типов, как в manta)
    и рекурсивный резолвер путей по `SendTables`;
  - типовые декодеры: quantized float (точный порт `quantizedfloat.go`,
    включая шаг снятия лишних флагов ROUNDDOWN/ROUNDUP/ENCODE_ZERO — без него
    поток теряет 1 бит на границах диапазона), coord/simtime/runetime,
    векторы (`Vector*`/`VectorWS`/`Quaternion` — покомпонентный floatFactory),
    `GameTime_t` → безусловный noscale, QAngle (включая `qangle_pitch_yaw`,
    `qangle_precise`), строки, handle/enum;
  - машина состояний create/update/delete/leave-PVS с применением
    `instancebaseline`.
  Инструменты отладки: `ENT_DEBUG=1|2|3` (команды сущностей / значения полей /
  операции field path), `RAW_PEEK=<полное.имя.поля>` (сырые биты потока),
  `QF_DEBUG=1` (параметры квантованного float).

Замер на реплее 8892914077: **все 67 450 пакетов** декодированы без потери
синхронизации за ~3.8 с; 23 132 создания сущностей, 7.59 млн обновлений,
2 155 живых сущностей в конце. `demoinfo --entities OUT.jsonl` пишет позиции
героев (`m_cellX/Y` + `m_vecX/Y` → мировые координаты, 4 849 сэмплов каждые
300 тиков), в сводке — итоговый net worth всех 10 слотов из
`CDOTA_DataRadiant`/`CDOTA_DataDire` (значения сверены с эталоном manta).

## Состояние: Go-обвязка parser-svc ✅ (спринт 7)

`svc/` — сервис-обвязка ядра (Гл. 5, Гл. 2.3):

- Kafka-консьюмер `match.downloaded` (franz-go, consumer group
  `replay-parser`, ручной коммит оффсетов — at-least-once);
- скачивание `.dem` из MinIO по `replay_url` (`s3://bucket/key`);
- запуск `demoinfo --events --entities` подпроцессом, контроль DESYNC,
  извлечение `match_id` из сводки;
- потоковая загрузка JSONL в ClickHouse по HTTP (`INSERT ... FORMAT
  JSONEachRow`, без буферизации файла в памяти): combat log →
  `ReplayEvents` (DAMAGE/HEAL/KILL/ABILITY_CAST/ITEM_PURCHASE),
  позиции героев → `PositionSnapshots`;
- публикация `replay.parsed` (конверт Гл. 2.3.3, trace_id сквозной);
  ошибки — в `dlq.parser` с причиной и исходным событием, оффсет
  коммитится в любом случае (битая запись не блокирует партицию).

Смоук-тест на реплее 8892914077 через живую инфраструктуру
(docker-compose): событие → скачивание 110.6 МиБ из MinIO → разбор →
56 252 строки в `ReplayEvents` + 4 849 в `PositionSnapshots` (счётчики
1:1 с combat log ядра) → `replay.parsed` за ~5.4 с (с горячим кэшем).

Запуск: `make parser-svc` (локально) или `docker build` по `Dockerfile`
(мультистейдж: C++ ядро + Go-бинарь в одном образе). Конфигурация —
env-переменные (`KAFKA_BROKERS`, `S3_ENDPOINT`, `CLICKHOUSE_URL`,
`DEMOINFO_PATH`, ...), см. `svc/internal/config`.

## Состояние: EconomyTimeline и статусы задач ✅ (спринт 8)

- `demoinfo --economy OUT.jsonl` — сэмплы `DataTeamPlayer_t` каждые
  300 тиков (net worth, total gold/XP, ласт-хиты, денаи по 10 слотам);
  parser-svc грузит их в `EconomyTimeline` (player_id 0-4 Radiant,
  5-9 Dire, сквозная нумерация как в `CDemoFileInfo`).
- Gateway получил `JobStatusConsumer`: `replay.parsed` → `done`
  (+ match_id, completed_at), `dlq.parser` → `failed`. Владелец
  перехода — gateway (владеет таблицей AnalysisJobs); обновление
  идемпотентно, безопасно при повторной доставке.

Сквозной тест через живую инфраструктуру: HTTP-загрузка 110.6 МиБ →
outbox → Kafka → парсер (56 252 события + 4 849 позиций + 4 490 строк
экономики за ~7.5 с) → `replay.parsed` → статус job `done` виден через
`GET /api/v1/jobs/{id}`. Путь ошибки проверен на битом событии: DLQ →
`failed`.

## Спринт 9: сводка --summary и ростер в replay.parsed ✅

- `demoinfo --summary OUT.json` — машиночитаемая сводка (match_id,
  победитель, режим, длительность, ростер team/name/hero с JSON-эскейпом
  произвольных ников); parser-svc читает её вместо разбора stdout.
- Payload `replay.parsed` расширен: `winner`, `duration_s`, `players[]`
  (порядок Radiant 0-4 → Dire 5-9 — согласован с player_id в
  EconomyTimeline). Потребитель — `apps/feature-extractor` (см. его README).

## Дальше (спринт 10)

- Датасет для обучения Win Probability: выгрузка MatchTimelineFeatures
  по многим матчам + бейзлайн-модель (Гл. 6.2.2, Гл. 7.2).
- Массовый прогон: Data Collector → очередь реальных реплеев.
