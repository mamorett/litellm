#!/bin/bash
# entrypoint.sh — Avvia discovery + LiteLLM in modo robusto
# Il discovery gira in background; LiteLLM diventa PID 1 via exec.
# Se il discovery muore inaspettatamente, viene riavviato.

set -euo pipefail

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] [Entrypoint] $*"
}

# ---------------------------------------------------------------------------
# Assicurati che la directory del config esista
# ---------------------------------------------------------------------------
CONFIG_DIR=$(dirname "$LITELLM_CONFIG_PATH")
mkdir -p "$CONFIG_DIR"

# ---------------------------------------------------------------------------
# Se discovery è disabilitato, avvia direttamente LiteLLM
# ---------------------------------------------------------------------------
if [ "${DISABLE_DISCOVERY:-false}" = "true" ]; then
    log "Discovery disabilitato (DISABLE_DISCOVERY=true)."
    if [ ! -f "$LITELLM_CONFIG_PATH" ]; then
        log "ATTENZIONE: $LITELLM_CONFIG_PATH non trovato. LiteLLM potrebbe fallire."
    fi
    log "Avvio LiteLLM..."
    exec litellm --config "$LITELLM_CONFIG_PATH" --port 4000 --host 0.0.0.0
fi

# ---------------------------------------------------------------------------
# Funzione per avviare il discovery in background e salvare il PID
# ---------------------------------------------------------------------------
DISCOVERY_PID=""

start_discovery() {
    python -u /app/discovery.py &
    DISCOVERY_PID=$!
    log "Discovery avviato (PID=$DISCOVERY_PID)"
}

# ---------------------------------------------------------------------------
# Cleanup: ferma il discovery quando l'entrypoint esce
# ---------------------------------------------------------------------------
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

# ---------------------------------------------------------------------------
# Avvia il discovery
# ---------------------------------------------------------------------------
log "Avvio script di discovery..."
start_discovery

# ---------------------------------------------------------------------------
# Aspetta che il config venga generato (max BOOT_TIMEOUT secondi)
# ---------------------------------------------------------------------------
BOOT_TIMEOUT="${BOOT_TIMEOUT:-60}"
log "In attesa del primo config ($LITELLM_CONFIG_PATH), timeout=${BOOT_TIMEOUT}s..."

elapsed=0
while [ ! -f "$LITELLM_CONFIG_PATH" ]; do
    sleep 1
    elapsed=$((elapsed + 1))

    # Controlla che il discovery sia ancora vivo
    if ! kill -0 "$DISCOVERY_PID" 2>/dev/null; then
        log "ERRORE: Il discovery è morto prima di generare il config. Uscita."
        exit 1
    fi

    if [ "$elapsed" -ge "$BOOT_TIMEOUT" ]; then
        log "ERRORE: Timeout di ${BOOT_TIMEOUT}s raggiunto senza config. Il backend è raggiungibile?"
        log "  SPARK_URL=$SPARK_URL"
        exit 1
    fi
done

log "Config trovato dopo ${elapsed}s."

# ---------------------------------------------------------------------------
# Avvia LiteLLM
# Usiamo un wrapper invece di 'exec' diretto così il trap EXIT viene eseguito
# quando LiteLLM termina, garantendo la pulizia del discovery.
# ---------------------------------------------------------------------------
log "Avvio LiteLLM Proxy (porta 4000)..."

# Avvia LiteLLM in foreground ma NON con exec, così il trap funziona
litellm --config "$LITELLM_CONFIG_PATH" --port 4000 --host 0.0.0.0 &
LITELLM_PID=$!
log "LiteLLM avviato (PID=$LITELLM_PID)"

# ---------------------------------------------------------------------------
# Watchdog: monitora entrambi i processi
# Se uno muore, ferma l'altro e esci con errore
# ---------------------------------------------------------------------------
while true; do
    sleep 5

    # Controlla LiteLLM
    if ! kill -0 "$LITELLM_PID" 2>/dev/null; then
        log "ERRORE: LiteLLM (PID=$LITELLM_PID) è morto inaspettatamente."
        exit 1
    fi

    # Controlla discovery — se muore, riavvialo
    if ! kill -0 "$DISCOVERY_PID" 2>/dev/null; then
        log "WARN: Discovery (PID=$DISCOVERY_PID) è morto. Riavvio..."
        start_discovery
    fi
done