#!/bin/bash
# entrypoint.sh — Avvia discovery + LiteLLM in modo robusto.
# LiteLLM non viene MAI bloccato dal backend non disponibile:
# se il backend è giù al boot, si usa un config di fallback
# e il discovery aggiornerà il modello appena torna online.

set -euo pipefail

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] [Entrypoint] $*"
}

CONFIG_DIR=$(dirname "$LITELLM_CONFIG_PATH")
mkdir -p "$CONFIG_DIR"

if [ "${DISABLE_DISCOVERY:-false}" = "true" ]; then
    log "Discovery disabilitato (DISABLE_DISCOVERY=true)."
    if [ ! -f "$LITELLM_CONFIG_PATH" ]; then
        log "ATTENZIONE: $LITELLM_CONFIG_PATH non trovato. LiteLLM potrebbe fallire."
    fi
    log "Avvio LiteLLM..."
    exec litellm --config "$LITELLM_CONFIG_PATH" --port 4000 --host 0.0.0.0
fi

DISCOVERY_PID=""
DISCOVERY_RESTARTS=0
MAX_DISCOVERY_RESTARTS=10
DISCOVERY_BACKOFF=5

start_discovery() {
    if [ "$DISCOVERY_RESTARTS" -ge "$MAX_DISCOVERY_RESTARTS" ]; then
        log "ERROR: Discovery restarted $DISCOVERY_RESTARTS times. Giving up."
        exit 1
    fi
    DISCOVERY_RESTARTS=$((DISCOVERY_RESTARTS + 1))
    python -u /app/discovery.py &
    DISCOVERY_PID=$!
    log "Discovery avviato (PID=$DISCOVERY_PID, restart #$DISCOVERY_RESTARTS)"
}

cleanup() {
    log "Segnale di arresto ricevuto."
    if [ -n "$DISCOVERY_PID" ] && kill -0 "$DISCOVERY_PID" 2>/dev/null; then
        log "Fermo il discovery (PID=$DISCOVERY_PID)..."
        kill -TERM "$DISCOVERY_PID" 2>/dev/null || true
        wait "$DISCOVERY_PID" 2>/dev/null || true
    fi
    log "Uscita."
}
trap cleanup EXIT TERM INT

log "Avvio script di discovery..."
start_discovery

# ---------------------------------------------------------------------------
# Assicurati che esista un config prima di avviare LiteLLM.
#
# 1. Config già esistente (avvio precedente) → usalo subito.
# 2. Discovery genera il config entro BOOT_TIMEOUT → ottimo.
# 3. Timeout scaduto → config di fallback, LiteLLM parte comunque.
#    Il discovery aggiornerà il modello via API appena il backend torna su.
#    Niente più restart loop per backend temporaneamente giù.
# ---------------------------------------------------------------------------
BOOT_TIMEOUT="${BOOT_TIMEOUT:-60}"

if [ -f "$LITELLM_CONFIG_PATH" ]; then
    log "Config esistente trovato, avvio immediato di LiteLLM."
else
    log "Nessun config trovato. Attendo il discovery (max ${BOOT_TIMEOUT}s)..."
    elapsed=0
    while [ ! -f "$LITELLM_CONFIG_PATH" ]; do
        sleep 1
        elapsed=$((elapsed + 1))

        if ! kill -0 "$DISCOVERY_PID" 2>/dev/null; then
            log "WARN: Il discovery è morto prematuramente, lo riavvio..."
            start_discovery
        fi

        if [ "$elapsed" -ge "$BOOT_TIMEOUT" ]; then
            log "WARN: Backend non raggiungibile dopo ${BOOT_TIMEOUT}s."
            log "Scrivo config di fallback — LiteLLM parte comunque."
            FALLBACK_API_BASE="${SPARK_URL%/models}"
            cat > "$LITELLM_CONFIG_PATH" <<EOF
model_list:
  - model_name: ${VIRTUAL_MODEL_NAME}
    litellm_params:
      model: openai/placeholder
      api_base: ${FALLBACK_API_BASE}
      api_key: ${LITELLM_API_KEY:-ollama}
litellm_settings:
  drop_params: true
  num_retries: 1
  request_timeout: 600
  success_callback:
    - langfuse
  failure_callback:
    - langfuse
EOF
            log "Config di fallback scritto. Il discovery sostituirà 'placeholder' appena il backend torna online."
            break
        fi
    done
fi

log "Config pronto, avvio LiteLLM Proxy (porta 4000)..."

litellm --config "$LITELLM_CONFIG_PATH" --port 4000 --host 0.0.0.0 &
LITELLM_PID=$!
echo "$LITELLM_PID" > /tmp/litellm.pid
log "LiteLLM avviato (PID=$LITELLM_PID)"

# Watchdog: LiteLLM muore → exit; Discovery muore → riavvia
while true; do
    sleep 5

    if ! kill -0 "$LITELLM_PID" 2>/dev/null; then
        if [ -f /tmp/litellm.restart-requested ]; then
            log "INFO: Restart controllato di LiteLLM richiesto dal discovery (cambio modello)."
            rm -f /tmp/litellm.restart-requested
            litellm --config "$LITELLM_CONFIG_PATH" --port 4000 --host 0.0.0.0 &
            LITELLM_PID=$!
            echo "$LITELLM_PID" > /tmp/litellm.pid
            log "LiteLLM riavviato con config aggiornato (PID=$LITELLM_PID)"
        else
            log "ERRORE: LiteLLM (PID=$LITELLM_PID) è morto inaspettatamente. Uscita."
            exit 1
        fi
    fi

    if ! kill -0 "$DISCOVERY_PID" 2>/dev/null; then
        log "WARN: Discovery (PID=$DISCOVERY_PID) è morto. Waiting ${DISCOVERY_BACKOFF}s before restart..."
        sleep "$DISCOVERY_BACKOFF"
        DISCOVERY_BACKOFF=$((DISCOVERY_BACKOFF * 2))
        [ "$DISCOVERY_BACKOFF" -gt 120 ] && DISCOVERY_BACKOFF=120
        start_discovery
    fi
done