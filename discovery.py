#!/usr/bin/env python3
"""
LiteLLM Auto-Discovery Script
Monitora un endpoint OpenAI-compatibile e aggiorna il modello in LiteLLM
quando il modello cambia. Supporta due strategie:
  1. Live hot-swap via API admin (richiede DB Prisma collegato a LiteLLM).
  2. Graceful restart: scrive il config su disco e richiede il restart di
     LiteLLM via file-flag / SIGTERM — usato automaticamente quando le
     API admin non sono disponibili (nessun DB configurato).

Context length detection (cascata):
  - vLLM:      campo max_model_len nel record /v1/models
  - SGLang:    campo context_length nel record /v1/models
  - llama.cpp: GET /props o /v1/props -> n_ctx
  - Ollama:    POST /api/show -> model_info.context_length
  - Fallback:  env DEFAULT_CONTEXT_LENGTH (default 262144)
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
# Context length di fallback quando nessun engine la espone
DEFAULT_CONTEXT     = int(os.getenv("DEFAULT_CONTEXT_LENGTH", "262144"))
# Configurazione opzionale dei callback (possono essere usati singolarmente o in qualsiasi combinazione)
LITELLM_CALLBACKS_RAW  = os.getenv("LITELLM_CALLBACKS", "").strip()
ENABLE_LANGFUSE_RAW    = os.getenv("ENABLE_LANGFUSE", "").strip().lower()
ENABLE_MLFLOW_RAW      = os.getenv("ENABLE_MLFLOW", "").strip().lower()
ENABLE_PROMETHEUS_RAW  = os.getenv("ENABLE_PROMETHEUS", "true").strip().lower()
# Variabili Langfuse (abilitato automaticamente se entrambe le chiavi sono presenti)
LANGFUSE_PUBLIC_KEY    = os.getenv("LANGFUSE_PUBLIC_KEY", "").strip()
LANGFUSE_SECRET_KEY    = os.getenv("LANGFUSE_SECRET_KEY", "").strip()
# Variabili MLflow (abilitato automaticamente se MLFLOW_TRACKING_URI è impostata)
MLFLOW_TRACKING_URI          = os.getenv("MLFLOW_TRACKING_URI", "").strip()
MLFLOW_EXPERIMENT_NAME       = os.getenv("MLFLOW_EXPERIMENT_NAME", "").strip()
MLFLOW_TRACKING_USERNAME     = os.getenv("MLFLOW_TRACKING_USERNAME", "").strip()
MLFLOW_TRACKING_PASSWORD     = os.getenv("MLFLOW_TRACKING_PASSWORD", "").strip()
MLFLOW_TRACKING_TOKEN        = os.getenv("MLFLOW_TRACKING_TOKEN", "").strip()
MLFLOW_TRACKING_INSECURE_TLS = os.getenv("MLFLOW_TRACKING_INSECURE_TLS", "false").strip().lower() or "false"
# Costi opzionali del modello (in USD per 1M token, oppure per singolo token come fallback)
INPUT_COST_PER_1M_RAW  = os.getenv("INPUT_COST_PER_1M_TOKENS", "").strip()
OUTPUT_COST_PER_1M_RAW = os.getenv("OUTPUT_COST_PER_1M_TOKENS", "").strip()
INPUT_COST_RAW         = os.getenv("INPUT_COST_PER_TOKEN", "").strip()
OUTPUT_COST_RAW        = os.getenv("OUTPUT_COST_PER_TOKEN", "").strip()

def _derive_api_base(models_url: str) -> str:
    """http://host:8000/v1/models  ->  http://host:8000/v1"""
    parsed = urlparse(models_url)
    base_path = parsed.path.rstrip("/")
    if base_path.endswith("/models"):
        base_path = base_path[: -len("/models")]
    return parsed._replace(path=base_path, query="", fragment="").geturl()

API_BASE = _derive_api_base(SPARK_URL)

# ---------------------------------------------------------------------------
# File-based handshake con entrypoint.sh per il restart controllato
# ---------------------------------------------------------------------------
_LITELLM_PID_FILE     = "/tmp/litellm.pid"
_LITELLM_RESTART_FLAG = "/tmp/litellm.restart-requested"

