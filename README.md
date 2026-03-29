# Dockerized LiteLLM Proxy

This repository provides a `Dockerfile` and GitHub Actions workflow to build and run the [LiteLLM](https://github.com/BerriAI/litellm) proxy server in a Docker container. LiteLLM is a powerful library that simplifies interactions with over 100+ LLM providers through a unified, OpenAI-compatible API.

This setup allows you to deploy a standalone, scalable, and resilient LLM gateway for your applications.

[![Build & Push Docker Image](https://github.com/gorgon/dev/litellm/actions/workflows/build-push.yaml/badge.svg)](https://github.com/gorgon/dev/litellm/actions/workflows/build-push.yaml)

## Key Features of LiteLLM

Using LiteLLM as a proxy provides numerous benefits:

- **Unified API:** Interact with providers like OpenAI, Azure, Cohere, Anthropic, Gemini, and self-hosted models (e.g., Ollama) using the consistent OpenAI API format.
- **Cost Management:** Track spending across all models and providers, set budgets, and view detailed usage logs.
- **Resilience & Reliability:** Automatically retry failed requests and implement fallback logic to switch between models or providers if an API call fails.
- **Load Balancing:** Distribute requests across multiple model deployments to improve performance and availability.
- **Centralized Key Management:** Securely manage all your API keys in one central configuration.
- **Streaming Support:** Stream responses from any provider, improving perceived performance and user experience.

## Getting Started

### Prerequisites

- [Docker](https://docs.docker.com/get-docker/) installed on your machine.
- An API key for at least one LLM provider (e.g., OpenAI, Cohere).

### 1. Create a Configuration File

The LiteLLM proxy is configured using a `config.yaml` file. Create a directory and place your configuration file inside it.

```bash
mkdir config
touch config/config.yaml
```

Add your model and API key details to `config/config.yaml`. Here is a basic example using OpenAI with Prometheus and Langfuse enabled:

```yaml
# config/config.yaml

model_list:
  - model_name: gpt-4
    litellm_params:
      model: gpt-4
      api_key: sk-xxxxxxxxxxxxxxxxxxxxxxxxxxxxx # Replace with your OpenAI API key

litellm_settings:
  # Set to 'DEBUG' for verbose logging
  set_verbose: True
  # Enable Prometheus and Langfuse callbacks
  callbacks: ["prometheus", "langfuse"]
```

### 2. Run the Docker Container

Run the container using the image from the GitHub Container Registry. This command maps port `4000` on your local machine to the container and mounts your configuration file. Don't forget to provide the Langfuse environment variables if you are using it.

```bash
docker run -d \
  --name litellm-proxy \
  -p 4000:4000 \
  -v $(pwd)/config:/config \
  -e LANGFUSE_PUBLIC_KEY="pk-lf-..." \
  -e LANGFUSE_SECRET_KEY="sk-lf-..." \
  -e LANGFUSE_HOST="https://cloud.langfuse.com" \
  ghcr.io/gorgon/dev/litellm:latest
```

### 3. Make a Request

You can now send requests to `http://localhost:4000` as if it were the OpenAI API.

```bash
curl -X POST http://localhost:4000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d 
    "model": "gpt-4",
    "messages": [
      {
        "role": "user",
        "content": "Hello, how are you?"
      }
    ]
  }
```

## Building the Image Manually

If you prefer to build the Docker image yourself, you can do so with the following command:

```bash
docker build -t my-litellm-proxy .
```

This repository is configured with a GitHub Actions workflow (`.github/workflows/build-push.yaml`) that automatically builds and pushes a multi-platform image (`linux/amd64`, `linux/arm64`) to `ghcr.io` whenever a new version tag (e.g., `v1.0.0`) is pushed.

## License

This project is licensed under the terms of the [MIT License](LICENSE).
