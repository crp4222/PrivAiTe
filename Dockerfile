FROM python:3.13-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY pyproject.toml .
COPY privaite/ privaite/
RUN pip install --no-cache-dir .

RUN python -m spacy download en_core_web_lg && \
    python -m spacy download fr_core_news_md

COPY config/ config/

EXPOSE 8400

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD curl -f http://localhost:8400/health || exit 1

CMD ["python", "-m", "privaite", "--config", "/app/config/privaite.yaml"]
