import { readdir, readFile, rm } from "node:fs/promises";
import { join } from "node:path";
import { sourceText } from "@openbot/i18n/source";
import { registerSecretValue } from "@openbot/logging";
import { Effect, Schema } from "effect";
import { writeFileAtomically } from "./atomic-json-file";
import { isMissingFileError } from "./file-errors";

/** An environment variable name, which is also the file name of the key. */
export const API_KEY_NAME_PATTERN = /^[A-Z][A-Z0-9_]{0,63}$/;
// Agent ids are `agent-<uuid>` or an older short form; anything else must not name a folder.
const AGENT_ID_PATTERN = /^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$/;

export interface SavedApiKey {
  name: string;
  path: string;
}

/** The cause stays private: it can hold a path. The message is what the user and the agent read. */
export class AgentApiKeyFailed extends Schema.TaggedError<AgentApiKeyFailed>()("AgentApiKeyFailed", {
  cause: Schema.Defect(),
}) {}

const failed = () => new AgentApiKeyFailed({ cause: new Error(sourceText("error.backend.apiKeySaveFailed")) });

/**
 * The API keys that the user entered for an agent through `request_api_key`. Each key is one file,
 * `<root>/<agentId>/<NAME>`, readable only by the user, outside every workspace. The agent gets the
 * path and reads the file in its own commands, so the value never travels in a tool argument or a
 * tool result. Each value is registered for redaction when it is saved and when OpenBot starts.
 */
export class AgentApiKeys {
  readonly #root: string;

  constructor(root: string) {
    this.#root = root;
  }

  readonly save = Effect.fn("AgentApiKeys.save")(function* (
    this: AgentApiKeys,
    agentId: string,
    name: string,
    value: string,
  ) {
    const path = yield* this.#keyPath(agentId, name);
    registerSecretValue(value);
    yield* writeFileAtomically(path, value, { createDirectory: true }).pipe(Effect.mapError(failed));
    return { name, path } satisfies SavedApiKey;
  });

  readonly list = Effect.fn("AgentApiKeys.list")(function* (this: AgentApiKeys, agentId: string) {
    const folder = yield* this.#agentFolder(agentId);
    const names = yield* Effect.tryPromise({ try: () => folderNames(folder), catch: failed });
    return names
      .filter((name) => API_KEY_NAME_PATTERN.test(name))
      .sort()
      .map((name): SavedApiKey => ({ name, path: join(folder, name) }));
  });

  /** False when the agent has no key with that name. */
  readonly remove = Effect.fn("AgentApiKeys.remove")(function* (this: AgentApiKeys, agentId: string, name: string) {
    const path = yield* this.#keyPath(agentId, name);
    return yield* Effect.tryPromise({
      try: () =>
        rm(path).then(
          () => true,
          (error: unknown) => (isMissingFileError(error) ? false : Promise.reject(error)),
        ),
      catch: failed,
    });
  });

  /** Removes every key of a deleted agent. */
  readonly removeAgent = Effect.fn("AgentApiKeys.removeAgent")(function* (this: AgentApiKeys, agentId: string) {
    const folder = yield* this.#agentFolder(agentId);
    yield* Effect.tryPromise({ try: () => rm(folder, { recursive: true, force: true }), catch: failed });
  });

  /** Registers every saved value for redaction. A file that cannot be read is skipped. */
  readonly registerSaved = Effect.fn("AgentApiKeys.registerSaved")(function* (this: AgentApiKeys) {
    const agents = yield* Effect.promise(() => folderNames(this.#root).catch((): string[] => []));
    for (const agentId of agents.filter((id) => AGENT_ID_PATTERN.test(id))) {
      const keys = yield* this.list(agentId).pipe(Effect.catch(() => Effect.succeed<SavedApiKey[]>([])));
      for (const key of keys) {
        const value = yield* Effect.promise(() => readFile(key.path, "utf8").catch(() => ""));
        registerSecretValue(value);
      }
    }
  });

  #agentFolder(agentId: string): Effect.Effect<string, AgentApiKeyFailed> {
    return AGENT_ID_PATTERN.test(agentId) ? Effect.succeed(join(this.#root, agentId)) : Effect.fail(failed());
  }

  #keyPath(agentId: string, name: string): Effect.Effect<string, AgentApiKeyFailed> {
    if (!API_KEY_NAME_PATTERN.test(name)) return Effect.fail(failed());
    return this.#agentFolder(agentId).pipe(Effect.map((folder) => join(folder, name)));
  }
}

/** The names in a folder; none when the folder is missing. */
async function folderNames(folder: string): Promise<string[]> {
  try {
    return await readdir(folder);
  } catch (error) {
    if (isMissingFileError(error)) return [];
    throw error;
  }
}
