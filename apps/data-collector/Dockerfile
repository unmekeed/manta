# Контекст сборки — КОРЕНЬ монорепо (нужен libs/):
#   docker build -f apps/data-collector/Dockerfile .
#
# ПОЧЕМУ НЕ ПРОСТО `build: ../apps/data-collector`. Сервис импортирует из
# libs/ по голому имени — `manta_grpc` и, со спринта 139, `dota_map`. Из
# контекста apps/data-collector каталог libs/ недостижим в принципе: COPY
# не умеет выходить за контекст. Образ при этом собирался успешно, падал
# при старте на ImportError и перезапускался по restart: unless-stopped —
# то есть ломался молча. На хозяйской машине это не всплывало:
# dev-recover.sh поднимает сервисы процессами с PYTHONPATH=src:$ROOT/libs,
# минуя Docker целиком.
FROM python:3.11-slim
WORKDIR /app
COPY apps/data-collector/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY apps/data-collector/src/ src/
COPY libs/ libs/
ENV PYTHONPATH=/app/src:/app/libs
USER nobody
ENTRYPOINT ["python", "-m", "collector"]
