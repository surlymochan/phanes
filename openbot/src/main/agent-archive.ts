// Reading and writing `.zip` archives that carry agents: the Grok Bot agent import and the
// OpenBot instance transfer share these checks, so both refuse the same unsafe entries.

import { createHash } from "node:crypto";
import { mkdir, readFile, stat, writeFile } from "node:fs/promises";
import { dirname, extname, isAbsolute, resolve } from "node:path";
import { AVATAR_MIME_TYPES, isValidAvatarImage } from "@openbot/contracts/avatar-images";
import { AGENT_IMPORT_LIMITS, type AvatarImageInput } from "@openbot/contracts/ipc";
import { sourceText } from "@openbot/i18n/source";
import { Effect, Schema } from "effect";
import { strFromU8, unzipSync } from "fflate";
import { isPathInside } from "../backend/path-containment";
import { type ArchiveOperationError, archiveCall, archiveFailure, archiveResult } from "./archive-effects";
import { isUnsafeArchivePath } from "./skill-package";

/** The most an archive may hold, packed and expanded. */
export interface ArchiveLimits {
  archiveBytes: number;
  files: number;
  /** The error texts, which name the limits. */
  tooLarge: string;
  expandedTooLarge: string;
  invalid: string;
}

export interface ArchiveEntry {
  size: number;
}

export const readArchiveEffect = Effect.fn("Archive.readArchive")(function* (
  path: string,
  limits: ArchiveLimits,
): Effect.fn.Return<Uint8Array, ArchiveOperationError> {
  const info = yield* archiveCall(() => stat(path));
  if (!info.isFile()) return yield* archiveFailure(new Error(sourceText("error.import.chooseZip")));
  if (info.size === 0 || info.size > limits.archiveBytes) return yield* archiveFailure(new Error(limits.tooLarge));
  return new Uint8Array(yield* archiveCall(() => readFile(path)));
});

/** Every file entry and its expanded size, checked, without inflating anything. */
export function listEntries(bytes: Uint8Array, limits: ArchiveLimits): Map<string, ArchiveEntry> {
  const entries = new Map<string, ArchiveEntry>();
  let expanded = 0;
  try {
    unzipSync(bytes, {
      filter: (file) => {
        const name = file.name.replaceAll("\\", "/");
        if (name.endsWith("/")) return false;
        if (isUnsafeEntry(name)) throw new UnsafeEntry(name);
        expanded += file.originalSize;
        if (entries.size >= limits.files || expanded > limits.archiveBytes) throw new UnsafeEntry(null);
        entries.set(name, { size: file.originalSize });
        return false;
      },
    });
  } catch (error) {
    if (error instanceof UnsafeEntry)
      throw new Error(
        error.entry ? sourceText("error.import.unsafeFile", { name: error.entry }) : limits.expandedTooLarge,
      );
    // A zip that Grok Bot is still writing has no central directory yet, so it reads as invalid.
    throw new Error(limits.invalid);
  }
  if (!entries.size) throw new Error(sourceText("error.import.empty"));
  return entries;
}

/** Inflates the file entries `include` names. A directory entry is never inflated. */
export function extract(bytes: Uint8Array, include: (name: string) => boolean): Record<string, Uint8Array> {
  const raw = unzipSync(bytes, {
    filter: (file) => {
      const name = file.name.replaceAll("\\", "/");
      return !name.endsWith("/") && include(name);
    },
  });
  return Object.fromEntries(Object.entries(raw).map(([name, data]) => [name.replaceAll("\\", "/"), data]));
}

/**
 * The file entries whose Unix mode, from an OpenBot export or a Unix zip tool, is executable. `unzipSync`
 * does not give the attributes, so this reads the central directory as `unzipSync` does. It returns no
 * names for a ZIP64 archive; an archive below the import limits does not need ZIP64.
 */
export function executableEntries(bytes: Uint8Array): Set<string> {
  const names = new Set<string>();
  const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
  let end = bytes.length - 22;
  while (end >= 0 && view.getUint32(end, true) !== 0x06054b50) end -= 1;
  const count = end < 0 ? 0 : view.getUint16(end + 8, true);
  let offset = end < 0 ? 0 : view.getUint32(end + 16, true);
  if (count === 0xffff || offset === 0xffffffff) return names;
  for (let index = 0; index < count; index += 1) {
    const nameLength = view.getUint16(offset + 28, true);
    // The high byte of "version made by" is the system: 3 is Unix and 19 is macOS.
    const system = view.getUint8(offset + 5);
    const mode = view.getUint32(offset + 38, true) >>> 16;
    if ((system === 3 || system === 19) && (mode & 0o111) !== 0) {
      const utf8 = (view.getUint16(offset + 8, true) & 0x800) !== 0;
      const name = strFromU8(bytes.subarray(offset + 46, offset + 46 + nameLength), !utf8).replaceAll("\\", "/");
      if (!name.endsWith("/")) names.add(name);
    }
    offset += 46 + nameLength + view.getUint16(offset + 30, true) + view.getUint16(offset + 32, true);
  }
  return names;
}

