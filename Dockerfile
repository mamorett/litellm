FROM python:3.11-slim

# 1. Versione LiteLLM fissata
ARG LITELLM_VERSION=1.82.3

# Installiamo dipendenze sistema
RUN apt-get update && apt-get install -y --no-install-recommends \
    git build-essential curl \
    && rm -rf /var/lib/apt/lists/*

# Aggiorniamo pip
RUN pip install --no-cache-dir --upgrade pip

# --- INSTALLAZIONE CERTIFICATA ---
# Forziamo Langfuse alla serie 2.x (es. 2.57.0) 
# La v2.x supporta il parametro 'sdk_integration' richiesto da LiteLLM 1.82.x
RUN pip install --no-cache-dir \
    "langfuse>=2.0.0,<3.0.0" \
    "litellm[proxy]==${LITELLM_VERSION}" \
    socksio \
    "httpx[socks]" \
    prometheus_client

# Entrypoint
ENTRYPOINT ["litellm"]
CMD ["--port", "4000", "--host", "0.0.0.0", "--config", "/config/config.yaml"]