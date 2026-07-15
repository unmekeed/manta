# Dota AI Analyst — System Specification / Спецификация системы

Full technical specification of the distributed intelligent Dota 2 match-analysis platform
**«Dota AI Analyst»** (v2.0.0-RELEASE). Available in two languages.

Полная техническая спецификация распределённой интеллектуальной платформы анализа матчей Dota 2
**«Dota AI Analyst»** (v2.0.0-RELEASE). Доступна на двух языках.

## Languages / Языки

| Language | Entry point |
|---|---|
| 🇷🇺 Русский (эталон) | [`ru/README.md`](ru/README.md) |
| 🇬🇧 English (translation) | [`en/README.md`](en/README.md) |

## Contents / Состав

- **14 chapters / глав** — context, architecture, per-service specs, data storage, replay parser,
  math models, API, frontend, security, MLOps, observability, deployment, project structure, roadmap.
- **Appendices / приложения:**
  - [`openapi/dota-ai-analyst.yaml`](openapi/dota-ai-analyst.yaml) — full REST API v1 contract / полный контракт REST API v1.
  - [`proto/services.proto`](proto/services.proto) — internal gRPC contracts / внутренние gRPC-контракты.

All diagrams use Mermaid (ER, UML, C4, sequence, state, Gantt); formulas use LaTeX. Both render
natively on GitHub. / Все диаграммы — Mermaid; формулы — LaTeX; рендерятся на GitHub.
