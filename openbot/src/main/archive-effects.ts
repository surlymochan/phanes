import type { FileHandle } from "node:fs/promises";
import { Effect, Result, Schema } from "effect";

/** Keeps archive failures typed until the native caller boundary. */
export class ArchiveOperationError extends Schema.TaggedError<ArchiveOperationError>()("ArchiveOperationError", {
  cause: Schema.Defect(),
}) {}

export function archiveFailure(cause: unknown): ArchiveOperationError {
  return cause instanceof ArchiveOperationError ? cause : new ArchiveOperationError({ cause });
}
export function archiveCall<A>(operation: () => A | Promise<A>): Effect.Effect<A, ArchiveOperationError> {
  return Effect.tryPromise({ try: () => Promise.resolve(operation()), catch: archiveFailure });
}
export const archiveSync = <A>(operation: () => A) => Effect.try({ try: operation, catch: archiveFailure });

/** Lets a native catch keep loop control and inspect the original operational error. */
export function archiveResult<A>(result: Result.Result<A, ArchiveOperationError>): A {
  if (Result.isFailure(result)) throw result.failure.cause;
  return result.success;
}

/**
 * Writes the chunk to the file and returns the offset behind it.
 *
 * A write can be short: the file system can write fewer bytes than the request. The write asks
 * again for the rest of the chunk, at the offset behind the bytes that were written. A write that
 * writes no byte at all fails the archive, so a truncated archive never reaches the user as a
 * saved file.
 */
export async function writeArchiveChunk(handle: FileHandle, chunk: Uint8Array, position: number): Promise<number> {
  let written = 0;
  while (written < chunk.length) {
    const { bytesWritten } = await handle.write(chunk, written, chunk.length - written, position + written);
    if (bytesWritten <= 0) {
      throw new Error(`The archive wrote no byte at offset ${position + written}.`);
    }
    written += bytesWritten;
  }
  return position + chunk.length;
}
