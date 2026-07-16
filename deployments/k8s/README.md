# Kubernetes-манифесты Manta (Гл. 12)

Развёртывание платформы по топологии спецификации (Гл. 12.1), адаптированной
к реальному стеку из 7 сервисов + автотрейн.

## Топология

| Namespace | Что живёт |
|---|---|
| `edge`   | api-gateway (×2), frontend (×2), Ingress |
| `ingest` | data-collector (singleton), replay-parser (×2 + HPA), feature-extractor |
| `ml`     | ml-service (gRPC, ×2), ml-autotrain (singleton), report-generator |
| `data`   | postgres, clickhouse, kafka (KRaft), minio — dev-grade StatefulSet'ы |

Наблюдаемость: поды аннотированы `prometheus.io/scrape` — Prometheus
(kube-prometheus-stack или свой) подхватывает их через kubernetes_sd;
правила алертов — `deployments/monitoring/alerts.yml`.

## Запуск

```sh
# собрать и запушить образы (тег = git SHA), затем:
cd deployments/k8s
kustomize edit set image \
  registry.local/manta/api-gateway=<registry>/manta/api-gateway:<sha> \
  # ... остальные сервисы
kubectl apply -k .

# миграции (однократно, из корня репо):
kubectl -n data port-forward svc/postgres 5432 &
kubectl -n data port-forward svc/clickhouse 8123 &
make migrate

# топики Kafka:
kubectl -n data exec kafka-0 -- /opt/kafka/bin/kafka-topics.sh \
  --bootstrap-server localhost:9092 --create --topic match.downloaded ...
```

## Отличия от спецификации (осознанные)

- **Данные dev-grade**: одиночные StatefulSet'ы вместо Patroni/шардов
  ClickHouse/Strimzi — операторы подключаются на этапе production
  (Гл. 12.1 остаётся целевой картиной).
- **HPA парсера по CPU**, а не по `kafka_consumergroup_lag` (Гл. 12.2.2):
  External-метрика требует prometheus-adapter/KEDA; парсинг CPU-bound,
  так что CPU — честный прокси. TODO при появлении адаптера.
- **ml-service без GPU-лимита** (Гл. 12.2.1): LightGBM-инференс CPU-bound;
  `nvidia.com/gpu` вернётся вместе с NN-ансамблем.
- **Секреты** — dev-значения в `config/env.yaml`; в production —
  SealedSecrets/ExternalSecrets. `manta-integrations` (Telegram, Anthropic)
  пуст по умолчанию — функции деградируют мягко.
