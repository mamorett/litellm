"""
Official LiteLLM async custom callbacks for MLflow.

Configured via `success_callback: ["mlflow_callback.log_mlflow_success"]` and
`failure_callback: ["mlflow_callback.log_mlflow_failure"]` in LiteLLM config.yaml.

Why a custom callback instead of LiteLLM's built-in "mlflow" string:
1. Model name: logs the actual backend model (model_info.base_model /
   litellm_params.model / standard_logging_object.model) into `mlflow.llm.model`,
   `model`, and span outputs instead of LiteLLM's virtual `model_name`.
2. Cost injection: computes `input_cost`, `output_cost`, and `total_cost` from
   INPUT_COST_PER_1M_TOKENS / OUTPUT_COST_PER_1M_TOKENS (or *_PER_TOKEN, or
   LiteLLM's response_cost) and writes `mlflow.llm.cost` + `response_cost` onto
   MLflow span attributes.
"""

import asyncio
import os
import threading
from datetime import datetime
from typing import Any, Optional

_init_lock = threading.Lock()
_mlflow_client: Any = None


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


INPUT_COST_PER_TOKEN = _parse_cost_env("INPUT_COST_PER_1M_TOKENS", "INPUT_COST_PER_TOKEN")
OUTPUT_COST_PER_TOKEN = _parse_cost_env("OUTPUT_COST_PER_1M_TOKENS", "OUTPUT_COST_PER_TOKEN")


def _get_client():
    global _mlflow_client
    if _mlflow_client is not None:
        return _mlflow_client
    with _init_lock:
        if _mlflow_client is None:
            import mlflow
            from mlflow.tracking import MlflowClient

            exp_name = os.getenv("MLFLOW_EXPERIMENT_NAME", "").strip()
            if exp_name:
                try:
                    mlflow.set_experiment(exp_name)
                except Exception as e:
                    print(
                        f"[MLflow Callback] WARN: failed to set experiment {exp_name!r}: {e}",
                        flush=True,
                    )
            _mlflow_client = MlflowClient()
            print(
                f"[MLflow Callback] MlflowClient initialized "
                f"(uri={os.getenv('MLFLOW_TRACKING_URI')!r}, "
                f"experiment={exp_name!r}, "
                f"input_cost_per_token={INPUT_COST_PER_TOKEN}, "
                f"output_cost_per_token={OUTPUT_COST_PER_TOKEN})",
                flush=True,
            )
    return _mlflow_client


def _resolve_real_model(kwargs: dict, response_obj: Any = None) -> Optional[str]:
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
) -> tuple[Optional[int], Optional[int], Optional[int]]:
    standard_obj = kwargs.get("standard_logging_object") or {}
    prompt_tokens = standard_obj.get("prompt_tokens")
    completion_tokens = standard_obj.get("completion_tokens")
    total_tokens = standard_obj.get("total_tokens")

    if (
        prompt_tokens is None or completion_tokens is None or total_tokens is None
    ) and response_obj is not None:
        usage = getattr(response_obj, "usage", None)
        if usage is not None:
            if prompt_tokens is None:
                prompt_tokens = getattr(usage, "prompt_tokens", None)
            if completion_tokens is None:
                completion_tokens = getattr(usage, "completion_tokens", None)
            if total_tokens is None:
                total_tokens = getattr(usage, "total_tokens", None)

    if total_tokens is None and prompt_tokens is not None and completion_tokens is not None:
        total_tokens = int(prompt_tokens) + int(completion_tokens)

    return prompt_tokens, completion_tokens, total_tokens


