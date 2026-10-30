# Audit: user flows without an E2E script

Date: 2026-10-27
Branch: `openswarm/20261027-191605-f69812`
Type: read-only audit. This document changes no product code.

## Scope and method

This audit compares the user flows that the documentation promises with the E2E scripts that the
repository holds. The reference set is the E2E scripts under `scripts/`:

- `scripts/*-e2e.ts`: 200 files
- `scripts/*-smoke.ts`: 13 files
- Other TypeScript under `scripts/`: 122 files (harnesses, builders, release tools)

The task also names the areas to audit: desktop, mobile, Team API, diagnostics, browser, provider
recovery, and the account and user flows.

Every finding was checked against the script bodies, not against the script names. A file name
alone never counted as coverage. The earlier saved review `review20261028-025918-fca4d7` covered
only the mobile logs. It is not used as evidence here.

Two rules shaped the result:

1. A flow counts as covered when a script reads the real modules of that flow and asserts a
   behaviour. A script that only fakes the flow does not count.
2. A missing scenario in an existing script is a different gap from a missing script. Both are
   reported, and each finding says which one it is.

## Coverage inventory

| Area | Scripts that cover it | State |
| --- | --- | --- |
| Provider drivers (Codex, Claude, ACP, OpenCode, Grok, Cursor, Cline, Pi, Muse, Antigravity) | 57 scripts, for example `provider-conformance-e2e.ts`, `provider-transcript-replay-e2e.ts` and `provider-loss-scope-e2e.ts` | Well covered |
| Agents, threads, tasks and the profile tools | 20 `agent-*-e2e.ts` scripts | Well covered |
| Routines and the scheduler | 11 `routine-*-e2e.ts` scripts | Well covered |
| Channels | 8 `channel-*-e2e.ts` scripts | Well covered |
| Workflows | 7 `workflow-*-e2e.ts` scripts | Well covered |
| Team API core routes | `team-api-routes-e2e.ts`, `team-api-legacy-clients-e2e.ts`, `agent-access-e2e.ts`, `agent-hide-restore-e2e.ts`, `workflow-team-api-e2e.ts` | Partly covered |
| Embedded browser | `browser-smoke.ts` (11 scenarios), plus 6 `browser-*-e2e.ts` scripts and `browser-secret-smoke.ts` | Partly covered |
| Web client | `web-client-threads-e2e.ts`, `web-channel-chat-e2e.ts`, `web-side-panel-e2e.ts`, `web-client-conversation-trim-e2e.ts`, `web-client-perf-e2e.ts` | Partly covered |
| Messaging | `messaging-discord-access-e2e.ts`, `messaging-pause-resume-e2e.ts`, `messaging-disconnect-orchestrator-e2e.ts`, `messaging-thread-roster-e2e.ts` | Discord and Slack only |
| Browser profiles, the shared panel and the vault | `browser-profile-agents-e2e.ts`, `team-api-routes-e2e.ts` | Partly covered |
| Mobile app screens | none | Not covered |
| Account menu exports | none | Not covered |
| Server import and transfer | `transfer-upload-cleanup-e2e.ts` (manifest only) | Not covered |
| App update and restart | none | Not covered |
| Remote desktop live test | `remote-desktop-runtime-smoke.ts` (the binary only) | Not covered |

## How to read a finding

- **Gap type** is `No script` when no E2E or smoke script touches the flow. It is
  `No scenario assertion` when a script exists but does not assert the behaviour that the
  documentation names.
- **Size** is the effort of the missing check. `S` is one script of about 150 lines. `M` is one
  script of about 300 to 500 lines, or two scripts. `L` needs a new harness, a simulator, or a
  second host.

---

## 1. The mobile app has no script that drives a screen

**Area**: mobile
**Gap type**: No script
**Size**: L

**Evidence**