/** A segment such as `D:` names another Windows drive, so `resolve` would leave the target folder. */
export function isUnsafeEntry(name: string): boolean {
  return isUnsafeArchivePath(name) || name.split("/").some((part) => /^[a-z]:/iu.test(part));
}

class UnsafeEntry extends Schema.TaggedError<UnsafeEntry>()("UnsafeEntry", {
  entry: Schema.NullOr(Schema.String),
  message: Schema.String,
}) {
  constructor(entry: string | null) {
    super({ entry, message: "Unsafe archive entry." });
  }
}

export const writeTreeEffect = Effect.fn("Archive.writeTree")(function* (
  root: string,
  files: ReadonlyArray<readonly [string, Uint8Array]>,
): Effect.fn.Return<void, ArchiveOperationError> {
  const base = resolve(root);
  for (const [name, data] of files) {
    const target = resolve(base, name);
    if (isAbsolute(name) || isUnsafeEntry(name) || target === base || !isPathInside(base, target))
      return yield* archiveFailure(new Error(sourceText("error.import.unsafeFile", { name })));
    yield* archiveCall(() => mkdir(dirname(target), { recursive: true })).pipe(Effect.uninterruptible);
    // `wx` refuses to replace a file, so an entry can never overwrite what is already there.
    yield* archiveCall(() => writeFile(target, data, { flag: "wx" })).pipe(Effect.uninterruptible);
  }
});

/**
 * Writes the workspace files of one agent and never replaces a file. When a file is already there,
 * for example `README.md` and `readme.md` on a disk that ignores case, the entry is saved beside it
 * as `README (2).md`. A file that still cannot be written is skipped. Each case adds a warning.
 * A file that `executable` names gets the execute bits that the umask allows, and never setuid or setgid.
 */
export const writeImportedFilesEffect = Effect.fn("Archive.writeImportedFiles")(function* (
  root: string,
  files: ReadonlyArray<readonly [string, Uint8Array]>,
  agentName: string,
  warnings: string[],
  executable: (name: string) => boolean = () => false,
): Effect.fn.Return<void, ArchiveOperationError> {
  const base = resolve(root);
  for (const [name, data] of files) {
    const mode = executable(name) ? 0o777 : 0o666;
    const target = resolve(base, name);
    if (isAbsolute(name) || isUnsafeEntry(name) || target === base || !isPathInside(base, target))
      return yield* archiveFailure(new Error(sourceText("error.import.unsafeFile", { name })));
    try {
      archiveResult(
        yield* Effect.result(
          archiveCall(() => mkdir(dirname(target), { recursive: true })).pipe(Effect.uninterruptible),
        ),
      );
      for (let copy = 1; ; copy += 1) {
        try {
          archiveResult(
            yield* Effect.result(
              archiveCall(() =>
                writeFile(copy === 1 ? target : copyName(target, copy), data, { flag: "wx", mode }),
              ).pipe(Effect.uninterruptible),
            ),
          );
          if (copy > 1)
            warnings.push(
              sourceText("error.import.fileRenamed", { name: agentName, file: name, saved: copyName(name, copy) }),
            );
          break;
        } catch (error) {
          if (!(error instanceof Error && "code" in error && error.code === "EEXIST")) throw error;
        }
      }
    } catch (error) {
      warnings.push(sourceText("error.import.fileSkipped", { name: agentName, file: name, reason: errorText(error) }));
    }
  }
});

/** `notes/plan.md` becomes `notes/plan (2).md`. */
function copyName(path: string, copy: number): string {
  const extension = extname(path);
  return `${path.slice(0, path.length - extension.length)} (${copy})${extension}`;
}

export function sha256(bytes: Uint8Array): string {
  return createHash("sha256").update(bytes).digest("hex");
}

export function avatarImage(bytes: Uint8Array | undefined): AvatarImageInput | null {
  if (!bytes) return null;
  const mimeType = AVATAR_MIME_TYPES.find((type) => isValidAvatarImage(type, bytes));
  return mimeType ? { mimeType, bytes } : null;
}

function errorText(error: unknown): string {
  const text = error instanceof Error ? error.message : sourceText("error.import.failed");
  return text.slice(0, AGENT_IMPORT_LIMITS.message);
}
