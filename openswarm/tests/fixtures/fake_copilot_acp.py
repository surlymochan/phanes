#!/usr/bin/env python3
"""Copilot ACP subset for lane tests: session/new carries models.availableModels with _meta usage fields.

FAKE_COPILOT_MODELS holds a JSON list of {"modelId", "copilotUsage", "copilotEnablement"} entries; the
session/new reply lists them as the real `copilot --acp` does, so the lane can check the model it selects.
"""
import json
import os
import sys


def send(message):
    sys.stdout.write(json.dumps(message) + "\n")
    sys.stdout.flush()


models = json.loads(os.environ.get("FAKE_COPILOT_MODELS", "[]"))
available = []
for entry in models:
    meta = {}
    if "copilotUsage" in entry:
        meta["copilotUsage"] = entry["copilotUsage"]
    if "copilotEnablement" in entry:
        meta["copilotEnablement"] = entry["copilotEnablement"]
    available.append({"modelId": entry["modelId"], "name": entry["modelId"], "_meta": meta})
config_options = [{"id": "model", "name": "Model", "category": "model", "type": "select",
                   "currentValue": available[0]["modelId"] if available else "",
                   "options": [{"value": item["modelId"], "name": item["modelId"]} for item in available]}]

for line in sys.stdin:
    try:
        request = json.loads(line)
    except ValueError:
        continue
    method = request.get("method")
    if method == "initialize":
        result = {"protocolVersion": 1, "agentCapabilities": {}, "agentInfo": {"name": "fake-copilot", "version": "1"}}
    elif method == "session/new":
        result = {"sessionId": "fake-copilot-session", "configOptions": config_options,
                  "models": {"currentModelId": config_options[0]["currentValue"], "availableModels": available}}
    elif method == "session/set_config_option":
        params = request.get("params") or {}
        config_options[0]["currentValue"] = params.get("value")
        result = {"configOptions": config_options}
    elif method == "session/prompt":
        result = {"stopReason": "end_turn"}
    else:
        continue
    send({"jsonrpc": "2.0", "id": request.get("id"), "result": result})