- `README.md:21` promises prompt-driven agent creation and editing on desktop and mobile.
- `apps/mobile/src/app/` holds 127 route files. They include
  `chat/[agentId].tsx`, `add-agent/index.tsx`, `agent-info/[agentId]/index.tsx`,
  `browser/[agentId].tsx`, `queued-messages/index.tsx`, `hosted-server/setup.tsx` and
  `plan-limits/index.tsx`.
- `apps/mobile/package.json:113-128` has no test script at all.
- The only scripts that name the mobile app are `scripts/mobile-share-e2e.ts`, which reads the
  share payload of the App Group, `scripts/live-activity-autolink-e2e.ts`,
  `scripts/live-activity-code-span-e2e.ts`, `scripts/live-activity-inline-fence-e2e.ts` and
  `scripts/live-activity-server-mute-e2e.ts`, which drive the push service on the host, and
  `scripts/composer-markdown-lists-e2e.ts`, which drives a Storybook story and imports two
  mobile model functions.
- `scripts/mobile-connected-devices-e2e.ts` drives the desktop account client. It does not open
  the phone.

**Why it matters**

The phone is a shipped product. A broken screen, a broken sign-in or a broken queue sheet on the
phone reaches users before any desktop check runs. The desktop scripts cannot catch it, because
the phone has its own screens, its own queue sheet and its own browser view.

**Fix**

Add one script `scripts/mobile-agent-profile-e2e.ts`. It starts the worktree stack with
`bun run dev:test-client`, drives the phone through the existing `dev:mobile` pairing, creates an
agent, edits its instructions, and reads the agent back from the host. Drive the screen through
the Expo development build and its accessibility tree, as `scripts/mobile-share-e2e.ts` already
does for the share payload. Write the report under `.openbot-build/`.

---

## 2. The account menu exports have no script

**Area**: diagnostics
**Gap type**: No script
**Size**: M

**Evidence**

- `README.md:31` promises local data and privacy-safe diagnostics exports from the account menu.
- `PRIVACY.md:1048-1058` describes both exports and names what the diagnostics report must not
  contain.
- `src/main/maintenance-service.ts:31` defines `exportOpenBotData`.
- `src/main/maintenance-service.ts:131` defines `exportDiagnostics`.
- `src/main/maintenance-service.ts:211-212` writes the privacy sentence into the report.
- `src/main/ipc/app-handlers.ts:295` and `src/main/ipc/app-handlers.ts:298` bind both operations.
- `packages/contracts/src/ipc-endpoints.ts:699-701` names the two channels.
- No script under `scripts/` names `exportDiagnostics`, `exportOpenBotData` or
  `maintenance:export`.

**Why it matters**

The privacy promise is written in the report itself and in `PRIVACY.md`. A change that puts a
conversation, a URL, an email address or a token into the diagnostics file would still pass every
check in the repository. The user mails that file to somebody else.

**Fix**

Add `scripts/maintenance-export-e2e.ts`. It runs the real `exportDiagnostics` and the real
`exportOpenBotData` on a temporary profile. It replaces the save dialog with a temporary path.
It seeds a conversation, an email address, a URL, a file path and a token. It then reads the JSON
and the ZIP and asserts that none of those values appear. It also asserts
`schemaVersion: 4` and the `scope.excludes` list of the manifest.

---

## 3. The server transfer does not move a real server

**Area**: desktop
**Gap type**: No scenario assertion
**Size**: M

**Evidence**

- `docs/architecture/agents.md:723-776` describes Server settings > Transfer. The file holds the
  agents, the routines, the memories, the skills, the shared folder and the server settings.
- `src/main/instance-transfer-service.ts:192` defines `InstanceTransferService`.
- `src/main/instance-transfer-manifest.ts` holds the frozen manifest.
- `scripts/transfer-upload-cleanup-e2e.ts:22` builds its export input with `agentIds: []`. It
  reads the wire manifest of an empty export and the cleanup of an upload. It moves no agent, no
  file and no setting.

**Why it matters**

A transfer is how a user moves a whole setup to a new computer. The rules in the document are
strict: no secret goes into the file, an invalid agent is left out, a failed agent is deleted, and
the shared files are copied. A regression in any of those rules moves user data, or publishes a
secret, and no script sees it.

