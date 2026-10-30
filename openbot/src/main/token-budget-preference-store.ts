import { DEFAULT_TOKEN_BUDGET, isTokenBudgetPreference, type TokenBudgetPreference } from "@openbot/contracts/ipc";
import { Effect, Result, Semaphore } from "effect";
import { isMissingFileError } from "../backend/file-errors";
import { type PreferenceFileFailure, readPreferenceFile, writePreferenceFile } from "./preference-file";

/**
 * The token limits of one agent: tokens per hour and tokens per turn, 0 for no limit. The backend
 * reads them on each usage report, so they are held in memory after `load` instead of read from
 * disk each time.
 *
 * A file that is missing, unreadable as JSON or holds an invalid limit reads as the default. Writes
 * are chained for the reason `busy-message-mode-preference-store.ts` chains its own.
 */
export class TokenBudgetPreferenceStore {
  readonly #path: string;
  #preference: TokenBudgetPreference = { ...DEFAULT_TOKEN_BUDGET };
  #writes = Semaphore.makeUnsafe(1);

  constructor(path: string) {
    this.#path = path;
  }

  load(): Effect.Effect<void, PreferenceFileFailure> {
    return Effect.gen({ self: this }, function* () {
      const loaded = yield* Effect.result(
        readPreferenceFile(this.#path, (parsed): TokenBudgetPreference | null =>
          typeof parsed === "object" &&
          parsed !== null &&
          "version" in parsed &&
          parsed.version === 1 &&
          isTokenBudgetPreference(parsed)
            ? { perHour: parsed.perHour, perTurn: parsed.perTurn }
            : null,
        ),
      );
      if (Result.isSuccess(loaded)) {
        if (loaded.success) this.#preference = loaded.success;
      } else if (!isMissingFileError(loaded.failure.cause) && !(loaded.failure.cause instanceof SyntaxError))
        return yield* loaded.failure;
    });
  }

  get(): TokenBudgetPreference {
    return { ...this.#preference };
  }

  readonly set = Effect.fn("TokenBudgetPreference.set")(function* (
    this: TokenBudgetPreferenceStore,
    { perHour, perTurn }: TokenBudgetPreference,
  ) {
    yield* this.#writes.withPermit(
      Effect.uninterruptible(
        writePreferenceFile(this.#path, { version: 1, perHour, perTurn }).pipe(
          Effect.tap(() =>
            Effect.sync(() => {
              this.#preference = { perHour, perTurn };
            }),
          ),
        ),
      ),
    );
    return this.get();
  });
}
