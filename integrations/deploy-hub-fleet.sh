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
required=(openswarm openswarm-fleet openswarm-auto hub-openswarm-bridge fleet-result-bundle openswarm-fleet-bundle-adapter)

git -C "$source_root" diff --quiet
git -C "$source_root" diff --cached --quiet
git -C "$source_root" fetch --no-tags origin main
git -C "$source_root" cat-file -e "$sha^{commit}"
for file in "${required[@]}"; do
  git -C "$source_root" cat-file -e "$sha:openswarm/bin/$file" || { echo "missing selected source helper: $file" >&2; exit 1; }
done
git -C "$source_root" cat-file -e "$sha:openswarm/policy.json" || { echo "missing selected policy" >&2; exit 1; }
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

git -C "$source_root" cat-file -e "$sha^{commit}"
backup_id="$(date -u +%Y%m%dT%H%M%SZ)-${sha:0:12}-$$"
backup_root="$HOME/.local/state/phanes-deploy-backups/$backup_id"
umask 077
mkdir -p "$backup_root"
for config in "$hub_plist" "$bridge_config" \
  "$HOME/Library/LaunchAgents/app.openswarm.phanes.k1.plist" \
  "$HOME/Library/LaunchAgents/app.openswarm.phanes.k2.plist"; do
  test -f "$config" || { echo "missing local configuration to back up: $config" >&2; exit 1; }
  cp -p "$config" "$backup_root/$(basename "$config")"
done
echo "local rollback configuration: $backup_root"
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

# Keep a clean, persistent source checkout for the Hub bridge.  The template
# itself stays on its own clean branch: running a deployment must never detach
# or overwrite the worktree which contains this script.
hub_source="$hub_runtime/releases/$sha/phanes-source"
prepare_hub_source() {
  if [ -d "$hub_source/.git" ]; then
    test "$(git -C "$hub_source" rev-parse HEAD)" = "$sha"
    test -z "$(git -C "$hub_source" status --porcelain)"
    return
  fi
  mkdir -p "$(dirname "$hub_source")"
  source_staging="$(dirname "$hub_source")/.phanes-source-$sha-$$"
  git clone --no-checkout "$source_root" "$source_staging"
  git -C "$source_staging" checkout --detach "$sha"
  mv "$source_staging" "$hub_source"
}
prepare_hub_source

