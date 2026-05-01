# Release Notes: Production Hardening Pass (v3.0.0)

This release focuses on **production reliability** and **observability**, addressing critical failures in the model autodiscovery system during backend swaps.

## 🚀 Key Improvements

### 🛡️ Robust Autodiscovery (Anti-Fragility)
- **Exponential Backoff Retries**: The discovery script now performs up to 4 retries with exponential backoff when hot-swapping models via the LiteLLM Admin API. This prevents silent failures during transient network or backend issues.
- **State Consistency**: Fixed a race condition where the internal state (`last_model`) was updated even if the live swap failed. Swaps are now atomic and retried until successful.
- **Model Verification**: After every swap, the script now queries the LiteLLM Proxy state to verify the new model is actually active before confirming the update.
- **Self-Healing Boot**: Detects if the container started with a "placeholder" config (due to backend being down at boot) and automatically corrects it on disk as soon as the backend becomes available.

### 📊 Observability & Monitoring
- **Prometheus Metrics**: New exporter on port `9100` providing real-time signals:
    - `discovery_backend_errors_total` (Alertable: indicates backend API issues)
    - `discovery_live_updates_ok_total` / `fail_total` (Track swap success rates)
    - `discovery_model_change_timestamp_seconds` (Heartbeat for model updates)
- **Native Healthchecks**: Added Docker `HEALTHCHECK` to the image, verifying LiteLLM readiness every 30s.

### 🔄 Process Management
- **Watchdog Backoff**: `entrypoint.sh` now uses exponential backoff when restarting the discovery process if it crashes, preventing CPU exhaustion loops.
- **Atomic Config Writes**: Improved robustness of disk-based config updates to prevent file corruption.

## 🛠️ How to Update
Pull the latest image from GHCR:
```bash
docker pull ghcr.io/mamorett/litellm:latest
```
Note: Ensure port `9100` is exposed if you wish to scrape Prometheus metrics.

---

## 📝 Release Template (Copy for future use)

### [Release Version] — [Title]

#### 🌟 New Features
- Feature A...
- Feature B...

#### 🐛 Bug Fixes
- Fixed X...
- Resolved Y...

#### 🛡️ Security & Robustness
- Changes to hardening, retries, or permissions.

#### 📊 Observability
- New metrics, logs, or traces.

#### ⚠️ Breaking Changes
- [ ] None
- [x] Details: ...

#### 🏗️ Internal Changes
- Dependency updates, refactoring.

#### 🚢 Deployment Notes
- New ENV variables or port requirements.
