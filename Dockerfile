FROM python:3.11-slim

# 1. Usa SOLO i numeri. Niente "-stable".
ARG LITELLM_VERSION=1.82.3

# Installiamo git e dipendenze sistema
RUN apt-get update && apt-get install -y --no-install-recommends \
    git build-essential curl \
    && rm -rf /var/lib/apt/lists/*

# Aggiorniamo pip e installiamo le dipendenze
# FIX: Invertiamo l'ordine e forziamo Langfuse >= 2.0.0 per supportare 'sdk_integration'
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir \
    "langfuse>=2.5.0" \
    "litellm[proxy]==${LITELLM_VERSION}" \
    socksio \
    "httpx[socks]" \
    prometheus_client

# Entrypoint
ENTRYPOINT ["litellm"]
CMD ["--port", "4000", "--host", "0.0.0.0", "--config", "/config/config.yaml"]