install_local_runtime() {
  mkdir -p "$hub_runtime/bin" "$hub_home"
  chmod 700 "$hub_runtime" "$hub_runtime/bin"
  # The command entrypoints resolve several sibling helpers (for example the
  # The helpers resolve siblings, policy.json, and selected docs from their
  # runtime root. Deploy the complete OpenSwarm tree from selected main.
  cp -pR "$hub_source/openswarm/." "$hub_runtime/"

  # openswarm-fleet intentionally fixes OPENSWARM_HOME to hub_home, so the
  # direct-fleet inventory belongs there. The existing capacity guard remains
  # at its established path and is supplied through launchd below.
  python3 - "$hub_home/fleet.json" "$hub_source" "$gocc_runtime" "$gocc_repo" "$gores1_runtime" "$gores1_repo" <<'PY'
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
  python3 - "$bridge_config" "$hub_source" <<'PY'
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
  if ! ssh -T -o BatchMode=yes -o ConnectTimeout=8 "$host" "git -C '$repo' cat-file -e '$sha^{commit}'"; then
    scp -q "$bundle" "$host:$remote_bundle"
  fi
  ssh -T -o BatchMode=yes -o ConnectTimeout=8 "$host" bash -s -- "$runtime" "$repo" "$remote_bundle" "$sha" "$bundle_ref" "$lanes" "$backup_id" <<'REMOTE'
set -euo pipefail
runtime="$1"; repo="$2"; bundle="$3"; sha="$4"; bundle_ref="$5"; lanes="${6//,/ }"; backup_id="$7"
umask 077
backup_root="$HOME/.local/state/phanes-deploy-backups/$backup_id"
mkdir -p "$backup_root"
if [ "$(uname -s)" = Darwin ]; then
  for label in app.openswarm.executor.codex app.openswarm.executor.agy app.openswarm.executor.agy2 \
    app.openswarm.executor.copilot app.openswarm.executor.copilot-complex; do
    plist="$HOME/Library/LaunchAgents/$label.plist"
    test -f "$plist" || { echo "missing launchd configuration to back up: $plist" >&2; exit 1; }
    cp -p "$plist" "$backup_root/$(basename "$plist")"
  done
else
  for unit in openswarm-executor-cursor.service openswarm-executor-pi.service \
    openswarm-executor-pi2.service openswarm-executor-m3.service openswarm-executor-m4.service; do
    unit_path="$(systemctl show -p FragmentPath --value "$unit")"
    test -f "$unit_path" || { echo "missing systemd configuration to back up: $unit" >&2; exit 1; }
    cp -p "$unit_path" "$backup_root/$(basename "$unit_path")"
  done
fi
echo "remote rollback configuration: $backup_root"
if ! git -C "$repo" cat-file -e "$sha^{commit}"; then
  git -C "$repo" fetch --no-tags "$bundle" "${bundle_ref}:refs/openswarm/releases/$sha"
fi
git -C "$repo" cat-file -e "$sha^{commit}"

release_root="$runtime/releases/$sha"
if [ ! -f "$release_root/openswarm/policy.json" ]; then
  # `openswarm` resolves sibling runner/helper scripts and policy.json from
  # its own tree. Archive the complete directory, not only its bin subtree.
  if [ -d "$release_root" ]; then
    git -C "$repo" archive "$sha" openswarm | tar -x -C "$release_root"
  else
    staging="$runtime/releases/.staging-$sha-$$"
    mkdir -p "$staging"
    git -C "$repo" archive "$sha" openswarm | tar -x -C "$staging"
    mv "$staging" "$release_root"
  fi
fi
release_bin="$release_root/openswarm/bin"
"$release_bin/openswarm-fleet" lane status >/dev/null

# These are stateless one-shot helpers used by the Hub transport.  Versioning
# them here does not restart a lane or replace the ordinary openswarm wrapper,
# and lets result retrieval work even when every lane is temporarily busy.
atomic_install() {
  source="$1"; destination="$2"
  temporary="$(mktemp "${destination}.XXXXXX")"
  install -m 700 "$source" "$temporary"
  mv -f "$temporary" "$destination"
}
atomic_install "$release_bin/openswarm-fleet-bundle-adapter" "$runtime/bin/openswarm-fleet-bundle-adapter"
atomic_install "$release_bin/fleet-result-bundle" "$runtime/bin/fleet-result-bundle"

bootstrap_launch_agent() {
  domain="$1"; plist="$2"
  for attempt in 1 2 3; do
    if launchctl bootstrap "$domain" "$plist"; then return 0; fi
    sleep 1
  done
  return 1
}

unload_launch_agent() {
  domain="$1"; label="$2"
  launchctl bootout "$domain/$label" || true
  for attempt in 1 2 3 4 5; do
    if ! launchctl print "$domain/$label" >/dev/null 2>&1; then return 0; fi
    sleep 1
  done
  return 1
}

roll_service() {
  lane="$1"
  if [ "$(uname -s)" = Darwin ]; then
    case "$lane" in
      C1) label=app.openswarm.executor.codex ;; A1) label=app.openswarm.executor.agy ;;
      A2) label=app.openswarm.executor.agy2 ;; GH1) label=app.openswarm.executor.copilot ;;
      GH2) label=app.openswarm.executor.copilot-complex ;; *) return 2 ;;
    esac
    plist="$HOME/Library/LaunchAgents/$label.plist"
    python3 - "$plist" "$release_bin/openswarm-fleet" "$lane" <<'PY'
import os, pathlib, plistlib, sys, tempfile
path, executable, lane = map(pathlib.Path, sys.argv[1:])
with path.open('rb') as source: data = plistlib.load(source)
arguments = data.get('ProgramArguments')
if not isinstance(arguments, list) or arguments[-3:] != ['lane', 'serve', str(lane)]: raise SystemExit('unexpected launchd lane definition')
arguments[0] = str(executable)
fd, temp = tempfile.mkstemp(prefix='openswarm-lane.', dir=path.parent)
with os.fdopen(fd, 'wb') as out: plistlib.dump(data, out)
os.chmod(temp, 0o600); os.replace(temp, path)
PY
    domain="gui/$(id -u)"
    unload_launch_agent "$domain" "$label" || return
    bootstrap_launch_agent "$domain" "$plist" || return
    launchctl print "$domain/$label" | grep -F "program = $release_bin/openswarm-fleet" >/dev/null || return
    launchctl print "$domain/$label" | grep -F "state = running" >/dev/null || return
  else
    case "$lane" in
      K1) unit=openswarm-executor-cursor.service ;; M1) unit=openswarm-executor-pi.service ;;
      M2) unit=openswarm-executor-pi2.service ;; M3) unit=openswarm-executor-m3.service ;;
      M4) unit=openswarm-executor-m4.service ;; *) return 2 ;;
    esac
    unit_path="$(systemctl show -p FragmentPath --value "$unit")"
    python3 - "$unit_path" "$release_bin/openswarm-fleet" "$lane" <<'PY'
