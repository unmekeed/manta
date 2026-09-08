# Контекст сборки — КОРЕНЬ монорепо (нужен libs/):
#   docker build -f apps/feature-extractor/Dockerfile .
#
# Сервис импортирует из libs/ по голому имени — `manta_grpc` и, со
# спринта 139, `dota_map`. Почему прежний контекст apps/feature-extractor
# ломал образ молча — см. apps/data-collector/Dockerfile.
FROM python:3.11-slim
WORKDIR /app
COPY apps/feature-extractor/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY apps/feature-extractor/src/ src/
# tools/ — по той же причине, что у data-collector (спринт 208):
# `backfill_farm_core.py` досчитывает фичи на БОЕВОЙ витрине, а его
# зависимости стоят только в образе.
COPY apps/feature-extractor/tools/ tools/
COPY libs/ libs/
ENV PYTHONPATH=/app/src:/app/libs
USER nobody
ENTRYPOINT ["python", "-m", "extractor"]
