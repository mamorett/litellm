#!/usr/bin/env python3
"""
LiteLLM Auto-Discovery Script
Monitora un endpoint OpenAI-compatibile e aggiorna il config di LiteLLM
quando il modello cambia. Usa scrittura atomica per evitare race condition.
"""

import os
import sys
import time
import signal
import tempfile
import requests
import yaml
from datetime import datetime
from urllib.parse import urlparse, urljoin

# ---------------------------------------------------------------------------
# Configurazione da ENV
# ---------------------------------------------------------------------------
SPARK_URL        = os.environ["SPARK_URL"]          # es. http://host:8000/v1/models
VIRTUAL_MODEL    = os.environ["VIRTUAL_MODEL_NAME"] # es. custom/model
CONFIG_PATH      = os.environ["LITELLM_CONFIG_PATH"]
POLL_INTERVAL    = int(os.getenv("POLL_INTERVAL", 10))
LITELLM_API_KEY  = os.getenv("LITELLM_API_KEY", "ollama")

# Ricava la api_base togliendo il path /models dall'URL
# es. http://host:8000/v1/models  ->  http://host:8000/v1
def _derive_api_base(models_url: str) -> str:
    parsed = urlparse(models_url)
    # Rimuove l'ultimo segmento del path (/models)
    base_path = parsed.path.rstrip("/")
    if base_path.endswith("/models"):
        base_path = base_path[: -len("/models")]
    return parsed._replace(path=base_path, query="", fragment="").geturl()

API_BASE = _derive_api_base(SPARK_URL)

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
# Scrittura atomica del config
# Scrive su un file temporaneo nella stessa directory, poi fa rename.
# rename(2) è atomico su POSIX: LiteLLM non vedrà mai un file parziale.
# ---------------------------------------------------------------------------
def write_config_atomic(real_model_name: str):
    config = {
        "model_list": [
            {
                "model_name": VIRTUAL_MODEL,
                "litellm_params": {
                    "model": f"openai/{real_model_name}",
                    "api_base": API_BASE,
                    "api_key": LITELLM_API_KEY,
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
            # Aumenta la robustezza: non bloccare su modelli che cambiano
            "num_retries": 2,
            "request_timeout": 30,
        },
        "general_settings": {
            # LiteLLM rileva le modifiche al config se questo è impostato
            "disable_spend_logs": False,
        },
    }

    config_dir = os.path.dirname(os.path.abspath(CONFIG_PATH))
    os.makedirs(config_dir, exist_ok=True)

    # Scrivi su tmp nella stessa partizione, poi rinomina (atomico)
    fd, tmp_path = tempfile.mkstemp(dir=config_dir, suffix=".yaml.tmp")
    try:
        with os.fdopen(fd, "w") as f:
            yaml.dump(config, f, default_flow_style=False, allow_unicode=True)
        os.replace(tmp_path, CONFIG_PATH)   # atomico su POSIX
    except Exception:
        # Cleanup del tmp in caso di errore
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise

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
# Loop principale
# ---------------------------------------------------------------------------
def fetch_current_model() -> str | None:
    """Ritorna l'ID del primo modello, o None in caso di errore."""
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


def main():
    info(f"Avvio monitoraggio su {SPARK_URL}")
    info(f"api_base derivata: {API_BASE}")
    info(f"Modello virtuale: {VIRTUAL_MODEL}")
    info(f"Config path: {CONFIG_PATH}")
    info(f"Poll interval: {POLL_INTERVAL}s")

    last_model: str | None = None
    consecutive_errors = 0

    while not _shutdown:
        current_model = fetch_current_model()

        if current_model is not None:
            consecutive_errors = 0
            if current_model != last_model:
                try:
                    write_config_atomic(current_model)
                    info(f"✅ Config aggiornato: {VIRTUAL_MODEL!r} -> {current_model!r}")
                    last_model = current_model
                except Exception as e:
                    error(f"Impossibile scrivere il config: {e}")
        else:
            consecutive_errors += 1
            if last_model is None and consecutive_errors == 1:
                # Primo avvio, backend non ancora disponibile: non scriviamo nulla.
                # L'entrypoint aspetterà il file prima di avviare LiteLLM.
                warn("Backend non disponibile al boot. Riprovo...")
            elif consecutive_errors % 6 == 0:   # log ogni ~60s
                warn(f"Backend irraggiungibile da {consecutive_errors * POLL_INTERVAL}s")

        # Sleep interrompibile da segnale
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