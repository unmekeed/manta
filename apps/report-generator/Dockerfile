# Контекст сборки — КОРЕНЬ монорепо (нужен libs/):
#   docker build -f apps/report-generator/Dockerfile .
#
# libs/ сюда копировался и раньше — ради data/heroes.json, — но в
# PYTHONPATH не попадал. Файл читается по пути, а `manta_grpc` и
# `wp_rates` импортируются по имени, и вот они не находились: образ падал
# при старте на ImportError. Каталог в образе есть, а импорт не работает —
# это выглядит настолько нормально, что не проверяется глазами.
FROM python:3.11-slim
WORKDIR /app
COPY apps/report-generator/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY apps/report-generator/src/ src/
COPY libs/ libs/
ENV PYTHONPATH=/app/src:/app/libs
USER nobody
ENTRYPOINT ["python", "-m", "reportgen"]
