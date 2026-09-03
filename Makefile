# Корневые цели монорепозитория Manta (Гл. 13.7 спецификации).

GO_SERVICES := apps/api-gateway apps/replay-parser/svc
COMPOSE     := docker compose -f deployments/docker-compose.yml

# Env-файл с секретами живёт ВНЕ репозитория. Пустое значение допустимо:
# цели, которым секреты не нужны, работают и без него.
MANTA_TRAIN_ENV ?= $(HOME)/manta-train.env

# Окружение бота Game Coordinator — тоже вне репозитория: dota2 тянет
# protobuf 3.20 и gevent, которым нечего делать рядом с коллекторами.
GC_VENV ?= $(HOME)/.manta-gc-venv

.PHONY: up down ps topics migrate migrate-pg migrate-ch doctor lint test pytest build clean
.PHONY: recover stop
.PHONY: ranks-seed ranks-fill ranks-report ranks-probe ranks-harvest ranks-scan
.PHONY: candidates-queue candidates-sql-test dedup-sql-test sql-test wp-rates-sql-test pytest-check
.PHONY: gc-venv gc-probe gc-node gc-login-check gc-token gc-token-login gc-probe-node gc-salts
.PHONY: golden-test signals-golden-update
.PHONY: wsl-anchor wsl-anchor-status wsl-anchor-stop
.PHONY: backup-drill heartbeat tg-test map-calibrate farm-core-backfill peer-sync
.PHONY: vps-bootstrap vps-up

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

migrate-pg:    ## Миграции PostgreSQL (только новые; журнал SchemaMigrations)
	./scripts/pg-migrate.sh

migrate-ch:    ## Миграции ClickHouse (только новые; журнал SchemaMigrations)
	./scripts/ch-migrate.sh

## Код -------------------------------------------------------------------------

lint:          ## Статический анализ Go-сервисов (gofmt + vet)
	@for s in $(GO_SERVICES); do \
		echo ">> gofmt $$s"; \
		out=$$(gofmt -l $$s); \
		if [ -n "$$out" ]; then echo "не отформатировано:"; echo "$$out"; \
			echo "лечение: gofmt -w $$s"; exit 1; fi; \
	done
	@for s in $(GO_SERVICES); do \
		echo ">> vet $$s"; (cd $$s && go vet ./...) || exit 1; \
	done

test: pytest   ## Unit-тесты: питоновские наборы всех сервисов и Go
	@for s in $(GO_SERVICES); do \
		echo ">> test $$s"; (cd $$s && go test ./...) || exit 1; \
	done