# ---------------------------------------------------------------------------
# Eccezione permanente: le API admin richiedono un DB che non è configurato.
# Il decorator @_retry NON deve riprovare su questo errore.
# ---------------------------------------------------------------------------
class _NoDBError(Exception):
    """LiteLLM admin API non disponibile senza database."""
    pass

# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------
backend_errors      = Counter("discovery_backend_errors_total", "Backend fetch failures")
live_updates_ok     = Counter("discovery_live_updates_ok_total", "Successful live model swaps")
live_updates_fail   = Counter("discovery_live_updates_fail_total", "Failed live model swaps")
current_model_gauge = Gauge("discovery_model_change_timestamp_seconds",
                            "Unix timestamp of last model change")
context_length_gauge = Gauge("discovery_context_length_tokens",
                             "Detected context length of the current model")

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
# Context length detection — cascata multi-engine
# ---------------------------------------------------------------------------

def _fetch_context_from_model_record(model_data: dict) -> tuple[int, str] | tuple[None, None]:
    """
    Prova i campi noti nel record /v1/models.
    - vLLM:   max_model_len
    - SGLang: context_length
    Ritorna (valore, nome_campo) oppure (None, None).
    """
    for field in ("max_model_len", "context_length"):
        val = model_data.get(field)
        if val:
            return int(val), field
    return None, None


def _fetch_context_llamacpp(api_base: str) -> int | None:
    """
    llama.cpp (llama-server) espone GET /props o /v1/props con n_ctx.
    Non fa parte del record /v1/models, quindi richiede una chiamata extra.
    """
    for path in ("/props", "/v1/props"):
        try:
            r = requests.get(f"{api_base}{path}", timeout=3)
            if r.status_code == 200:
                val = r.json().get("n_ctx")
                if val:
                    return int(val)
        except Exception:
            pass
    return None


def _fetch_context_ollama(api_base: str, model_name: str) -> int | None:
    """
    Ollama espone POST /api/show con context_length dentro model_info.
    L'endpoint è fuori dal path /v1, quindi usiamo la base senza prefisso.
    """
    # Risali alla radice del server (prima di /v1)
    parsed = urlparse(api_base)
    root = parsed._replace(path="", query="", fragment="").geturl()
    try:
        r = requests.post(
            f"{root}/api/show",
            json={"name": model_name},
            timeout=3,
        )
        if r.status_code == 200:
            val = r.json().get("model_info", {}).get("context_length")
            if val:
                return int(val)
    except Exception:
        pass
    return None


def detect_context_length(model_data: dict, model_id: str) -> int:
    """
    Determina la context length del modello corrente con cascata:
      1. Campo nel record /v1/models (vLLM: max_model_len, SGLang: context_length)
      2. llama.cpp /props
      3. Ollama /api/show
      4. DEFAULT_CONTEXT (da env)
    Logga sempre da dove proviene il valore.
    """
    # 1. Record /v1/models (nessuna request extra)
    ctx, field = _fetch_context_from_model_record(model_data)
    if ctx:
        info(f"Context length rilevata dal record /v1/models (campo '{field}'): {ctx}")
        return ctx

    # 2. llama.cpp /props
    ctx = _fetch_context_llamacpp(API_BASE)
    if ctx:
        info(f"Context length rilevata via llama.cpp /props (n_ctx): {ctx}")
        return ctx

    # 3. Ollama /api/show
    ctx = _fetch_context_ollama(API_BASE, model_id)
    if ctx:
        info(f"Context length rilevata via Ollama /api/show: {ctx}")
        return ctx

    # 4. Fallback
    warn(f"Context length non rilevata da nessun engine — uso DEFAULT_CONTEXT_LENGTH={DEFAULT_CONTEXT}")
    return DEFAULT_CONTEXT

