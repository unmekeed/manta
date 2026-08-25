FROM ghcr.io/mlflow/mlflow:v2.16.2

# MLflow запускается во внутренней сети без DNS/интернета. Зависимость
# backend-store должна устанавливаться при сборке, а не в restart-loop.
RUN python -m pip install --no-cache-dir psycopg2-binary==2.9.10
