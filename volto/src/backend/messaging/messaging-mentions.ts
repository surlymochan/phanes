import { Effect, Fiber, Option } from "effect";
import type { MessagingAdapterError } from "./messaging-types";

/**
 * The most names one prompt or tool call looks up. Each is one platform call on a cache miss, and the
 * prompt context has a short deadline.
 */
const NAME_LOOKUPS = 20;
/**
 * How long a message waits for the names. The lookups go on after this, so their names are in the
 * adapter's cache for the next message, but this one keeps the ids: a name lookup must not cost the
 * prompt its context, which has a deadline of its own.
 */
const NAME_DEADLINE_MS = 1_500;

/**
 * The display name of each user id, for `withMentionNames`. An id whose lookup fails, that is past
 * the lookup limit, or that is not named by the deadline keeps no name and so stays an id.
 */
export const mentionNames = Effect.fnUntraced(function* (
  ids: Iterable<string>,
  nameOf: (userId: string) => Effect.Effect<string, MessagingAdapterError>,
) {
  const distinct = [...new Set(ids)].slice(0, NAME_LOOKUPS);
  if (distinct.length === 0) return new Map<string, string>();
  // Detached: a lookup can take long, and the deadline must not wait for it.
  const lookups = yield* Effect.forkDetach(
    Effect.forEach(
      distinct,
      (id) =>
        nameOf(id).pipe(
          Effect.map((name) => [id, name.replace(/\s+/g, " ").trim()] as const),
          Effect.catch(() => Effect.succeed([id, ""] as const)),
        ),
      { concurrency: 4 },
    ),
  );
  const entries = yield* Fiber.join(lookups).pipe(Effect.timeoutOption(NAME_DEADLINE_MS));
  return new Map(Option.getOrElse(entries, () => []).filter(([id, name]) => name && name !== id));
});

/** Writes each `@<id>` that has a name as `@<name>`. */
export function withMentionNames(text: string, names: ReadonlyMap<string, string>): string {
  if (names.size === 0) return text;
  return text.replace(/@([A-Za-z0-9]+)/g, (mention, id: string) => {
    const name = names.get(id);
    return name ? `@${name}` : mention;
  });
}
