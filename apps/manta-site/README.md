# Manta Sites frontend

Новый интерфейс аналитики матчей, опубликованный через OpenAI Sites. Он
хранится отдельно от `apps/frontend`: существующий Vite-клиент пока остаётся
частью VPS compose и не заменяется этим коммитом.

## Подключённый публичный API

- `GET /api/v1/matches` — карточки, поиск и cursor-пагинация;
- `GET /api/v1/matches/{id}/analysis` — разбор и флаги `available`;
- `GET /api/v1/matches/{id}/timeline` — поминутная WP-кривая;
- `GET /api/v1/heroes` — справочник героев;
- `POST /api/v1/draft/simulate` — симулятор драфта.

Браузер обращается к same-origin маршруту `/api/manta/*`. Серверный обработчик
проксирует только перечисленные выше публичные маршруты. Адрес шлюза задаётся
переменной `MANTA_API_BASE`; значение по умолчанию — `https://mantaml.com`.
Если публичный шлюз ещё не выложен, интерфейс явно переключается в демо-режим.

## Локальный запуск

```bash
pnpm install --frozen-lockfile
pnpm dev
```

Production-проверка:

```bash
pnpm build
pnpm exec oxlint app/page.tsx 'app/api/manta/[...path]/route.ts'
```
