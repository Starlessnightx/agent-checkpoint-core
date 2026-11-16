from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit


@dataclass(frozen=True)
class ModelCapabilities:
    """Provider-reported model limits used for planning, never as task budgets."""

    model: str
    context_window_tokens: int | None = None
    input_capacity_tokens: int | None = None
    max_output_tokens: int | None = None
    source: str = "provider"
    supports_image_input: bool | None = None


_CONTEXT_KEYS = (
    "context_window_tokens",
    "context_window",
    "context_length",
    "max_context_tokens",
    "max_context_length",
)
_INPUT_KEYS = ("max_input_tokens", "input_token_limit")
_OUTPUT_KEYS = (
    "max_output_tokens",
    "max_completion_tokens",
    "output_token_limit",
)

# Documentation fallback is deliberately restricted to the official endpoint:
# a gateway may map an identical model ID to a different context capacity.
CATALOG_VERSION = "2026-10-03"
_OPENAI_CATALOG = {
    "gpt-6-astra": (1_050_000, 128_000),
    "gpt-6.1-sol": (1_050_000, 128_000),
    "gpt-6-luna": (1_050_000, 128_000),
}


def catalog_capabilities(model: str, base_url: str) -> ModelCapabilities | None:
    if urlsplit(base_url).hostname != "api.openai.com":
        return None
    limits = _OPENAI_CATALOG.get(model)
    if limits is None:
        return None
    return ModelCapabilities(
        model=model, context_window_tokens=limits[0], max_output_tokens=limits[1],
        source=f"catalog:{CATALOG_VERSION}:https://developers.openai.com/api/docs/models",
    )


def metadata_model_id(model: str, base_url: str) -> str:
    """Resolve only documented aliases on their own official provider."""
    if urlsplit(base_url).hostname == "api.deepseek.com" and model in {
        "deepseek-v4-flash", "deepseek-v4-flash-vision-exp"
    }:
        # https://api-docs.deepseek.com/quick_start/pricing/ (2026-10-03)
        return "deepseek-flash"
    return model


def capabilities_from_payload(
    payload: Any, model: str, *, source: str = "provider"
) -> ModelCapabilities | None:
    """Extract common limit fields from OpenAI-compatible model metadata."""
    entry = _select_model_entry(payload, model)
    if entry is None:
        return None
    containers = [entry]
    for key in ("capabilities", "limits", "metadata", "architecture"):
        nested = entry.get(key)
        if isinstance(nested, dict):
            containers.append(nested)
    context = _first_positive(containers, _CONTEXT_KEYS)
    input_capacity = _first_positive(containers, _INPUT_KEYS)
    output = _first_positive(containers, _OUTPUT_KEYS)
    vision = None
    for container in containers:
        explicit = container.get("supports_image_input", container.get("supports_vision"))
        modalities = container.get("input_modalities")
        if isinstance(explicit, bool):
            vision = explicit
            break
        if isinstance(modalities, list) and modalities and all(isinstance(item, str) for item in modalities):
            vision = "image" in modalities
            break
    if context is None and input_capacity is None and output is None and vision is None:
        return None
    return ModelCapabilities(
        model=model,
        context_window_tokens=context,
        input_capacity_tokens=input_capacity,
        max_output_tokens=output,
        source=source,
        supports_image_input=vision,
    )


def _select_model_entry(payload: Any, model: str) -> dict[str, Any] | None:
    if not isinstance(payload, dict):
        return None
    data = payload.get("data")
    if isinstance(data, list):
        entries = [item for item in data if isinstance(item, dict)]
        exact = next(
            (item for item in entries if str(item.get("id", "")) == model), None
        )
        return exact
    identifier = payload.get("id")
    return payload if identifier is None or str(identifier) == model else None


def _first_positive(
    containers: list[dict[str, Any]], keys: tuple[str, ...]
) -> int | None:
    for container in containers:
        for key in keys:
            value = container.get(key)
            if value is None or isinstance(value, bool):
                continue
            try:
                parsed = int(value)
            except (TypeError, ValueError):
                continue
            if parsed > 0:
                return parsed
    return None
