// Run: TMPDIR="$PWD/../.tmp/test-tmp" bun scripts/rename-contracts-e2e.ts
// Runs the released link, billing, storage, and sealed-payload paths without a remote service.
import assert from "node:assert/strict";
import { mkdir, writeFile } from "node:fs/promises";
import { resolve } from "node:path";
import { hmac } from "@noble/hashes/hmac.js";
import { sha256 } from "@noble/hashes/sha2.js";
import { utf8ToBytes } from "@noble/hashes/utils.js";
import { createVoltoAgentTemplateUrl, parseAgentTemplateUrl } from "../packages/contracts/src/agent-template-links";
import { BILLING_METADATA, billingLookupKey, parseBillingLookupKey } from "../packages/contracts/src/billing";
import { createMobileConnectUrl, parseMobileConnectUrl } from "../packages/contracts/src/mobile-connect";
import { createVoltoPluginUrl, parsePluginUrl } from "../packages/contracts/src/plugin-links";
import { liveActivityHostSecret, liveActivityKeys } from "../packages/team-client/src/live-activity-seal";
import { pushNotificationKeys } from "../packages/team-client/src/push-notification-seal";
import { createWorkspacePreferences } from "../packages/team-client/src/workspace-preferences";
import { codexOwnTransports } from "../src/backend/mcp-provider-shapes";

const checks: string[] = [];
const mobile = { apiUrl: "https://accounts.example.com", ticket: "a".repeat(32) };
const mobileLink = createMobileConnectUrl(mobile);
assert.ok(mobileLink.startsWith("openbot://"));
assert.deepEqual(parseMobileConnectUrl(mobileLink), mobile);
assert.equal(parseMobileConnectUrl(mobileLink.replace("openbot:", "https:")), null);
const pluginLink = createVoltoPluginUrl("example-plugin");
assert.equal(pluginLink, "openbot://plugins/example-plugin");
assert.equal(parsePluginUrl(pluginLink), "example-plugin");
const templateLink = createVoltoAgentTemplateUrl("a".repeat(22));
assert.equal(templateLink, `openbot://agents/${"a".repeat(22)}`);
assert.equal(parseAgentTemplateUrl(templateLink), "a".repeat(22));
checks.push("released Mobile Connect, plugin, and agent links round-trip; unrelated scheme rejected");
assert.equal(billingLookupKey("starter", "month"), "openbot_starter_month");
assert.deepEqual(parseBillingLookupKey("openbot_starter_month"), { plan: "starter", interval: "month" });
assert.deepEqual(BILLING_METADATA, { userId: "openbot_user_id", serverId: "openbot_server_id" });
checks.push("existing Stripe lookup keys and metadata still decode");
const saved = { version: 1, hidden: ["old-agent"], pinned: ["saved-agent"] };
const disk = new Map<string, string>();
const storage = {
  get: (key: string) => {
    assert.ok(key.startsWith("openbot.workspace.v1."));
    return disk.get(key) ?? JSON.stringify(saved);
  },
  set: (key: string, value: string) => disk.set(key, value),
};
const preferences = createWorkspacePreferences("https://accounts.example.com", "user", storage);
assert.deepEqual(preferences.read("host"), { hidden: saved.hidden, pinned: saved.pinned });
preferences.write("host", { hidden: [], pinned: ["new-agent"] });
assert.deepEqual(createWorkspacePreferences("https://accounts.example.com", "user", storage).read("host"), {
  hidden: [],
  pinned: ["new-agent"],
});
checks.push("existing workspace preferences load and survive a new store instance after a write");
const secret = new Uint8Array(32).fill(7);
const legacy = (label: string) => hmac(sha256, secret, utf8ToBytes(label));
assert.deepEqual(liveActivityKeys(secret).seal, legacy("openbot live activity seal"));
assert.deepEqual(liveActivityHostSecret(secret, "host"), legacy("openbot live activity host host"));
assert.deepEqual(pushNotificationKeys(secret).seal, legacy("openbot push notification seal"));
checks.push("existing notification secrets derive the released keys");
assert.deepEqual(codexOwnTransports({ github: { command: "openbot-mcp", enabled: false } }), {});
assert.deepEqual(codexOwnTransports({ user: { command: "node" } }), { user: "stdio" });
checks.push("released Codex MCP placeholders keep saved tool approvals and stay distinct from user servers");
const output = resolve(import.meta.dirname, "../.volto-build/rename-contracts-e2e");
await mkdir(output, { recursive: true });
await writeFile(resolve(output, "report.json"), `${JSON.stringify({ passed: true, checks }, null, 2)}\n`);
process.stdout.write(`${JSON.stringify({ passed: true, checks })}\n`);
