import { readFileSync } from "node:fs";
import { createOpenBotLogger } from "@openbot/logging";
import { Effect, Schema, Semaphore } from "effect";
import { writeJsonFileAtomically } from "../atomic-json-file";

const logger = createOpenBotLogger("messaging-status-posts");

const Target = Schema.Struct({ platformChannelId: Schema.String, replyThreadId: Schema.NullOr(Schema.String) });

/**
 * The last post of a turn that did not reach the platform. It is sent when the connection is back.
 * The text is the one that would have gone out, so a secret is already taken out of an answer.
 */
const PendingFinal = Schema.Struct({
  answer: Schema.Boolean,
  text: Schema.String,
  /** The reaction that shows on the person's message, or null for a follow-up turn. */
  reaction: Schema.NullOr(Schema.Literals(["done", "stopped", "failed"])),
});
export type PendingFinal = typeof PendingFinal.Type;

const StatusPost = Schema.Struct({
  connectionId: Schema.String,
  target: Target,
  messageId: Schema.NullOr(Schema.String),
  stopToken: Schema.NullOr(Schema.String),
  final: Schema.optional(PendingFinal),
});
export type StatusPost = typeof StatusPost.Type;

const StopButton = Schema.Struct({ linkId: Schema.String, authorId: Schema.String, connectionId: Schema.String });
export type StopButton = typeof StopButton.Type;

const StatusPostsFile = Schema.Struct({
  version: Schema.Literal(1),
  posts: Schema.Record(Schema.String, StatusPost),
  stops: Schema.Record(Schema.String, StopButton),
});
const decodeStatusPostsFile = Schema.decodeUnknownSync(StatusPostsFile);

/**
 * Owns the status posts that wait for the end of a turn, by `linkId:platformMessageId`, and the Stop
 * buttons in them, by token. They are kept in a file, so that after a restart OpenBot still edits the
 * "Waiting" and "Working" posts of the requests that ended while it was closed, and a Stop button
 * still works. With no path, they are kept in memory only.
 */
export class MessagingStatusPosts {
  readonly posts = new Map<string, StatusPost>();
  readonly stops = new Map<string, StopButton>();
  /**
   * The posts that came from the file and that this run has not changed. Only for these is a request
   * that is not pending a request that ended while OpenBot was closed.
   */
  readonly restored = new Set<string>();
  readonly #path: string | null;
  readonly #writes = Semaphore.makeUnsafe(1);

  constructor(path: string | null) {
    this.#path = path;
  }

  /**
   * Reads the file at once, with no wait. A missing or unreadable file starts empty: the posts it
   * named stay as they are on the platform. Posts of a connection that is gone are left out.
   */
  load(connectionIds: ReadonlySet<string>): void {
    const path = this.#path;
    if (!path) return;
    let file: typeof StatusPostsFile.Type;
    try {
      file = decodeStatusPostsFile(JSON.parse(readFileSync(path, "utf8")));
    } catch (error) {
      if (!(error instanceof Error && "code" in error && error.code === "ENOENT"))
        logger.warn("The status posts of messaging conversations could not be read.");
      return;
    }
    for (const [key, post] of Object.entries(file.posts)) {
      if (!connectionIds.has(post.connectionId)) continue;
      this.posts.set(key, post);
      this.restored.add(key);
    }
    for (const [token, stop] of Object.entries(file.stops))
      if (connectionIds.has(stop.connectionId)) this.stops.set(token, stop);
  }

  /** Writes the current posts. A failed write is logged: the posts in memory still work. */
  readonly save = Effect.fn("MessagingStatusPosts.save")(function* (this: MessagingStatusPosts) {
    const path = this.#path;
    if (!path) return;
    yield* this.#writes.withPermit(
      Effect.suspend(() =>
        writeJsonFileAtomically(
          path,
          { version: 1, posts: Object.fromEntries(this.posts), stops: Object.fromEntries(this.stops) },
          { createDirectory: true },
        ),
      ).pipe(
        Effect.catch((failure) =>
          Effect.sync(() =>
            logger.warn("The status posts of messaging conversations could not be saved.", {
              operation: failure.operation,
            }),
          ),
        ),
      ),
    );
  });
}