**Fix**

Extend `scripts/transfer-upload-cleanup-e2e.ts` or add `scripts/instance-transfer-e2e.ts`. It
creates two agents with routines, memories and skills, exports them with files and settings, and
imports the archive into a second temporary profile. It asserts that the agent count, the routine
count and the shared file list match. It asserts that a key in the MCP settings is empty in the
export, that a URL loses its query, and that an unsafe path is refused.

---

## 4. The Grok Bot import moves nothing

**Area**: desktop
**Gap type**: No script
**Size**: M

**Evidence**

- `README.md:307-311` documents Import agents from Grok Bot.
- `docs/architecture/agents.md:703-722` describes the import. It creates agents, publishes
  skills, creates channels with their members and their lead, and deletes an agent whose step
  fails.
- `src/main/agent-import-service.ts:152` defines `AgentImportService`.
- `src/main/agent-import-manifest.ts` and `src/main/agent-archive.ts` hold the format and the
  safety checks.
- No script under `scripts/` names `AgentImportService` or `agent-import`.

**Why it matters**

The import writes into the local profile from a file the user picked. A wrong path rule, a
missing skill or a half-created channel leaves the user with a broken setup and no way to tell
what went wrong.

**Fix**

Add `scripts/agent-import-e2e.ts`. It builds an export with the documented format, applies it
through the real `AgentImportService`, and asserts that each agent, skill, routine, memory and
channel exists. It adds an archive with one unsafe path and asserts that the import refuses it
and still creates the other agents.

---

## 5. The app update and the restart have no script

**Area**: desktop, provider recovery
**Gap type**: No script
**Size**: M

**Evidence**

- `README.md:598` says that installed builds check GitHub Releases for updates and expose
  download and restart controls in the account popover.
- `src/main/update-service.ts:179` defines `UpdateService`.
- `packages/contracts/src/ipc-endpoints.ts:1250-1256` names `update:check`, `update:download`,
  `update:install` and `update:cancel-scheduled-restart`.
- `src/renderer/src/features/updates/` holds the restart toasts and the "what is new" dialog.
- No script under `scripts/` names `UpdateService` or `update-service`.

**Why it matters**

The update restarts the app. The service has busy phases, a scheduled restart, an idle restart and
a sibling scan. A wrong busy phase restarts the app while a turn runs, and the user loses the
answer. A wrong scheduled restart restarts at the wrong time.

**Fix**

Add `scripts/app-update-e2e.ts`. It runs the real `UpdateService` with a fake
`electron-updater` boundary. It asserts that a turn that runs holds the install, that a queued
turn keeps the busy phase, that a cancel ends the wait, that a scheduled restart picks the next
idle moment, and that a failed download keeps the old version.

---

## 6. The gateway of the remote browser view has no script

**Area**: browser, mobile, web client
**Gap type**: No script
**Size**: L

**Evidence**

- `docs/architecture/browser.md` describes the live view in the Tab lifetime section. The phone
  and the browser client watch the tab of an agent, and can point, type, use a context menu, ask
  for a viewport and use the clipboard.
- `src/main/browser-view-gateway.ts:132` defines `BrowserViewGateway`. It owns the session, the
  frame codec, the acknowledgement limit, the cursor, the context menu, the viewport and the
  clipboard answers.
- `packages/contracts/src/team-protocol/browser-view-v1.ts` holds the frozen wire.
- `packages/team-client/src/browser-view.ts` holds the client half.
- `scripts/browser-smoke-electron.ts:2205`, `scripts/browser-smoke-electron.ts:2241` and
  `scripts/browser-smoke-electron.ts:2339` drive the primitives of `BrowserHost` itself
  (`startView`, `dispatchViewInput` and `copyViewSelection`) in the `live-view` and
  `secret-handoff` scenarios. The gateway above them is not driven.
- `scripts/team-session-end-e2e.ts:66-67` uses a stand-in that only records a call. It never
  opens a socket.
