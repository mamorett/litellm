#!/usr/bin/env python3
"""
LiteLLM Auto-Discovery Script
Monitora un endpoint OpenAI-compatibile e aggiorna il modello in LiteLLM
a caldo via API admin (DELETE + POST /model) quando il modello cambia.
Il config su disco viene scritto solo al boot (per il primo avvio).
"""

import os
import sys
import time
import signal
import tempfile
import requests
import yaml
from functools import wraps
from datetime import datetime
from urllib.parse import urlparse
from prometheus_client import start_http_server, Counter, Gauge

# ---------------------------------------------------------------------------
# Configurazione da ENV
# ---------------------------------------------------------------------------
SPARK_URL           = os.environ["SPARK_URL"]           # es. http://host:8000/v1/models
VIRTUAL_MODEL       = os.environ["VIRTUAL_MODEL_NAME"]  # es. trithemius/artemis
CONFIG_PATH         = os.environ["LITELLM_CONFIG_PATH"]
POLL_INTERVAL       = int(os.getenv("POLL_INTERVAL", 10))
BACKEND_API_KEY     = os.getenv("LITELLM_API_KEY", "ollama")
# Indirizzo interno del proxy LiteLLM (stesso container)
LITELLM_PROXY_URL   = os.getenv("LITELLM_PROXY_URL", "http://localhost:4000")
# Master key per le API admin di LiteLLM (opzionale ma consigliata)
LITELLM_MASTER_KEY  = os.getenv("LITELLM_MASTER_KEY", "")

def _derive_api_base(models_url: str) -> str:
    """http://host:8000/v1/models  ->  http://host:8000/v1"""
    parsed = urlparse(models_url)
    base_path = parsed.path.rstrip("/")
    if base_path.endswith("/models"):
        base_path = base_path[: -len("/models")]
    return parsed._replace(path=base_path, query="", fragment="").geturl()

API_BASE = _derive_api_base(SPARK_URL)

# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------
backend_errors  = Counter("discovery_backend_errors_total", "Backend fetch failures")
live_updates_ok = Counter("discovery_live_updates_ok_total", "Successful live model swaps")
live_updates_fail = Counter("discovery_live_updates_fail_total", "Failed live model swaps")
current_model_gauge = Gauge("discovery_model_change_timestamp_seconds",
                             "Unix timestamp of last model change")

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
def log(level: str, msg: str):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] [Discovery] [{level}] {msg}", flush=True)

def info(msg):  log("INFO ", msg)
def warn(msg):  log("WARN ", msg)
def error(msg): log("ERROR", msg)

# ---------------------------------------------------------------------------
# Scrittura atomica del config su disco (usata solo al boot)
# ---------------------------------------------------------------------------
def _build_config(real_model_name: str) -> dict:
    return {
        "model_list": [
            {
                "model_name": VIRTUAL_MODEL,
                "litellm_params": {
                    "model": f"openai/{real_model_name}",
                    "api_base": API_BASE,
                    "api_key": BACKEND_API_KEY,
                },
                "model_info": {
                    "base_model": real_model_name,
                },
            }
        ],
        "litellm_settings": {
            "success_callback": ["langfuse"],
            "failure_callback": ["langfuse"],
            "drop_params": True,
            "num_retries": 1,
            "request_timeout": 600,
            "stream_timeout": 600,
        },
    }

def write_config_atomic(real_model_name: str):
    config = _build_config(real_model_name)
    config_dir = os.path.dirname(os.path.abspath(CONFIG_PATH))
    os.makedirs(config_dir, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=config_dir, suffix=".yaml.tmp")
    try:
        with os.fdopen(fd, "w") as f:
            yaml.dump(config, f, default_flow_style=False, allow_unicode=True)
        os.replace(tmp_path, CONFIG_PATH)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise

def _disk_config_is_placeholder() -> bool:
    try:
        with open(CONFIG_PATH) as f:
            return "placeholder" in f.read()
    except Exception:
        return False

# ---------------------------------------------------------------------------
# Aggiornamento a caldo via API admin LiteLLM
# LiteLLM espone DELETE /model e POST /model per gestire i modelli a runtime.
# Documentazione: https://docs.litellm.ai/docs/proxy/model_management
# ---------------------------------------------------------------------------
def _admin_headers() -> dict:
    h = {"Content-Type": "application/json"}
    if LITELLM_MASTER_KEY:
        h["Authorization"] = f"Bearer {LITELLM_MASTER_KEY}"
    return h

