from __future__ import annotations

import json
import http.client
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import OrderedDict
from copy import deepcopy
from typing import Any

from .model_capabilities import ModelCapabilities, capabilities_from_payload, catalog_capabilities, metadata_model_id
from .model_limits import ModelContextLengthError, ModelLimits, ModelPricing
from .media.images import ModelImageInputError, has_images


def _image_setting(value: str) -> bool | None:
    normalized = value.strip().lower()
    if normalized == "auto":
        return None
    if normalized in {"true", "1", "yes"}:
        return True
    if normalized in {"false", "0", "no"}:
        return False
    raise ValueError("MODEL_SUPPORTS_IMAGES must be auto, true or false")


def _is_image_capability_error(detail: str) -> bool:
    lowered = detail.lower()
    if any(term in lowered for term in ("image format", "image type", "mime", "image detail", "base64", "decode image")):
        return False
    return any(term in lowered for term in ("image", "vision", "multimodal")) and any(term in lowered for term in ("not support", "unsupported", "text-only", "text only"))

class OpenAICompatibleModel:
    supports_cache_key = True

    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        *,
        request_timeout_seconds: float = 180,
        retry_attempts: int = 4,
        max_output_tokens_per_call: int | None = None,
        context_window_tokens: int | None = None,
        output_reserve_tokens: int | None = None,
        input_capacity_tokens: int | None = None,
        pricing: ModelPricing | None = None,
        supports_image_input: bool | None = None,
        reasoning_effort: str | None = None,
        parallel_tool_calls: bool = True,
        api_mode: str = "chat_completions",
        responses_stream: bool = False,
        responses_previous_response_id: bool = False,
        responses_store: bool = False,
        prompt_cache_retention: str | None = "24h",
    ):
        self.base_url = base_url.rstrip("/")
        if api_mode not in {"chat_completions", "responses"}:
            raise ValueError("api_mode must be chat_completions or responses")
        self.api_mode = api_mode
        self.responses_stream = responses_stream
        self.responses_previous_response_id = bool(responses_previous_response_id)
        self.responses_store = bool(responses_store)
        self.prompt_cache_retention = prompt_cache_retention
        self.api_key = api_key
        self.model = model
        self.request_timeout_seconds = request_timeout_seconds
        self.retry_attempts = retry_attempts
        self.max_output_tokens_per_call = max_output_tokens_per_call
        self.context_window_tokens = context_window_tokens
        self.output_reserve_tokens = output_reserve_tokens
        self.input_capacity_tokens = input_capacity_tokens
        self.provider_max_output_tokens: int | None = None
        self.capability_source = "explicit" if context_window_tokens else "unknown"
        self._capability_discovery_attempted = False
        self.pricing = pricing
        self.supports_image_input = supports_image_input
        ModelLimits(
            context_window_tokens,
            max_output_tokens_per_call,
            output_reserve_tokens,
            input_capacity_tokens,
        )
        self.thinking_type: str | None = None
        self.response_format: str | None = None
        self.reasoning_effort = self._effort(reasoning_effort)
        self.parallel_tool_calls = bool(parallel_tool_calls)
        self._usage_lock = threading.Lock()
        self._usage = {
            "input_tokens": 0,
            "cache_read_tokens": 0,
            "cache_miss_tokens": 0,
            "output_tokens": 0,
            "requests": 0,
        }
        self._last_usage: dict[str, int] = {}
        self._response_lock = threading.Lock()
        self._response_states: OrderedDict[str, dict[str, Any]] = OrderedDict()

    def checkpoint_state(self, session_id: str) -> dict[str, Any]:
        """Resolved configuration only; credentials and transport locks stay live."""
        from dataclasses import asdict
        if self.responses_previous_response_id:
            raise ValueError("provider-side previous_response_id is not portable checkpoint state")
        selected = ("base_url", "api_mode", "responses_stream", "responses_previous_response_id",
                    "responses_store", "prompt_cache_retention", "model", "request_timeout_seconds",
                    "retry_attempts", "max_output_tokens_per_call", "context_window_tokens",
                    "output_reserve_tokens", "input_capacity_tokens", "provider_max_output_tokens",
                    "capability_source", "supports_image_input", "thinking_type", "response_format",
                    "reasoning_effort", "parallel_tool_calls")
        return {"schema_version": 1, "configuration": {key: getattr(self, key) for key in selected},
                "pricing": asdict(self.pricing) if self.pricing is not None else None,
                "capability_discovery_attempted": self._capability_discovery_attempted,
                "credential_reference": "fresh_deployment_required", "authority_restored": False,
                "continuation": "full_messages", "source_cache_key": session_id}

    @classmethod
    def from_config(cls, config: Any) -> "OpenAICompatibleModel":
        selected = getattr(config, "spec", config)
        model = getattr(selected, "model", None)
        if model is None:
            raise TypeError("OpenAICompatibleModel requires a Python HarnessSpec")
        api_key_env = model.api_key_env
        base_url_env = model.base_url_env
        model_env = model.model_env
        default_base_url = model.default_base_url
        default_model = model.default_model
        request_timeout_seconds = model.request_timeout_seconds
        retry_attempts = model.retry_attempts
        max_output_tokens_per_call = model.max_output_tokens_per_call
        context_window_tokens = model.context_window_tokens
        output_reserve_tokens = model.output_reserve_tokens
        output_override = os.getenv("MODEL_MAX_OUTPUT_TOKENS")
        window_override = os.getenv("MODEL_CONTEXT_WINDOW_TOKENS")
        reserve_override = os.getenv("MODEL_OUTPUT_RESERVE_TOKENS")
        if output_override is not None:
            max_output_tokens_per_call = _positive_setting(
                output_override, "MODEL_MAX_OUTPUT_TOKENS"
            )
        if window_override is not None:
            context_window_tokens = _positive_setting(
                window_override, "MODEL_CONTEXT_WINDOW_TOKENS"
            )
        if reserve_override is not None:
            output_reserve_tokens = _positive_setting(
                reserve_override, "MODEL_OUTPUT_RESERVE_TOKENS"
            )
        ModelLimits(
            context_window_tokens,
            max_output_tokens_per_call,
            output_reserve_tokens,
        )
        input_price = _optional_float_setting(
            os.getenv("MODEL_INPUT_USD_PER_MILLION"), model.input_usd_per_million,
            "MODEL_INPUT_USD_PER_MILLION",
        )
        cached_price = _optional_float_setting(
            os.getenv("MODEL_CACHED_INPUT_USD_PER_MILLION"),
            model.cached_input_usd_per_million,
            "MODEL_CACHED_INPUT_USD_PER_MILLION",
        )
        output_price = _optional_float_setting(
            os.getenv("MODEL_OUTPUT_USD_PER_MILLION"), model.output_usd_per_million,
            "MODEL_OUTPUT_USD_PER_MILLION",
        )
        if (input_price is None) != (output_price is None):
            raise ValueError("input and output model prices must be configured together")
        if cached_price is not None and input_price is None:
            raise ValueError("cached input price requires input and output prices")
        pricing = (
            ModelPricing(input_price, output_price, cached_price)
            if input_price is not None and output_price is not None else None
        )
        api_key = os.getenv(api_key_env, "")
        if not api_key:
            api_key = os.getenv("DEEPSEEK_API_KEY", "")
        if not api_key:
            raise RuntimeError("MODEL_API_KEY or DEEPSEEK_API_KEY is required")
        parallel_env = os.getenv("MODEL_PARALLEL_TOOL_CALLS", "true").strip().lower()
        if parallel_env not in {"true", "1", "yes", "on", "false", "0", "no", "off"}:
            raise ValueError("MODEL_PARALLEL_TOOL_CALLS must be a boolean")
        parallel_tool_calls = parallel_env in {"true", "1", "yes", "on"}
        responses_previous_response_id = os.getenv(
            "MODEL_RESPONSES_PREVIOUS_ID",
            str(getattr(model, "responses_previous_response_id", False)),
        ).strip().lower() in {"true", "1", "yes", "on"}
        responses_store = os.getenv(
            "MODEL_RESPONSES_STORE",
            str(getattr(model, "responses_store", False)),
        ).strip().lower() in {"true", "1", "yes", "on"}
        prompt_cache_retention = (
            os.getenv(
                "MODEL_PROMPT_CACHE_RETENTION",
                str(getattr(model, "prompt_cache_retention", "24h") or ""),
            ).strip() or None
        )
        return cls(
            os.getenv(base_url_env, default_base_url),
            api_key,
            os.getenv(model_env, default_model),
            request_timeout_seconds=request_timeout_seconds,
            retry_attempts=retry_attempts,
            max_output_tokens_per_call=max_output_tokens_per_call,
            supports_image_input=_image_setting(os.getenv("MODEL_SUPPORTS_IMAGES", "auto")),
            context_window_tokens=context_window_tokens,
            output_reserve_tokens=output_reserve_tokens,
            pricing=pricing,
            reasoning_effort=os.getenv("MODEL_REASONING_EFFORT", "high"),
            parallel_tool_calls=parallel_tool_calls,
            api_mode=os.getenv("MODEL_API_MODE", getattr(model, "api_mode", "chat_completions")),
            responses_stream=os.getenv("MODEL_RESPONSES_STREAM", str(getattr(model, "responses_stream", False))).lower() in {"1", "true", "yes"},
            responses_previous_response_id=responses_previous_response_id,
            responses_store=responses_store,
            prompt_cache_retention=prompt_cache_retention,
        )

    def discover_capabilities(self) -> ModelCapabilities | None:
        """Best-effort discovery from OpenAI-compatible model metadata.

        Discovery changes planning metadata only. It never applies a generation
        limit and failures leave the provider-rejection recovery path intact.
        """
        if self._capability_discovery_attempted:
            return None
        self._capability_discovery_attempted = True
        if self.context_window_tokens is not None:
            return None
        enabled = os.getenv("MODEL_CAPABILITY_DISCOVERY", "1").strip().lower()
        if enabled in {"0", "false", "no", "off"}:
            return None
        encoded = urllib.parse.quote(self.model, safe="")
        metadata_id = metadata_model_id(self.model, self.base_url)
        urls = (f"{self.base_url}/models/{encoded}", f"{self.base_url}/models")
        for url in urls:
            request = urllib.request.Request(
                url,
                headers={"Authorization": f"Bearer {self.api_key}"},
                method="GET",
            )
            try:
                with urllib.request.urlopen(
                    request, timeout=min(5.0, self.request_timeout_seconds)
                ) as response:
                    payload = json.loads(response.read())
            except (
                urllib.error.HTTPError,
                urllib.error.URLError,
                TimeoutError,
                OSError,
                http.client.IncompleteRead,
                json.JSONDecodeError,
            ):
                continue
            capabilities = capabilities_from_payload(
                payload, metadata_id, source=url
            )
            if capabilities is None:
                continue
            try:
                ModelLimits(
                    capabilities.context_window_tokens,
                    self.max_output_tokens_per_call,
                    self.output_reserve_tokens,
                    capabilities.input_capacity_tokens,
                )
            except ValueError:
                continue
            self.context_window_tokens = capabilities.context_window_tokens
            self.input_capacity_tokens = capabilities.input_capacity_tokens
            self.provider_max_output_tokens = capabilities.max_output_tokens
            self.capability_source = capabilities.source
            if self.supports_image_input is None:
                self.supports_image_input = capabilities.supports_image_input
            return capabilities
        fallback = catalog_capabilities(self.model, self.base_url)
        if fallback is not None:
            self.context_window_tokens = fallback.context_window_tokens
            self.provider_max_output_tokens = fallback.max_output_tokens
            self.capability_source = fallback.source
        return fallback

    def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        temperature: float = 0,
        cache_key: str | None = None,
    ) -> dict[str, Any]:
        if self.supports_image_input is False and any(has_images(message) for message in messages):
            raise ModelImageInputError("The configured model/provider does not support native image input.")
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
        }
        if self.max_output_tokens_per_call is not None:
            payload["max_tokens"] = self.max_output_tokens_per_call
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
            if self.parallel_tool_calls:
                payload["parallel_tool_calls"] = True
        if self.thinking_type:
            payload["thinking"] = {"type": self.thinking_type}
        if self.reasoning_effort is not None and self.thinking_type != "disabled":
            payload["reasoning_effort"] = self.reasoning_effort
        if self.response_format:
            payload["response_format"] = {"type": self.response_format}
        value = None
        full_items: list[dict[str, Any]] | None = None
        if self.api_mode == "responses":
            if self.thinking_type == "disabled":
                raise ValueError("Responses requires explicit model-supported reasoning_effort; thinking_type=disabled is not portable")
            from .responses_protocol import conversation_items, request as responses_request
            cache_key = cache_key if self.supports_cache_key else None
            state = None
            if cache_key:
                with self._response_lock:
                    state = self._response_states.get(cache_key)
            full_items = conversation_items(messages)[1]
            input_items = full_items
            previous_response_id = None
            if state is not None:
                prefix = state.get("items") or []
                if len(full_items) >= len(prefix) and full_items[:len(prefix)] == prefix:
                    delta = full_items[len(prefix):]
                    if self.responses_previous_response_id and state.get("response_id") and delta:
                        previous_response_id = state["response_id"]
                        input_items = delta
            payload = responses_request(
                messages, tools, self.model, self.reasoning_effort,
                self.max_output_tokens_per_call,
                cache_key=cache_key,
                previous_response_id=previous_response_id,
                store=self.responses_store or bool(previous_response_id),
                input_items=input_items,
                prompt_cache_retention=self.prompt_cache_retention if cache_key else None,
                reasoning_summary="auto" if self.model.lower().split("/")[-1].startswith("gpt-") else None,
            )
            if tools:
                payload["parallel_tool_calls"] = self.parallel_tool_calls
            if self.response_format:
                payload["text"] = {"format": {"type": self.response_format}}
            if self.responses_stream:
                payload["stream"] = True
        else:
            payload["messages"] = [{k: v for k, v in m.items() if k != "responses_items"} for m in messages]
        from uuid import uuid4
        from harness.api_audit import record_wire

        call_id = uuid4().hex
        for attempt in range(self.retry_attempts):
            request_id = uuid4().hex
            audit_metadata = {"call_id": call_id, "request_id": request_id,
                              "attempt": attempt + 1, "provider_model": self.model,
                              "transport": self.api_mode}
            wire_body = json.dumps(payload).encode()
            record_wire("request", json.loads(wire_body), secret=self.api_key, **audit_metadata)
            started = time.monotonic()
            request = urllib.request.Request(
                f"{self.base_url}/{'responses' if self.api_mode == 'responses' else 'chat/completions'}",
                data=wire_body,
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                method="POST",
            )
            try:
                with urllib.request.urlopen(
                    request,
                    timeout=self.request_timeout_seconds,
                ) as response:
                    if self.api_mode == "responses" and self.responses_stream:
                        from .responses_protocol import read_stream
                        value = read_stream(response, lambda event: record_wire(
                            "stream_event", event, secret=self.api_key, **audit_metadata))
                        raw_response = json.dumps(value).encode()
                    else:
                        raw_response = response.read()
                try:
                    value = json.loads(raw_response)
                except json.JSONDecodeError:
                    record_wire("response", {"unparsed_body": raw_response.decode(errors="replace")},
                                secret=self.api_key, **audit_metadata,
                                duration_seconds=time.monotonic() - started)
                    raise
                record_wire("response", value, secret=self.api_key, **audit_metadata,
                            duration_seconds=time.monotonic() - started)
                break
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode(errors="replace")
                record_wire("error", {"status": exc.code, "body": detail}, secret=self.api_key,
                            **audit_metadata, duration_seconds=time.monotonic() - started,
                            error_type="HTTPError")
                from .provider_recovery import provider_error, ProviderBillingError
                classified = provider_error(detail, exc.code)
                if isinstance(classified, ProviderBillingError):
                    raise classified from exc
                if exc.code in {400, 422} and any(has_images(message) for message in messages) and _is_image_capability_error(detail):
                    self.supports_image_input = False
                    raise ModelImageInputError(f"model API does not support image input: {detail[:1000]}") from exc
                if exc.code in {400, 413, 422} and _is_context_length_error(detail):
                    raise ModelContextLengthError(
                        f"model API context limit: {detail}"
                    ) from exc
                if exc.code not in {401, 429} and exc.code < 500:
                    raise RuntimeError(
                        f"model API HTTP {exc.code}: {detail}"
                    ) from exc
                if attempt == self.retry_attempts - 1:
                    raise RuntimeError(
                        f"model API HTTP {exc.code}: {detail}"
                    ) from exc
            except (
                urllib.error.URLError,
                TimeoutError,
                OSError,
                http.client.IncompleteRead,
                json.JSONDecodeError,
            ) as exc:
                record_wire("error", {"message": str(exc)}, secret=self.api_key,
                            **audit_metadata, duration_seconds=time.monotonic() - started,
                            error_type=type(exc).__name__)
                if attempt == self.retry_attempts - 1:
                    raise RuntimeError(
                        "model API connection failed: "
                        f"{getattr(exc, 'reason', exc)}"
                    ) from exc
            time.sleep(2 ** attempt)
        if value is None:
            raise RuntimeError("model API returned no response")
        if self.api_mode == "responses":
            from .responses_protocol import response as responses_response
            value = responses_response(value)
        request_usage = self._record_usage(value.get("usage"))
        choice = value["choices"][0]
        if self.api_mode == "responses" and cache_key:
            from .responses_protocol import build_items
            items_after = (full_items or []) + build_items([choice["message"]])
            with self._response_lock:
                self._response_states[cache_key] = {
                    "items": deepcopy(items_after),
                    "response_id": choice["message"].get("_response_id"),
                }
                self._response_states.move_to_end(cache_key)
                while len(self._response_states) > 32:
                    self._response_states.popitem(last=False)
        return {
            **choice["message"],
            "_finish_reason": choice.get("finish_reason"),
            # Keep usage tied to this response. Parent and child sessions may
            # share one thread-safe provider instance and complete in parallel.
            "_usage": request_usage,
        }

    def configure_generation(
        self,
        *,
        thinking_type: str | None = None,
        response_format: str | None = None,
        reasoning_effort: str | None = None,
    ) -> "OpenAICompatibleModel":
        if thinking_type not in {None, "enabled", "disabled"}:
            raise ValueError("thinking_type must be enabled, disabled, or None")
        if response_format not in {None, "text", "json_object"}:
            raise ValueError("unsupported response_format")
        self.thinking_type = thinking_type
        self.response_format = response_format
        if reasoning_effort is not None:
            self.reasoning_effort = self._effort(reasoning_effort)
        return self

    @staticmethod
    def _effort(value: str | None) -> str | None:
        if value in (None, "auto"):
            return None
        if value not in {"none", "minimal", "low", "medium", "high", "xhigh", "max"}:
            raise ValueError("reasoning_effort must be auto, none, minimal, low, medium, high, xhigh or max")
        return value

    def configure_output_limit(
        self,
        max_output_tokens_per_call: int | None,
    ) -> "OpenAICompatibleModel":
        """Set a workload-specific output limit.

        ``None`` omits ``max_tokens`` and delegates the limit to the provider.
        """
        if (
            max_output_tokens_per_call is not None
            and max_output_tokens_per_call < 1
        ):
            raise ValueError("max_output_tokens_per_call must be positive or None")
        self.max_output_tokens_per_call = max_output_tokens_per_call
        return self

    @property
    def limits(self) -> ModelLimits:
        return ModelLimits(
            self.context_window_tokens,
            self.max_output_tokens_per_call,
            self.output_reserve_tokens,
            self.input_capacity_tokens,
        )

    def usage_snapshot(self) -> dict[str, int]:
        """Return cumulative provider-reported usage for this model instance."""
        with self._usage_lock:
            return dict(self._usage)

    def last_usage(self) -> dict[str, int]:
        """Return usage for the most recently completed request."""
        with self._usage_lock:
            return dict(self._last_usage)

    def _record_usage(self, raw: Any) -> dict[str, int]:
        usage = raw if isinstance(raw, dict) else {}
        details = usage.get("prompt_tokens_details", usage.get("input_tokens_details"))
        details = details if isinstance(details, dict) else {}
        input_tokens = _integer(
            usage.get("prompt_tokens", usage.get("input_tokens", 0))
        )
        output_tokens = _integer(
            usage.get("completion_tokens", usage.get("output_tokens", 0))
        )
        cache_read = _integer(
            usage.get(
                "prompt_cache_hit_tokens",
                details.get("cached_tokens", usage.get("cache_read_tokens", 0)),
            )
        )
        cache_miss = _integer(
            usage.get("prompt_cache_miss_tokens", max(0, input_tokens - cache_read))
        )
        normalized = {
            "input_tokens": input_tokens,
            "cache_read_tokens": cache_read,
            "cache_miss_tokens": cache_miss,
            "output_tokens": output_tokens,
            "requests": 1,
        }
        with self._usage_lock:
            self._last_usage = normalized
            for key, amount in normalized.items():
                self._usage[key] += amount
        return dict(normalized)


def _integer(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _positive_setting(value: str, name: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise ValueError(f"{name} must be a positive integer") from exc
    if parsed < 1:
        raise ValueError(f"{name} must be a positive integer")
    return parsed


def _optional_float_setting(
    override: str | None, configured: float | None, name: str
) -> float | None:
    if override is None:
        return configured
    try:
        value = float(override)
    except ValueError as exc:
        raise ValueError(f"{name} must be a nonnegative number") from exc
    if not 0 <= value < float("inf"):
        raise ValueError(f"{name} must be a finite nonnegative number")
    return value


def _is_context_length_error(detail: str) -> bool:
    lowered = detail.lower()
    return any(marker in lowered for marker in (
        "context_length_exceeded", "context length", "context window",
        "maximum context", "too many tokens", "prompt is too long",
    ))
