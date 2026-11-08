"""Check released runtime identities and pinned assets against the rename baseline."""

import hashlib, json, pathlib, subprocess
root = pathlib.Path(__file__).resolve().parents[2]
checks = []

def check(label, passed):
    checks.append({'check': label, 'passed': bool(passed)})

def old(path):
    return subprocess.check_output(['git', 'show', '7869dad74:openbot/' + path.removeprefix('volto/').replace('volto-database', 'openbot-database')], cwd=root)

def has(path, values):
    current = (root / path).read_text()
    before = old(path).decode()
    for value in values:
        check(path + ': ' + value, value in before and value in current)
has('volto/src/backend/agent-data/agent-database-host.ts', ['"openbot_metadata"'])
has('volto/src/backend/agent-data/agent-database-protocol.ts', ['"openbot_metadata"'])
has('volto/src/backend/browser-state.ts', ['openbot-browser-agent-'])
has('volto/src/backend/volto-database.ts', ['openbot-thread-'])
has('volto/src/main/github-connector-config.ts', ['"openbotgit"'])
has('volto/src/main/linux-desktop-entry.ts', ['x-scheme-handler/openbot;'])
has('volto/resources/cli/openbot-cli.mjs', ['"OpenBot", "automation"'])
has('volto/apps/auth-api/package.json', ['migrations apply openbot-auth --local', 'migrations apply openbot-auth --remote'])
has('volto/apps/auth-api/src/server/hosted-server-service.ts', ['scripts/hosting/openbot-boat-pre-stop', 'openbot.service', '/run/openbot-pre-stop'])
has('volto/apps/auth-api/src/server/boat-client.ts', ['scripts/hosting/openbot-hosted-env'])
has('volto/src/main/sunshine-moonlight-runtime.ts', ['/api/openbot/setup', '/api/openbot/test', '/api/openbot/displays', 'openbot-remote-slot-'])
has('volto/src/main/remote-screen-gateway.ts', ['"openbot-moonlight"', '"openbotRemoteViewer"', '"/openbot-paste.js"', '"/openbot-clipboard"'])
has('volto/src/backend/messaging/slack/slack-events.ts', ['"openbot_accept"', '"openbot_decline"', '"openbot_stop"'])
has('volto/packages/contracts/src/team-protocol/v1.ts', ['OpenBot-Protocol-Version', 'OpenBot-App-Version', 'OpenBot-Capabilities'])
has('volto/src/main/team-webrtc-host-peer.ts', ['OpenBot-Protocol-Version', 'OpenBot-App-Version', 'OpenBot-Capabilities', 'X-OpenBot-WebRTC-Session', 'openbot-token.'])
has('volto/apps/auth-api/src/server/request-auth.ts', ['OpenBot-Timestamp', 'OpenBot-Signature'])
has('volto/apps/auth-api/src/server/browser-api.ts', ['X-OpenBot-Browser'])
has('volto/src/main/webhook-security.ts', ['X-OpenBot-Signature'])
has('volto/scripts/install-host-update-agent.sh', ['/Applications/OpenBot.app', 'Application Support/OpenBot/HostManager', 'app.openbot.desktop.relaunch.plist', 'app.openbot.host-manager.plist'])
has('volto/remote/nginx/openbot-remote-nginx-reload.path', ['/openbot-remote_acme-data/', 'Unit=openbot-remote-nginx-reload.service'])
lockpath = 'volto/native-runtime.lock.json'
lock = json.loads((root / lockpath).read_text())
check('native lock byte-identical to published baseline', (root / lockpath).read_bytes() == old(lockpath))
for name in ['sunshine', 'moonlightWeb']:
    patch = lock['remoteDesktop'][name]['patch']
    path = 'volto/' + patch['path']
    data = (root / path).read_bytes()
    check(name + ' patch byte-identical to pinned baseline', data == old(path))
    check(name + ' patch SHA-256 matches lock', hashlib.sha256(data).hexdigest() == patch['sha256'])
