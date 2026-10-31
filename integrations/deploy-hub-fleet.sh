#!/usr/bin/env bash
set -euo pipefail

# Deploy the existing Hub fleet path. Default mode is deliberately read-only.
usage() {
  cat <<'EOF'
Usage: integrations/deploy-hub-fleet.sh [--apply] [--restart-hub] <main-sha>

Without --apply this only validates the real local/gocc/gores1 paths and the
selected commit. --apply rolls only idle, clean lanes. --restart-hub is a
separate explicit step because the OpenBot.app launchd environment changes.
EOF
}

apply=0
restart_hub=0
while (($#)); do
  case "$1" in
    --apply) apply=1 ;;
    --restart-hub) restart_hub=1 ;;
    -h|--help) usage; exit 0 ;;
    *) break ;;
  esac
  shift
done
if (($# != 1)) || ! [[ "$1" =~ ^[0-9a-f]{40}$ ]]; then usage >&2; exit 2; fi
sha="$1"

source_root="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
hub_runtime="${OPENSWARM_HUB_RUNTIME:-$HOME/tools/openswarm-hub-local}"
hub_home="${OPENSWARM_HUB_HOME:-$HOME/.claude/openswarm-executor-candidate}"
bridge_config="${OPENSWARM_HUB_BRIDGE_CONFIG:-$HOME/.claude/openswarm-hub-bridge/config.json}"
hub_plist="${OPENBOT_HUB_PLIST:-$HOME/Library/LaunchAgents/app.openbot.hub.local.plist}"
local_runtime="${OPENSWARM_LOCAL_RUNTIME:-$HOME/.local/share/phanes-openswarm-422}"
local_home="${OPENSWARM_LOCAL_HOME:-$HOME/.claude/openswarm-phanes-422}"

# Verified current paths. They may be overridden only for a deliberate host migration.
gocc_runtime="${OPENSWARM_GOCC_RUNTIME:-/Users/surlymo/tools/openswarm-executor-candidate}"
gocc_repo="${OPENSWARM_GOCC_REPO:-/Users/surlymo/work/phanes-fleet-d6f3f2f2}"
gores1_runtime="${OPENSWARM_GORES1_RUNTIME:-/root/tools/openswarm-executor-candidate}"
gores1_repo="${OPENSWARM_GORES1_REPO:-/root/work/phanes-fleet-d6f3f2f2}"
required=(openswarm openswarm-fleet openswarm-auto hub-openswarm-bridge fleet-result-bundle)

for file in "${required[@]}"; do
  test -x "$source_root/openswarm/bin/$file" || { echo "missing source helper: $file" >&2; exit 1; }
done
git -C "$source_root" diff --quiet
git -C "$source_root" diff --cached --quiet
git -C "$source_root" cat-file -e "$sha^{commit}"
test -f "$bridge_config" && test -f "$hub_plist"

remote_preflight() {
  local host="$1" runtime="$2" repo="$3" lanes="$4"
  ssh -T -o BatchMode=yes -o ConnectTimeout=8 "$host" bash -s -- "$runtime" "$repo" "$lanes" <<'REMOTE'
set -euo pipefail
runtime="$1"; repo="$2"; lanes="${3//,/ }"
test -x "$runtime/bin/openswarm-fleet"
git -C "$repo" rev-parse --is-inside-work-tree >/dev/null
status="$("$runtime/bin/openswarm-fleet" lane status)"
for lane in $lanes; do test -d "$HOME/.claude/openswarm-executor-candidate/lanes/$lane/wt"; done
python3 - "$lanes" "$status" <<'PY'
import json, sys
targets = set(sys.argv[1].split())
rows = json.loads(sys.argv[2])
excluded = [f"{row.get('name')}:{row.get('backend')}:{row.get('state')}" for row in rows if row.get("name") not in targets]
if excluded: print("preserved, excluded from 4:2:2 Hub routing: " + ", ".join(excluded))
PY
REMOTE
}

echo "preflight: Hub=$hub_runtime; gocc=$gocc_repo; gores1=$gores1_repo"
remote_preflight gocc "$gocc_runtime" "$gocc_repo" 'C1,A1,A2,GH1,GH2'
remote_preflight gores1 "$gores1_runtime" "$gores1_repo" 'K1,M1,M2,M3,M4'
test -x "$local_runtime/bin/openswarm"
local_status="$(OPENSWARM_HOME="$local_home" "$local_runtime/bin/openswarm" lane status)"
for lane in K1 K2; do test -d "$local_home/lanes/$lane/wt"; done
python3 - "$local_status" <<'PY'
import json, sys
for row in json.loads(sys.argv[1]):
    if row.get("name") in {"M3", "M4"}:
        print(f"local Pi lane not rolled unless recovered: {row.get('name')}:{row.get('state')}")
PY
if (( ! apply )); then
  echo "read-only preflight passed for $sha; no lane, reservation, runtime, or service changed."
  exit 0
fi

git -C "$source_root" fetch --no-tags origin main
git -C "$source_root" cat-file -e "$sha^{commit}"
git -C "$source_root" checkout --detach "$sha"
stage="$(mktemp -d "${TMPDIR:-/tmp}/phanes-hub-fleet.XXXXXX")"
trap 'rm -rf "$stage"' EXIT
bundle="$stage/phanes-$sha.bundle"
# A raw object ID is not a bundle revision name when it is already reachable
# from a local ref, and Git can silently reject it as empty. Name the selected
# commit with a short-lived ref, build a self-contained bundle, then remove it.
bundle_ref="refs/openswarm/deploy-staging/$sha"
git -C "$source_root" update-ref "$bundle_ref" "$sha"
trap 'git -C "$source_root" update-ref -d "$bundle_ref" || true; rm -rf "$stage"' EXIT
git -C "$source_root" bundle create "$bundle" "$bundle_ref"
git -C "$source_root" bundle verify "$bundle" >/dev/null
git -C "$source_root" update-ref -d "$bundle_ref"
trap 'rm -rf "$stage"' EXIT

install_local_runtime() {
  mkdir -p "$hub_runtime/bin" "$hub_home"
  chmod 700 "$hub_runtime" "$hub_runtime/bin"
  for file in "${required[@]}"; do
    install -m 700 "$source_root/openswarm/bin/$file" "$hub_runtime/bin/$file"
  done

  # openswarm-fleet intentionally fixes OPENSWARM_HOME to hub_home, so the
  # direct-fleet inventory belongs there. The existing capacity guard remains
  # at its established path and is supplied through launchd below.
  python3 - "$hub_home/fleet.json" "$source_root" "$gocc_runtime" "$gocc_repo" "$gores1_runtime" "$gores1_repo" <<'PY'
import json, os, pathlib, sys, tempfile
path, local_repo, gocc_bin, gocc_repo, gores_bin, gores_repo = map(pathlib.Path, sys.argv[1:])
payload = {"hosts": [
    {"name": "local", "kind": "local", "repos": {"openbot": str(local_repo)}},
    {"name": "gocc", "kind": "ssh", "ssh": "gocc", "openswarm": str(gocc_bin / "bin/openswarm-fleet-bundle-adapter"), "repos": {"openbot": str(gocc_repo)}},
    {"name": "gores1", "kind": "ssh", "ssh": "gores1", "openswarm": str(gores_bin / "bin/openswarm-fleet-bundle-adapter"), "repos": {"openbot": str(gores_repo)}},
]}
path.parent.mkdir(parents=True, exist_ok=True)
fd, temp = tempfile.mkstemp(prefix="fleet.", dir=path.parent)
with os.fdopen(fd, "w") as out:
    json.dump(payload, out, indent=2); out.write("\n")
os.chmod(temp, 0o600); os.replace(temp, path)
PY

  # Preserve the user's configured Hub workspace; only move its result-fetch
  # repository to the clean checkout pinned to the requested main commit.
  python3 - "$bridge_config" "$source_root" <<'PY'
import json, os, pathlib, sys, tempfile
path, repo = map(pathlib.Path, sys.argv[1:])
data = json.load(path.open())
workspace = data.get("workspace")
if not isinstance(workspace, str) or not pathlib.Path(workspace).is_dir():
    raise SystemExit("Hub bridge workspace is missing or invalid")
data["repo"] = str(repo)
fd, temp = tempfile.mkstemp(prefix="config.", dir=path.parent)
with os.fdopen(fd, "w") as out:
    json.dump(data, out, indent=2); out.write("\n")
os.chmod(temp, 0o600); os.replace(temp, path)
PY

  # This only writes the next-process environment. It deliberately does not
  # reload the app unless --restart-hub was supplied at the end of this script.
  python3 - "$hub_plist" "$hub_runtime" <<'PY'
import os, pathlib, plistlib, sys, tempfile
path, runtime = map(pathlib.Path, sys.argv[1:])
with path.open("rb") as source: data = plistlib.load(source)
env = data.setdefault("EnvironmentVariables", {})
env.update({
  "OPENSWARM_HUB_LOCAL_ROUTER": "1",
  "OPENSWARM_AUTO_LOCAL_HOST": "local",
  "OPENSWARM_AUTO_FLEET_HOSTS": '["gores1","gocc"]',
  "OPENSWARM_AUTO_CLI": str(runtime / "bin/openswarm-fleet"),
  "OPENSWARM_AUTO_HOST_BACKENDS": '{"local":[],"gores1":["pi"],"gocc":["agy","copilot"]}',
  "OPENSWARM_FLEET_GUARD_REQUIRED": "1",
  "OPENSWARM_FLEET_GUARD_CONFIG": str(pathlib.Path.home() / ".claude/openswarm-phanes-422/fleet-capacity.json"),
})
fd, temp = tempfile.mkstemp(prefix="openbot-hub.", dir=path.parent)
with os.fdopen(fd, "wb") as out: plistlib.dump(data, out)
os.chmod(temp, 0o600); os.replace(temp, path)
PY
}

remote_apply() {
  local host="$1" runtime="$2" repo="$3" lanes="$4" remote_bundle="/tmp/phanes-$sha.bundle"
  scp -q "$bundle" "$host:$remote_bundle"
  ssh -T -o BatchMode=yes -o ConnectTimeout=8 "$host" bash -s -- "$runtime" "$repo" "$remote_bundle" "$sha" "$bundle_ref" "$lanes" <<'REMOTE'
set -euo pipefail
runtime="$1"; repo="$2"; bundle="$3"; sha="$4"; bundle_ref="$5"; lanes="${6//,/ }"
git -C "$repo" fetch --no-tags "$bundle" "${bundle_ref}:refs/openswarm/releases/$sha"
git -C "$repo" cat-file -e "$sha^{commit}"

release_root="$runtime/releases/$sha"
if [ ! -d "$release_root/openswarm/bin" ]; then
  staging="$runtime/releases/.staging-$sha-$$"
  mkdir -p "$staging"
  git -C "$repo" archive "$sha" openswarm/bin/openswarm openswarm/bin/openswarm-fleet \
    openswarm/bin/openswarm-auto openswarm/bin/hub-openswarm-bridge \
    openswarm/bin/fleet-result-bundle openswarm/bin/openswarm-fleet-bundle-adapter | tar -x -C "$staging"
  chmod 700 "$staging/openswarm/bin"/*
  mv "$staging" "$release_root"
fi
release_bin="$release_root/openswarm/bin"
installed=0
for lane in $lanes; do
  status="$("$runtime/bin/openswarm-fleet" lane status)"
  if python3 - "$lane" "$status" <<'PY'
import json, sys
lane, status = sys.argv[1:]
rows = {row.get("name"): row for row in json.loads(status)}
row = rows.get(lane)
if row is None: raise SystemExit(2)
if row.get("state") != "idle" or row.get("current_run"): raise SystemExit(3)
PY
  then
    :
  else
    code=$?
    if [ "$code" -eq 3 ]; then echo "deferred busy lane: $lane"; continue; fi
    echo "invalid lane status for $lane" >&2; exit "$code"
  fi
  wt="$HOME/.claude/openswarm-executor-candidate/lanes/$lane/wt"
  if test -n "$(git -C "$wt" status --porcelain)"; then
    echo "deferred dirty lane worktree: $lane"; continue
  fi
  # C1/K1 use older source repositories, so each lane's own common Git dir
  # receives the selected object before its worktree is detached at main.
  git -C "$wt" fetch --no-tags "$bundle" "${bundle_ref}:refs/openswarm/releases/$sha"
  if [ "$installed" -eq 0 ]; then
    # These named helpers add source-host bundle support without replacing the
    # shared active runtime. Ordinary calls continue through the old wrapper.
    install -m 700 "$release_bin/openswarm-fleet-bundle-adapter" "$runtime/bin/openswarm-fleet-bundle-adapter"
    install -m 700 "$release_bin/fleet-result-bundle" "$runtime/bin/fleet-result-bundle"
    installed=1
  fi
  git -C "$wt" checkout --detach "$sha"
  "$release_bin/openswarm-fleet" lane down "$lane"
  "$release_bin/openswarm-fleet" lane up "$lane"
  after="$("$runtime/bin/openswarm-fleet" lane status)"
  python3 - "$lane" "$after" "$release_bin/openswarm" <<'PY'
import json, subprocess, sys
lane, status, executable = sys.argv[1:]
row = next((row for row in json.loads(status) if row.get("name") == lane), None)
if not row or row.get("state") != "idle" or not isinstance(row.get("supervisor_pid"), int):
    raise SystemExit("new lane supervisor is not idle")
command = subprocess.check_output(["ps", "-p", str(row["supervisor_pid"]), "-o", "command="], text=True)
if executable not in command:
    raise SystemExit("new lane supervisor did not use selected release")
PY
  echo "rolled lane: $lane"
done
if [ "$installed" -eq 0 ]; then echo "no idle clean lane on this host; retry later"; fi
REMOTE
}

local_apply() {
  local release_root="$local_runtime/releases/$sha" release_bin="$local_runtime/releases/$sha/openswarm/bin"
  if [ ! -d "$release_bin" ]; then
    mkdir -p "$release_root/openswarm/bin"
    for file in "${required[@]}" openswarm-fleet-bundle-adapter; do
      install -m 700 "$source_root/openswarm/bin/$file" "$release_bin/$file"
    done
  fi
  for lane in K1 K2; do
    local status code wt after
    status="$(OPENSWARM_HOME="$local_home" "$local_runtime/bin/openswarm" lane status)"
    if python3 - "$lane" "$status" <<'PY'
import json, sys
lane, status = sys.argv[1:]
row = next((row for row in json.loads(status) if row.get("name") == lane), None)
if row is None: raise SystemExit(2)
if row.get("state") != "idle" or row.get("current_run"): raise SystemExit(3)
PY
    then :; else
      code=$?
      if [ "$code" -eq 3 ]; then echo "deferred busy local lane: $lane"; continue; fi
      echo "invalid local lane status: $lane" >&2; return "$code"
    fi
    wt="$local_home/lanes/$lane/wt"
    if test -n "$(git -C "$wt" status --porcelain)"; then
      echo "deferred dirty local lane worktree: $lane"; continue
    fi
    git -C "$wt" fetch --no-tags "$bundle" "${bundle_ref}:refs/openswarm/releases/$sha"
    git -C "$wt" checkout --detach "$sha"
    OPENSWARM_HOME="$local_home" OPENSWARM_FLEET_GUARD_REQUIRED=1 \
      OPENSWARM_FLEET_GUARD_CONFIG="$local_home/fleet-capacity.json" \
      "$release_bin/openswarm" lane down "$lane"
    OPENSWARM_HOME="$local_home" OPENSWARM_FLEET_GUARD_REQUIRED=1 \
      OPENSWARM_FLEET_GUARD_CONFIG="$local_home/fleet-capacity.json" \
      "$release_bin/openswarm" lane up "$lane"
    after="$(OPENSWARM_HOME="$local_home" "$local_runtime/bin/openswarm" lane status)"
    python3 - "$lane" "$after" "$release_bin/openswarm" <<'PY'
import json, subprocess, sys
lane, status, executable = sys.argv[1:]
row = next((row for row in json.loads(status) if row.get("name") == lane), None)
if not row or row.get("state") != "idle" or not isinstance(row.get("supervisor_pid"), int):
    raise SystemExit("new local supervisor is not idle")
command = subprocess.check_output(["ps", "-p", str(row["supervisor_pid"]), "-o", "command="], text=True)
if executable not in command:
    raise SystemExit("new local supervisor did not use selected release")
PY
    echo "rolled local lane: $lane"
  done
}

remote_apply gocc "$gocc_runtime" "$gocc_repo" 'C1,A1,A2,GH1,GH2'
remote_apply gores1 "$gores1_runtime" "$gores1_repo" 'K1,M1,M2,M3,M4'
local_apply
install_local_runtime

if (( restart_hub )); then
  # OpenBot records its dispatches here. Refuse a restart while it still has a
  # live handoff; finished history is not a blocker.
  python3 - "$HOME/Library/Application Support/OpenBot/openbot-dispatch-v1.json" <<'PY'
import json, pathlib, sys
path = pathlib.Path(sys.argv[1])
if path.exists():
    active = [row.get("runId") for row in json.load(path.open()).get("runs", [])
              if row.get("status") in {"queued", "pending", "running"}]
    if active: raise SystemExit("refusing Hub restart with active dispatches: " + ",".join(active))
PY
  domain="gui/$(id -u)"
  launchctl bootout "$domain/app.openbot.hub.local" || true
  launchctl bootstrap "$domain" "$hub_plist"
  echo "OpenBot.app relaunched with direct Hub fleet configuration."
else
  echo "Files staged. Hub plist changed but OpenBot.app was not restarted."
  echo "When the coordinator selects the window: $0 --apply --restart-hub $sha"
fi
