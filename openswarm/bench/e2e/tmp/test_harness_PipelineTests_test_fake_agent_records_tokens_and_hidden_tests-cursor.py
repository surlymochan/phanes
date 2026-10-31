#!/usr/bin/env python3
import json, pathlib, sys
root = pathlib.Path.cwd()
if (root / "_hidden_acceptance").exists():
    print("hidden tests were visible to the agent", file=sys.stderr)
    sys.exit(3)
target = root / "src" / "paginate.js"
target.write_text(target.read_text().replace(
    "const start = page * pageSize;",
    "const start = (page - 1) * pageSize;",
))
sys.stdout.write(json.dumps({
    "type": "system", "subtype": "init", "session_id": "sess-fake", "model": "grok-4.7-high"
}) + "\n")
sys.stdout.write(json.dumps({
    "type": "result", "subtype": "success", "session_id": "sess-fake",
    "usage": {"inputTokens": 11, "outputTokens": 5, "cacheReadTokens": 7, "cacheWriteTokens": 2}
}) + "\n")
