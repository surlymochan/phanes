"""Check released resource and storage identifiers without contacting services."""

import json
from pathlib import Path
import re
import subprocess

ROOT = Path(__file__).resolve().parents[2]
BASELINE = "7869dad74"
checks = []


def original(path):
    return subprocess.check_output(
        ["git", "show", BASELINE + ":" + path.replace("volto/", "openbot/", 1)],
        cwd=ROOT,
    ).decode()


def check(name, condition):
    checks.append({"check": name, "passed": bool(condition)})


for app in ["auth-api", "site-router"]:
    path = f"volto/apps/{app}/wrangler.jsonc"
    pattern = r'"(?:name|database_name|database_id|bucket_name|preview_bucket_name|REMOTE_TICKET_KEY_ID|APNS_TOPIC)"\s*:\s*"[^"]+"'
    check(app + " resources", re.findall(pattern, original(path)) == re.findall(pattern, (ROOT / path).read_text()))

path = "volto/apps/mobile/app.json"
old = json.loads(original(path))["expo"]
new = json.loads((ROOT / path).read_text())["expo"]
for key in ["slug", "scheme", "owner", "extra", "updates"]:
    check("Expo " + key, new[key] == old[key])
for platform, key in [("ios", "bundleIdentifier"), ("android", "package")]:
    check(platform + " " + key, new[platform][key] == old[platform][key])
sound_plugin = "./plugins/with-notification-sounds"
before_sounds = next(p[1]["sounds"] for p in old["plugins"] if isinstance(p, list) and p[0] == sound_plugin)
after_sounds = next(p[1]["sounds"] for p in new["plugins"] if isinstance(p, list) and p[0] == sound_plugin)
check("Released notification sound paths", before_sounds == after_sounds)
for sound in after_sounds:
    path = ROOT / "volto/apps/mobile" / sound
    check("Notification asset exists: " + sound, path.is_file())
    baseline_path = "7869dad74:openbot/apps/mobile/" + sound.removeprefix("./")
    check("Notification asset bytes: " + sound, path.read_bytes() == subprocess.check_output(["git", "show", baseline_path], cwd=ROOT))

# The external release repository is not part of the product rename.
for path in sorted((ROOT / "volto/packages/i18n/src/messages").rglob("server.ts")):
    relative = str(path.relative_to(ROOT))
    pattern = r"github\.com/nightly-labs/[^\s/]+/releases"
    check("External release repository: " + relative, re.findall(pattern, original(relative)) == re.findall(pattern, path.read_text()))

for plugin in ["./plugins/with-share-target", "expo-widgets", "./plugins/with-notification-service"]:
    before = next(p[1] for p in old["plugins"] if isinstance(p, list) and p[0] == plugin)
    after = next(p[1] for p in new["plugins"] if isinstance(p, list) and p[0] == plugin)
    if "ios" in before:
        before, after = before["ios"], after["ios"]
    for key in ["appGroupId", "extensionBundleIdentifier", "bundleIdentifier", "groupIdentifier"]:
        if key in before:
            check(plugin + " " + key, before[key] == after[key])

for path in ["volto/CHANGELOG.md", "volto/apps/mobile/CHANGELOG.md", "volto/apps/mobile/google-services.json"]:
    check("Exact history or Firebase config: " + path, original(path) == (ROOT / path).read_text())

stored_keys = 0
for path in (ROOT / "volto/apps/mobile/src").rglob("*.ts"):
    before = original(str(path.relative_to(ROOT)))
    for key in re.findall(r'["`]((?:openbot\.(?:mobile|host-key|share-target|after-first-unlock|app-lock|recent-agents|server-order|default-server))[^"`\n]*)["`]', before):
        check(str(path.relative_to(ROOT)) + ": " + key, key in path.read_text())
        stored_keys += 1

for path, values in {
    "volto/remote/compose.yaml": ["name: openbot-remote", "container_name: openbot-coturn"],
    "volto/remote/compose.dev.yaml": ["name: openbot-remote-dev"],
    "volto/remote/nginx/signal.openbot.run.conf": ["/openbot-remote_acme-data/"],
    "volto/apps/auth-api/src/server/hosted-server-service.ts": ["openbot-hosted-claim-key:v1", "openbot-hosted-claim:v1:", "`openbot-${plan}-`"],
    "volto/apps/auth-api/src/server/hosted-site-service.ts": [r"\.openbot\.site"],
    "volto/apps/auth-api/src/server/browser-api.ts": ["__Host-openbot-web"],
    "volto/apps/auth-api/src/server/billing-service.ts": ["openbot-customer-"],
    "volto/apps/mobile/plugins/with-notification-service.js": ["<key>OpenBotAppGroup</key>"],
    "volto/apps/mobile/plugins/notification-service/NotificationService.swift": ['"OpenBotAppGroup"'],
}.items():
    for value in values:
        check(path + ": " + value, value in (ROOT / path).read_text())

# Protected documents must remain byte-identical to the pre-rename baseline.
for path in ["HANDOFF.md", "volto/docs/ARCHITECTURE.md"]:
    check("Protected document baseline: " + path, original(path) == (ROOT / path).read_text())
check("README retained AppArmor path exists", (ROOT / "volto/build/linux/openbot.apparmor").is_file())

report = {
    "status": "passed" if all(c["passed"] for c in checks) else "failed",
    "baseline": BASELINE,
    "storedKeys": stored_keys,
    "scope": "Offline config and source checks. No deployment or device launch.",
    "checks": checks,
}
output = ROOT / "volto/.volto-build/external-mobile-compat-report.json"
output.parent.mkdir(parents=True, exist_ok=True)
output.write_text(json.dumps(report, indent=2) + "\n")
print(json.dumps({"status": report["status"], "checks": len(checks), "report": str(output.relative_to(ROOT))}))
raise SystemExit(0 if report["status"] == "passed" else 1)
