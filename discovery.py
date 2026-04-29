import requests
import time
import os
import yaml
import sys
from datetime import datetime

# Parametri da ENV
SPARK_URL = os.getenv("SPARK_URL")
VIRTUAL_MODEL_NAME = os.getenv("VIRTUAL_MODEL_NAME")
CONFIG_PATH = os.getenv("LITELLM_CONFIG_PATH")
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL", 10))

def log(msg):
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [Discovery] {msg}", flush=True)

def write_config(real_model_name):
    config = {
        "model_list": [{
            "model_name": VIRTUAL_MODEL_NAME,
            "litellm_params": {
                "model": real_model_name,
                "custom_llm_provider": "openai",
                "api_base": os.path.dirname(SPARK_URL), # Prende la base dell'URL (es. /v1)
                "api_key": "ollama"
            },
            "model_info": { "base_model": real_model_name }
        }],
        "litellm_settings": {
            "success_callback": ["langfuse"],
            "failure_callback": ["langfuse"],
            "drop_params": True
        }
    }
    with open(CONFIG_PATH, "w") as f:
        yaml.dump(config, f, default_flow_style=False)

if __name__ == "__main__":
    log(f"Avvio monitoraggio su {SPARK_URL}")
    last_model = None
    
    while True:
        try:
            r = requests.get(SPARK_URL, timeout=3)
            r.raise_for_status()
            current = r.json()["data"][0]["id"]
            
            if current != last_model:
                write_config(current)
                last_model = current
                log(f"✅ Modello aggiornato: {VIRTUAL_MODEL_NAME} -> {current}")
        except Exception as e:
            if last_model is None:
                write_config("loading...")
                last_model = "loading"
                log("⚠️ Backend non raggiungibile al boot. Creato config temporaneo.")
        
        time.sleep(POLL_INTERVAL)