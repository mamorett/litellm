FROM python:3.11-slim

# 1. Usa SOLO i numeri. Niente "-stable".
# 2. Usa una versione che esiste davvero (attualmente siamo alla 1.82.3)
ARG LITELLM_VERSION=1.82.3

# Installiamo git e dipendenze sistema
RUN apt-get update && apt-get install -y --no-install-recommends \
    git build-essential curl \
    && rm -rf /var/lib/apt/lists/*

# Aggiorniamo pip e installiamo LiteLLM con dipendenze SOCKS in un unico layer
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir "litellm[proxy]==${LITELLM_VERSION}" socksio "httpx[socks]"

# Entrypoint
ENTRYPOINT ["litellm"]
CMD ["--port", "4000", "--host", "0.0.0.0", "--config", "/config/config.yaml"]