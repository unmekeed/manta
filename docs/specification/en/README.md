# SYSTEM SPECIFICATION (TECHNICAL REQUIREMENTS)

## Dota 2 Intelligent Match Analysis Platform — "Manta"

| Parameter | Value |
|---|---|
| **Project codename** | Manta |
| **Specification version** | 2.0.0-RELEASE |
| **Status** | Approved |
| **Authoring language** | Russian (source of truth), English (translation) |
| **Revision date** | 2026-07-13 |
| **Document class** | System Requirements Specification (SRS) + Architecture Design Document (ADD) |
| **Target audience** | Architects, backend/ML/frontend engineers, DevOps/SRE, product managers, QA |

---

## Purpose of the document

This document is the complete technical specification of the distributed intelligent Dota 2 match
analysis platform. It consolidates functional requirements, non-functional requirements (NFRs),
architecture of all microservices, database schemas, mathematical game-evaluation models, ML model
specifications, API contracts (REST/gRPC), frontend architecture, the MLOps loop, the security
model, the observability strategy, the deployment plan and the delivery roadmap.

The document is designed as the **Single Source of Truth** for building the system. Each chapter is
self-contained and can serve as a working brief for the corresponding team.

---

## Document structure

| # | Chapter | File | Contents |
|---|---|---|---|
| 1 | General provisions and system context | [01-general-provisions.md](01-general-provisions.md) | Business goals, roles, boundaries, NFRs, glossary, C4 L1 |
| 2 | Microservice architecture and infrastructure | [02-microservice-architecture.md](02-microservice-architecture.md) | Topology, 12 services, Kafka, gRPC, patterns, C4 L2 |
| 3 | Detailed microservice specifications | [03-service-specifications.md](03-service-specifications.md) | Per service: responsibility, API, SLO, dependencies |
| 4 | Data storage architecture and Feature Store | [04-data-storage.md](04-data-storage.md) | PostgreSQL DDL, ClickHouse, ER diagrams, Feature Store |
| 5 | Data processing module and Replay Parser | [05-replay-parser.md](05-replay-parser.md) | `.dem` parsing, ETL pipeline, event schemas, data quality |
| 6 | Mathematical models, AI and evaluation modules | [06-mathematical-models.md](06-mathematical-models.md) | Win Probability, Safety Index, ML stack, error detection |
| 7 | API design and interface specifications | [07-api-specifications.md](07-api-specifications.md) | OpenAPI, gRPC proto, errors, versioning, pagination |
| 8 | Frontend architecture and visualization | [08-frontend.md](08-frontend.md) | React/TypeScript, Zustand, Canvas/WebGL, components |
| 9 | Security, authentication and authorization | [09-security.md](09-security.md) | JWT, RBAC, threat model, encryption, compliance |
| 10 | MLOps, testing, logging and CI/CD | [10-mlops-cicd.md](10-mlops-cicd.md) | MLflow, DVC, data drift, testing pyramid, pipelines |
| 11 | Observability, monitoring and SRE | [11-observability.md](11-observability.md) | Metrics, tracing, logs, alerts, error budget, dashboards |
| 12 | Deployment and infrastructure (K8s, IaC) | [12-deployment.md](12-deployment.md) | Kubernetes, Helm, Terraform, autoscaling, environments |
| 13 | Project structure down to files and modules | [13-project-structure.md](13-project-structure.md) | Monorepo, directory tree, per-service modules |
| 14 | Implementation roadmap | [14-roadmap.md](14-roadmap.md) | Phases, sprints, milestones, risks, acceptance criteria |

**Appendices:**

- [OpenAPI specification](../openapi/manta.yaml) — full REST API v1 contract.
- [gRPC Protobuf contracts](../proto/) — internal RPC service definitions.

---

## Diagram legend

All diagrams use **Mermaid** notation and render directly on GitHub/GitLab. Types used:

| Diagram type | Purpose | Mermaid directive |
|---|---|---|
| Context (C4 L1) | System and external actors | `flowchart` |
| Container (C4 L2) | Services and stores | `flowchart` |
| ER diagram | Relational data model | `erDiagram` |
| Sequence diagram | Inter-service scenarios | `sequenceDiagram` |
| Component (UML) | Internal service structure | `flowchart` / `classDiagram` |
| State diagram | Entity lifecycle | `stateDiagram-v2` |
| Deployment diagram | Kubernetes topology | `flowchart` |
| Gantt chart | Roadmap | `gantt` |

### Node color semantics

| Color | Class | Meaning |
|---|---|---|
| 🟦 Blue | `svc` | Application microservice |
| 🟩 Green | `store` | Data store |
| 🟨 Yellow | `queue` | Message broker / queue |
| 🟥 Red | `ext` | External system |
| ⬜ Gray | `infra` | Infrastructure component |

---

## Requirement conventions

- **MUST** — a hard requirement; its absence blocks the release.
- **SHOULD** — recommended; deviation requires justification in an ADR.
- **MAY** — optional capability.

Each formalized requirement carries an identifier of the form `<CATEGORY>-<SUBCATEGORY>-<NN>`,
e.g. `NFR-PERF-01`, `FR-DRAFT-04`, `SEC-AUTH-02`.

---

## How to read the document

1. **Product / management** — Chapters 1, 14.
2. **Architects** — Chapters 1–4, 9, 11, 12.
3. **Backend engineers** — Chapters 2, 3, 4, 5, 7.
4. **ML engineers** — Chapters 5, 6, 10.
5. **Frontend engineers** — Chapters 7, 8.
6. **DevOps / SRE** — Chapters 10, 11, 12, 13.
7. **QA** — Chapters 1 (NFRs), 7, 10.
