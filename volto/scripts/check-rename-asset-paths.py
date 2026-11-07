"""Check renamed file references and released content URLs without a build or service."""

import json
from pathlib import Path
import re
import subprocess

ROOT = Path(__file__).resolve().parents[2]
BASELINE = "7869dad74"
checks = []


def original(path):
    return subprocess.check_output(
        ["git", "show", BASELINE + ":" + path.replace("volto/", "openbot/", 1)], cwd=ROOT
    ).decode()


def check(name, condition):
    checks.append({"check": name, "passed": bool(condition)})


def resolves(path):
    extensions = [".ts", ".tsx", ".js", ".mjs", ".cjs", ".json", ".css"]
    return any(candidate.is_file() for candidate in [
        path, *[Path(str(path) + extension) for extension in extensions],
        path / "index.ts", path / "index.tsx",
    ])


files = subprocess.check_output(["git", "ls-files", "-z", "volto"], cwd=ROOT).decode().split("\0")
# Imports are checked at their source directory. Runtime paths resolved from a build directory
# have a different base and are covered by the runtime identity audit.
imports = re.compile(r'(?:\bfrom\s*|\b(?:import|require)\s*\(\s*)["\'](\.{1,2}/[^"\'\n]+)["\']')
for relative in files:
    path = ROOT / relative
    if path.suffix not in [".ts", ".tsx", ".js", ".mjs", ".cjs"]:
        continue
    for match in imports.finditer(path.read_text()):
        specifier = match[1]
        if not re.search("openbot|volto", specifier, re.IGNORECASE):
            continue
        check(relative + ": " + specifier, resolves(path.parent / specifier.split("?", 1)[0]))

for path in ["volto/apps/auth-api/src/lib/guides.ts", "volto/apps/auth-api/src/lib/news.ts"]:
    pattern = r'\bslug:\s*"([^"]+)"'
    check("Released article URLs: " + path,
          re.findall(pattern, original(path)) == re.findall(pattern, (ROOT / path).read_text()))
for path in ["volto/apps/auth-api/src/content/guides/index.ts", "volto/apps/auth-api/src/content/news/index.ts"]:
    pattern = r'^\s*"([^"]+)":'
    check("Released article body keys: " + path,
          re.findall(pattern, original(path), re.MULTILINE) == re.findall(pattern, (ROOT / path).read_text(), re.MULTILINE))
for path in ["volto/apps/auth-api/src/routes/news/openbot-vs-grokbot.ts",
             "volto/apps/auth-api/src/routes/news/og/openbot-vs-grokbot[.]png.ts"]:
    pattern = r'createFileRoute\("([^"]+)"\)'
    check("Released redirect route: " + path,
          re.findall(pattern, original(path)) == re.findall(pattern, (ROOT / path).read_text()))
path = "volto/apps/auth-api/content-art/manifest.json"
manifest = json.loads((ROOT / path).read_text())
check("Content art paths match existing published paths", set(manifest) == set(json.loads(original(path))))
for asset in manifest:
    check("Content art exists: " + asset, (ROOT / "volto/apps/auth-api/content-art" / asset).is_file())
path = "volto/apps/auth-api/src/lib/site-metadata.ts"
pattern = r'(?:openbot|volto)-social\.png'
check("Published social image URL", re.findall(pattern, original(path)) == re.findall(pattern, (ROOT / path).read_text()))
check("Published social image exists", (ROOT / "volto/apps/auth-api/public/openbot-social.png").is_file())
check("Renderer seed logo exists", (ROOT / "volto/src/renderer/src/assets/openbot-logo-dev.png").is_file())
report = {
    "baseline": BASELINE,
    "scope": "Static source imports, actual assets and released content URL paths. No build or service access.",
    "passed": all(item["passed"] for item in checks),
    "checks": checks,
}
output = ROOT / "volto/.volto-build/rename-asset-paths/report.json"
output.parent.mkdir(parents=True, exist_ok=True)
output.write_text(json.dumps(report, indent=2) + "\n")
print(json.dumps({"passed": report["passed"], "checks": len(checks), "failed": [item for item in checks if not item["passed"]]}))
raise SystemExit(0 if report["passed"] else 1)
