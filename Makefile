# Корневые цели монорепозитория Manta (Гл. 13.7 спецификации).

GO_SERVICES := apps/api-gateway apps/replay-parser/svc
COMPOSE     := docker compose -f deployments/docker-compose.yml

.PHONY: up down ps topics migrate migrate-pg migrate-ch lint test build clean

## Инфраструктура -------------------------------------------------------------

up:            ## Поднять локальную инфраструктуру (PG, CH, Kafka, Redis, MinIO)
	$(COMPOSE) up -d
	$(COMPOSE) ps

down:          ## Остановить инфраструктуру (данные сохраняются в volumes)
	$(COMPOSE) down

ps:            ## Статус контейнеров
	$(COMPOSE) ps

topics:        ## Создать Kafka-топики по реестру Гл. 2.3.1
	./infra/kafka/create-topics.sh

migrate: migrate-pg migrate-ch  ## Применить все миграции

migrate-pg:    ## Миграции PostgreSQL
	PGPASSWORD=dota_dev_password psql -h localhost -U dota -d manta \
		-v ON_ERROR_STOP=1 -f infra/migrations/postgres/001_init.sql

migrate-ch:    ## Миграции ClickHouse (все файлы по порядку)
	@for f in infra/migrations/clickhouse/*.sql; do \
		echo ">> $$f"; \
		docker exec -i manta-clickhouse-1 clickhouse-client \
			--user dota --password dota_dev_password --multiquery \
			< $$f || exit 1; \
	done

## Код -------------------------------------------------------------------------

lint:          ## Статический анализ Go-сервисов
	@for s in $(GO_SERVICES); do \
		echo ">> vet $$s"; (cd $$s && go vet ./...) || exit 1; \
	done

test:          ## Unit-тесты
	@for s in $(GO_SERVICES); do \
		echo ">> test $$s"; (cd $$s && go test ./...) || exit 1; \
	done

build:         ## Сборка бинарей
	@for s in $(GO_SERVICES); do \
		echo ">> build $$s"; (cd $$s && go build ./...) || exit 1; \
	done

clean:
	rm -rf bin/

## Replay Parser (C++) ---------------------------------------------------------

parser-build:  ## Собрать ядро парсера и CLI demoinfo
	cmake -B apps/replay-parser/build -S apps/replay-parser -DCMAKE_BUILD_TYPE=Release
	cmake --build apps/replay-parser/build -j4

parser-test: parser-build  ## Unit-тесты ядра парсера
	ctest --test-dir apps/replay-parser/build --output-on-failure

parser-svc: parser-build  ## Запустить Go-обвязку парсера локально
	cd apps/replay-parser/svc && \
		DEMOINFO_PATH=$(CURDIR)/apps/replay-parser/build/demoinfo \
		go run ./cmd/parser-svc

## ML Service ------------------------------------------------------------------

proto-gen:     ## Сгенерировать Python-стабы gRPC из proto/ (источник истины)
	python3 -m grpc_tools.protoc -I proto \
		--python_out=apps/ml-service/src/gen \
		--grpc_python_out=apps/ml-service/src/gen \
		proto/services.proto
	python3 -m grpc_tools.protoc -I proto \
		--python_out=apps/report-generator/src/reportgen/gen \
		--grpc_python_out=apps/report-generator/src/reportgen/gen \
		proto/services.proto

ml-serve:      ## Запустить gRPC-сервер ML Service
	cd apps/ml-service && PYTHONPATH=src python3 -m app

ml-train:      ## Обучить Win Probability (реальные матчи из ClickHouse)
	cd apps/ml-service && PYTHONPATH=src python3 -m training.train_winprob $(TRAIN_ARGS)

report-gen:    ## Запустить Report Generator (Kafka-петля)
	cd apps/report-generator && PYTHONPATH=src python3 -m reportgen

ml-auto-train: ## Автономное переобучение (порог новых матчей + гейт)
	cd apps/ml-service && PYTHONPATH=src python3 -m training.auto

ml-status:     ## Статус обучения: production-версия, разрыв датасета, кандидаты
	cd apps/ml-service && PYTHONPATH=src python3 -m training.status

ml-audit:      ## Аудит датасета: сдвиг приора, длительности, дубли
	cd apps/ml-service && PYTHONPATH=src python3 -m training.audit

stack-up:      ## Весь конвейер в контейнерах (инфраструктура + приложения)
	$(COMPOSE) --profile apps up -d --build

stack-down:    ## Остановить весь конвейер
	$(COMPOSE) --profile apps down
