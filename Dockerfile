FROM python:3.11-slim

# v1.83.14: versione stabile e sicura post-hardening CI/CD v2 e patch CVE-2026-42208
# (evita le versioni compromesse 1.82.7, 1.82.8 e 1.107.0 e mantiene compatibilità con langfuse 2.x)
ARG LITELLM_VERSION=1.83.14

# Dipendenze di sistema
RUN apt-get update && apt-get install -y --no-install-recommends \
        git build-essential curl \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir --upgrade pip

# Dipendenze Python
# Nota: langfuse 3.x ha breaking changes — teniamo 2.x per compatibilità con LiteLLM 1.x
RUN pip install --no-cache-dir \
    "langfuse>=2.0.0,<3.0.0" \
    "mlflow>=2.14.0" \
    "litellm[proxy]==${LITELLM_VERSION}" \
    "requests>=2.31.0" \
    "pyyaml>=6.0" \
    "socksio" \
    "httpx[socks]" \
    "prometheus_client"

RUN mkdir -p /app /config
WORKDIR /app

COPY discovery.py       /app/discovery.py
COPY mlflow_callback.py /app/mlflow_callback.py
COPY entrypoint.sh      /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh

# Variabili d'ambiente con default ragionevoli
ENV SPARK_URL="http://host.docker.internal:8000/v1/models" \
    VIRTUAL_MODEL_NAME="custom/model" \
    LITELLM_CONFIG_PATH="/config/config.yaml" \
    LITELLM_API_KEY="ollama" \
    POLL_INTERVAL="10" \
    BOOT_TIMEOUT="60" \
    DISABLE_DISCOVERY="false" \
    DEFAULT_CONTEXT_LENGTH="262144" \
    LITELLM_CALLBACKS="" \
    ENABLE_LANGFUSE="" \
    ENABLE_MLFLOW="" \
    ENABLE_PROMETHEUS="true" \
    MLFLOW_TRACKING_URI="" \
    MLFLOW_EXPERIMENT_NAME="" \
    MLFLOW_TRACKING_USERNAME="" \
    MLFLOW_TRACKING_PASSWORD="" \
    MLFLOW_TRACKING_TOKEN="" \
    MLFLOW_TRACKING_INSECURE_TLS="false" \
    INPUT_COST_PER_1M_TOKENS="" \
    OUTPUT_COST_PER_1M_TOKENS=""

EXPOSE 4000

HEALTHCHECK --interval=30s --timeout=5s --start-period=90s --retries=3 \
  CMD curl -sf http://localhost:4000/health/readiness || exit 1

ENTRYPOINT ["/app/entrypoint.sh"]