// Creating the ZIP of a backup. macOS and Windows keep the tool of their platform: `ditto` on
// macOS and `Compress-Archive` on Windows. Linux ships neither tool, so it writes the ZIP here.

import { execFile } from "node:child_process";
import { open, readdir, stat } from "node:fs/promises";
import { basename, join } from "node:path";
import { promisify } from "node:util";
import { Effect } from "effect";
import { Zip, ZipPassThrough } from "fflate";
import { type ArchiveOperationError, archiveCall, writeArchiveChunk } from "./archive-effects";

const execFileAsync = promisify(execFile);

/** The size of the chunk that the archive reads from a file and writes to the ZIP. */
const CHUNK_SIZE = 64 * 1024;

/**
 * Writes the ZIP of the backup folder.
 *
 * The platform decides the tool. macOS keeps `ditto` and Windows keeps `Compress-Archive`. Linux
 * has neither, and the export used `ditto` on every platform that was not Windows, so the export
 * failed on Linux. Linux writes the ZIP in the process instead.
 */
export const createBackupArchive = Effect.fn("Archive.createBackupArchive")(function* (
  platform: NodeJS.Platform,
  exportRoot: string,
  destination: string,
): Effect.fn.Return<void, ArchiveOperationError> {
  if (platform === "win32") {
    yield* archiveCall(() =>
      execFileAsync("powershell.exe", [
        "-NoProfile",
        "-NonInteractive",
        "-Command",
        `Compress-Archive -LiteralPath '${powerShellLiteral(exportRoot)}' -DestinationPath '${powerShellLiteral(destination)}' -Force`,
      ]),
    ).pipe(Effect.uninterruptible);
    return;
  }
  if (platform === "darwin") {
    yield* archiveCall(() =>
      execFileAsync("/usr/bin/ditto", ["-c", "-k", "--keepParent", exportRoot, destination]),
    ).pipe(Effect.uninterruptible);
    return;
  }
  yield* archiveCall(() => writeArchive(exportRoot, destination)).pipe(Effect.uninterruptible);
});

/**
 * Writes the ZIP of the export folder.
 *
 * The archive holds the folder itself, as `ditto --keepParent` and `Compress-Archive` do, so a
 * reader finds the manifest below `OpenBot Backup`. Every entry keeps the mode of the file it
 * holds, and the archive itself holds the mode of the user alone, as the manifest and the
 * diagnostics report do.
 *
 * The files go into the archive one at a time, in chunks of a fixed size and without compression,
 * so the export holds one chunk of a file in the memory of the main process, not the whole file.
 */
async function writeArchive(exportRoot: string, destination: string): Promise<void> {
  const handle = await open(destination, "w", 0o600);
  try {
    let pending = Promise.resolve();
    let failure: unknown = null;
    let position = 0;
    const zip = new Zip((error, chunk) => {
      if (error) failure = error;
      else
        pending = pending.then(() =>
          writeArchiveChunk(handle, chunk, position).then(
            (end) => {
              position = end;
            },
            (cause: unknown) => {
              failure = cause;
            },
          ),
        );
    });
    const folder = basename(exportRoot);
    for (const file of await files(exportRoot)) {
      const entry = new ZipPassThrough(`${folder}/${file.name}`);
      // 3 is Unix, and the mode of a regular file keeps its type bits and its permission bits.
      entry.os = 3;
      entry.attrs = (0o100000 | (file.mode & 0o777)) << 16;
      zip.add(entry);
      const source = await open(file.path, "r");
      try {
        const buffer = new Uint8Array(CHUNK_SIZE);
        for (;;) {
          const { bytesRead } = await source.read(buffer, 0, CHUNK_SIZE, null);
          if (bytesRead === 0) break;
          // The chunk stays in the memory of the process until the write of it is done, so the
          // export waits for that write before it reads the next chunk.
          entry.push(buffer.subarray(0, bytesRead));
          await pending;
          if (failure) throw failure;
        }
        entry.push(new Uint8Array(0), true);
      } finally {
        await source.close();
      }
      await pending;
      if (failure) throw failure;
    }
    zip.end();
    await pending;
    if (failure) throw failure;
  } finally {
    await handle.close();
  }
}

interface ArchiveFile {
  /** The name of the file below the export folder. */
  name: string;
  /** The path of the file on the disk. */
  path: string;
  /** The mode of the file on the disk. */
  mode: number;
}

/** Every file of the export folder, with its name below the folder and its mode. */
async function files(root: string): Promise<ArchiveFile[]> {
  const found: ArchiveFile[] = [];
  async function visit(directory: string, prefix: string): Promise<void> {
    for (const entry of await readdir(directory, { withFileTypes: true })) {
      const path = join(directory, entry.name);
      if (entry.isDirectory()) await visit(path, `${prefix}${entry.name}/`);
      else if (entry.isFile()) {
        const info = await stat(path);
        found.push({ name: `${prefix}${entry.name}`, path, mode: info.mode });
      }
    }
  }
  await visit(root, "");
  return found;
}

function powerShellLiteral(value: string): string {
  return value.replaceAll("'", "''");
}
