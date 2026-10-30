// Checks the ZIP of the backup export that Linux writes in the process. The check puts a fault
// into the file system: the write of the archive is short, and the file system writes no byte at
// all. The old code wrote every chunk of the archive once and took the count of the write for the
// truth, so a short write left a truncated ZIP that the export reported as a saved backup. The
// check holds that the export writes the whole archive when a write is short, and that it fails
// when a write makes no progress. A step holds that the archive keeps the mode of the user and
// leaves out the links, and a last step holds that the export reads a large file in bounded
// memory.
// Run: `bun scripts/backup-archive-e2e.ts`.
// Writes .openbot-build/backup-archive-e2e/report.json.

import { mock } from "bun:test";
import assert from "node:assert/strict";
import { mkdirSync, writeFileSync } from "node:fs";
import { mkdir, mkdtemp, open, readFile, rm, stat, symlink, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { strFromU8, unzipSync, Zip, ZipPassThrough } from "fflate";
import { runCauseEffect } from "../src/backend/effect-boundary";

const OUT = resolve(import.meta.dirname, "../.openbot-build/backup-archive-e2e");
/** The size of the file that the export must hold in bounded memory. */
const LARGE_BYTES = 64 * 1024 * 1024;
/** The size of the block that the check writes the large file with. */
const WRITE_BLOCK = 1024 * 1024;

interface Step {
  name: string;
  passed: boolean;
  detail?: unknown;
  error?: string;
}

const report: { passed: boolean; steps: Step[] } = { passed: false, steps: [] };

async function step<T>(
  name: string,
  run: () => Promise<T>,
  summarize: (value: T) => unknown = (value) => value,
): Promise<T> {
  process.stdout.write(`- ${name}\n`);
  try {
    const value = await run();
    report.steps.push({ name, passed: true, detail: summarize(value) });
    return value;
  } catch (error) {
    report.steps.push({ name, passed: false, error: error instanceof Error ? error.message : String(error) });
    throw error;
  }
}

// The file system of the check. It writes at most `writeLimit` bytes per call, like a file system
// whose buffer is full. A limit of zero writes no byte at all. The built-ins of node have no seam
// to inject a fault into, so the check takes the module apart and puts the fault into the open.
const realFs = await import("node:fs/promises");
const realOpen = realFs.open;
let writeLimit = Number.POSITIVE_INFINITY;

mock.module("node:fs/promises", () => ({
  ...realFs,
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

// The module under the fault reads the file system when it loads, so it loads after the fault.
const { createBackupArchive } = await import("../src/main/backup-archive");

const root = await mkdtemp(join(tmpdir(), "openbot-backup-archive-e2e-"));
const folder = join(root, "OpenBot Backup");
const CONTENT = "synthetic backup content\n".repeat(400);
const MANIFEST = '{"schemaVersion":4,"synthetic":true}\n';

await mkdir(join(folder, "attachments"), { recursive: true });
await writeFile(join(folder, "openbot-data.json"), MANIFEST, "utf8");
await writeFile(join(folder, "attachments", "note.txt"), CONTENT, "utf8");

try {
  await step(
    "the old write left the archive short",
    async () => {
      // The old code wrote every chunk of the archive once and took the count of the write for
      // the truth. The file system wrote only the first bytes of the chunk, so the archive ended
      // in the middle of the file, and the export reported it as a saved backup.
      writeLimit = 64;
      const chunks: Uint8Array[] = [];
      const zip = new Zip((error, chunk) => {
        if (error) throw error;
        chunks.push(chunk);
      });
      const entry = new ZipPassThrough("OpenBot Backup/attachments/note.txt");
      zip.add(entry);
      entry.push(new TextEncoder().encode(CONTENT), true);
      zip.end();
      const path = join(root, "old.zip");
      const handle = await open(path, "w", 0o600);
      for (const chunk of chunks) await handle.write(chunk);
      await handle.close();
      const bytes = new Uint8Array(await readFile(path));
      const whole = chunks.reduce((sum, chunk) => sum + chunk.length, 0);
      assert.ok(
        bytes.byteLength < whole,
        `The fault did not shorten the archive: ${bytes.byteLength} of ${whole} bytes.`,
      );
      assert.throws(() => unzipSync(bytes), "A reader opened the truncated archive.");
      return { bytes: bytes.byteLength, whole };
    },
    (value) => ({ bytes: value.bytes, of: value.whole }),
  );

  await step(
    "the export writes the whole archive when the write is short",
    async () => {
      // The same fault, now in the export. The archive must hold every byte of every file.
      writeLimit = 512;
      const archive = join(root, "short.zip");
      await runCauseEffect(createBackupArchive("linux", folder, archive));
      const bytes = new Uint8Array(await readFile(archive));
      const entries = unzipSync(bytes);
      const note = entries["OpenBot Backup/attachments/note.txt"];
      assert.ok(note, "The archive holds no note.");
      assert.equal(strFromU8(note), CONTENT, "The archive holds another note.");
      const manifest = entries["OpenBot Backup/openbot-data.json"];
      assert.ok(manifest, "The archive holds no manifest.");
      assert.equal(strFromU8(manifest), MANIFEST, "The archive holds another manifest.");
      return { bytes: bytes.byteLength, entries: Object.keys(entries).length };
    },
    (value) => ({ bytes: value.bytes, entries: value.entries }),
  );

  await step(
    "the export fails when the write makes no progress",
    async () => {
      // A write that writes no byte never ends. The export must fail instead of reporting a
      // saved backup that holds nothing.
      writeLimit = 0;
      const archive = join(root, "stuck.zip");
      await assert.rejects(
        () => runCauseEffect(createBackupArchive("linux", folder, archive)),
        /wrote no byte/,
        "The export reported a backup that it did not write.",
      );
      return { failed: true };
    },
    () => ({ failed: true }),
  );

  await step(
    "the archive keeps the mode of the user and leaves out the links",
    async () => {
      // The export must not follow a link out of the folder, and the archive belongs to the user
      // alone.
      await symlink(join(folder, "attachments", "note.txt"), join(folder, "note-link.txt"));
      writeLimit = Number.POSITIVE_INFINITY;
      const archive = join(root, "private.zip");
      await runCauseEffect(createBackupArchive("linux", folder, archive));
      const mode = (await stat(archive)).mode & 0o777;
      assert.equal(mode, 0o600, `The archive is readable by others: ${mode.toString(8)}.`);
      const entries = Object.keys(unzipSync(new Uint8Array(await readFile(archive))));
      assert.ok(!entries.includes("OpenBot Backup/note-link.txt"), "The archive holds the link.");
      assert.ok(
        entries.every((name) => name.startsWith("OpenBot Backup/")),
        `The archive holds an entry outside the folder: ${entries.join(", ")}.`,
      );
      return { mode: "600", entries: entries.length };
    },
    (value) => ({ mode: value.mode, entries: value.entries }),
  );

  await step(
    "the export holds a large file in bounded memory",
    async () => {
      // The file holds 64 MiB of synthetic data. The export reads it in chunks and waits for the
      // write of every chunk, so the memory of the process must not grow with the file.
      const large = join(folder, "large.bin");
      const fixture = await realOpen(large, "w", 0o600);
      try {
        const block = new Uint8Array(WRITE_BLOCK).fill(0x5a);
        for (let written = 0; written < LARGE_BYTES; written += WRITE_BLOCK) await fixture.write(block);
      } finally {
        await fixture.close();
      }
      writeLimit = Number.POSITIVE_INFINITY;
      const before = process.memoryUsage().rss;
      const archive = join(root, "large.zip");
      await runCauseEffect(createBackupArchive("linux", folder, archive));
      const growth = process.memoryUsage().rss - before;
      assert.ok(growth < LARGE_BYTES / 4, `The export held ${growth} bytes of the file in the memory of the process.`);
      const entries = unzipSync(new Uint8Array(await readFile(archive)));
      const stored = entries["OpenBot Backup/large.bin"];
      assert.ok(stored, "The archive holds no large file.");
      assert.equal(stored.byteLength, LARGE_BYTES, "The archive holds another size of the large file.");
      assert.equal(stored[0], 0x5a, "The archive holds another content of the large file.");
      assert.equal(stored[LARGE_BYTES - 1], 0x5a, "The archive holds another content of the large file.");
      return { bytes: LARGE_BYTES, growth };
    },
    (value) => ({ bytes: value.bytes, growth: value.growth }),
  );

  await step(
    "the old way held the whole file in the memory of the process",
    async () => {
      // The old code read the whole file before it wrote it. The memory of the process grew by
      // the size of the file, so the export did not hold the backup in bounded memory.
      const before = process.memoryUsage().rss;
      const whole = await readFile(join(folder, "large.bin"));
      const growth = process.memoryUsage().rss - before;
      assert.equal(whole.byteLength, LARGE_BYTES, "The file holds another size.");
      assert.ok(
        growth > LARGE_BYTES / 2,
        `The memory of the process grew by ${growth} bytes only, so the bound of the export says nothing.`,
      );
      return { bytes: whole.byteLength, growth };
    },
    (value) => ({ bytes: value.bytes, growth: value.growth }),
  );

  report.passed = true;
} finally {
  await rm(root, { recursive: true, force: true });
  mkdirSync(OUT, { recursive: true });
  writeFileSync(join(OUT, "report.json"), `${JSON.stringify(report, null, 2)}\n`);
  process.stdout.write(`Report: ${join(OUT, "report.json")}\n`);
}