def _compute_cost(
    kwargs: dict,
    prompt_tokens: Optional[int],
    completion_tokens: Optional[int],
) -> Optional[dict[str, float]]:
    has_custom_cost = (
        INPUT_COST_PER_TOKEN is not None or OUTPUT_COST_PER_TOKEN is not None
    )
    if has_custom_cost and (prompt_tokens is not None or completion_tokens is not None):
        in_cost = (
            float(prompt_tokens) * INPUT_COST_PER_TOKEN
            if prompt_tokens is not None and INPUT_COST_PER_TOKEN is not None
            else 0.0
        )
        out_cost = (
            float(completion_tokens) * OUTPUT_COST_PER_TOKEN
            if completion_tokens is not None and OUTPUT_COST_PER_TOKEN is not None
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


def _construct_input(kwargs: dict) -> dict:
    inputs: dict[str, Any] = {"messages": kwargs.get("messages")}
    if tools := kwargs.get("tools"):
        inputs["tools"] = tools
    optional_params = kwargs.get("optional_params") or {}
    for key in ("functions", "tools", "stream", "tool_choice", "user"):
        if (value := optional_params.get(key)) is not None:
            inputs[key] = value
    if prediction := kwargs.get("prediction"):
        inputs["prediction"] = prediction
    return inputs


def _to_ns(dt: Any) -> int:
    if isinstance(dt, datetime):
        return int(dt.timestamp() * 1e9)
    if isinstance(dt, (int, float)):
        return int(dt * 1e9) if dt < 1e12 else int(dt)
    return int(datetime.now().timestamp() * 1e9)


def _transform_tags(tag_list: Optional[list]) -> dict[str, str]:
    tags: dict[str, str] = {}
    if not tag_list:
        return tags
    for tag in tag_list:
        tag_str = str(tag)
        if ":" in tag_str:
            k, v = tag_str.split(":", 1)
            tags[k.strip()] = v.strip()
        else:
            tags[tag_str.strip()] = ""
    return tags


def _record_trace_sync(
    kwargs: dict, response_obj: Any, start_time: Any, end_time: Any, is_error: bool
) -> None:
    import mlflow
    from mlflow.entities import SpanEvent, SpanStatusCode, SpanType

    client = _get_client()
    call_type = kwargs.get("call_type", "completion")
    span_name = f"litellm-{call_type}"
    span_type = SpanType.EMBEDDING if call_type == "embeddings" else SpanType.LLM

    start_time_ns = _to_ns(start_time)
    end_time_ns = _to_ns(end_time)

    inputs = _construct_input(kwargs)
    real_model = _resolve_real_model(kwargs, response_obj)
    litellm_params = kwargs.get("litellm_params") or {}
    standard_obj = kwargs.get("standard_logging_object") or {}

    provider = (
        kwargs.get("custom_llm_provider")
        or litellm_params.get("custom_llm_provider")
        or "openai"
    )
    prompt_tokens, completion_tokens, total_tokens = _extract_token_counts(
        kwargs, response_obj
    )
    cost_info = _compute_cost(kwargs, prompt_tokens, completion_tokens)

    attributes: dict[str, Any] = {
        "litellm_call_id": kwargs.get("litellm_call_id"),
        "call_type": call_type,
        "model": real_model,
        "mlflow.llm.provider": provider,
    }
    if real_model:
        attributes["mlflow.llm.model"] = real_model
    if model_group := standard_obj.get("model_group"):
        attributes["litellm_model_group"] = model_group
    if api_base := (standard_obj.get("api_base") or litellm_params.get("api_base")):
        attributes["api_base"] = api_base
    if (cache_hit := standard_obj.get("cache_hit")) is not None:
        attributes["cache_hit"] = cache_hit
    if (
        prompt_tokens is not None
        or completion_tokens is not None
        or total_tokens is not None
    ):
        attributes["mlflow.chat.tokenUsage"] = {
            "input_tokens": prompt_tokens,
            "output_tokens": completion_tokens,
            "total_tokens": total_tokens,
        }
    if cost_info is not None:
        attributes["mlflow.llm.cost"] = cost_info
        attributes["response_cost"] = cost_info["total_cost"]

    tags = _transform_tags(standard_obj.get("request_tags"))

    if active_span := mlflow.get_current_active_span():
        span = client.start_span(
            name=span_name,
            trace_id=active_span.request_id,
            parent_id=active_span.span_id,
            span_type=span_type,
            inputs=inputs,
            attributes=attributes,
            start_time_ns=start_time_ns,
        )
    else:
        span = client.start_trace(
            name=span_name,
            span_type=span_type,
            inputs=inputs,
            attributes=attributes,
            tags=tags,
            start_time_ns=start_time_ns,
        )

    if is_error and (exception := kwargs.get("exception")):
        try:
            span.add_event(SpanEvent.from_exception(exception))
        except Exception:
            pass

    try:
        from mlflow.tracing.utils import set_span_chat_messages, set_span_chat_tools

        input_messages = inputs.get("messages") or []
        output_messages = [
            c.message.model_dump(exclude_none=True)
            for c in getattr(response_obj, "choices", [])
            if getattr(c, "message", None) is not None
        ]
        if messages := [*input_messages, *output_messages]:
            set_span_chat_messages(span, messages)
        if tools := inputs.get("tools"):
            set_span_chat_tools(span, tools)
    except Exception:
        pass

    outputs = response_obj
    if real_model and outputs is not None and hasattr(outputs, "model_copy"):
        try:
            outputs = outputs.model_copy(update={"model": real_model})
        except Exception:
            pass

    status = SpanStatusCode.ERROR if is_error else SpanStatusCode.OK
    if span.parent_id is None:
        client.end_trace(
            trace_id=span.request_id,
            outputs=outputs,
            status=status,
            end_time_ns=end_time_ns,
        )
    else:
        client.end_span(
            trace_id=span.request_id,
            span_id=span.span_id,
            outputs=outputs,
            status=status,
            end_time_ns=end_time_ns,
        )


async def log_mlflow_success(kwargs, response_obj, start_time, end_time):
    """Async success_callback entrypoint for LiteLLM Proxy."""
    try:
        await asyncio.to_thread(
            _record_trace_sync, kwargs, response_obj, start_time, end_time, False
        )
    except Exception as e:
        print(f"[MLflow Callback] ERROR logging success trace: {e}", flush=True)


async def log_mlflow_failure(kwargs, response_obj, start_time, end_time):
    """Async failure_callback entrypoint for LiteLLM Proxy."""
    try:
        await asyncio.to_thread(
            _record_trace_sync, kwargs, response_obj, start_time, end_time, True
        )
    except Exception as e:
        print(f"[MLflow Callback] ERROR logging failure trace: {e}", flush=True)


print("[MLflow Callback] module loaded", flush=True)