import os, pathlib, re, sys, tempfile
path, executable, lane = map(pathlib.Path, sys.argv[1:])
text = path.read_text(); replacement = f'ExecStart={executable} lane serve {lane}'
updated, count = re.subn(r'^ExecStart=.* lane serve ' + re.escape(str(lane)) + r'$', replacement, text, flags=re.M)
if count != 1: raise SystemExit('unexpected systemd lane definition')
fd, temp = tempfile.mkstemp(prefix='openswarm-lane.', dir=path.parent)
with os.fdopen(fd, 'w') as out: out.write(updated)
os.chmod(temp, 0o644); os.replace(temp, path)
PY
    systemctl daemon-reload || return
    systemctl restart "$unit" || return
    test "$(systemctl show -p ActiveState --value "$unit")" = active || return
    systemctl show -p ExecStart --value "$unit" | grep -F "$release_bin/openswarm-fleet" >/dev/null || return
  fi
}

restore_service() {
  lane="$1"
  if [ "$(uname -s)" = Darwin ]; then
    case "$lane" in
      C1) label=app.openswarm.executor.codex ;; A1) label=app.openswarm.executor.agy ;;
      A2) label=app.openswarm.executor.agy2 ;; GH1) label=app.openswarm.executor.copilot ;;
      GH2) label=app.openswarm.executor.copilot-complex ;; *) return 2 ;;
    esac
    plist="$HOME/Library/LaunchAgents/$label.plist"
    cp -p "$backup_root/$label.plist" "$plist"
    domain="gui/$(id -u)"
    unload_launch_agent "$domain" "$label" || return
    bootstrap_launch_agent "$domain" "$plist"
    launchctl print "$domain/$label" | grep -F "state = running" >/dev/null
  else
    case "$lane" in
      K1) unit=openswarm-executor-cursor.service ;; M1) unit=openswarm-executor-pi.service ;;
      M2) unit=openswarm-executor-pi2.service ;; M3) unit=openswarm-executor-m3.service ;;
      M4) unit=openswarm-executor-m4.service ;; *) return 2 ;;
    esac
    unit_path="$(systemctl show -p FragmentPath --value "$unit")"
    cp -p "$backup_root/$(basename "$unit_path")" "$unit_path"
    systemctl daemon-reload
    systemctl restart "$unit"
    test "$(systemctl show -p ActiveState --value "$unit")" = active
  fi
}
rolled=0
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
  git -C "$wt" checkout --detach "$sha"
  if ! roll_service "$lane"; then
    echo "service reload failed; restoring lane: $lane" >&2
    restore_service "$lane"
    exit 1
  fi
  ready=0
  for attempt in $(seq 1 15); do
    after="$("$runtime/bin/openswarm-fleet" lane status)"
    if python3 - "$lane" "$after" "$release_bin/openswarm-fleet" <<'PY'
import json, subprocess, sys
lane, status, executable = sys.argv[1:]
row = next((row for row in json.loads(status) if row.get("name") == lane), None)
if not row or row.get("state") != "idle" or not isinstance(row.get("supervisor_pid"), int):
    raise SystemExit(1)
command = subprocess.check_output(["ps", "-p", str(row["supervisor_pid"]), "-o", "command="], text=True)
if executable not in command: raise SystemExit(1)
PY
    then ready=1; break; fi
    sleep 1
  done
  if [ "$ready" -ne 1 ]; then
    echo "new lane supervisor did not become ready; restoring lane: $lane" >&2
    restore_service "$lane"
    exit 1
  fi
  rolled=1
  echo "rolled lane: $lane"
done
if [ "$rolled" -eq 0 ]; then echo "no idle clean lane on this host; retry later"; fi
REMOTE
}