- No script under `scripts/` names `BrowserViewGateway`, `browser-view-gateway` or
  `createRemoteBrowserView`.

**Why it matters**

The gateway is the part between a remote member and the browser. Its rules are strict: the
frames stay outside the tab queue, a lost frame is replaced and not replayed, a click lands on
the frame the user sees, and the secret never crosses the socket. A wrong rule sends a click to
the wrong place on a page, or sends the text of a password to a phone. The smoke test of the
`BrowserHost` primitives cannot see these rules.

**Fix**

Add `scripts/browser-view-gateway-e2e.ts`. It runs the real `BrowserViewGateway` over a loopback
WebSocket with the real `BrowserHost` and a synthetic page. It asserts the frame order, the
acknowledgement rule, the pointer input, the cursor, the context menu, the viewport request, the
`copied` and `copyTooLarge` answers, and the refusal of a clipboard write from another site.

---

## 7. Telegram has no script

**Area**: messaging
**Gap type**: No script
**Size**: M

**Evidence**

- `docs/messaging.md:120-155` describes Connect Telegram and What happens in Telegram. A mention
  starts a conversation, a reply chain keeps its thread, an approval can be answered in the chat
  by the person who wrote the message, and files go both ways.
- `src/backend/messaging/telegram/telegram-driver.ts:373` defines the Telegram driver.
- `src/backend/messaging/telegram/telegram-updates.ts`,
  `src/backend/messaging/telegram/telegram-transport.ts` and
  `src/backend/messaging/telegram/telegram-chats.ts` hold the rest.
- `remote/api/src/telegram.ts` holds the Signal side.
- `scripts/messaging-discord-access-e2e.ts` drives the Discord driver end to end.
  `scripts/messaging-pause-resume-e2e.ts` drives a Slack workspace and a Discord server.
- In both of those scripts, Telegram is a stand-in: `scripts/messaging-pause-resume-e2e.ts:100`
  sets `telegram: { available: () => false, ... }`.

**Why it matters**

Discord and Slack have a script. Telegram has none, although it is a shipped connector with its
own thread rule and its own approval rule. A change that breaks the reply chain or the approval
button reaches users and no check sees it.

**Fix**

Add `scripts/messaging-telegram-e2e.ts`. It sends an update through the normalizer of
`remote/api/src/telegram.ts` into the real Telegram driver on a temporary profile. It asserts the
thread of a reply chain, the mention that starts a conversation, the approval that only the
author can answer, and the file that goes both ways.

---

## 8. The provider switch is faked everywhere

**Area**: desktop, provider recovery
**Gap type**: No scenario assertion
**Size**: S

**Evidence**

- `docs/architecture/providers.md` describes Local provider switches. The service holds the agent
  assignment lock while it turns a provider off. It refuses the change while an agent uses the
  provider. A failed write restores the old state.
- `src/main/ipc/provider-handlers.ts:45` binds `app:set-provider-on`.
- `src/backend/agent-service.ts:3478` defines `setProviderOn` on the service. It holds the
  agent-assignment lock while it turns a provider off.
- `src/backend/agent/provider-runtime.ts:671` defines `setProviderOn` on the runtime. Line 680
  refuses a provider that is assigned or busy. Line 684 refuses a busy refresh. The failed write
  path restores the old state.
- `scripts/provider-update-after-app-upgrade-e2e.ts:73` fakes the state with
  `isProviderOn: (provider) => provider !== OFF || offTurnedOn`.
- `scripts/provider-runtime-remove-e2e.ts` reads the store as if a provider were off.
- No script under `scripts/` names `setProviderOn` or `ProviderUseSettingsStore`.

**Why it matters**

Turning a provider off is a user action in the Providers section. A wrong switch strands the
agents that use the provider, or leaves the file in a state that does not match the screen. The
scripts that need the state fake it, so a change in the real switch is invisible.

**Fix**