def _litellm_ready() -> bool:
    """Controlla se LiteLLM è già up e risponde."""
    try:
        r = requests.get(f"{LITELLM_PROXY_URL}/health/readiness", timeout=2)
        return r.status_code == 200
    except Exception:
        return False

def _get_model_id_from_proxy() -> str | None:
    """
    Recupera l'model_id interno di LiteLLM per VIRTUAL_MODEL,
    necessario per la DELETE /model.
    """
    try:
        r = requests.get(
            f"{LITELLM_PROXY_URL}/model/info",
            headers=_admin_headers(),
            timeout=5,
        )
        if r.status_code != 200:
            return None
        data = r.json()
        for m in data.get("data", []):
            if m.get("model_name") == VIRTUAL_MODEL:
                return m.get("model_info", {}).get("id")
    except Exception as e:
        warn(f"Impossibile ottenere model_info dal proxy: {e}")
    return None

def _verify_model_active(expected_model: str, retries: int = 3) -> bool:
    """Confirm LiteLLM's /model/info now reflects expected_model."""
    for _ in range(retries):
        try:
            r = requests.get(f"{LITELLM_PROXY_URL}/model/info",
                             headers=_admin_headers(), timeout=5)
            if r.status_code == 200:
                for m in r.json().get("data", []):
                    if m.get("model_name") == VIRTUAL_MODEL:
                        actual = m.get("litellm_params", {}).get("model", "")
                        if actual == f"openai/{expected_model}":
                            return True
        except Exception:
            pass
        time.sleep(2)
    return False

