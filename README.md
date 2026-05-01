# Dockerized LiteLLM Proxy with Autodiscovery

This repository provides a `Dockerfile` and GitHub Actions workflow to build and run the [LiteLLM](https://github.com/BerriAI/litellm) proxy server with an integrated **Autodiscovery Script**.

This setup is designed for dynamic environments where the backend model might change (e.g., using Ollama or a custom provider that updates its model list) and you want LiteLLM to automatically update its configuration.

[![Build & Publish LiteLLM Custom](https://github.com/mamorett/litellm/actions/workflows/build-push.yaml/badge.svg)](https://github.com/mamorett/litellm/actions/workflows/build-push.yaml)

## Features

- **Autodiscovery Script:** A background process (`discovery.py`) monitors a backend API and automatically updates the LiteLLM `config.yaml` when a new model is detected.
- **Production Hardened:** Built-in retry logic, model swap verification, and self-healing for dynamic backends.
- **Observability:** Prometheus metrics and Docker healthchecks included out-of-the-box.
- **Unified OpenAI-Compatible API:** Exposes any backend model through a consistent OpenAI API format.
- **Langfuse Integration:** Pre-configured to support Langfuse for observability and cost tracking.
- **Multi-Platform Docker Image:** Builds for both `amd64` and `arm64` architectures.

## Configuration & Environment Variables

The container can be configured using the following environment variables:

| Variable | Default | Description |
| :--- | :--- | :--- |
| `SPARK_URL` | `http://host.docker.internal:8000/v1/models` | The URL to poll for model discovery. |
| `VIRTUAL_MODEL_NAME` | `custom/model` | The name under which the discovered model will be exposed in LiteLLM. |
| `LITELLM_CONFIG_PATH` | `/config/config.yaml` | Path where the configuration file will be generated/read. |
| `POLL_INTERVAL` | `10` | Interval in seconds between discovery polls. |
| `DISABLE_DISCOVERY` | `false` | Set to `true` to disable the discovery script and use a static config. |
| `LITELLM_MASTER_KEY` | `""` | Optional master key for LiteLLM admin APIs. |

## Observability

### Prometheus Metrics
The discovery script exposes metrics on port `9100`:
- `discovery_backend_errors_total`: Count of failed backend polls.
- `discovery_live_updates_ok_total`: Successful model hot-swaps.
- `discovery_live_updates_fail_total`: Failed model hot-swaps (after retries).
- `discovery_model_change_timestamp_seconds`: Timestamp of the last successful model update.

### Healthcheck
The container includes a native Docker healthcheck that verifies the LiteLLM proxy readiness every 30 seconds.

## Operational Modes

### 1. Autodiscovery Mode (Default)

In this mode, the container starts a discovery script that polls `SPARK_URL`. It will wait for the first successful discovery before starting the LiteLLM proxy.

```bash
docker run -d \
  --name litellm-proxy \
  -p 4000:4000 \
  -p 9100:9100 \
  -e SPARK_URL="http://your-backend:8000/v1/models" \
  -e VIRTUAL_MODEL_NAME="my-awesome-model" \
  ghcr.io/mamorett/litellm:latest
```

### 2. Static Mode (Discovery Disabled)

If you already have a static `config.yaml` and don't need autodiscovery, you can disable the script.

```bash
docker run -d \
  --name litellm-proxy \
  -p 4000:4000 \
  -e DISABLE_DISCOVERY="true" \
  -v $(pwd)/config.yaml:/config/config.yaml \
  ghcr.io/gorgon/dev/litellm:latest
```

## Monitoring & Observability

To enable Langfuse tracking, provide the following variables:

```bash
docker run -d \
  --name litellm-proxy \
  -p 4000:4000 \
  -e LANGFUSE_PUBLIC_KEY="pk-lf-..." \
  -e LANGFUSE_SECRET_KEY="sk-lf-..." \
  -e LANGFUSE_HOST="https://cloud.langfuse.com" \
  ghcr.io/gorgon/dev/litellm:latest
```

## CI/CD Workflow

The image is automatically built and pushed to GitHub Container Registry (GHCR) only when a **new release is published** in the repository.

- **Trigger:** `on: release: types: [published]`
- **Tags:**
    - `latest`
    - The version number from the `Dockerfile` (e.g., `1.82.3`)
    - The release tag name (e.g., `v1.2.0`)

## License

This project is licensed under the terms of the [MIT License](LICENSE).