Add `scripts/provider-use-switch-e2e.ts`. It drives the real `AgentService.setProviderOn` with a
real `ProviderUseSettingsStore` on a temporary profile. It asserts that the switch is refused
while an agent uses the provider, that the refusal carries the "in use" text, that the store keeps
the old value after a failed write, and that a provider that is off offers no model.

---

## 9. A broken provider release has no rollback check

**Area**: provider recovery
**Gap type**: No scenario assertion
**Size**: M

**Evidence**

- `docs/architecture/providers.md` describes Provider CLI updates. The text says that an
  activation failure removes the rejected artifact and preserves the old runtime, so a release
  that does not start leaves the last working CLI in use.
- `src/main/provider-runtime-manager.ts:924` defines `#activateEffect`. The comment above it names
  the rule.
- `src/main/provider-runtime-manager.ts:955` removes the install root when the activation fails.
- `scripts/provider-update-busy-e2e.ts` covers the wait for a running turn, the hold of new
  turns, the cancel and the rule that two installs never run at once.
- `scripts/provider-update-after-app-upgrade-e2e.ts` covers the offer after an app upgrade.
- `src/main/provider-runtime-failure-log.test.ts:20` drives the manager, but it checks the log
  line of a failed download only. It does not look at the runtime that stays in use.
- Neither script drives a candidate that fails to start.

**Why it matters**

The provider CLIs are updated in the background. A release that does not start must not take the
provider down for every agent on the computer. This is the rule that keeps a bad upstream release
from stopping the product.

**Fix**

Add `scripts/provider-update-rollback-e2e.ts`. It runs the real `ProviderRuntimeManager` with a
fake candidate that fails its version probe. It asserts that the old version still starts, that
the rejected install is gone, that the status shows the failure, and that a later start selects
the old version.

---

## 10. The remote desktop live test has no script

**Area**: desktop, provider recovery
**Gap type**: No script
**Size**: L

**Evidence**

- `docs/architecture/computer-use.md` describes Remote desktop permission checks and live tests.
  The gateway refuses a second test session, refuses a display switch during a test, and closes
  the panel when its stream ends.
- `src/main/remote-screen-gateway.ts:148` defines `RemoteScreenGateway`.
- `src/main/remote-screen-gateway.ts:321` refuses a second session during a test.
- `scripts/remote-desktop-runtime-smoke.ts` checks the runtime artifact only. It starts no
  gateway.
- `scripts/remote-desktop-browser-harness.ts` starts a real gateway, but it is not an E2E script
  and no script imports it. It writes no report and no check fails when it breaks.

**Why it matters**

The remote desktop is the way a member watches a hosted server. The gateway carries the setup
checks, the single test session and the input that the member sends. A wrong rule lets two
members send input at once, or lets a test panel outlive its stream.

**Fix**

Add `scripts/remote-desktop-live-test-e2e.ts`. It starts the real `RemoteScreenGateway` with a
fake Sunshine runtime on loopback. It asserts that a second live test is refused, that a display
switch is refused during a test, that the panel closes when the stream ends, and that a host
without the permissions answers `host_permissions_required`.

---

## 11. The phone notification route is not on the wire in any script

**Area**: mobile, Team API
**Gap type**: No scenario assertion
**Size**: M

**Evidence**

- `docs/architecture/channels-and-messaging.md` describes iPhone notifications and Android
  notifications. The phone registers with every host that supports the capability.
- `packages/contracts/src/team-protocol/push-notifications-v1.ts:28` freezes the iOS capability.
  `packages/contracts/src/team-protocol/push-notifications-android-v1.ts` freezes the Android one.
- `src/main/team-api/route-push-notifications.ts:21` defines the route.
- `scripts/desktop-notifications-e2e.ts:119` starts the real `PushNotificationService` and calls
  `register` on it directly. It never sends a request to the route.
- No script under `scripts/` names `routePushNotifications` or `push-notifications-v1`.

**Why it matters**

The registration route is the frozen wire between the phone and the host. A wrong field name or a
missing check means that the phone stops getting notifications, or that a host keeps a token of a
phone that left. The service test does not see the wire.