def _retry(max_attempts: int = 4, base_delay: float = 2.0):
    """Decorator: retry on exception or False return value."""
    def decorator(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            for attempt in range(1, max_attempts + 1):
                try:
                    result = fn(*args, **kwargs)
                    if result is not False:
                        return result
                except Exception as e:
                    result = e
                delay = base_delay * (2 ** (attempt - 1))
                warn(f"{fn.__name__} attempt {attempt}/{max_attempts} failed ({result}), retry in {delay:.0f}s")
                time.sleep(delay)
            error(f"{fn.__name__} failed after {max_attempts} attempts")
            return False
        return wrapper
    return decorator

@_retry(max_attempts=4, base_delay=2.0)
def update_model_live(new_real_model: str) -> bool:
    """
    Aggiorna il modello in LiteLLM a caldo:
    1. DELETE /model  (rimuove il vecchio)
    2. POST   /model  (aggiunge il nuovo)
    Ritorna True se successo.
    """
    headers = _admin_headers()

    # 1. Trova l'ID interno e cancella il vecchio modello
    model_id = _get_model_id_from_proxy()
    if model_id:
        try:
            r = requests.delete(
                f"{LITELLM_PROXY_URL}/model/delete",
                headers=headers,
                json={"id": model_id},
                timeout=5,
            )
            if r.status_code not in (200, 204):
                warn(f"DELETE /model ha risposto {r.status_code}: {r.text[:200]}")
        except Exception as e:
            warn(f"Errore DELETE /model: {e}")
    else:
        info("Model ID non trovato nel proxy, salto la DELETE.")

    # 2. Registra il nuovo modello
    payload = {
        "model_name": VIRTUAL_MODEL,
        "litellm_params": {
            "model": f"openai/{new_real_model}",
            "api_base": API_BASE,
            "api_key": BACKEND_API_KEY,
        },
        "model_info": {
            "base_model": new_real_model,
        },
    }
    try:
        r = requests.post(
            f"{LITELLM_PROXY_URL}/model/new",
            headers=headers,
            json=payload,
            timeout=5,
        )
        if r.status_code not in (200, 201):
            warn(f"POST /model/new ha risposto {r.status_code}: {r.text[:300]}")
            return False
    except Exception as e:
        warn(f"Errore POST /model/new: {e}")
        return False
        
    # 3. Verifica
    if _verify_model_active(new_real_model):
        return True
    else:
        error("Verifica fallita: il modello non risulta aggiornato nel proxy.")
        return False

# ---------------------------------------------------------------------------
# Graceful shutdown
# ---------------------------------------------------------------------------
_shutdown = False

def _handle_signal(signum, frame):
    global _shutdown
    info(f"Segnale {signum} ricevuto, uscita in corso...")
    _shutdown = True

signal.signal(signal.SIGTERM, _handle_signal)
signal.signal(signal.SIGINT, _handle_signal)

# ---------------------------------------------------------------------------
# Fetch modello dal backend Spark/Ollama
# ---------------------------------------------------------------------------
def fetch_current_model() -> str | None:
    try:
        resp = requests.get(SPARK_URL, timeout=5)
        resp.raise_for_status()
        data = resp.json()
        models = data.get("data", [])
        if not models:
            warn("La risposta non contiene modelli.")
            return None
        return models[0]["id"]
    except requests.exceptions.ConnectionError:
        warn("Backend non raggiungibile (ConnectionError).")
    except requests.exceptions.Timeout:
        warn("Backend non risponde (Timeout).")
    except (KeyError, IndexError, ValueError) as e:
        error(f"Risposta malformata: {e}")
    except Exception as e:
        error(f"Errore inatteso: {e}")
    return None

# ---------------------------------------------------------------------------
# Loop principale
# ---------------------------------------------------------------------------
def main():
    info(f"Avvio monitoraggio su {SPARK_URL}")
    info(f"api_base derivata:  {API_BASE}")
    info(f"Modello virtuale:   {VIRTUAL_MODEL}")
    info(f"Config path:        {CONFIG_PATH}")
    info(f"LiteLLM proxy URL:  {LITELLM_PROXY_URL}")
    info(f"Poll interval:      {POLL_INTERVAL}s")

    # Start Prometheus metrics server
    start_http_server(9100)
    info("Prometheus metrics esposte sulla porta 9100")

    last_model: str | None = None
    consecutive_errors = 0
    litellm_was_ready = False
    
    disk_config_is_stale = _disk_config_is_placeholder()
    if disk_config_is_stale:
        warn("Config su disco contiene 'placeholder' — verrà riscritto al primo discovery.")

    while not _shutdown:
        current_model = fetch_current_model()

        if current_model is not None:
            consecutive_errors = 0

            if current_model != last_model or disk_config_is_stale:
                if current_model != last_model:
                    info(f"Modello cambiato/rilevato: {last_model!r} -> {current_model!r}")
                
                # Sempre aggiorna il config su disco (serve al prossimo restart)
                disk_ok = False
                try:
                    write_config_atomic(current_model)
                    disk_ok = True
                    disk_config_is_stale = False
                    info("Config su disco aggiornato.")
                except Exception as e:
                    error(f"Impossibile scrivere il config: {e}")

                litellm_ready = _litellm_ready()
                if litellm_ready:
                    if not litellm_was_ready:
                        litellm_was_ready = True
                        info("LiteLLM è online, userò l'API admin per aggiornamenti live.")
                        
                    if disk_ok:
                        if update_model_live(current_model):
                            info(f"✅ Modello aggiornato live: {VIRTUAL_MODEL!r} -> {current_model!r}")
                            last_model = current_model
                            live_updates_ok.inc()
                            current_model_gauge.set(time.time())
                        else:
                            warn("Aggiornamento live fallito. Verrà ritentato al prossimo ciclo.")
                            live_updates_fail.inc()
                            # Non aggiorniamo last_model così riprova al prossimo ciclo
                    else:
                        warn("Config non scritto — verrà ritentato al prossimo ciclo.")
                else:
                    info("LiteLLM non ancora online, il modello sarà caricato all'avvio.")
                    if disk_ok:
                        last_model = current_model
                        current_model_gauge.set(time.time())

        else:
            consecutive_errors += 1
            backend_errors.inc()
            if last_model is None and consecutive_errors == 1:
                warn("Backend non disponibile al boot. Riprovo...")
            elif consecutive_errors % 6 == 0:
                warn(f"Backend irraggiungibile da {consecutive_errors * POLL_INTERVAL}s")

        for _ in range(POLL_INTERVAL):
            if _shutdown:
                break
            time.sleep(1)

    info("Discovery terminato.")

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        error(f"Crash fatale: {e}")
        sys.exit(1)