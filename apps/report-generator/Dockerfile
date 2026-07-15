# Контекст сборки — КОРЕНЬ монорепо (нужен libs/data/heroes.json):
#   docker build -f apps/report-generator/Dockerfile .
FROM python:3.11-slim
WORKDIR /app
COPY apps/report-generator/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY apps/report-generator/src/ src/
COPY libs/ libs/
ENV PYTHONPATH=/app/src
USER nobody
ENTRYPOINT ["python", "-m", "reportgen"]
