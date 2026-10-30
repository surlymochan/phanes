// Run: bun scripts/transfer-upload-cleanup-e2e.ts
// Report: .openbot-build/transfer-upload-cleanup-e2e.json
//
// The last check puts a fault into the file system: the write of the archive is short, and the file
// system writes no byte at all. The old code wrote every chunk of the archive once and took the
// count of the write for the truth, so a short write left a truncated ZIP that the export reported
// as a saved file. The check holds that the export writes the whole archive when a write is short,
// and that it fails when a write makes no progress.
//
// The export of an agent with files read every file whole, so the memory of the process grew with
// the size of the file. A check now exports a file of 64 MiB in an isolated child process and
// holds that the memory of the process stays far below the size of the file. The other checks hold
// that the archive holds every file with its bytes and its mode, that an empty file and a file of
// exactly one chunk travel whole, and that a file that is gone is reported and left out.

import { mock } from "bun:test";
import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { createHash } from "node:crypto";
import { chmod, mkdir, mkdtemp, open, readdir, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import type { AgentSummary } from "@openbot/contracts/ipc";
import { sourceText } from "@openbot/i18n/source";
import { Effect, Schema } from "effect";
import { strToU8, unzipSync, Zip, ZipPassThrough, zipSync } from "fflate";
import { runCauseEffect } from "../src/backend/effect-boundary";
import { executableEntries, sha256 } from "../src/main/agent-archive";
import {
  agentPrefix,
  decodeTransferManifest,
  INSTANCE_TRANSFER_FORMAT,
  INSTANCE_TRANSFER_MANIFEST,
  INSTANCE_TRANSFER_VERSION,
} from "../src/main/instance-transfer-manifest";
import { type InstanceTransferOptions, InstanceTransferService } from "../src/main/instance-transfer-service";
import { LocalSkillFailure } from "../src/main/local-skill-library";
import { SkillMarketplaceFailure } from "../src/main/skill-marketplace-service";

// The file system of the check. It writes at most `writeLimit` bytes per call, like a file system
// whose buffer is full. A limit of zero writes no byte at all. The built-ins of node have no seam
// to inject a fault into, so the check takes the module apart and puts the fault into the open.
// The limit is off until a check sets it, so the other checks write their files as they do.
// A path in `deletedPaths` is gone when the export reads it, like a file that a running agent
// deletes after the export found it.
const realFs = await import("node:fs/promises");
const realOpen = realFs.open;
const realLstat = realFs.lstat;
let writeLimit = Number.POSITIVE_INFINITY;
const deletedPaths = new Set<string>();

mock.module("node:fs/promises", () => ({
  ...realFs,
  lstat: async (path: Parameters<typeof realLstat>[0]) => {
    const info = await realLstat(path);
    if (typeof path === "string" && deletedPaths.has(path)) await realFs.rm(path, { force: true });
    return info;
  },
  open: async (...args: Parameters<typeof realOpen>) => {
    const handle = await realOpen(...args);
    return {
      write: async (
        buffer: Uint8Array,
        offset?: number,
        length?: number,
        position?: number | null,
      ): Promise<{ bytesWritten: number; buffer: Uint8Array }> => {
        const start = offset ?? 0;
        const count = Math.min(length ?? buffer.length - start, writeLimit);
        if (count <= 0) return { bytesWritten: 0, buffer };
        const result = await handle.write(buffer, start, count, position ?? null);
        return { bytesWritten: count, buffer: result.buffer };
      },
      read: (buffer: Uint8Array, offset?: number, length?: number, position?: number | null) =>
        handle.read(buffer, offset, length, position),
      close: () => handle.close(),
    };
  },
}));

const run = Effect.runPromise;
const exportInput = { agentIds: [], includeFiles: false, includeSettings: false };
const manifest = {
  format: INSTANCE_TRANSFER_FORMAT,
  version: INSTANCE_TRANSFER_VERSION,
  source: { appVersion: "e2e", exportedAt: "2026-10-14T00:00:00.000Z" },
  agents: [],
  settings: { mcpServers: [], customProviders: [], customAgents: [], sidebarSections: [], routineFlows: [] },
  shared: null,
  signedIn: [],
};
const bytes = zipSync({ [INSTANCE_TRANSFER_MANIFEST]: strToU8(JSON.stringify(manifest)) });
// Empty exports omit settings and cannot be imported. Check their wire manifest too.
const decodeArchiveManifest = Schema.decodeUnknownSync(
  Schema.fromJsonString(
    Schema.Struct({
      format: Schema.Literal(INSTANCE_TRANSFER_FORMAT),
      version: Schema.Literal(INSTANCE_TRANSFER_VERSION),
      source: Schema.Struct({ appVersion: Schema.Literal("e2e"), exportedAt: Schema.String }),
      agents: Schema.Array(Schema.Never),
      settings: Schema.NullOr(
        Schema.Struct({
          mcpServers: Schema.Array(Schema.Never),
          customProviders: Schema.Array(Schema.Never),
          customAgents: Schema.Array(Schema.Never),
          sidebarSections: Schema.Array(Schema.Never),
          routineFlows: Schema.Array(Schema.Never),
        }),
      ),
      shared: Schema.Null,
      signedIn: Schema.Array(Schema.Never),
    }),
  ),
);

function unexpected(operation: string): Error {
  return new Error(`Unexpected boundary call: ${operation}`);
}

/** The library of the checks: no export publishes a skill, so every call must fail. */
const library: ReturnType<InstanceTransferOptions["skills"]["library"]> = {
  list: () => Effect.fail(new LocalSkillFailure({ cause: unexpected("library.list") })),
  create: (_agentId, _folder) => Effect.fail(new LocalSkillFailure({ cause: unexpected("library.create") })),
  revise: (_agentId, _id, _revision, _folder) =>
    Effect.fail(new LocalSkillFailure({ cause: unexpected("library.revise") })),
  withdraw: (_id, _revision) => Effect.fail(new LocalSkillFailure({ cause: unexpected("library.withdraw") })),
};

/** The settings of the checks: no export carries settings, so every call must fail. */
function settingsPort(): InstanceTransferOptions["settings"] {
  return {
    mcpServers: () => {
      throw unexpected("mcpServers");
    },
    saveMcpServer: (_config) => Effect.fail(unexpected("saveMcpServer")),
    customProviders: () => {
      throw unexpected("customProviders");
    },
    saveCustomProvider: (_input) => Effect.fail(unexpected("saveCustomProvider")),
    customAgents: () => {
      throw unexpected("customAgents");
    },
    providersOff: () => {
      throw unexpected("providersOff");
    },
    sidebar: () => {
      throw unexpected("sidebar");
    },
    mutateSidebar: (_action) => Effect.fail(unexpected("mutateSidebar")),
    routineFlowLinks: (_agentId) => Effect.fail(unexpected("routineFlowLinks")),
    connectRoutineFlow: (_input) => Effect.fail(unexpected("connectRoutineFlow")),
    signedInProviders: () => [],
  };
}

function service(uploadDirectory: string): InstanceTransferService {
  // These checks transfer an empty instance. Other boundary calls must fail.
  const agents: InstanceTransferOptions["agents"] = {
    listAgents: () => [],
    listRoutines: (_agentId) => {
      throw unexpected("listRoutines");
    },
    listMemories: (_agentId) => {
      throw unexpected("listMemories");
    },
    getMemorySelection: (_agentId) => {
      throw unexpected("getMemorySelection");
    },
    setMemoryInclusion: (_input) => {
      throw unexpected("setMemoryInclusion");
    },
    resolveAvatar: (_agentId) => {
      throw unexpected("resolveAvatar");
    },
    listModels: () => {
      throw unexpected("listModels");
    },
    createAgentProfile: (_input) => Effect.fail(unexpected("createAgentProfile")),
    updateAgent: (_input) => Effect.fail(unexpected("updateAgent")),
    createRoutine: (_input, _options) => {
      throw unexpected("createRoutine");
    },
    createMemory: (_input) => {
      throw unexpected("createMemory");
    },
    memoryLimit: () => {
      throw unexpected("memoryLimit");
    },
    initializeMemorySelection: (_agentId) => {
      throw unexpected("initializeMemorySelection");
    },
    setAvatar: (_agentId, _image) => Effect.fail(unexpected("setAvatar")),
    deleteAgent: (_agentId) => Effect.fail(unexpected("deleteAgent")),
  };
  const skills: InstanceTransferOptions["skills"] = {
    library: () => library,
    installLocal: (_input) => Effect.fail(new SkillMarketplaceFailure({ cause: unexpected("installLocal") })),
  };
  const settings: InstanceTransferOptions["settings"] = settingsPort();
  const options: InstanceTransferOptions = {
    uploadDirectory,
    sharedRoot: join(dirname(uploadDirectory), "shared"),
    appVersion: "e2e",
    agents,
    skills,
    settings,
    timezone: () => "UTC",
  };
  return new InstanceTransferService(options);
}

/** The size of the chunk that the export reads from a file. */
const CHUNK_SIZE = 64 * 1024;
/** The size of the file that the export must hold in bounded memory. */
const LARGE_BYTES = 64 * 1024 * 1024;
/** The name of the file of the workspace that the export reads in chunks. */
const LARGE_NAME = "large.bin";
/** The agent of the exports that carry files. */
const agent: AgentSummary = {
  id: "agent-1",
  name: "Agent One",
  title: "",
  description: "",
  notifications: false,
  provider: "codex",
  model: "gpt-5.5",
  reasoningEffort: "medium",
  threadId: null,
  workspacePath: "",
  preview: "",
  updatedAt: null,
  avatarSeed: "agent-1",
  avatarHue: null,
  avatarUrl: null,
};

/** A service that exports one agent with the files of its workspace. */
function fileService(workspace: string, sharedRoot: string): InstanceTransferService {
  const summary: AgentSummary = { ...agent, workspacePath: workspace };
  const agents: InstanceTransferOptions["agents"] = {
    listAgents: () => [summary],
    listRoutines: () => [],
    listMemories: () => [],
    getMemorySelection: () => ({ selections: [] }),
    setMemoryInclusion: (_input) => {
      throw unexpected("setMemoryInclusion");
    },
    resolveAvatar: () => null,
    listModels: () => [],
    createAgentProfile: (_input) => Effect.fail(unexpected("createAgentProfile")),
    updateAgent: (_input) => Effect.fail(unexpected("updateAgent")),
    createRoutine: (_input, _options) => {
      throw unexpected("createRoutine");
    },
    createMemory: (_input) => {
      throw unexpected("createMemory");
    },
    memoryLimit: () => 0,
    initializeMemorySelection: (_agentId) => {
      throw unexpected("initializeMemorySelection");
    },
    setAvatar: (_agentId, _image) => Effect.fail(unexpected("setAvatar")),
    deleteAgent: (_agentId) => Effect.fail(unexpected("deleteAgent")),
  };
  const options: InstanceTransferOptions = {
    sharedRoot,
    appVersion: "e2e",
    agents,
    skills: {
      library: () => library,
      installLocal: (_input) => Effect.fail(new SkillMarketplaceFailure({ cause: unexpected("installLocal") })),
    },
    settings: settingsPort(),
    timezone: () => "UTC",
  };
  return new InstanceTransferService(options);
}

/** The hash of a file, read in chunks so the memory of the process stays small. */
async function fileHash(path: string): Promise<string> {
  const hash = createHash("sha256");
  const handle = await open(path, "r");
  try {
    const buffer = new Uint8Array(CHUNK_SIZE);
    for (;;) {
      const { bytesRead } = await handle.read(buffer, 0, CHUNK_SIZE, null);
      if (bytesRead === 0) break;
      hash.update(buffer.subarray(0, bytesRead));
    }
  } finally {
    await handle.close();
  }
  return hash.digest("hex");
}

async function names(directory: string): Promise<string[]> {
  return (await readdir(directory)).sort();
}

async function verifyArchives(directory: string, expected: number): Promise<string[]> {
  const files = await names(directory);
  assert.equal(files.length, expected);
  for (const file of files) {
    const archive = unzipSync(await readFile(join(directory, file)));
    const manifestBytes = archive[INSTANCE_TRANSFER_MANIFEST];
    assert.ok(manifestBytes, `Missing manifest: ${file}`);
    const value = decodeArchiveManifest(new TextDecoder("utf-8", { fatal: true }).decode(manifestBytes));
    assert.ok(Number.isFinite(Date.parse(value.source.exportedAt)), `Invalid export date: ${file}`);
  }
  return files;
}

async function seed(directory: string): Promise<string[]> {
  const previous = service(directory);
  await run(previous.stageUpload(async () => bytes, "previous-owner"));
  await run(previous.exportForMember(exportInput));
  await writeFile(join(directory, "interrupted.zip.part"), bytes);
  return names(directory);
}

async function restart(directory: string): Promise<string[]> {
  const child = spawnSync(
    process.execPath,
    [...process.execArgv, fileURLToPath(import.meta.url), "--seed", directory],
    { encoding: "utf8" },
  );
  assert.equal(child.status, 0, child.stderr);
  const orphans = Schema.decodeUnknownSync(Schema.fromJsonString(Schema.Array(Schema.String)))(child.stdout);
  assert.equal(orphans.length, 3);
  return [...orphans];
}

async function checkRestart(root: string, firstWriter: "upload" | "export"): Promise<void> {
  const directory = join(root, firstWriter, "instance-transfer-uploads");
  const orphans = await restart(directory);
  const current = service(directory);
  let exported: string | undefined;
  if (firstWriter === "export") exported = await run(current.exportForMember(exportInput));
  const [alice, bob] = await Promise.all([
    run(current.stageUpload(async () => bytes, "alice")),
    run(current.stageUpload(async () => bytes, "bob")),
  ]);
  for (const orphan of orphans) {
    await assert.rejects(readFile(join(directory, orphan)), { code: "ENOENT" }, `Old file remains: ${orphan}`);
  }
  if (!exported) exported = await run(current.exportForMember(exportInput));
  await verifyArchives(directory, 3);
  const before = await names(directory);
  await run(current.discard(alice.token, "bob"));
  await assert.rejects(run(current.apply({ token: alice.token, keys: [], includeSettings: false }, "bob")));
  assert.deepEqual(await names(directory), before);
  await run(current.discard(alice.token, "alice"));
  await verifyArchives(directory, 2);
  const result = await run(current.apply({ token: bob.token, keys: [], includeSettings: false }, "bob"));
  assert.deepEqual(result.agents, []);
  assert.deepEqual(await names(directory), [exported.slice(directory.length + 1)]);
  await assert.rejects(run(current.apply({ token: bob.token, keys: [], includeSettings: false }, "bob")));
  await rm(exported);
}

async function checkConcurrent(root: string): Promise<void> {
  const directory = join(root, "concurrent", "instance-transfer-uploads");
  await mkdir(directory, { recursive: true });
  await writeFile(join(directory, "old.zip"), bytes);
  const current = service(directory);
  const [alice, exported, bob] = await Promise.all([
    run(current.stageUpload(async () => bytes, "alice")),
    run(current.exportForMember(exportInput)),
    run(current.stageUpload(async () => bytes, "bob")),
  ]);
  await verifyArchives(directory, 3);
  await assert.rejects(readFile(join(directory, "old.zip")), { code: "ENOENT" });
  await Promise.all([
    run(current.apply({ token: alice.token, keys: [], includeSettings: false }, "alice")),
    run(current.apply({ token: bob.token, keys: [], includeSettings: false }, "bob")),
  ]);
  await readFile(exported);
  await rm(exported);
}

async function checkRetry(root: string): Promise<void> {
  const blocker = join(root, "blocked");
  const directory = join(blocker, "instance-transfer-uploads");
  await writeFile(blocker, "A file blocks the parent directory.");
  const current = service(directory);
  const failed = await Promise.allSettled([
    run(current.stageUpload(async () => bytes, "alice")),
    run(current.exportForMember(exportInput)),
  ]);
  assert.ok(failed.every((result) => result.status === "rejected"));
  await rm(blocker);
  const preview = await run(current.stageUpload(async () => bytes, "alice"));
  await verifyArchives(directory, 1);
  await run(current.discard(preview.token, "alice"));
  assert.deepEqual(await names(directory), []);
}

async function checkLocal(root: string): Promise<void> {
  const path = join(root, "user-owned.zip");
  await writeFile(path, bytes);
  const current = service(join(root, "local", "instance-transfer-uploads"));
  const local = await run(current.stage(path));
  const uploaded = await run(current.stageUpload(async () => bytes, "alice"));
  await run(current.discard(local.token));
  assert.deepEqual(await readFile(path), Buffer.from(bytes));
  await assert.rejects(run(current.stageUpload(async () => new Uint8Array([1, 2, 3]), "alice")));
  await verifyArchives(join(root, "local", "instance-transfer-uploads"), 1);
  await run(current.apply({ token: uploaded.token, keys: [], includeSettings: false }, "alice"));
  assert.deepEqual(await readFile(path), Buffer.from(bytes));
}

async function checkArchiveWrite(root: string): Promise<void> {
  const directory = join(root, "archive-write", "instance-transfer-uploads");
  await mkdir(directory, { recursive: true });
  const current = service(directory);

  // The export without a fault is the reference: the archive holds the manifest of the instance.
  const reference = await run(current.exportForMember(exportInput));
  const referenceBytes = new Uint8Array(await readFile(reference));
  const referenceEntries = unzipSync(referenceBytes);
  const referenceManifest = referenceEntries[INSTANCE_TRANSFER_MANIFEST];
  assert.ok(referenceManifest, "The reference archive holds no manifest.");

  // The old code wrote every chunk of the archive once and took the count of the write for the
  // truth. The file system wrote only the first bytes of the chunk, so the archive ended in the
  // middle of the file, and the export reported it as a saved file.
  writeLimit = 64;
  const chunks: Uint8Array[] = [];
  const zip = new Zip((error, chunk) => {
    if (error) throw error;
    chunks.push(chunk);
  });
  const entry = new ZipPassThrough(INSTANCE_TRANSFER_MANIFEST);
  zip.add(entry);
  entry.push(referenceManifest, true);
  zip.end();
  const old = join(root, "archive-write", "old.zip");
  const handle = await open(old, "w");
  try {
    for (const chunk of chunks) await handle.write(chunk);
  } finally {
    await handle.close();
  }
  const oldBytes = new Uint8Array(await readFile(old));
  assert.ok(
    oldBytes.byteLength < referenceBytes.byteLength,
    `The fault did not shorten the archive: ${oldBytes.byteLength} of ${referenceBytes.byteLength} bytes.`,
  );
  assert.throws(() => unzipSync(oldBytes), "A reader opened the truncated archive.");

  // The same fault, now in the export. The archive must hold every byte of the manifest.
  const exported = await run(current.exportForMember(exportInput));
  const bytes = new Uint8Array(await readFile(exported));
  assert.equal(bytes.byteLength, referenceBytes.byteLength, "The export wrote a shorter archive.");
  const exportedManifest = unzipSync(bytes)[INSTANCE_TRANSFER_MANIFEST];
  assert.ok(exportedManifest, "The archive holds no manifest.");
  const decoded = decodeArchiveManifest(new TextDecoder("utf-8", { fatal: true }).decode(exportedManifest));
  assert.equal(decoded.format, INSTANCE_TRANSFER_FORMAT, "The archive holds another format.");
  assert.equal(decoded.version, INSTANCE_TRANSFER_VERSION, "The archive holds another version.");
  assert.deepEqual(decoded.agents, [], "The archive holds another agent.");
  await verifyArchives(directory, 2);

  // A write that writes no byte never ends. The export must fail instead of reporting a saved
  // file that holds nothing.
  const stuckDirectory = join(root, "stuck-write", "instance-transfer-uploads");
  await mkdir(stuckDirectory, { recursive: true });
  const stuck = service(stuckDirectory);
  writeLimit = 0;
  await assert.rejects(
    () => runCauseEffect(stuck.exportForMember(exportInput)),
    /wrote no byte/,
    "The export reported an archive that it did not write.",
  );
  assert.deepEqual(await names(stuckDirectory), [], "The failed export left a file behind.");
  writeLimit = Number.POSITIVE_INFINITY;
}

/** The peak of the memory of the process, in bytes. */
function peakMemory(): number {
  return process.resourceUsage().maxRSS * 1024;
}

/** What a child process reports about the file it read and the memory it used. */
const decodeMeasurement = Schema.decodeUnknownSync(
  Schema.fromJsonString(Schema.Struct({ peak: Schema.Number, size: Schema.Number, hash: Schema.String })),
);

async function checkExportContent(root: string): Promise<void> {
  writeLimit = Number.POSITIVE_INFINITY;
  const workspace = join(root, "content", "workspace");
  await mkdir(workspace, { recursive: true });
  // An empty file, a file of exactly one chunk, a file of one chunk and one byte, and a file with
  // the mode of an executable.
  await writeFile(join(workspace, "empty.bin"), new Uint8Array(0));
  await writeFile(join(workspace, "chunk.bin"), new Uint8Array(CHUNK_SIZE).fill(0x41));
  await writeFile(join(workspace, "chunk-plus.bin"), new Uint8Array(CHUNK_SIZE + 1).fill(0x42));
  await writeFile(join(workspace, "script.sh"), "#!/bin/sh\necho synthetic\n");
  await chmod(join(workspace, "script.sh"), 0o755);

  const current = fileService(workspace, join(root, "content", "shared"));
  const path = join(root, "content", "transfer.zip");
  const result = await run(
    current.exportTo(path, { agentIds: [agent.id], includeFiles: true, includeSettings: false }),
  );
  assert.equal(result.saved, true, "The export did not save a file.");
  assert.deepEqual(result.warnings, [], "The export reported a warning.");
  const bytes = new Uint8Array(await readFile(path));
  const entries = unzipSync(bytes);

  // The manifest travels as the export wrote it.
  const manifestBytes = entries[INSTANCE_TRANSFER_MANIFEST];
  assert.ok(manifestBytes, "The archive holds no manifest.");
  const decoded = decodeTransferManifest(manifestBytes);
  assert.deepEqual(decoded.warnings, [], "The manifest holds a warning.");
  assert.equal(decoded.manifest.agents.length, 1, "The manifest holds another number of agents.");
  const [exported] = decoded.manifest.agents;
  assert.ok(exported, "The manifest holds no agent.");
  assert.equal(exported.key, agent.id, "The manifest holds another agent key.");
  assert.equal(exported.name, agent.name, "The manifest holds another agent name.");
  const folder = exported.files;
  assert.ok(folder, "The manifest holds no folder of files.");
  assert.equal(folder, `${agentPrefix(agent.id)}workspace`, "The manifest holds another folder.");

  // Every file travels with its bytes.
  for (const name of ["empty.bin", "chunk.bin", "chunk-plus.bin", "script.sh"]) {
    const entry: Uint8Array | undefined = entries[`${folder}/${name}`];
    assert.ok(entry, `The archive holds no ${name}.`);
    const source = await readFile(join(workspace, name));
    assert.equal(entry.byteLength, source.byteLength, `The archive holds another size of ${name}.`);
    assert.equal(sha256(entry), sha256(source), `The archive holds another content of ${name}.`);
  }

  // The mode of the file stays, so the import restores the bits.
  const executable = executableEntries(bytes);
  assert.ok(executable.has(`${folder}/script.sh`), "The archive lost the mode of the script.");
  assert.equal(executable.size, 1, "The archive holds another executable file.");
}

async function checkExportMissingFile(root: string): Promise<void> {
  writeLimit = Number.POSITIVE_INFINITY;
  const workspace = join(root, "missing", "workspace");
  await mkdir(workspace, { recursive: true });
  const gone = join(workspace, "gone.bin");
  await writeFile(gone, "gone\n");
  await writeFile(join(workspace, "kept.bin"), "kept\n");
  const current = fileService(workspace, join(root, "missing", "shared"));
  const path = join(root, "missing", "transfer.zip");
  // The file system reports the file as gone when the export reads it, like a file that a running
  // agent deletes after the export found it.
  deletedPaths.add(gone);
  try {
    const result = await run(
      current.exportTo(path, { agentIds: [agent.id], includeFiles: true, includeSettings: false }),
    );
    assert.equal(result.saved, true, "The export did not save a file.");
    assert.deepEqual(
      result.warnings,
      [sourceText("error.import.transferFilesGone", { number: 1 })],
      "The export did not report the file that was gone.",
    );
  } finally {
    deletedPaths.delete(gone);
  }
  const entries = unzipSync(new Uint8Array(await readFile(path)));
  const folder = `${agentPrefix(agent.id)}workspace/`;
  assert.ok(entries[`${folder}kept.bin`], "The archive holds no file.");
  assert.ok(!entries[`${folder}gone.bin`], "The archive holds the file that was gone.");
}

async function checkExportMemory(root: string): Promise<void> {
  writeLimit = Number.POSITIVE_INFINITY;
  const workspace = join(root, "memory", "workspace");
  await mkdir(workspace, { recursive: true });
  const source = join(workspace, LARGE_NAME);
  const fixture = await open(source, "w");
  try {
    const block = new Uint8Array(1024 * 1024).fill(0x5a);
    for (let written = 0; written < LARGE_BYTES; written += block.length) await fixture.write(block);
  } finally {
    await fixture.close();
  }
  const output = join(root, "memory", "transfer.zip");

  // The export runs in a child process, so the memory of this process does not count.
  const exported = spawnSync(
    process.execPath,
    [...process.execArgv, fileURLToPath(import.meta.url), "--export", workspace, output],
    { encoding: "utf8" },
  );
  assert.equal(exported.status, 0, exported.stderr);
  const measurement = decodeMeasurement(exported.stdout);

  // The way that reads the whole file grows the memory of the process by the size of the file.
  const whole = spawnSync(
    process.execPath,
    [...process.execArgv, fileURLToPath(import.meta.url), "--read-whole", source],
    { encoding: "utf8" },
  );
  assert.equal(whole.status, 0, whole.stderr);
  const baseline = decodeMeasurement(whole.stdout);

  assert.equal(measurement.size, LARGE_BYTES, "The archive holds another size of the large file.");
  assert.equal(measurement.hash, await fileHash(source), "The archive holds another content of the large file.");
  assert.equal(baseline.hash, measurement.hash, "The baseline read another file.");
  assert.ok(
    measurement.peak < baseline.peak - LARGE_BYTES / 2,
    `The export held the file in the memory of the process: ${measurement.peak} bytes of peak memory, the whole-file way ${baseline.peak} bytes.`,
  );
}

interface Report {
  status: "running" | "passed" | "failed";
  checks: Array<{ name: string; status: "passed" | "failed" }>;
  error?: string;
}

if (process.argv[2] === "--seed") {
  const directory = process.argv[3];
  assert.ok(directory, "The seed directory is required.");
  process.stdout.write(JSON.stringify(await seed(directory)));
} else if (process.argv[2] === "--export") {
  // The export of the child process, with the memory of the process after it.
  const workspace = process.argv[3];
  const output = process.argv[4];
  assert.ok(workspace && output, "The workspace and the output path are required.");
  const current = fileService(workspace, join(dirname(output), "shared"));
  await run(current.exportTo(output, { agentIds: [agent.id], includeFiles: true, includeSettings: false }));
  // The peak of the memory is read before the archive is read, so the reading does not count.
  const peak = peakMemory();
  const entries = unzipSync(new Uint8Array(await readFile(output)));
  const entry: Uint8Array | undefined = entries[`${agentPrefix(agent.id)}workspace/${LARGE_NAME}`];
  assert.ok(entry, "The archive holds no large file.");
  process.stdout.write(JSON.stringify({ peak, size: entry.byteLength, hash: sha256(entry) }));
} else if (process.argv[2] === "--read-whole") {
  // The way that read every file whole, as the reference of the memory.
  const path = process.argv[3];
  assert.ok(path, "The path is required.");
  const bytes = await readFile(path);
  const peak = peakMemory();
  process.stdout.write(JSON.stringify({ peak, size: bytes.byteLength, hash: sha256(bytes) }));
} else {
  const reportPath = fileURLToPath(new URL("../.openbot-build/transfer-upload-cleanup-e2e.json", import.meta.url));
  const root = await mkdtemp(join(tmpdir(), "transfer-upload-cleanup-"));
  const report: Report = { status: "running", checks: [] };
  let activeCheck: string | undefined;
  try {
    const checks: Array<[string, () => Promise<void>]> = [
      ["restart-upload-first", () => checkRestart(root, "upload")],
      ["restart-export-first", () => checkRestart(root, "export")],
      ["concurrent-writers", () => checkConcurrent(root)],
      ["preparation-retry", () => checkRetry(root)],
      ["local-file-and-invalid-upload", () => checkLocal(root)],
      ["archive-write", () => checkArchiveWrite(root)],
      ["export-content", () => checkExportContent(root)],
      ["export-missing-file", () => checkExportMissingFile(root)],
      ["export-bounded-memory", () => checkExportMemory(root)],
    ];
    for (const [name, check] of checks) {
      activeCheck = name;
      await check();
      report.checks.push({ name, status: "passed" });
    }
    report.status = "passed";
  } catch (error) {
    report.status = "failed";
    if (activeCheck) report.checks.push({ name: activeCheck, status: "failed" });
    report.error = error instanceof Error ? error.message : String(error);
    process.exitCode = 1;
  } finally {
    await rm(root, { recursive: true, force: true });
    await mkdir(dirname(reportPath), { recursive: true });
    await writeFile(reportPath, `${JSON.stringify(report, null, 2)}\n`);
    process.stdout.write(`${JSON.stringify(report)}\n`);
  }
}
