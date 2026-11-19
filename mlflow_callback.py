"""
Custom MLflow callback for LiteLLM Proxy.

Fixes two issues with LiteLLM's built-in MlflowLogger:
1. Model name: logs the actual backend model (e.g. from model_info.base_model or
   litellm_params.model) into `mlflow.llm.model`, `model`, and span outputs,
   instead of the virtual model_name presented by LiteLLM Proxy.
2. Cost injection: computes input_cost, output_cost, and total_cost from
   INPUT_COST_PER_1M_TOKENS / OUTPUT_COST_PER_1M_TOKENS (or *_PER_TOKEN, or
   LiteLLM's response_cost) and writes `mlflow.llm.cost` + `response_cost` onto
   the MLflow span attributes (for both streaming and non-streaming calls).
"""

import os
from typing import Any, Optional

from litellm.integrations.mlflow import MlflowLogger


def _parse_cost_env(per_1m_key: str, per_token_key: str) -> Optional[float]:
    raw_1m = os.getenv(per_1m_key, "").strip()
    if raw_1m:
        try:
            return float(raw_1m) / 1_000_000.0
        except ValueError:
            pass
    raw_tok = os.getenv(per_token_key, "").strip()
    if raw_tok:
        try:
            return float(raw_tok)
        except ValueError:
            pass
    return None


def _resolve_real_model(kwargs: dict, response_obj: Any = None) -> Optional[str]:
    """Extract the underlying backend model name rather than LiteLLM's virtual model_name."""
    litellm_params = kwargs.get("litellm_params") or {}
    metadata = kwargs.get("metadata") or litellm_params.get("metadata") or {}
    model_info = metadata.get("model_info") or litellm_params.get("model_info") or {}
    standard_obj = kwargs.get("standard_logging_object") or {}
    model_group = standard_obj.get("model_group") or metadata.get("model_group")

    candidates = [
        model_info.get("base_model"),
        litellm_params.get("model"),
        standard_obj.get("model"),
        kwargs.get("model"),
    ]
    for candidate in candidates:
        if candidate and candidate != model_group:
            return str(candidate)

    for candidate in candidates:
        if candidate:
            return str(candidate)

    if response_obj is not None and getattr(response_obj, "model", None):
        return str(response_obj.model)
    return None


def _extract_token_counts(
    kwargs: dict, response_obj: Any = None
) -> tuple[Optional[int], Optional[int]]:
    standard_obj = kwargs.get("standard_logging_object") or {}
    prompt_tokens = standard_obj.get("prompt_tokens")
    completion_tokens = standard_obj.get("completion_tokens")

    if (prompt_tokens is None or completion_tokens is None) and response_obj is not None:
        usage = getattr(response_obj, "usage", None)
        if usage is not None:
            if prompt_tokens is None:
                prompt_tokens = getattr(usage, "prompt_tokens", None)
            if completion_tokens is None:
                completion_tokens = getattr(usage, "completion_tokens", None)

    return prompt_tokens, completion_tokens


class CustomMlflowLogger(MlflowLogger):
    """MlflowLogger subclass that preserves the real backend model name and injects MLflow costs."""

    def __init__(self):
        super().__init__()
        self._input_cost_per_token = _parse_cost_env(
            "INPUT_COST_PER_1M_TOKENS", "INPUT_COST_PER_TOKEN"
        )
        self._output_cost_per_token = _parse_cost_env(
            "OUTPUT_COST_PER_1M_TOKENS", "OUTPUT_COST_PER_TOKEN"
        )
        self._span_id_to_real_model: dict[str, str] = {}

    def _compute_cost(
        self, kwargs: dict, response_obj: Any = None
    ) -> Optional[dict[str, float]]:
        prompt_tokens, completion_tokens = _extract_token_counts(kwargs, response_obj)

        has_custom_cost = (
            self._input_cost_per_token is not None
            or self._output_cost_per_token is not None
        )
        if has_custom_cost and (
            prompt_tokens is not None or completion_tokens is not None
        ):
            in_cost = (
                float(prompt_tokens) * self._input_cost_per_token
                if prompt_tokens is not None and self._input_cost_per_token is not None
                else 0.0
            )
            out_cost = (
                float(completion_tokens) * self._output_cost_per_token
                if completion_tokens is not None
                and self._output_cost_per_token is not None
                else 0.0
            )
            return {
                "input_cost": in_cost,
                "output_cost": out_cost,
                "total_cost": in_cost + out_cost,
            }

        standard_obj = kwargs.get("standard_logging_object") or {}
        response_cost = standard_obj.get("response_cost")
        if response_cost is None:
            response_cost = kwargs.get("response_cost")
        if response_cost is not None:
            try:
                return {"total_cost": float(response_cost)}
            except (TypeError, ValueError):
                pass
        return None

    def _extract_attributes(self, kwargs):
        attributes = super()._extract_attributes(kwargs)

        real_model = _resolve_real_model(kwargs)
        if real_model:
            attributes["model"] = real_model
            attributes["mlflow.llm.model"] = real_model

        litellm_params = kwargs.get("litellm_params") or {}
        provider = (
            kwargs.get("custom_llm_provider")
            or litellm_params.get("custom_llm_provider")
            or "openai"
        )
        attributes["mlflow.llm.provider"] = provider

        standard_obj = kwargs.get("standard_logging_object") or {}
        if model_group := standard_obj.get("model_group"):
            attributes["litellm_model_group"] = model_group

        cost_info = self._compute_cost(kwargs)
        if cost_info is not None:
            attributes["mlflow.llm.cost"] = cost_info
            attributes["response_cost"] = cost_info["total_cost"]

        return attributes

    def _start_span_or_trace(self, kwargs, start_time):
        span = super()._start_span_or_trace(kwargs, start_time)
        real_model = _resolve_real_model(kwargs)
        if real_model and span is not None:
            with self._lock:
                self._span_id_to_real_model[span.span_id] = real_model
        return span

    def _extract_and_set_chat_attributes(self, span, kwargs, response_obj):
        super()._extract_and_set_chat_attributes(span, kwargs, response_obj)
        if span is None:
            return
        cost_info = self._compute_cost(kwargs, response_obj)
        if cost_info is not None:
            span.set_attribute("mlflow.llm.cost", cost_info)
            span.set_attribute("response_cost", cost_info["total_cost"])

    def _end_span_or_trace(self, span, outputs, end_time_ns, status):
        real_model = None
        if span is not None:
            with self._lock:
                real_model = self._span_id_to_real_model.pop(span.span_id, None)
        if real_model and outputs is not None and hasattr(outputs, "model_copy"):
            try:
                outputs = outputs.model_copy(update={"model": real_model})
            except Exception:
                pass
        super()._end_span_or_trace(
            span=span,
            outputs=outputs,
            end_time_ns=end_time_ns,
            status=status,
        )


mlflow_logger = CustomMlflowLogger()