# ЗАЧЕМ ЭТА ЦЕЛЬ. До спринта 190 `make test` гонял только Go, а
# питоновские наборы запускались поштучно — тот сервис, по которому шёл
# спринт. Из-за этого test_wp_features_sync в report-generator горел
# красным со спринта 84, был замечен, снабжён комментарием «дописывать
# сюда» — и всё равно забыт на волнах 185-187: двадцать две новые фичи
# опять не доехали до Predict, и увидели это только на ревизии.
# Тест-сторож бесполезен, если его никто не запускает, а комментарий
# читают те, кто и так помнит. Поэтому набор ходит по ВСЕМ apps/*.
#
# Наборы, которым нужна живая БД, помечены skip и отваливаются сами.
# Цикл не останавливается на первом красном: знать надо про все.
#
# Сервис берётся по наличию tests/test_*.py, а не по наличию каталога
# tests: у replay-parser он есть, но лежит там C++ (ctest, цель
# parser-test). Пустой прогон pytest возвращает код 5, и цель падала бы
# на сервисе, у которого питоновских тестов нет и не планируется.
pytest: pytest-check ## Питоновские наборы всех сервисов
	@fail=""; \
	for s in $$(ls -d apps/*/tests/test_*.py \
	            | xargs -n1 dirname | xargs -n1 dirname | sort -u); do \
		echo ">> pytest $$s"; \
		(cd $$s && PYTHONPATH=src:$(CURDIR)/libs python3 -m pytest tests -q) \
			|| fail="$$fail $$s"; \
	done; \
	if [ -n "$$fail" ]; then \
		echo; echo "красные наборы:$$fail"; exit 1; \
	fi

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
	python3 -m grpc_tools.protoc -I proto \
		--python_out=apps/similarity/src/gen \
		--grpc_python_out=apps/similarity/src/gen \
		proto/services.proto
	python3 -m grpc_tools.protoc -I proto \
		--python_out=apps/draft/src/gen \
		--grpc_python_out=apps/draft/src/gen \
		proto/services.proto
	python3 -m grpc_tools.protoc -I proto \
		--python_out=apps/coach/src/gen \
		--grpc_python_out=apps/coach/src/gen \
		proto/services.proto
	python3 -m grpc_tools.protoc -I proto \
		--python_out=apps/feature-store/src/gen \
		--grpc_python_out=apps/feature-store/src/gen \
		proto/services.proto
	python3 -m grpc_tools.protoc -I proto \
		--python_out=apps/feature-extractor/src/gen \
		--grpc_python_out=apps/feature-extractor/src/gen \
		proto/services.proto
	PATH=$$PATH:$$HOME/go/bin protoc -I proto \
		--go_out=proto/gen/go --go_opt=module=github.com/unmekeed/manta/proto \
		--go-grpc_out=proto/gen/go --go-grpc_opt=module=github.com/unmekeed/manta/proto \
		proto/services.proto

ml-serve:      ## Запустить gRPC-сервер ML Service
	cd apps/ml-service && PYTHONPATH=src:$(CURDIR)/libs python3 -m app

ml-train:      ## Обучить Win Probability (реальные матчи из ClickHouse)
	cd apps/ml-service && PYTHONPATH=src:$(CURDIR)/libs python3 -m training.train_winprob $(TRAIN_ARGS)

report-gen:    ## Запустить Report Generator (Kafka-петля)
	cd apps/report-generator && PYTHONPATH=src:$(CURDIR)/libs python3 -m reportgen

sim-serve:     ## Similarity Engine: gRPC-поиск похожих матчей (:50052)
	cd apps/similarity && PYTHONPATH=src:$(CURDIR)/libs python3 -m serve

draft-serve:   ## Draft Engine: рекомендации пиков (:50053)
	cd apps/draft && PYTHONPATH=src:$(CURDIR)/libs python3 -m serve_draft

coach-serve:   ## AI Coach: план тренировки из отчётов + RAG (:50054)
	cd apps/coach && PYTHONPATH=src:$(CURDIR)/libs python3 -m serve_coach

fs-serve:      ## Feature Store: онлайн-фичи поверх Redis (:50055)
	cd apps/feature-store && PYTHONPATH=src:$(CURDIR)/libs python3 -m serve_features

ml-train-risk: ## Обучить Death-Risk модель на реплейных позициях (C5)
	cd apps/ml-service && PYTHONPATH=src:$(CURDIR)/libs python3 -m training.risk $(RISK_ARGS)

ml-train-laning: ## Обучить Laning-модель на combat-логе первых 5 минут (C5)
	cd apps/ml-service && PYTHONPATH=src:$(CURDIR)/libs python3 -m training.laning $(LANING_ARGS)

ml-train-draft: ## Draft Prior: P(win|составы) + OOF-прайоры в MatchDraft (F3)
	cd apps/ml-service && PYTHONPATH=src:$(CURDIR)/libs python3 -m training.draft_prior $(DRAFT_ARGS)

ml-tune:       ## Подбор гиперпараметров WP через Optuna (F7): ARGS="--trials 60 --apply"
	cd apps/ml-service && PYTHONPATH=src:$(CURDIR)/libs python3 -m training.tune $(ARGS)

ml-auto-train: ## Автономное переобучение (порог новых матчей + гейт)
	cd apps/ml-service && PYTHONPATH=src:$(CURDIR)/libs python3 -m training.auto

ml-status:     ## Статус обучения: production-версия, разрыв датасета, кандидаты
	cd apps/ml-service && PYTHONPATH=src:$(CURDIR)/libs python3 -m training.status

ml-ablation:   ## Ablation фич WP: какая заслужила место (ARGS="--each")
	cd apps/ml-service && PYTHONPATH=src:$(CURDIR)/libs python3 -m training.ablation $(ARGS)

wp-rates-sql-test: pytest-check ## Проверить окна производных (G1) на живом ClickHouse
	cd apps/ml-service && MANTA_TEST_CH=1 \
	PYTHONPATH=src:$(CURDIR)/libs python3 -m pytest tests/test_rates_sql.py -v

wsl-anchor:    ## Держать WSL живым: окно терминала можно закрыть на крестик
	@./scripts/wsl-anchor.sh run &
	@sleep 1
	@./scripts/wsl-anchor.sh status

wsl-anchor-status: ## Переживёт ли стек закрытие окна WSL
	@./scripts/wsl-anchor.sh status

wsl-anchor-stop: ## Снять якорь (WSL снова погаснет с последним окном)
	@./scripts/wsl-anchor.sh stop

golden-test:   pytest-check ## Регресс ЗНАЧЕНИЙ фич: три эталона, без БД и сети
	cd apps/data-collector && PYTHONPATH=src:$(CURDIR)/libs \
	python3 -m pytest tests/test_signals_golden.py -q
	cd apps/ml-service && PYTHONPATH=src:$(CURDIR)/libs \
	python3 -m pytest tests/test_rates_golden.py tests/test_vector_golden.py \
	                  tests/test_feature_coverage.py -q

signals-golden-update: ## ПЕРЕПИСАТЬ эталоны фич (только когда формулу меняют осознанно)
	@echo "Эталоны будут перезаписаны. Это правка СМЫСЛА, а не обновление кэша:"
	@echo "каждое изменившееся число обязано попасть в diff коммита и быть"
	@echo "объяснённым в его сообщении. Если числа поехали неожиданно —"
	@echo "это не устаревший эталон, это регресс."
	@echo
	cd apps/data-collector && python3 tools/regen_golden.py
	cd apps/ml-service && PYTHONPATH=src:$(CURDIR)/libs \
	python3 tools/regen_rates_golden.py
	cd apps/ml-service && PYTHONPATH=src:$(CURDIR)/libs \
	python3 tools/regen_vector_golden.py
	@echo
	@git --no-pager diff --stat -- '*/tests/fixtures/golden_*.json' || true

pytest-check:  ## Проверить, что pytest вообще установлен (иначе внятная подсказка)
	@python3 -c "import pytest" 2>/dev/null || { \
		echo "pytest не установлен в этом python3."; \
		echo "лечение (Debian/Ubuntu, PEP 668 — pip в системный python закрыт):"; \
		echo "    sudo apt install -y python3-pytest"; \
		echo "если пакета нет — тем же способом, каким ставились numpy и lightgbm:"; \
		echo "    python3 -m pip install --break-system-packages pytest"; \
		echo "(тесты на живой БД запускаются руками и в CI не участвуют)"; \
		exit 2; }

gc-venv:       ## Отдельное окружение для замера Game Coordinator
	python3 -m venv $(GC_VENV)
	$(GC_VENV)/bin/pip install -q -r scripts/gc-requirements.txt
	@echo "готово: $(GC_VENV)"
	@echo "дальше — логин и пароль бота в $(MANTA_TRAIN_ENV):"
	@echo "  STEAM_BOT_LOGIN=... / STEAM_BOT_PASSWORD=..."

gc-node:       ## Поставить steam-user для проверки входа новым путём
	cd scripts/gc-node && npm install --silent --no-fund --no-audit
	@echo "готово: scripts/gc-node/node_modules"

gc-login-check: ## Пускает ли Steam НОВЫМ путём аутентификации (проверка гипотезы)
	@test -d scripts/gc-node/node_modules || { echo "сначала make gc-node"; exit 2; }
	set -a; [ -f $(MANTA_TRAIN_ENV) ] && . $(MANTA_TRAIN_ENV); set +a; \
	GC_STATE_DIR=$${GC_STATE_DIR:-$(HOME)/.manta-gc-node} \
	node scripts/gc-node/login-check.mjs

# Через обёртку, а не прямым вызовом node: она грузит env, различает
# «бюджет на сегодня» и настоящую поломку и ровно её же гоняет cron.
# Второй путь запуска однажды разошёлся бы с первым, и расходились бы они
# молча — а проверять руками стали бы тот, что короче.
gc-salts:      ## Добыть порцию солей у GC в таблицу ReplaySalts
	./scripts/gc-salts.sh

gc-probe-node: ## Замер GC через steam-user: ARGS="--ids-file /tmp/ids.txt --limit 200"
	@test -d scripts/gc-node/node_modules || { echo "сначала make gc-node"; exit 2; }
	set -a; [ -f $(MANTA_TRAIN_ENV) ] && . $(MANTA_TRAIN_ENV); set +a; \
	node scripts/gc-node/gc-probe.mjs $(ARGS)

gc-token-login: ## Пускает ли Steam по СОХРАНЁННОМУ токену (пароль не нужен)
	@test -d scripts/gc-node/node_modules || { echo "сначала make gc-node"; exit 2; }
	set -a; [ -f $(MANTA_TRAIN_ENV) ] && . $(MANTA_TRAIN_ENV); set +a; \
	node scripts/gc-node/token-login.mjs

gc-token:      ## Обменять пароль на refresh-токен для замера (нужно редко)
	@test -d scripts/gc-node/node_modules || { echo "сначала make gc-node"; exit 2; }
	set -a; [ -f $(MANTA_TRAIN_ENV) ] && . $(MANTA_TRAIN_ENV); set +a; \
	node scripts/gc-node/get-token.mjs

gc-probe:      ## Замер GC: ARGS=login | "details --limit 200" | bulk
	@test -x $(GC_VENV)/bin/python || { echo "сначала make gc-venv"; exit 2; }
	set -a; [ -f $(MANTA_TRAIN_ENV) ] && . $(MANTA_TRAIN_ENV); set +a; \
	$(GC_VENV)/bin/python scripts/gc-probe.py $(ARGS)

ml-audit:      ## Аудит датасета: сдвиг приора, длительности, дубли
	cd apps/ml-service && PYTHONPATH=src:$(CURDIR)/libs python3 -m training.audit

recover:       ## Восстановить dev-стек после перезапуска среды (идемпотентно)
	MANTA_TRAIN_ENV=$(MANTA_TRAIN_ENV) ./scripts/dev-recover.sh

stop:          ## Остановить хостовые процессы (контейнеры и данные не трогает)
	./scripts/dev-stop.sh

daily-report:  ## Снимок doctor+collect-report+ml-audit в MANTA_REPORT_DIR (30 дней)
	MANTA_TRAIN_ENV=$(MANTA_TRAIN_ENV) ./scripts/daily-report.sh

doctor:        ## Health-check конвейера по ДАННЫМ (топики, лаг, свежесть, квота)
	./scripts/doctor.sh

collect-report: ## Почему упал темп сбора + покрытие фич: ARGS="rate|features|logs"
	MANTA_TRAIN_ENV=$(MANTA_TRAIN_ENV) ./scripts/collect-report.sh $(ARGS)

backfill:      ## Пересчёт фич по сохранённому JSON, без вызовов API: ARGS="--limit 50"
	cd apps/data-collector && PYTHONPATH=src:$(CURDIR)/libs python3 -m collector.backfill $(ARGS)

vps-bootstrap: ## Развернуть Manta на чистом VPS: ARGS=--check
	./scripts/vps-bootstrap.sh $(ARGS)

vps-up:        ## Поднять стек с наложением для VPS (порты на 127.0.0.1)
	docker compose -f deployments/docker-compose.yml \
		-f deployments/docker-compose.vps.yml --profile apps up -d

peer-sync:     ## Втянуть слепки соседних машин из облака: ARGS=--dry-run
	./scripts/peer-sync.sh $(ARGS)

farm-core-backfill: ## Досчитать farm_core на старых матчах: ARGS="--dry-run"
	cd apps/feature-extractor && PYTHONPATH=src:$(CURDIR)/libs \
		python3 tools/backfill_farm_core.py $(ARGS)

ranks-harvest: ## Посеять кэш рангов из сохранённого JSON в MinIO, без вызовов API
	MANTA_TRAIN_ENV=$(MANTA_TRAIN_ENV) ./scripts/ranks.sh harvest $(ARGS)

ranks-seed:    ## Набрать account_id из потока Valve: ARGS="--matches 2000"
	MANTA_TRAIN_ENV=$(MANTA_TRAIN_ENV) ./scripts/ranks.sh seed $(ARGS)

ranks-fill:    ## Опросить очередь рангов: ARGS="--budget 500 --resolver stratz"
	MANTA_TRAIN_ENV=$(MANTA_TRAIN_ENV) ./scripts/ranks.sh fill $(ARGS)

ranks-scan:    ## Замер отбора: воронка причин + развёртка порогов ARGS="--matches 2000"
	MANTA_TRAIN_ENV=$(MANTA_TRAIN_ENV) ./scripts/ranks.sh scan $(ARGS)

candidates-queue: ## Очередь своей разбивки: состояния + выборка для проверки точности
	MANTA_TRAIN_ENV=$(MANTA_TRAIN_ENV) ./scripts/ranks.sh queue

candidates-sql-test: ## SQL метрики точности отбора — на живом Postgres
	./scripts/sql-test.sh tests/test_candidates_sql.py

# Запуск через scripts/sql-test.sh, а не напрямую: pytest на хосте нет и
# быть не должно — всё живёт в контейнерах (спринт 152).
sql-test:      ## Все SQL-тесты на живой базе (pytest на хосте не нужен)
	./scripts/sql-test.sh $(ARGS)

dedup-sql-test: ## Дедуп по возможностям — на живом Postgres
	./scripts/sql-test.sh tests/test_dedup_sql.py

replay-parked: ## Что стоит в парковке (реплеи, которые не удалось взять)
	./scripts/replay-retry.sh

replay-retry:  ## Попробовать забрать припаркованные реплеи (вручную!)
	./scripts/replay-retry.sh --run

ranks-report:  ## Кэш рангов: сколько накоплено и какую долю потока он закрывает
	MANTA_TRAIN_ENV=$(MANTA_TRAIN_ENV) ./scripts/ranks.sh report

ranks-probe:   ## Что разрешено токену STRATZ: player / players / поля ранга
	MANTA_TRAIN_ENV=$(MANTA_TRAIN_ENV) ./scripts/ranks.sh probe

dashboard:     ## Живой дашборд наблюдаемости без Docker/Grafana (:9107)
	python3 scripts/dashboard.py

dataset-export: ## Слепок датасета для переноса на другую машину (E2)
	./scripts/dataset-sync.sh export $(OUT)

dataset-import: ## Идемпотентно влить слепок: make dataset-import IN=файл.tar
	./scripts/dataset-sync.sh import $(IN)

backup:        ## Слепок датасета в MANTA_BACKUP_DIR с ротацией KEEP_DAYS (E1)
	./scripts/backup.sh

backup-drill:  ## УЧЕНИЯ: восстановить слепок во ВРЕМЕННЫЕ базы и сверить строки
	./scripts/backup-drill.sh $(ARGS)

map-calibrate: ## Замерить настоящие границы карты по своим данным (спринт 139)
	PYTHONPATH=libs python3 tools/map-calibrate.py

heartbeat:     ## Сторож: состояние одним сообщением в Telegram (для cron)
	./scripts/heartbeat.sh

tg-test:       ## Проверить, что канал Telegram вообще доставляет
	@set -a; [ -f $(MANTA_TRAIN_ENV) ] && . $(MANTA_TRAIN_ENV); set +a; \
	if [ -z "$$TELEGRAM_BOT_TOKEN" ] || [ -z "$$TELEGRAM_CHAT_ID" ]; then \
		echo "TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID не заданы в $(MANTA_TRAIN_ENV)"; \
		echo "Без них сторож молчит — и поломку сообщать будет некуда."; \
		exit 2; \
	fi; \
	curl -s --max-time 20 \
		"https://api.telegram.org/bot$$TELEGRAM_BOT_TOKEN/sendMessage" \
		-d "chat_id=$$TELEGRAM_CHAT_ID" -d "parse_mode=HTML" \
		--data-urlencode "text=🧪 <b>Manta</b>: проверка канала с $$(hostname)" \
		| grep -q '"ok":true' \
		&& echo "доставлено — проверь чат" \
		|| { echo "Telegram НЕ принял сообщение: проверь токен и chat_id"; exit 1; }

loadtest:      ## Нагрузочные тесты NFR-PERF/SCAL (D5): make loadtest ARGS="--only rest"
	python3 scripts/loadtest.py $(ARGS)

security-scan: ## SAST/SCA + поиск секретов и дефолтных кредов (D6)
	./scripts/security-scan.sh

stack-up:      ## Весь конвейер в контейнерах (инфраструктура + приложения)
	$(COMPOSE) --profile apps up -d --build

stack-down:    ## Остановить весь конвейер
	$(COMPOSE) --profile apps down