for sound in ['task', 'warm', 'mech', 'bubble', 'press']:
    has('volto/packages/contracts/src/notification-relay.ts', ['openbot-' + sound + '.wav'])
    has('volto/apps/mobile/src/features/push-notifications/android-notification-channels.ts', ['openbot_' + sound + '.wav'])
    check(sound + ' notification resource exists', (root / ('volto/apps/mobile/assets/sounds/openbot-' + sound + '.wav')).is_file())
p = 'volto/apps/mobile/modules/quick-actions/android/src/main/java/run/openbot/quickactions/QuickActionsModule.kt'
for name in ['routines', 'search', 'agent']:
    has(p, ['R.drawable.openbot_quick_action_' + name])
    check(name + ' drawable resource exists', (root / ('volto/apps/mobile/modules/quick-actions/android/src/main/res/drawable/openbot_quick_action_' + name + '.xml')).is_file())
server = (root / 'volto/src/main/server-memory.ts').read_text()
check('server process scan accepts old and current markers', '\\0OPENBOT_AGENT_PROCESS=1\\0' in server and 'VOLTO_AGENT_PROCESS' in server)
check('pinned runtime receives old setup variable', 'OPENBOT_REMOTE_SETUP: "1"' in (root / 'volto/src/main/sunshine-moonlight-runtime.ts').read_text())
has('volto/packages/contracts/src/validation.ts', ['host\\.openbot\\.run'])
has('volto/apps/site-router/src/index.ts', ['\\.openbot\\.site'])
check('legacy Docker source commit build argument accepted', 'ARG OPENBOT_SOURCE_COMMIT=unknown\nARG VOLTO_SOURCE_COMMIT=$OPENBOT_SOURCE_COMMIT' in (root / 'volto/remote/api/Dockerfile').read_text())
check('legacy GitHub private key secret accepted', 'secrets.VOLTO_GITHUB_APP_PRIVATE_KEY || secrets.OPENBOT_GITHUB_APP_PRIVATE_KEY' in (root / 'volto/.github/workflows/ci.yml').read_text())
check('development state directory retained', 'DEVELOPMENT_STATE_DIRECTORY = ".openbot"' in (root / 'volto/scripts/development-secrets.ts').read_text())
has('volto/packages/contracts/src/ipc-mcp-servers.ts', ['"openbot-computer-use"', '"openbot-github"'])
has('volto/src/backend/mcp-provider-shapes.ts', ['"openbot-mcp"', '`openbot_${name}`'])
has('volto/packages/contracts/src/ipc-agent-analytics.ts', ['"openbot:workflows"'])
has('volto/scripts/install-server.sh', ['OpenBot-$version-$architecture.AppImage', 'resources/hosting/openbot-server-setup'])
has('volto/scripts/verify-update-artifacts.ts', ['"OpenBot.app"'])
has('volto/scripts/create-github-app.ts', ['".config", "openbot"'])
has('volto/scripts/stripe-bootstrap.ts', ['`openbot_${plan}`', 'metadata[openbot_plan]', 'metadata[openbot_interval]', 'openbot_license_prices'])
has('volto/src/main/managed-skill-service.ts', ['"openbot-site-hosting"', '"openbot-skill-creator"', '"openbot-data"'])
report = {'baseline': '7869dad74', 'scope': 'Offline baseline identity and asset checks; no live service or device contacted. Dynamic checks run separately.', 'passed': all((x['passed'] for x in checks)), 'checks': checks}
path = root / 'volto/.volto-build/runtime-identity-audit.json'
path.parent.mkdir(parents=True, exist_ok=True)
path.write_text(json.dumps(report, indent=2) + '\n')
print(json.dumps({'passed': report['passed'], 'checks': len(checks), 'failed': [x for x in checks if not x['passed']]}))
raise SystemExit(not report['passed'])
