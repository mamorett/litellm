FROM python:3.11-slim

# 1. Versione LiteLLM fissata
ARG LITELLM_VERSION=1.82.3

# Installiamo dipendenze sistema
RUN apt-get update && apt-get install -y --no-install-recommends \
    git build-essential curl \
    && rm -rf /var/lib/apt/lists/*

# Aggiorniamo pip
RUN pip install --no-cache-dir --upgrade pip

# Installazione librerie per LiteLLM e lo script di discovery
RUN pip install --no-cache-dir \
    "langfuse>=2.0.0,<3.0.0" \
    "litellm[proxy]==${LITELLM_VERSION}" \
    "requests" \
    "pyyaml" \
    socksio \
    "httpx[socks]" \
    prometheus_client

# Creiamo le directory necessarie
RUN mkdir -p /app /config
WORKDIR /app

# Copiamo gli script (li definiremo sotto)
COPY discovery.py /app/discovery.py
COPY entrypoint.sh /app/entrypoint.sh

# Permessi di esecuzione
RUN chmod +x /app/entrypoint.sh

# Variabili d'ambiente di default (sovrascrivibili a runtime)
ENV SPARK_URL="http://host.docker.internal:8000/v1/models" \
    VIRTUAL_MODEL_NAME="trithemius/artemis" \
    LITELLM_CONFIG_PATH="/config/config.yaml" \
    POLL_INTERVAL=10

# Usiamo lo script come entrypoint
ENTRYPOINT ["/app/entrypoint.sh"]