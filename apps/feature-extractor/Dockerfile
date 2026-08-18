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
COPY libs/ libs/
ENV PYTHONPATH=/app/src:/app/libs
USER nobody
ENTRYPOINT ["python", "-m", "extractor"]
