// Starts the real `ProviderRuntimeManager` over a store with downloaded provider CLIs, as main does,
// with no network (#3321). Checks that the downloaded CLI of a provider that is off is removed with
// its partial downloads and verification records, and reads as not downloaded after; that a provider
// that is on, whose CLI path the user set, or Pi while a key-only provider that runs on it is on, is
// refused; that a version a program holds stays whole and the removal reports it; that this
// instance does not stamp a provider that is off; and that a version another
// instance stamped after this one is kept.
// `bun scripts/provider-runtime-remove-e2e.ts`.
// Writes .openbot-build/provider-runtime-remove-e2e/report.json.
import assert from "node:assert/strict";
import { chmod, mkdir, mkdtemp, rm, stat, utimes, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { basename, dirname, join, resolve } from "node:path";
import type { AgentProviderId, ManagedProviderId } from "@openbot/contracts/agent-providers";
import { runCauseEffect } from "../src/backend/effect-boundary";
import { ProviderRuntimeManager } from "../src/main/provider-runtime-manager";

const OUT = resolve(import.meta.dirname, "../.openbot-build/provider-runtime-remove-e2e");
const DAY_MS = 24 * 60 * 60 * 1000;
/** Off since long ago, with two versions, a partial download and a verification record. */
const OFF: ManagedProviderId = "cursor";
/** On: its download is not removed. */
const ON: ManagedProviderId = "opencode";
/** Off, but a sibling instance stamped its version a moment ago. */
const SIBLING: ManagedProviderId = "cline";
/** On at the start, then turned off: this instance's own stamp does not keep it. */
const TURNED_OFF: ManagedProviderId = "grok";
/** On at the start, then turned off; a sibling stamped it after this instance did. */
const SHARED: ManagedProviderId = "antigravity";
/** Off, and the user pointed OpenBot at a CLI of their own. */
const CONFIGURED: ManagedProviderId = "claude";
const CONFIGURED_ENV = `OPENBOT_${CONFIGURED.toUpperCase()}_PATH`;
/** Off, but DeepSeek, which is on, runs on its CLI. */
const PI: ManagedProviderId = "pi";
/** Off, but its versions cannot be moved: a program holds them, as Windows does with an open CLI. */
const HELD: ManagedProviderId = "muse";

interface Report {
  passed: boolean;
  offStampedAtStart?: boolean;
  bytesBefore?: Partial<Record<ManagedProviderId, number>>;
  versionsBefore?: number;
  removed?: { versions: string[]; partials: string[]; records: string[]; phase: string; version: string | null };
  bytesAfter?: number;
  onRefused?: string;
  configuredRefused?: string;
  configuredKept?: boolean;
  piRefused?: string;
  piKept?: boolean;
  heldRefused?: string;
  heldWhole?: boolean;
  siblingKept?: boolean;
  turnedOffRemoved?: boolean;
  sharedKept?: boolean;
}

const root = await mkdtemp(join(tmpdir(), "openbot-provider-runtime-remove-e2e-"));
const store = join(root, "provider-runtimes");
const downloads = join(root, "downloads");
const report: Report = { passed: false };
const off = new Set<AgentProviderId>([OFF, SIBLING, CONFIGURED, PI, HELD]);
const previousConfigured = process.env[CONFIGURED_ENV];

const manager = new ProviderRuntimeManager({
  root: store,
  downloadRoot: downloads,
  isProviderOn: (provider) => !off.has(provider),
  availableDiskBytes: async () => Number.MAX_SAFE_INTEGER,
  fetchImpl: async () => {
    throw new TypeError("fetch failed");
  },
});

/** The directory that holds the versions of one provider CLI for this computer's target. */
function targetRoot(provider: ManagedProviderId): string {
  const pinned = manager.executablePath(provider);
  assert.ok(pinned, "this platform has managed runtimes");
  return dirname(dirname(dirname(pinned)));
}

/** Puts a version with an executable in the store, last stamped at `stampedAt`. */
async function install(provider: ManagedProviderId, version: string, stampedAt: Date): Promise<string> {
  const pinned = manager.executablePath(provider);
  assert.ok(pinned);
  const path = join(targetRoot(provider), version);
  await mkdir(join(path, "bin"), { recursive: true });
  await writeFile(join(path, "bin", basename(pinned)), "#!/bin/sh\n".padEnd(4096, "#"), { mode: 0o755 });
  await writeFile(join(dirname(path), `.${version}.verified.json`), "{}\n");
  await utimes(path, stampedAt, stampedAt);
  return path;
}

async function exists(path: string): Promise<boolean> {
  return stat(path).then(
    () => true,
    () => false,
  );
}

async function refusal(provider: ManagedProviderId): Promise<string> {
  const error = await runCauseEffect(manager.removeDownload(provider)).then(
    () => null,
    (failure: unknown) => failure,
  );
  assert.ok(error instanceof Error, `${provider} is refused`);
  return error.message;
}

try {
  process.env[CONFIGURED_ENV] = join(root, "user-cli", CONFIGURED);
  const old = new Date(Date.now() - 30 * DAY_MS);
  const offVersions = [await install(OFF, "0.0.1", old), await install(OFF, "0.0.2", old)];
  const target = basename(targetRoot(OFF));
  const partials = [
    join(downloads, `${OFF}-${target}-0.0.3.partial`),
    join(downloads, `${OFF}-${target}-0.0.3.partial.json`),
  ];
  await mkdir(downloads, { recursive: true });
  for (const partial of partials) await writeFile(partial, "partial");
  const onVersion = await install(ON, "0.0.1", old);
  const siblingVersion = await install(SIBLING, "0.0.1", new Date());
  const turnedOffVersion = await install(TURNED_OFF, "0.0.1", old);
  const sharedVersion = await install(SHARED, "0.0.1", old);
  const configuredVersion = await install(CONFIGURED, "0.0.1", old);
  const piVersion = await install(PI, "0.0.1", old);
  const heldVersion = await install(HELD, "0.0.1", old);

  await runCauseEffect(manager.initialize());
  // A provider that is on is stamped at start: the one older version is its working fallback.
  assert.ok((await stat(onVersion)).mtimeMs > old.getTime(), "a provider that is on is stamped at start");
  report.offStampedAtStart = (await stat(offVersions[1] ?? "")).mtimeMs > old.getTime();
  assert.equal(report.offStampedAtStart, false, "a provider that is off is not stamped");

  const measure = async (provider: ManagedProviderId) =>
    (await runCauseEffect(manager.measureDownload(provider))).bytes;
  report.bytesBefore = {
    [OFF]: await measure(OFF),
    [ON]: await measure(ON),
    [SIBLING]: await measure(SIBLING),
    [CONFIGURED]: await measure(CONFIGURED),
    [PI]: await measure(PI),
  };
  assert.ok((report.bytesBefore[OFF] ?? 0) >= 2 * 4096, "both versions of the off provider count");
  report.versionsBefore = (await runCauseEffect(manager.measureDownload(OFF))).versions;
  assert.equal(report.versionsBefore, 2, "the page names how many versions it removes");
  assert.equal(report.bytesBefore[ON], 0, "a provider that is on offers nothing to remove");
  assert.equal(report.bytesBefore[SIBLING], 0, "a version a sibling stamped is not counted");
  assert.equal(report.bytesBefore[CONFIGURED], 0, "a provider with a CLI path offers nothing to remove");
  assert.equal(report.bytesBefore[PI], 0, "Pi offers nothing to remove while DeepSeek runs on it");

  const snapshot = await runCauseEffect(manager.removeDownload(OFF));
  const removed = {
    versions: (await Promise.all(offVersions.map(exists))).every((found) => !found)
      ? offVersions.map((p) => basename(p))
      : [],
    partials: (await Promise.all(partials.map(exists))).every((found) => !found)
      ? partials.map((p) => basename(p))
      : [],
    records: (await exists(join(targetRoot(OFF), ".0.0.1.verified.json"))) ? [] : [".0.0.1.verified.json"],
    phase: snapshot.providers[OFF].phase,
    version: snapshot.providers[OFF].version,
  };
  report.removed = removed;
  assert.equal(removed.versions.length, 2, "every version is removed");
  assert.equal(removed.partials.length, 2, "the partial download is removed");
  assert.equal(removed.records.length, 1, "the verification records are removed");
  assert.equal(removed.phase, "not-downloaded", "the provider reads as not downloaded");
  assert.equal(removed.version, null, "no older copy is left to name");
  report.bytesAfter = await measure(OFF);
  assert.equal(report.bytesAfter, 0, "nothing is left to remove");

  report.onRefused = await refusal(ON);
  assert.ok(await exists(onVersion), "the download of a provider that is on stays");
  report.configuredRefused = await refusal(CONFIGURED);
  report.configuredKept = await exists(configuredVersion);
  assert.ok(report.configuredKept, "the store copy of a provider with a CLI path stays");
  report.piRefused = await refusal(PI);
  report.piKept = await exists(piVersion);
  assert.ok(report.piKept, "the Pi CLI that DeepSeek runs on stays");

  // A directory that refuses the move stays whole, and the removal says that it did not finish.
  await chmod(targetRoot(HELD), 0o555);
  try {
    report.heldRefused = await refusal(HELD);
  } finally {
    await chmod(targetRoot(HELD), 0o755);
  }
  const heldExecutable = manager.executablePath(HELD);
  report.heldWhole =
    Boolean(heldExecutable) && (await exists(join(heldVersion, "bin", basename(heldExecutable ?? ""))));
  assert.ok(report.heldWhole, "a version that cannot be moved keeps its executable");

  await runCauseEffect(manager.removeDownload(SIBLING));
  report.siblingKept = await exists(siblingVersion);
  assert.ok(report.siblingKept, "a version a sibling stamped recently stays");

  // Turned off after this instance stamped both at start. A sibling stamps one of them later.
  off.add(TURNED_OFF);
  off.add(SHARED);
  const sharedStamp = new Date((await stat(sharedVersion)).mtimeMs + 5000);
  await utimes(sharedVersion, sharedStamp, sharedStamp);
  await runCauseEffect(manager.removeDownload(TURNED_OFF));
  report.turnedOffRemoved = !(await exists(turnedOffVersion));
  assert.ok(report.turnedOffRemoved, "this instance's own stamp does not keep a version");
  await runCauseEffect(manager.removeDownload(SHARED));
  report.sharedKept = await exists(sharedVersion);
  assert.ok(report.sharedKept, "a sibling's stamp after this instance's keeps a version");

  report.passed = true;
} finally {
  if (previousConfigured === undefined) delete process.env[CONFIGURED_ENV];
  else process.env[CONFIGURED_ENV] = previousConfigured;
  await runCauseEffect(manager.stop()).catch(() => undefined);
  await mkdir(OUT, { recursive: true });
  await writeFile(join(OUT, "report.json"), `${JSON.stringify(report, null, 2)}\n`);
  await rm(root, { recursive: true, force: true });
}
process.stdout.write(`${JSON.stringify(report, null, 2)}\n`);