**Fix**

Add `scripts/push-notifications-route-e2e.ts`. It runs a real `TeamApiServer` on loopback and
sends the registration and the removal that a phone sends. It reads each reply with the frozen
codec. It asserts that a registration that the codec refuses answers 400
`pushNotificationsUnsupported`, that an Android registration without a token is refused, that the
host keeps the registration after a restart, and that a removal drops it.

---

## 12. A published agent can carry a secret

**Area**: desktop
**Gap type**: No scenario assertion
**Size**: M

**Evidence**

- `docs/architecture/agents.md` describes Agent templates. A template is public to anyone with its
  link. An account may have at most five published agents. A second publish updates the same
  link. An unpublish clears the content.
- `src/main/agent-template-service.ts:148` defines `AgentTemplateService`.
- `src/main/agent-template-service.ts:417-436` refuses a secret in the name, the title, the
  description, a routine name and a skill.
- `scripts/agent-template-page-e2e.ts` opens the public page of a shared agent. It does not publish.

**Why it matters**

A published template is a public page. The service refuses a secret rather than hiding it,
because a hidden secret would publish an agent that does not do what its owner wrote. That rule,
the limit of five, and the unpublish have no check.

**Fix**

Add `scripts/agent-template-publish-e2e.ts`. It runs the real `AgentTemplateService` with a fake
account Worker. It publishes an agent and reads the payload back. It asserts that a name with a
token is refused, that the second publish updates the same template, that the sixth agent is
refused, and that an unpublish empties the content.

---

## 13. The routine calendar feed has no script

**Area**: desktop
**Gap type**: No script
**Size**: S

**Evidence**

- `docs/architecture/agents.md` describes Routine calendar feed. The URL holds a token. A wrong
  token gets a 404. A request with an `Origin` header or a foreign `Host` gets a 403 before the
  token is checked. The log never holds the URL.
- `src/main/routine-feed-server.ts:59` defines `RoutineFeedServer`.
- `src/main/routine-feed-ics.ts` builds the events.
- `src/renderer/src/features/servers/ServerSettingsModal.tsx:697` and
  `src/renderer/src/features/servers/ServerSettingsModal.tsx:856` show the feed row.
- No script under `scripts/` names `RoutineFeedServer` or `routine-feed`.

**Why it matters**

The feed is a loopback server that hands a token to a calendar app. The order of the checks
matters: the origin and host checks must come before the token check. A wrong order lets a web
page probe the token. The feed itself must list the runs of the next 30 days.

**Fix**

Add `scripts/routine-feed-e2e.ts`. It starts the real `RoutineFeedServer` on loopback with the
real routine store. It asserts the 403 for a foreign host and for an `Origin` header, the 404 for
a wrong token, the events of the next 30 days, and that a new token replaces the old one.

---

## 14. Leaving a joined server has no script

**Area**: Team API, account
**Gap type**: No script
**Size**: S

**Evidence**

- `docs/architecture/servers.md` describes Leaving a server. The membership and every session of it
  end on all of the member's devices. The owner is refused.
- `packages/contracts/src/team-protocol/current.ts:113` freezes the capability
  `member-leave-v1`.
- `packages/contracts/src/team-api-routes.ts:68` names the route `/v1/team/leave`.
- `src/main/team-api/route-team.ts:83` serves it.
- `scripts/team-session-revoke-e2e.ts` covers the admin side of a membership change.
- No script under `scripts/` names `member-leave`, `team/leave` or `TEAM_MEMBER_LEAVE`.

**Why it matters**

Leaving is the only way a member removes their own access without an admin. The rule that every
session of the member ends on every device is the point of the action. A wrong rule leaves the
member in the team.

**Fix**

Add the case to `scripts/team-session-revoke-e2e.ts` or add `scripts/team-leave-e2e.ts`. It runs
a real `TeamApiServer` on loopback. It asserts that a member ends its own membership, that the
owner is refused, that the event socket of the member closes, and that a second request is
refused.

---

