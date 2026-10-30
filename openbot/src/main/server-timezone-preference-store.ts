import type { HostTimezoneSettings } from "@openbot/contracts/ipc";
import { isDynamicRecord, isString } from "@openbot/contracts/runtime-values";
import { sourceText } from "@openbot/i18n/source";
import { Effect, Result, Semaphore } from "effect";
import { localTimezone } from "../backend/agent/routine-tools";
import { isMissingFileError } from "../backend/file-errors";
import { validTimezone } from "./agent-marketplace-service";
import { type PreferenceFileFailure, readPreferenceFile, writePreferenceFile } from "./preference-file";

/**
 * The server timezone. A routine that an agent creates with no zone from the tool call or the
 * sender's message runs on it, so a server on a UTC computer can keep its members' clock across a
 * restart. `null` is automatic: the system zone of this computer, which every such routine used
 * before the setting existed.
 *
 * A file that is missing, unreadable as JSON, or names a zone this runtime cannot resolve reads as
 * automatic. Writes are chained for the reason `update-preference-store.ts` chains its own.
 */
export class ServerTimezonePreferenceStore {
  readonly #path: string;
  #timezone: string | null = null;
  #writes = Semaphore.makeUnsafe(1);

  constructor(path: string) {
    this.#path = path;
  }

  load(): Effect.Effect<void, PreferenceFileFailure> {
    return Effect.gen({ self: this }, function* () {
      const loaded = yield* Effect.result(
        readPreferenceFile(this.#path, (parsed): string | null =>
          isDynamicRecord(parsed) && parsed.version === 1 && isString(parsed.timezone) && validTimezone(parsed.timezone)
            ? parsed.timezone
            : null,
        ),
      );
      if (Result.isSuccess(loaded)) this.#timezone = loaded.success;
      else if (!isMissingFileError(loaded.failure.cause) && !(loaded.failure.cause instanceof SyntaxError))
        return yield* loaded.failure;
    });
  }

  get(): HostTimezoneSettings {
    return { timezone: this.#timezone, systemTimezone: localTimezone() };
  }

  /** The zone that applies now: the chosen one, or the system zone. */
  effective(): string {
    return this.#timezone ?? localTimezone();
  }

  /** The caller reads the zone with `parseServerTimezone` first. */
  readonly set = Effect.fn("ServerTimezonePreference.set")((timezone: string | null) =>
    this.#writes.withPermit(
      Effect.uninterruptible(
        writePreferenceFile(this.#path, { version: 1, timezone }).pipe(
          Effect.tap(() =>
            Effect.sync(() => {
              this.#timezone = timezone;
            }),
          ),
          Effect.map(() => this.get()),
        ),
      ),
    ),
  );
}

/** A zone this runtime resolves, or `null` for automatic. */
export function parseServerTimezone(value: unknown): string | null {
  if (value === null) return null;
  if (!isString(value) || value.length > 128 || !validTimezone(value))
    throw new Error(sourceText("error.host.timezoneInvalid"));
  return value;
}