# ---------------------------------------------------------------------------
# Helper: parsing costi modello e callback attivi
# ---------------------------------------------------------------------------
def _parse_cost(raw_per_1m: str, raw_per_token: str, label: str) -> float | None:
    """
    Converte il costo da INPUT/OUTPUT_COST_PER_1M_TOKENS (dividendo per 1_000_000)
    oppure da INPUT/OUTPUT_COST_PER_TOKEN come fallback.
    """
    if raw_per_1m:
        try:
            return float(raw_per_1m) / 1_000_000.0
        except ValueError:
            warn(f"Valore non valido per {label}_COST_PER_1M_TOKENS={raw_per_1m!r}, ignorato.")
    if raw_per_token:
        try:
            return float(raw_per_token)
        except ValueError:
            warn(f"Valore non valido per {label}_COST_PER_TOKEN={raw_per_token!r}, ignorato.")
    return None

INPUT_COST_PER_TOKEN  = _parse_cost(INPUT_COST_PER_1M_RAW, INPUT_COST_RAW, "INPUT")
OUTPUT_COST_PER_TOKEN = _parse_cost(OUTPUT_COST_PER_1M_RAW, OUTPUT_COST_RAW, "OUTPUT")


def _active_callbacks() -> list[str]:
    """
    Ritorna la lista di callback attivi (langfuse, mlflow, prometheus).
    Tutti e tre sono opzionali e combinabili liberamente:
      - LITELLM_CALLBACKS: se impostata (es. "mlflow,prometheus" o "none"), ha priorità assoluta
      - langfuse:   attivo se ENABLE_LANGFUSE=true oppure (ENABLE_LANGFUSE != false e LANGFUSE_PUBLIC_KEY + LANGFUSE_SECRET_KEY presenti)
      - mlflow:     attivo se ENABLE_MLFLOW=true oppure (ENABLE_MLFLOW != false e MLFLOW_TRACKING_URI presente)
      - prometheus: attivo se ENABLE_PROMETHEUS=true (default: true; disattivabile con ENABLE_PROMETHEUS=false)
    """
    if LITELLM_CALLBACKS_RAW:
        if LITELLM_CALLBACKS_RAW.lower() in ("none", "off", "false"):
            return []
        return [cb.strip() for cb in LITELLM_CALLBACKS_RAW.split(",") if cb.strip()]

    callbacks: list[str] = []

    langfuse_on = (
        ENABLE_LANGFUSE_RAW == "true"
        or (ENABLE_LANGFUSE_RAW != "false" and bool(LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY))
    )
    if langfuse_on:
        callbacks.append("langfuse")

    mlflow_on = (
        ENABLE_MLFLOW_RAW == "true"
        or (ENABLE_MLFLOW_RAW != "false" and bool(MLFLOW_TRACKING_URI))
    )
    if mlflow_on:
        callbacks.append("mlflow")

    prometheus_on = ENABLE_PROMETHEUS_RAW not in ("false", "0", "no", "off")
    if prometheus_on:
        callbacks.append("prometheus")

    return callbacks


def _litellm_params(real_model_name: str) -> dict:
    params: dict = {
        "model": real_model_name,         # nome puro, senza prefisso openai/
        "custom_llm_provider": "openai",  # routing verso backend OpenAI-compatibile
        "api_base": API_BASE,
        "api_key": BACKEND_API_KEY,
    }
    if INPUT_COST_PER_TOKEN is not None:
        params["input_cost_per_token"] = INPUT_COST_PER_TOKEN
    if OUTPUT_COST_PER_TOKEN is not None:
        params["output_cost_per_token"] = OUTPUT_COST_PER_TOKEN
    return params


def _model_info(real_model_name: str, max_ctx: int) -> dict:
    model_info: dict = {
        "base_model": real_model_name,
        "max_input_tokens": max_ctx,
        "max_tokens": max_ctx,
        "max_model_len": max_ctx,    # esposto in /v1/models ai client
        "context_window": max_ctx,   # letto da alcuni client (es. Open WebUI)
    }
    if INPUT_COST_PER_TOKEN is not None:
        model_info["input_cost_per_token"] = INPUT_COST_PER_TOKEN
    if OUTPUT_COST_PER_TOKEN is not None:
        model_info["output_cost_per_token"] = OUTPUT_COST_PER_TOKEN
    return model_info


