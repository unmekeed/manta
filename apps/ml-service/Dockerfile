# Контекст сборки — КОРЕНЬ монорепо (нужен libs/):
#   docker build -f apps/ml-service/Dockerfile .
#
# Сервис импортирует из libs/ по голому имени — `manta_grpc` и `wp_rates`.
# Последний там лежит не случайно: производные фичи считаются В ДВУХ
# сервисах, здесь и в report-generator, и разъехавшиеся списки дали бы не
# падение, а РАЗНЫЕ ЧИСЛА у одной модели. Почему прежний контекст
# apps/ml-service ломал образ молча — см. apps/data-collector/Dockerfile.
FROM python:3.11-slim
RUN apt-get update && apt-get install -y --no-install-recommends libgomp1 \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY apps/ml-service/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY apps/ml-service/src/ src/
COPY libs/ libs/
# models/ СЮДА НЕ КОПИРУЕТСЯ, и это не упущение. Каталог перечислен в
# .gitignore (`apps/*/models/`) — он выход обучения, а не вход сборки. На
# чистой выкладке (CI, VPS) его просто нет, и прежний `COPY models/`
# ронял сборку образа целиком. В контейнер модель приходит из реестра:
# MODEL_PATH=registry://win_probability/production, а обучение внутри
# контейнера пишет в /tmp (см. MODEL_OUT у ml-autotrain).
ENV PYTHONPATH=/app/src:/app/libs
USER nobody
EXPOSE 50051
# По умолчанию — gRPC-сервер инференса (MLService, Гл. 3.7).
# Обучение: docker run ... -m training.train_winprob
# CLI WP-кривой: docker run ... -m predictors.win_probability MATCH_ID
ENTRYPOINT ["python"]
CMD ["-m", "app"]
