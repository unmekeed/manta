FROM python:3.11-slim
RUN apt-get update && apt-get install -y --no-install-recommends libgomp1 \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY src/ src/
COPY models/ models/
ENV PYTHONPATH=/app/src
USER nobody
EXPOSE 50051
# По умолчанию — gRPC-сервер инференса (MLService, Гл. 3.7).
# Обучение: docker run ... -m training.train_winprob
# CLI WP-кривой: docker run ... -m predictors.win_probability MATCH_ID
ENTRYPOINT ["python"]
CMD ["-m", "app"]
