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
# tools/ — не роскошь (спринт 208). Разовые разборы (`tier_audit.py`,
# `republish_orphans.py`) нужны ИМЕННО на боевой машине, а зависимости у
# них те же, что у сервиса, и стоят только здесь. Пока каталога в образе
# не было, `make tier-audit` гонял хостовый python3 и падал на первом же
# импорте, а `republish_orphans.py` заносили внутрь через `docker cp`
# руками — то есть цена уже платилась, просто вручную.
COPY apps/data-collector/tools/ tools/
COPY libs/ libs/
ENV PYTHONPATH=/app/src:/app/libs
USER nobody
ENTRYPOINT ["python", "-m", "collector"]
