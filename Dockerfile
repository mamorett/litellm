FROM python:3.11-slim

ARG LITELLM_VERSION=1.82.3

# Dipendenze di sistema
RUN apt-get update && apt-get install -y --no-install-recommends \
        git build-essential curl \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir --upgrade pip

# Dipendenze Python
# Nota: langfuse 3.x ha breaking changes — teniamo 2.x per compatibilità con LiteLLM 1.x
RUN pip install --no-cache-dir \
    "langfuse>=2.0.0,<3.0.0" \
    "litellm[proxy]==${LITELLM_VERSION}" \
    "requests>=2.31.0" \
    "pyyaml>=6.0" \
    "socksio" \
    "httpx[socks]" \
    "prometheus_client"

RUN mkdir -p /app /config
WORKDIR /app

COPY discovery.py   /app/discovery.py
COPY entrypoint.sh  /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh

# Variabili d'ambiente con default ragionevoli
ENV SPARK_URL="http://host.docker.internal:8000/v1/models" \
    VIRTUAL_MODEL_NAME="custom/model" \
    LITELLM_CONFIG_PATH="/config/config.yaml" \
    LITELLM_API_KEY="ollama" \
    POLL_INTERVAL="10" \
    BOOT_TIMEOUT="60" \
    DISABLE_DISCOVERY="false"

EXPOSE 4000

ENTRYPOINT ["/app/entrypoint.sh"]