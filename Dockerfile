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

# Pre-download the default ONNX Privacy Filter model and tokenizer so the
# container starts fast and works offline from the first request.
RUN python -c "from privaite.config.schema import OnnxDetectorConfig; from privaite.pii.detector_onnx import download_onnx_model; config = OnnxDetectorConfig(); download_onnx_model(repo_id=config.model_name, revision=config.revision)" && \
    python -c "from privaite.config.schema import OnnxDetectorConfig; from transformers import AutoTokenizer; config = OnnxDetectorConfig(); AutoTokenizer.from_pretrained(config.model_name, revision=config.revision, trust_remote_code=config.trust_remote_code)"

COPY config/ config/

EXPOSE 8400

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD curl -f http://localhost:8400/health || exit 1

# Config selection, in order: a mounted config/privaite.yaml wins; otherwise, if
# OPENAI_API_KEY is set, the drop-in OpenAI config is used (docker run -e
# OPENAI_API_KEY=... just works); otherwise the local Ollama example config, so
# the image still boots out of the box.
CMD ["sh", "-c", "if [ -f /app/config/privaite.yaml ]; then CFG=/app/config/privaite.yaml; elif [ -n \"$OPENAI_API_KEY\" ]; then CFG=/app/config/privaite.openai.yaml; else CFG=/app/config/privaite.example.yaml; fi; exec python -m privaite --config \"$CFG\""]