## 15. Hosted sites per server have no script

**Area**: Team API, desktop
**Gap type**: No script
**Size**: M

**Evidence**

- `docs/architecture/servers.md` describes Hosted sites per server. A site belongs to the server
  that published it. The active-site limit comes from the plan of the server. A request that names
  another bucket is refused with 409. Removing a server moves its sites to the unlinked bucket.
- `packages/contracts/src/team-protocol/hosted-sites-v1.ts` freezes the capability.
- `src/main/team-api/route-hosted-sites.ts:15` defines the route.
- In `scripts/agent-api-keys-e2e.ts:82`, `scripts/dispatch-approval-e2e.ts:96`,
  `scripts/mcp-tool-consent-e2e.ts:241` and `scripts/ui-blocks-e2e.ts:112`, `hostedSites` is only
  a stand-in for an approval. No script sends a site request.

**Why it matters**

A hosted site is public content that the user owns. The rule that a member never sees the sites
of another bucket, and that a site of another server is refused, keeps one member from deleting
the content of another. Nothing checks it.

**Fix**

Add `scripts/hosted-sites-e2e.ts`. It runs a real `TeamApiServer` on loopback with a fake site
service. It asserts that a member sees only the sites of the host, that a delete of a site of
another bucket answers 409 `site_other_server`, and that a member without the admin role cannot
delete.

---

## Gaps below the top 15

These are real gaps with a smaller user impact. They are listed so that the count of fifteen is
not read as the count of all gaps.

| Flow | Evidence | Gap type | Size |
| --- | --- | --- | --- |
| Conversation file search on a joined server | `packages/contracts/src/team-protocol/conversation-file-search-v1.ts:28`, `src/main/team-api/route-conversation-files.ts:21` | No script | S |
| The size limits of a browser download | `src/backend/browser-host.ts:188-193` sets four active downloads and a 2 GB limit. `scripts/browser-smoke-electron.ts:1777-1806` covers only the normal download. | No scenario assertion | S |
| The rename and the clear of a browser profile | `src/main/browser-profile-service.ts:37-41`. `scripts/browser-profile-agents-e2e.ts` calls `create` and `delete` only. | No scenario assertion | S |
| Files in the web client | `docs/web-client.md` lists files in review scope 5. No script uploads a file through `/app`. | No script | M |
| The creation of an agent in the web client | `docs/web-client.md` lists creation in review scope 6. No script creates an agent through `/app`. | No script | M |

## Flows that only look uncovered

The following flows have no script with a matching name. A script covers them anyway.

- The browser download flow. `scripts/browser-smoke.ts` drives it.
- The live view of `BrowserHost`. `scripts/browser-smoke.ts` drives `startView`,
  `dispatchViewInput` and `copyViewSelection` in its `live-view` and `secret-handoff` scenarios.
  Only the gateway above them is missing (finding 6).
- The browser profiles. `scripts/browser-profile-agents-e2e.ts` drives the agent side.
- The account sign-in and the connected devices. `scripts/mobile-connected-devices-e2e.ts` and
  `scripts/central-auth-session-file-e2e.ts` drive them.
- The commercial license. `scripts/stripe-flows-e2e.ts` has ten `license-*` scenarios.
- The hosted server plans. `scripts/stripe-flows-e2e.ts` and the scripts under
  `apps/auth-api/scripts/` drive them.
- The provider usage reset. `scripts/codex-usage-reset-e2e.ts` drives it.
- The approval policy. `scripts/mcp-tool-consent-e2e.ts` and `scripts/dispatch-approval-e2e.ts`
  drive it.

## Limits of this audit

- This audit reads the repository. It ran no script and no test.
- The findings rank the flows by user impact. The order is a judgement, not a measurement.
- A flow with no script may still be covered by a vitest file or by a script under
  `apps/auth-api/scripts/`. Both were checked for the findings above. Only the fifteen findings
  and the five rows of the smaller table above had no check of any kind.
- The audit does not judge the quality of the checks that exist. It judges only their presence.
