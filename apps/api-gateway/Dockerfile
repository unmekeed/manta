# Контекст сборки — КОРЕНЬ монорепо:
#   docker build -f apps/api-gateway/Dockerfile .
#
# ПОЧЕМУ НЕ ПРОСТО `build: ../apps/api-gateway`. В go.mod стоит
#
#     replace github.com/unmekeed/manta/proto => ../../proto/gen/go
#
# то есть модуль лежит ЗА пределами каталога сервиса. Из контекста
# apps/api-gateway каталог proto/ недостижим в принципе — COPY не умеет
# выходить за контекст, — и `go mod download` падал на
# «reading /proto/gen/go/go.mod: no such file or directory».
#
# Болезнь ровно та же, что была у питоновских сервисов до спринта 139:
# сборка ищет за границей контекста. Там это был libs/, здесь proto/.
#
# Раскладка внутри образа ПОВТОРЯЕТ раскладку репозитория, иначе путь
# ../../proto/gen/go из go.mod не разрешится: сервис лежит в
# /src/apps/api-gateway, а модуль — в /src/proto/gen/go.
FROM golang:1.25.13 AS build
WORKDIR /src
# Сначала то, от чего зависит go mod download, и только оно: слой с
# зависимостями переживает правку кода сервиса и не пересобирается.
COPY proto/gen/go proto/gen/go
COPY apps/api-gateway/go.mod apps/api-gateway/go.sum apps/api-gateway/
WORKDIR /src/apps/api-gateway
RUN go mod download
COPY apps/api-gateway/ .
RUN CGO_ENABLED=0 go build -trimpath -ldflags="-s -w" -o /out/api-gateway ./cmd/server

FROM gcr.io/distroless/static-debian12:nonroot
COPY --from=build /out/api-gateway /api-gateway
EXPOSE 8080
USER nonroot
ENTRYPOINT ["/api-gateway"]
