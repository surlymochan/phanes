"""Copilot ACP model list: parse `availableModels` and check one model against the usage cap.

Copilot runs only inside the monthly included allowance, so a lane must not select a model that is
missing, disabled, or priced above the policy cap. This module is pure: it does no I/O and sends no
requests. It reads what the lane already logged from `session/new`.
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, Iterable, List, Optional, Union

OK, MISSING, DISABLED, TOO_EXPENSIVE = "ok", "missing", "disabled", "too_expensive"
ENABLED_VALUES = ("enabled", "true", "1", "yes", "on", "available", "included")
_MULTIPLIER = re.compile(r"^\s*(\d+(?:\.\d+)?|\.\d+)\s*x?\s*$", re.IGNORECASE)
_EPSILON = 1e-9


def parse_multiplier(value: Any) -> Optional[float]:
    """`1x` -> 1.0, `0.33x` -> 0.33, `1.5` -> 1.5; anything missing or unparsable -> None."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value) if value >= 0 else None
    if not isinstance(value, str):
        return None
    match = _MULTIPLIER.match(value)
    return float(match.group(1)) if match else None


def _enabled(value: Any) -> bool:
    """Fail closed: only an explicit enabled marker counts; absent or unknown values are not enabled."""
    if isinstance(value, bool):
        return value
    return isinstance(value, str) and value.strip().lower() in ENABLED_VALUES


def _model_entry(item: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    model_id = item.get("modelId")
    if not isinstance(model_id, str) or not model_id.strip():
        return None
    meta = item.get("_meta") if isinstance(item.get("_meta"), dict) else {}
    price = meta.get("copilotPriceCategory")
    return {
        "value": model_id.strip(),
        "usage_multiplier": parse_multiplier(meta.get("copilotUsage")),
        "enabled": _enabled(meta.get("copilotEnablement")),
        "price_category": price if isinstance(price, str) and price.strip() else None,
    }


def _find_lists(node: Any, found: List[list]) -> None:
    """Collect every `availableModels` list in a response, wherever the envelope nests it."""
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "availableModels" and isinstance(value, list):
                found.append(value)
            else:
                _find_lists(value, found)
    elif isinstance(node, list):
        for value in node:
            _find_lists(value, found)


def _as_documents(events_or_json: Any) -> Iterable[Any]:
    if isinstance(events_or_json, (bytes, bytearray)):
        events_or_json = events_or_json.decode("utf-8", errors="replace")
    if isinstance(events_or_json, str):
        text = events_or_json.strip()
        if not text:
            return []
        try:
            return [json.loads(text)]
        except ValueError:
            docs = []
            for line in text.splitlines():
                try:
                    docs.append(json.loads(line))
                except ValueError:
                    continue
            return docs
    if isinstance(events_or_json, dict):
        return [events_or_json]
    if isinstance(events_or_json, list):
        return events_or_json
    return []


def parse_available_models(events_or_json: Union[str, bytes, dict, list, None]) -> List[Dict[str, Any]]:
    """Models from ACP `session/new` replies in a JSON document, a JSONL log, or a list of events.

    The first occurrence of a model id wins.
    """
    models: Dict[str, Dict[str, Any]] = {}
    for document in _as_documents(events_or_json):
        lists: List[list] = []
        _find_lists(document, lists)
        for entries in lists:
            for item in entries:
                entry = _model_entry(item) if isinstance(item, dict) else None
                if entry and entry["value"] not in models:
                    models[entry["value"]] = entry
    return list(models.values())


def check(model: str, models: Iterable[Dict[str, Any]], max_multiplier: float) -> Dict[str, Any]:
    """Classify one model as ok / missing / disabled / too_expensive, with an operator-readable detail.

    An unknown multiplier counts as too expensive: the cap must be proven, not assumed.
    """
    listed = list(models)
    # Mirror the runtime: an exact id wins, otherwise the id before any `[...]` suffix matches.
    entry = next((m for m in listed if m.get("value") == model), None)
    if entry is None:
        entry = next((m for m in listed if str(m.get("value", "")).split("[", 1)[0] == model), None)
    if entry is None:
        return {"status": MISSING, "model": model, "detail": f"{model} is not in the Copilot ACP model list"}
    if not entry.get("enabled", False):
        return {"status": DISABLED, "model": model,
                "detail": f"{model} is not enabled for this account (copilotEnablement not 'enabled')"}
    multiplier = entry.get("usage_multiplier")
    if multiplier is None:
        return {"status": TOO_EXPENSIVE, "model": model,
                "detail": f"{model} reports no usage multiplier; the cap {max_multiplier:g}x cannot be verified"}
    if multiplier > max_multiplier + _EPSILON:
        return {"status": TOO_EXPENSIVE, "model": model,
                "detail": f"{model} costs {multiplier:g}x, above the cap {max_multiplier:g}x"}
    return {"status": OK, "model": model, "usage_multiplier": multiplier,
            "detail": f"{model} is enabled at {multiplier:g}x (cap {max_multiplier:g}x)"}
