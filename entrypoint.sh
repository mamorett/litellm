#!/bin/bash
set -e

echo "[Entrypoint] Avvio del sistema multi-processo..."

# 1. Assicuriamoci che la directory del config esista
mkdir -p $(dirname "$LITELLM_CONFIG_PATH")

# 2. Lanciamo lo script di discovery in background
# Usiamo -u per avere i log di python non bufferizzati
python -u /app/discovery.py &
DISCOVERY_PID=$!

# 3. Aspettiamo che il primo file config.yaml venga generato
echo "[Entrypoint] In attesa del primo file di configurazione..."
MAX_RETRIES=30
COUNT=0
while [ ! -f "$LITELLM_CONFIG_PATH" ]; do
    sleep 1
    COUNT=$((COUNT+1))
    if [ $COUNT -ge $MAX_RETRIES ]; then
        echo "[Entrypoint] Errore: Il discovery non ha generato il config in tempo."
        exit 1
    fi
done

echo "[Entrypoint] Configurazione pronta. Avvio LiteLLM Proxy..."

# 4. Lanciamo LiteLLM
# Usiamo 'exec' così LiteLLM diventa il PID 1 e riceve correttamente i segnali di stop (SIGTERM)
exec litellm --config "$LITELLM_CONFIG_PATH" --port 4000 --host 0.0.0.0