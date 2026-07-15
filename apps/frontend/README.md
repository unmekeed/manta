# Frontend

SPA разбора матчей (Гл. 8): React 18 + TypeScript + Vite,
TanStack Query (серверное состояние), Recharts (графики),
React Router. Без UI-кита — лёгкая тёмная тема.

## Страницы

- `/` — список разобранных матчей (`GET /api/v1/matches`);
- `/matches/:id` — разбор: WP-кривая с net worth diff (Recharts, две
  оси), таблица игроков (лейнинг/импакт, счёт ошибок), ключевые ошибки
  по ΔWP, нарратив, версии отчёта/модели.

## Запуск

```bash
npm install
npm run dev        # dev-сервер c прокси /api → localhost:8080
npm run build      # прод-сборка в dist/
npm run preview    # раздача сборки (тот же прокси)
```

В compose-профиле `apps` собирается в nginx-образ (порт 3000),
`/api` проксируется в api-gateway.

## Дальше (Гл. 8)

- Radar-граф оценок игрока, heatmap позиций (PositionSnapshots).
- WebSocket live-обновления по report.generated.
- Vitest/Playwright-тесты компонентов.