local_roll_service() {
  local lane="$1" release_bin="$2" label plist domain
  case "$lane" in
    K1) label=app.openswarm.phanes.k1 ;;
    K2) label=app.openswarm.phanes.k2 ;;
    *) echo "no verified local launchd label for $lane" >&2; return 2 ;;
  esac
  plist="$HOME/Library/LaunchAgents/$label.plist"
  python3 - "$plist" "$release_bin/openswarm" "$lane" <<'PY'
import os, pathlib, plistlib, sys, tempfile
path, executable, lane = map(pathlib.Path, sys.argv[1:])
with path.open('rb') as source: data = plistlib.load(source)
arguments = data.get('ProgramArguments')
if not isinstance(arguments, list) or arguments[-3:] != ['lane', 'serve', str(lane)]: raise SystemExit('unexpected local launchd lane definition')
arguments[0] = str(executable)
fd, temp = tempfile.mkstemp(prefix='openswarm-lane.', dir=path.parent)
with os.fdopen(fd, 'wb') as out: plistlib.dump(data, out)
os.chmod(temp, 0o600); os.replace(temp, path)
PY
  domain="gui/$(id -u)"
  launchctl bootout "$domain/$label" || true
  local unloaded=0
  for attempt in 1 2 3 4 5; do
    if ! launchctl print "$domain/$label" >/dev/null 2>&1; then unloaded=1; break; fi
    sleep 1
  done
  test "$unloaded" -eq 1 || return 1
  local started=0 attempt
  for attempt in 1 2 3; do
    if launchctl bootstrap "$domain" "$plist"; then started=1; break; fi
    sleep 1
  done
  test "$started" -eq 1 || return 1
  launchctl print "$domain/$label" | grep -F "program = $release_bin/openswarm" >/dev/null || return
  launchctl print "$domain/$label" | grep -F "state = running" >/dev/null || return
}

local_restore_service() {
  local lane="$1" label plist domain
  case "$lane" in
    K1) label=app.openswarm.phanes.k1 ;;
    K2) label=app.openswarm.phanes.k2 ;;
    *) return 2 ;;
  esac
  plist="$HOME/Library/LaunchAgents/$label.plist"
  cp -p "$backup_root/$label.plist" "$plist"
  domain="gui/$(id -u)"
  launchctl bootout "$domain/$label" || true
  local unloaded=0
  for attempt in 1 2 3 4 5; do
    if ! launchctl print "$domain/$label" >/dev/null 2>&1; then unloaded=1; break; fi
    sleep 1
  done
  test "$unloaded" -eq 1 || return 1
  local started=0 attempt
  for attempt in 1 2 3; do
    if launchctl bootstrap "$domain" "$plist"; then started=1; break; fi
    sleep 1
  done
  test "$started" -eq 1
  launchctl print "$domain/$label" | grep -F "state = running" >/dev/null
}

local_apply() {
  local release_root="$local_runtime/releases/$sha" release_bin="$local_runtime/releases/$sha/openswarm/bin"
  if [ ! -f "$release_root/openswarm/policy.json" ]; then
    mkdir -p "$release_root"
    git -C "$hub_source" archive "$sha" openswarm | tar -x -C "$release_root"
  fi
  OPENSWARM_HOME="$local_home" "$release_bin/openswarm" lane status >/dev/null
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
    if ! local_roll_service "$lane" "$release_bin"; then
      echo "local service reload failed; restoring lane: $lane" >&2
      local_restore_service "$lane"
      return 1
    fi
    ready=0
    for attempt in $(seq 1 15); do
      after="$(OPENSWARM_HOME="$local_home" "$local_runtime/bin/openswarm" lane status)"
      if python3 - "$lane" "$after" "$release_bin/openswarm" <<'PY'
import json, subprocess, sys
lane, status, executable = sys.argv[1:]
row = next((row for row in json.loads(status) if row.get("name") == lane), None)
if not row or row.get("state") != "idle" or not isinstance(row.get("supervisor_pid"), int):
    raise SystemExit(1)
command = subprocess.check_output(["ps", "-p", str(row["supervisor_pid"]), "-o", "command="], text=True)
if executable not in command: raise SystemExit(1)
PY
      then ready=1; break; fi
      sleep 1
    done
    if [ "$ready" -ne 1 ]; then
      echo "new local supervisor did not become ready; restoring lane: $lane" >&2
      local_restore_service "$lane"
      return 1
    fi
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
