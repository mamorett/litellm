FROM python:3.11-slim

# Argomento build-time per gestire la versione
ARG LITELLM_VERSION=1.79.1-stable

# Installiamo git e dipendenze di sistema minime
RUN apt-get update && apt-get install -y --no-install-recommends \
    git build-essential curl \
    && rm -rf /var/lib/apt/lists/*

# Installiamo LiteLLM
RUN pip install --no-cache-dir "litellm[proxy]==${LITELLM_VERSION}"

# Entrypoint standard
ENTRYPOINT ["litellm"]
CMD ["--port", "4000", "--host", "0.0.0.0", "--config", "/config/config.yaml"]