# ---------------------------------------------------------------------------
# Scrittura atomica del config su disco (usata al boot e per restart controllato)
# ---------------------------------------------------------------------------
def _build_config(real_model_name: str, max_ctx: int) -> dict:
    callbacks = _active_callbacks()
    litellm_settings: dict = {
        "drop_params": True,
        "num_retries": 1,
        "request_timeout": 600,
        "stream_timeout": 600,
    }
    if callbacks:
        litellm_settings["success_callback"] = list(callbacks)
        litellm_settings["failure_callback"] = list(callbacks)
    return {
        "model_list": [
            {
                "model_name": VIRTUAL_MODEL,
                "litellm_params": _litellm_params(real_model_name),
                "model_info": _model_info(real_model_name, max_ctx),
            }
        ],
        "litellm_settings": litellm_settings,
    }

def write_config_atomic(real_model_name: str, max_ctx: int):
    config = _build_config(real_model_name, max_ctx)
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
                        if actual == expected_model:
                            return True
        except Exception:
            pass
        time.sleep(2)
    return False

def _retry(max_attempts: int = 4, base_delay: float = 2.0, no_retry_on: tuple = ()):
    """Decorator: retry on exception or False return value.
    Exceptions listed in no_retry_on are re-raised immediately without retry.
    """
    def decorator(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            for attempt in range(1, max_attempts + 1):
                try:
                    result = fn(*args, **kwargs)
                    if result is not False:
                        return result
                    raise ValueError("returned False")
                except no_retry_on:
                    raise  # permanent error — propagate immediately
                except Exception as e:
                    result = e
                delay = base_delay * (2 ** (attempt - 1))
                warn(f"{fn.__name__} attempt {attempt}/{max_attempts} failed ({result}), retry in {delay:.0f}s")
                time.sleep(delay)
            error(f"{fn.__name__} failed after {max_attempts} attempts")
            return False
        return wrapper
    return decorator

@_retry(max_attempts=4, base_delay=2.0, no_retry_on=(_NoDBError,))
def update_model_live(new_real_model: str, max_ctx: int) -> bool:
    """
    Aggiorna il modello in LiteLLM a caldo:
    1. DELETE /model  (rimuove il vecchio)
    2. POST   /model  (aggiunge il nuovo con context length corretta)
    Ritorna True se successo.
    Lancia _NoDBError se le API admin richiedono un DB non configurato.
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

    # 2. Registra il nuovo modello con context length e costi opzionali
    payload = {
        "model_name": VIRTUAL_MODEL,
        "litellm_params": _litellm_params(new_real_model),
        "model_info": _model_info(new_real_model, max_ctx),
    }
    try:
        r = requests.post(
            f"{LITELLM_PROXY_URL}/model/new",
            headers=headers,
            json=payload,
            timeout=5,
        )
        if r.status_code in (200, 201):
            pass  # success, continue to verify
        else:
            body = r.text
            # Detect permanently disabled admin API (no DB) — do not retry
            if r.status_code == 500 and "No DB Connected" in body:
                raise _NoDBError(
                    "LiteLLM admin API richiede un database Prisma. "
                    "Fallback automatico al restart controllato."
                )
            warn(f"POST /model/new ha risposto {r.status_code}: {body[:300]}")
            return False
    except _NoDBError:
        raise  # let @_retry propagate without retrying
    except Exception as e:
        warn(f"Errore POST /model/new: {e}")
        return False

    # 3. Verifica
    if _verify_model_active(new_real_model):
        return True
    else:
        error("Verifica fallita: il modello non risulta aggiornato nel proxy.")
        return False


def _request_litellm_restart() -> bool:
    """
    Richiede a entrypoint.sh di riavviare LiteLLM in modo controllato:
    - Scrive il flag /tmp/litellm.restart-requested
    - Invia SIGTERM al PID di LiteLLM
    entrypoint.sh rileva il flag, lo rimuove e riavvia LiteLLM
    (che leggerà il config aggiornato su disco).
    """
    try:
        with open(_LITELLM_PID_FILE) as f:
            pid = int(f.read().strip())
    except Exception as e:
        error(f"Impossibile leggere il PID di LiteLLM da {_LITELLM_PID_FILE}: {e}")
        return False
    try:
        # Segnala PRIMA il flag, poi il SIGTERM
        with open(_LITELLM_RESTART_FLAG, "w") as f:
            f.write("1")
        os.kill(pid, signal.SIGTERM)
        info(f"Restart di LiteLLM richiesto (PID={pid}). Il processo si riavvierà con il config aggiornato.")
        return True
    except ProcessLookupError:
        # LiteLLM è già morto — il watchdog lo riavvierà comunque
        warn(f"LiteLLM (PID={pid}) già terminato. Il watchdog provvederà al riavvio.")
        return True
    except Exception as e:
        error(f"Errore durante la richiesta di restart: {e}")
        # Rimuovi il flag per evitare restart a catena
        try:
            os.unlink(_LITELLM_RESTART_FLAG)
        except OSError:
            pass
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
# Fetch modello dal backend (vLLM / SGLang / llama.cpp / Ollama)
# Ritorna (model_id, context_length) oppure (None, None)
# ---------------------------------------------------------------------------
def fetch_current_model() -> tuple[str, int] | tuple[None, None]:
    try:
        resp = requests.get(SPARK_URL, timeout=5)
        resp.raise_for_status()
        data = resp.json()
        models = data.get("data", [])
        if not models:
            warn("La risposta non contiene modelli.")
            return None, None
        model_data = models[0]
        model_id = model_data["id"]
        ctx = detect_context_length(model_data, model_id)
        return model_id, ctx
    except requests.exceptions.ConnectionError:
        warn("Backend non raggiungibile (ConnectionError).")
    except requests.exceptions.Timeout:
        warn("Backend non risponde (Timeout).")
    except (KeyError, IndexError, ValueError) as e:
        error(f"Risposta malformata: {e}")
    except Exception as e:
        error(f"Errore inatteso: {e}")
    return None, None

# ---------------------------------------------------------------------------
# Loop principale
# ---------------------------------------------------------------------------
def main():
    info(f"Avvio monitoraggio su {SPARK_URL}")
    info(f"api_base derivata:        {API_BASE}")
    info(f"Modello virtuale:         {VIRTUAL_MODEL}")
    info(f"Config path:              {CONFIG_PATH}")
    info(f"LiteLLM proxy URL:        {LITELLM_PROXY_URL}")
    info(f"Poll interval:            {POLL_INTERVAL}s")
    info(f"Default context fallback: {DEFAULT_CONTEXT}")
    info(f"Callbacks attivi:         {_active_callbacks()}")
    if MLFLOW_TRACKING_URI:
        exp_info = f", experiment={MLFLOW_EXPERIMENT_NAME!r}" if MLFLOW_EXPERIMENT_NAME else ""
        if MLFLOW_TRACKING_TOKEN:
            auth_info = ", auth=bearer-token"
        elif MLFLOW_TRACKING_USERNAME and MLFLOW_TRACKING_PASSWORD:
            auth_info = f", auth=basic(user={MLFLOW_TRACKING_USERNAME!r})"
        elif MLFLOW_TRACKING_USERNAME or MLFLOW_TRACKING_PASSWORD:
            auth_info = ", auth=incomplete-basic-credentials"
            warn("MLflow: MLFLOW_TRACKING_USERNAME e MLFLOW_TRACKING_PASSWORD devono essere entrambi impostati per Basic Auth.")
        else:
            auth_info = ", auth=none"
        tls_info = ", insecure_tls=true" if MLFLOW_TRACKING_INSECURE_TLS in ("true", "1", "yes") else ""
        info(f"MLflow abilitato:         uri={MLFLOW_TRACKING_URI!r}{exp_info}{auth_info}{tls_info}")
    if INPUT_COST_PER_TOKEN is not None or OUTPUT_COST_PER_TOKEN is not None:
        in_str  = f"${INPUT_COST_PER_TOKEN * 1_000_000:.4f}/1M" if INPUT_COST_PER_TOKEN is not None else "n/a"
        out_str = f"${OUTPUT_COST_PER_TOKEN * 1_000_000:.4f}/1M" if OUTPUT_COST_PER_TOKEN is not None else "n/a"
        info(f"Costi modello configurati: input={in_str}, output={out_str}")

    # Start Prometheus metrics server if enabled
    active_cbs = _active_callbacks()
    if "prometheus" in active_cbs:
        start_http_server(9100)
        info("Prometheus metrics esposte sulla porta 9100")
    else:
        info("Prometheus disabilitato (ENABLE_PROMETHEUS=false)")

    last_model: str | None = None
    last_ctx: int = 0
    consecutive_errors = 0
    litellm_was_ready = False
    # True = prova le API admin; False = usa il restart controllato
    admin_api_available = True

    disk_config_is_stale = _disk_config_is_placeholder()
    if disk_config_is_stale:
        warn("Config su disco contiene 'placeholder' — verrà riscritto al primo discovery.")

    while not _shutdown:
        current_model, current_ctx = fetch_current_model()

        if current_model is not None:
            consecutive_errors = 0

            model_changed = current_model != last_model
            ctx_changed    = current_ctx != last_ctx
            needs_update   = model_changed or ctx_changed or disk_config_is_stale

            if needs_update:
                if model_changed:
                    info(f"Modello cambiato: {last_model!r} -> {current_model!r}")
                if ctx_changed and last_ctx != 0:
                    info(f"Context length cambiata: {last_ctx} -> {current_ctx}")

                # Sempre aggiorna il config su disco (serve al prossimo restart)
                disk_ok = False
                try:
                    write_config_atomic(current_model, current_ctx)
                    disk_ok = True
                    disk_config_is_stale = False
                    info(f"Config su disco aggiornato (model={current_model!r}, ctx={current_ctx}).")
                except Exception as e:
                    error(f"Impossibile scrivere il config: {e}")

                litellm_ready = _litellm_ready()
                if litellm_ready:
                    if not litellm_was_ready:
                        litellm_was_ready = True
                        strategy = "API admin" if admin_api_available else "restart controllato"
                        info(f"LiteLLM è online. Strategia di aggiornamento: {strategy}.")

                    if disk_ok:
                        if admin_api_available:
                            try:
                                ok = update_model_live(current_model, current_ctx)
                            except _NoDBError as e:
                                warn(f"API admin non disponibili ({e}). "
                                     "Passo alla strategia restart controllato per tutti i cicli futuri.")
                                admin_api_available = False
                                ok = False

                            if ok:
                                info(f"✅ Modello aggiornato live: {VIRTUAL_MODEL!r} -> {current_model!r} (ctx={current_ctx})")
                                last_model = current_model
                                last_ctx   = current_ctx
                                live_updates_ok.inc()
                                current_model_gauge.set(time.time())
                                context_length_gauge.set(current_ctx)
                            elif not admin_api_available:
                                # Prima volta in modalità restart: eseguiamo subito
                                if _request_litellm_restart():
                                    info(f"♻  Restart richiesto per applicare modello: {current_model!r} (ctx={current_ctx})")
                                    last_model = current_model
                                    last_ctx   = current_ctx
                                    live_updates_ok.inc()
                                    current_model_gauge.set(time.time())
                                    context_length_gauge.set(current_ctx)
                                else:
                                    live_updates_fail.inc()
                            else:
                                warn("Aggiornamento live fallito. Verrà ritentato al prossimo ciclo.")
                                live_updates_fail.inc()
                        else:
                            # Modalità restart controllato (no DB)
                            if _request_litellm_restart():
                                info(f"♻  Restart richiesto per applicare modello: {current_model!r} (ctx={current_ctx})")
                                last_model = current_model
                                last_ctx   = current_ctx
                                live_updates_ok.inc()
                                current_model_gauge.set(time.time())
                                context_length_gauge.set(current_ctx)
                            else:
                                live_updates_fail.inc()
                    else:
                        warn("Config non scritto — verrà ritentato al prossimo ciclo.")
                else:
                    info("LiteLLM non ancora online, il modello sarà caricato all'avvio.")
                    if disk_ok:
                        last_model = current_model
                        last_ctx   = current_ctx
                        current_model_gauge.set(time.time())
                        context_length_gauge.set(current_ctx)

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