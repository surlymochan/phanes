import { randomUUID } from "node:crypto";
import { readFile, rm } from "node:fs/promises";
import { homedir } from "node:os";
import { join, resolve } from "node:path";
import type { OpenswarmSettings } from "@openbot/contracts/ipc";
import { sourceText } from "@openbot/i18n/source";
import { redactText } from "@openbot/logging";
import { Effect, Exit, Schema, Scope, Semaphore } from "effect";
import { writeFileAtomically, writeJsonFileAtomically } from "../atomic-json-file";
import { isMissingFileError } from "../file-errors";
import {
  assertDispatchInputValues,
  type DispatchCommandOutput,
  DispatchFailed,
  type DispatchFileTransport,
  dispatchCommand,
  dispatchFailure,
  removeRemoteDispatchCard,
  writeRemoteDispatchCard,
} from "./dispatch-process";
import type { DispatchCaller, DispatchWork } from "./dispatch-tools";

const identifier = Schema.String.check(Schema.isPattern(/^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/));
const terminalStatus = Schema.Literals([
  "done",
  "partial",
  "failed",
  "budget_exceeded",
  "stopped",
  "unroutable",
  "dry-run",
]);
const verifySchema = Schema.Struct({
  rc: Schema.optional(Schema.Number),
  cmd: Schema.optional(Schema.String),
  tail: Schema.optional(Schema.String),
  seconds: Schema.optional(Schema.Number),
  failing: Schema.optional(Schema.Array(Schema.String)),
});
const resultSchema = Schema.Struct({
  status: terminalStatus,
  branch: Schema.optional(Schema.NullOr(Schema.String)),
  commit: Schema.optional(Schema.NullOr(Schema.String)),
  result: Schema.optional(Schema.NullOr(Schema.Struct({ summary: Schema.optional(Schema.String) }))),
  verify: Schema.optional(Schema.NullOr(verifySchema)),
});
const recordSchema = Schema.Struct({
  runId: identifier,
  host: identifier,
  agentId: Schema.String,
  threadId: Schema.String,
  title: Schema.String,
  binary: Schema.String,
  idempotencyKey: Schema.String,
  status: Schema.Union([terminalStatus, Schema.Literals(["running", "cancelling"])]),
  branch: Schema.NullOr(Schema.String),
  commit: Schema.NullOr(Schema.String),
  startedAt: Schema.String,
  summary: Schema.optional(Schema.String),
  verify: Schema.optional(Schema.NullOr(verifySchema)),
  delivered: Schema.Boolean,
});
export type DispatchRecord = typeof recordSchema.Type;
const stateSchema = Schema.Struct({ version: Schema.Literal(1), runs: Schema.Array(recordSchema) });
const infoSchema = Schema.Struct({
  id: identifier,
  state: Schema.optional(Schema.String),
  commit: Schema.optional(Schema.NullOr(Schema.String)),
  result: Schema.NullOr(resultSchema),
});
const acknowledgement = Schema.Struct({ id: identifier });
const fleetSchema = Schema.Struct({
  hosts: Schema.Array(
    Schema.Struct({
      name: identifier,
      kind: Schema.optional(Schema.Literals(["local", "ssh", "instabox"])),
      ssh: Schema.optional(Schema.String),
      box: Schema.optional(Schema.String),
      repos: Schema.optional(Schema.Record(Schema.String, Schema.String)),
    }),
  ),
});
type DispatchFleet = typeof fleetSchema.Type;

const io = <A>(body: () => Promise<A>) =>
  Effect.tryPromise({ try: body, catch: (cause) => new DispatchFailed({ cause }) });
const step = <A>(body: () => A) => Effect.try({ try: body, catch: (cause) => new DispatchFailed({ cause }) });
const runKey = (host: string, runId: string) => JSON.stringify([host, runId]);
const commandJson = <S extends Schema.ConstraintDecoder<unknown>>(schema: S, output: string): S["Type"] => {
  try {
    return Schema.decodeUnknownSync(schema)(JSON.parse(output));
  } catch {
    // The output can include login-shell text before the JSON value.
  }

  const candidates: Array<{ start: number; end: number; value: unknown }> = [];
  const stack: Array<{ opening: string; start: number }> = [];
  let inString = false;
  let escaped = false;
  for (let index = 0; index < output.length; index += 1) {
    const character = output[index];
    if (character === undefined) continue;
    if (stack.length === 0) {
      if (character === "{" || character === "[") {
        stack.push({ opening: character, start: index });
      }
      continue;
    }
    if (inString) {
      if (escaped) escaped = false;
      else if (character === "\\") escaped = true;
      else if (character === '"') inString = false;
      else if (character === "\n" || character === "\r") {
        // A raw line break cannot occur inside valid JSON strings.
        stack.length = 0;
        inString = false;
        escaped = false;
      }
      continue;
    }
    if (character === '"') inString = true;
    else if (character === "{" || character === "[") stack.push({ opening: character, start: index });
    else if (character === "}" || character === "]") {
      const expected = character === "}" ? "{" : "[";
      const frameIndex = stack.findLastIndex((frame) => frame.opening === expected);
      if (frameIndex < 0) {
        stack.length = 0;
        continue;
      }
      const frame = stack[frameIndex];
      if (!frame) continue;
      stack.length = frameIndex;
      try {
        candidates.push({ start: frame.start, end: index, value: JSON.parse(output.slice(frame.start, index + 1)) });
      } catch {
        // A balanced shell fragment is not always JSON.
      }
    }
  }

  const topLevelCandidates: typeof candidates = [];
  const containingEnds: number[] = [];
  candidates.sort((left, right) => left.start - right.start || right.end - left.end);
  for (const candidate of candidates) {
    while (containingEnds.length > 0 && (containingEnds.at(-1) ?? -1) < candidate.start) containingEnds.pop();
    if (containingEnds.length === 0) topLevelCandidates.push(candidate);
    containingEnds.push(candidate.end);
  }

  for (const candidate of topLevelCandidates.reverse()) {
    try {
      return Schema.decodeUnknownSync(schema)(candidate.value);
    } catch {
      // Try the next top-level value before the line-by-line fallback.
    }
  }

  for (const line of output.split(/\r?\n/u).reverse()) {
    try {
      return Schema.decodeUnknownSync(schema)(JSON.parse(line));
    } catch {
      // Login shell profiles can print text before openswarm's final JSON line.
    }
  }
  throw new Error("openswarm returned invalid JSON");
};
const parseCommandJson = <S extends Schema.ConstraintDecoder<unknown>>(schema: S, command: DispatchCommandOutput) =>
  Effect.try({
    try: () => commandJson(schema, command.output),
    catch: (cause) =>
      dispatchFailure({
        cause,
        reason: sourceText("error.backend.dispatchOutputUnreadable"),
        step: "parse",
        exitCode: command.exitCode,
      }),
  });
const isTerminal = (record: DispatchRecord) => record.status !== "running" && record.status !== "cancelling";
const expandHome = (path: string) => (path.startsWith("~/") ? join(homedir(), path.slice(2)) : path);

/** Redacts executor-controlled text again before it reaches the local dispatch record. */
export function redactDispatchRecord(record: DispatchRecord, redact: (text: string) => string): DispatchRecord {
  const safeText = (text: string) => redact(redactText(text));
  return {
    ...record,
    title: safeText(record.title),
    branch: record.branch === null ? null : safeText(record.branch),
    commit: record.commit === null ? null : safeText(record.commit),
    ...(record.summary === undefined ? {} : { summary: safeText(record.summary) }),
    ...(record.verify === undefined
      ? {}
      : {
          verify:
            record.verify === null
              ? null
              : {
                  ...record.verify,
                  ...(record.verify.cmd === undefined ? {} : { cmd: safeText(record.verify.cmd) }),
                  ...(record.verify.tail === undefined ? {} : { tail: safeText(record.verify.tail) }),
                  ...(record.verify.failing === undefined ? {} : { failing: record.verify.failing.map(safeText) }),
                },
        }),
  };
}

export interface DispatchServiceOptions {
  dataPath: string;
  settings(): OpenswarmSettings;
  redact(text: string): string;
  /** Persists the status message and mailbox delivery before it returns. */
  publish(record: DispatchRecord, terminal: boolean): Effect.Effect<void, DispatchFailed>;
  failed(agentId: string): void;
}

/** Owns durable run records and the watcher scope. Shutdown leaves executor work intact. */
export class DispatchService {
  readonly #options: DispatchServiceOptions;
  readonly #writes = Semaphore.makeUnsafe(1);
  readonly #watching = new Set<string>();
  #runs: DispatchRecord[] = [];
  #scope = Scope.makeUnsafe();
  #started = false;

  constructor(options: DispatchServiceOptions) {
    this.#options = options;
  }

  readonly start = Effect.fn("DispatchService.start")(function* (this: DispatchService) {
    if (this.#started) return;
    const contents = yield* io(() => readFile(join(this.#options.dataPath, "openbot-dispatch-v1.json"), "utf8")).pipe(
      Effect.catch((failure) => (isMissingFileError(failure.cause) ? Effect.succeed(null) : Effect.fail(failure))),
    );
    if (contents !== null) {
      const runs = [...(yield* step(() => Schema.decodeUnknownSync(stateSchema)(JSON.parse(contents)))).runs];
      const safeRuns = runs.map((record) => redactDispatchRecord(record, this.#options.redact));
      if (JSON.stringify(runs) !== JSON.stringify(safeRuns)) yield* this.#save(safeRuns);
      else this.#runs = safeRuns;
    }
    this.#started = true;
    for (const record of this.#runs) if (!record.delivered) yield* this.#watch(record.host, record.runId);
  });

  readonly dispose = Effect.fn("DispatchService.dispose")(function* (this: DispatchService) {
    this.#started = false;
    yield* Scope.close(this.#scope, Exit.void);
    this.#scope = Scope.makeUnsafe();
    this.#watching.clear();
  }).bind(this);

  readonly dispatch = Effect.fn("DispatchService.dispatch")(function* (
    this: DispatchService,
    input: DispatchWork,
    caller: DispatchCaller,
  ) {
    return yield* this.#writes.withPermit(
      Effect.gen({ self: this }, function* () {
        if (!this.#started || caller.inExecutionThread)
          return yield* new DispatchFailed({ cause: new Error(sourceText("error.backend.dispatchOwnChat")) });
        if (caller.workspaceOnly && input.verify) {
          return yield* new DispatchFailed({
            cause: new Error(sourceText("error.backend.dispatchVerifyWorkspace")),
            reason: sourceText("error.backend.dispatchVerifyWorkspace"),
          });
        }
        const existing = this.#runs.find(
          (run) =>
            run.idempotencyKey === caller.idempotencyKey &&
            run.agentId === caller.agentId &&
            run.threadId === caller.threadId,
        );
        if (existing) return { runId: existing.runId, host: existing.host };
        const settings = this.#options.settings();
        const binary = expandHome(settings.binaryPath || "openswarm");
        const host = input.host ?? (settings.defaultHost || "local");
        // A Workspace-only agent may not choose an executor that has no write confinement.
        if (caller.workspaceOnly && (input.lane || (input.backend && input.backend !== "codex"))) {
          return yield* new DispatchFailed({
            cause: new Error(sourceText("error.backend.dispatchExecutorWorkspace")),
            reason: sourceText("error.backend.dispatchExecutorWorkspace"),
          });
        }
        const backend = caller.workspaceOnly ? "codex" : input.backend;
        const lane = input.lane ?? (backend || input.model ? "" : settings.defaultLane);
        yield* Effect.try({
          try: () =>
            assertDispatchInputValues(
              [
                { name: "--title", value: input.title },
                { name: "--card", value: input.card },
                ...(input.lane === undefined ? [] : [{ name: "--lane", value: input.lane }]),
                ...(input.host === undefined ? [] : [{ name: "--host", value: input.host }]),
                ...(input.backend === undefined ? [] : [{ name: "--backend", value: input.backend }]),
                ...(input.model === undefined ? [] : [{ name: "--model", value: input.model }]),
                ...(input.verify === undefined ? [] : [{ name: "--verify", value: input.verify }]),
              ],
              binary,
            ),
          catch: (cause) =>
            dispatchFailure({
              cause,
              reason: cause instanceof Error ? cause.message : sourceText("error.backend.dispatchSpawnFailed"),
              step: "spawn",
            }),
        });
        const card = this.#options.redact(input.card);
        const title = this.#options.redact(input.title);
        const fleet = host === "local" ? undefined : yield* this.#fleet();
        const executorHost = fleet?.hosts.find((entry) => entry.name === host);
        if (host !== "local" && !executorHost) {
          return yield* new DispatchFailed({ cause: new Error(sourceText("error.backend.dispatchRepository")) });
        }
        const cardPath = join(this.#options.dataPath, "dispatch-cards", `${randomUUID()}.md`);
        let cwd = caller.workspacePath;
        if (host !== "local" && !lane) {
          if (!fleet)
            return yield* new DispatchFailed({ cause: new Error(sourceText("error.backend.dispatchRepository")) });
          cwd = yield* this.#remoteRepository(host, cwd, fleet);
        }
        const makeArgs = (taskFile: string) => {
          const args = lane ? ["lane", "assign", lane] : ["run", "--detach", "--cwd", cwd];
          args.push("--task-file", taskFile, "--title", title, "--budget-minutes", String(input.budgetMinutes ?? 40));
          if (input.verify) args.push("--verify", this.#options.redact(input.verify));
          if (backend) args.push("--backend", backend);
          if (input.model) args.push("--model", input.model);
          return args;
        };
        const localCard = host === "local" || executorHost?.kind === "local";
        const transport = this.#dispatchFileTransport(executorHost);
        const output = yield* Effect.acquireUseRelease(
          writeFileAtomically(cardPath, card, { createDirectory: true }).pipe(
            Effect.mapError((failure) =>
              dispatchFailure({
                cause: failure.cause,
                reason: sourceText("error.backend.dispatchSaveFailed"),
                step: "save",
              }),
            ),
          ),
          () => {
            if (localCard) return this.#command(binary, host, makeArgs(cardPath));
            if (!transport) {
              return Effect.fail(
                new DispatchFailed({ cause: new Error(`OpenSwarm host ${host} has no supported file transport.`) }),
              );
            }
            return Effect.acquireUseRelease(
              writeRemoteDispatchCard(transport, card),
              (remotePath) => this.#command(binary, host, makeArgs(remotePath)),
              (remotePath) => removeRemoteDispatchCard(transport, remotePath).pipe(Effect.catch(() => Effect.void)),
            );
          },
          () => io(() => rm(cardPath, { force: true })).pipe(Effect.catch(() => Effect.void)),
        );
        const { id: runId } = yield* parseCommandJson(acknowledgement, output);
        const record: DispatchRecord = {
          runId,
          host,
          agentId: caller.agentId,
          threadId: caller.threadId,
          title,
          binary,
          idempotencyKey: caller.idempotencyKey,
          status: "running",
          branch: null,
          commit: null,
          startedAt: new Date().toISOString(),
          delivered: false,
        };
        const saved = yield* Effect.result(this.#save([...this.#runs, record], output.exitCode));
        if (saved._tag === "Failure") {
          // Do not leave an accepted executor without a durable owner.
          yield* this.#command(binary, host, ["stop", runId]).pipe(Effect.catch(() => Effect.void));
          return yield* saved.failure;
        }
        yield* this.#options
          .publish(record, false)
          .pipe(Effect.catch(() => Effect.sync(() => this.#options.failed(record.agentId))));
        yield* this.#watch(host, runId);
        return { runId, host };
      }),
    );
  }, Effect.uninterruptible);

  readonly status = Effect.fn("DispatchService.status")(function* (
    this: DispatchService,
    runId: string,
    caller: DispatchCaller,
    host?: string,
  ) {
    const record = yield* this.#owned(runId, caller, host);
    return {
      runId: record.runId,
      host: record.host,
      status: record.status,
      branch: record.branch,
      commit: record.commit,
      summary: record.summary,
      verify: record.verify,
      startedAt: record.startedAt,
    };
  });

  readonly abort = Effect.fn("DispatchService.abort")(function* (
    this: DispatchService,
    runId: string,
    caller: DispatchCaller,
    host?: string,
  ) {
    return yield* this.#writes.withPermit(
      Effect.gen({ self: this }, function* () {
        const record = yield* this.#owned(runId, caller, host);
        if (isTerminal(record)) return { runId, host: record.host, status: record.status };
        const output = yield* this.#command(record.binary, record.host, ["stop", runId]);
        yield* this.#save(
          this.#runs.map((run) =>
            runKey(run.host, run.runId) === runKey(record.host, record.runId) ? { ...run, status: "cancelling" } : run,
          ),
          output.exitCode,
        );
        return { runId, host: record.host, status: "cancelling" };
      }),
    );
  }, Effect.uninterruptible);

  #owned(runId: string, caller: DispatchCaller, host?: string) {
    return Effect.try({
      try: () => {
        const ownedRuns = this.#runs.filter(
          (run) =>
            run.runId === runId &&
            run.agentId === caller.agentId &&
            run.threadId === caller.threadId &&
            (host === undefined || run.host === host),
        );
        if (caller.inExecutionThread) {
          const reason = sourceText("error.backend.dispatchUnknown");
          throw new DispatchFailed({ cause: new Error(reason), reason });
        }
        if (host === undefined && ownedRuns.length > 1) {
          const reason = sourceText("error.backend.dispatchHostRequired");
          throw new DispatchFailed({ cause: new Error(reason), reason });
        }
        const record = ownedRuns.length === 1 ? ownedRuns[0] : undefined;
        if (!record) {
          const reason = sourceText("error.backend.dispatchUnknown");
          throw new DispatchFailed({ cause: new Error(reason), reason });
        }
        return record;
      },
      catch: (cause) => (cause instanceof DispatchFailed ? cause : new DispatchFailed({ cause })),
    });
  }

  #command(binary: string, host: string, args: string[]) {
    return dispatchCommand(binary, host === "local" ? args : ["fleet", "exec", host, "--", ...args]);
  }

  #save(runs: DispatchRecord[], exitCode: number | null = null) {
    const safeRuns = runs.map((record) => redactDispatchRecord(record, this.#options.redact));
    return writeJsonFileAtomically(
      join(this.#options.dataPath, "openbot-dispatch-v1.json"),
      { version: 1, runs: safeRuns },
      { createDirectory: true },
    ).pipe(
      Effect.mapError((failure) =>
        dispatchFailure({
          cause: failure.cause,
          reason: sourceText("error.backend.dispatchSaveFailed"),
          step: "save",
          exitCode,
        }),
      ),
      Effect.tap(() =>
        Effect.sync(() => {
          this.#runs = safeRuns;
        }),
      ),
    );
  }

  #fleet() {
    const root = process.env.OPENSWARM_HOME || join(homedir(), ".claude", "openswarm");
    return io(() => readFile(join(root, "fleet.json"), "utf8")).pipe(
      Effect.flatMap((contents) => step(() => Schema.decodeUnknownSync(fleetSchema)(JSON.parse(contents)))),
    );
  }

  #remoteRepository(host: string, cwd: string, fleet: DispatchFleet) {
    return step(() => {
      const local = fleet.hosts.find((entry) => entry.name === "local");
      const key = Object.entries(local?.repos ?? {}).find(
        ([, path]) => resolve(expandHome(path)) === resolve(cwd),
      )?.[0];
      const remote = key ? fleet.hosts.find((entry) => entry.name === host)?.repos?.[key] : undefined;
      if (!remote) throw new DispatchFailed({ cause: new Error(sourceText("error.backend.dispatchRepository")) });
      return remote;
    });
  }

  #dispatchFileTransport(host: DispatchFleet["hosts"][number] | undefined): DispatchFileTransport | null {
    if (host?.kind === "ssh" && host.ssh) return { kind: "ssh", destination: host.ssh };
    if (host?.kind === "instabox" && host.box) return { kind: "instabox", destination: host.box };
    return null;
  }

  readonly #watch = Effect.fn("DispatchService.watch")(function* (this: DispatchService, host: string, runId: string) {
    const key = runKey(host, runId);
    if (this.#watching.has(key)) return;
    this.#watching.add(key);
    yield* this.#poll(host, runId).pipe(
      Effect.ensuring(Effect.sync(() => this.#watching.delete(key))),
      Effect.forkIn(this.#scope, { startImmediately: false }),
    );
  });

  readonly #poll = Effect.fn("DispatchService.poll")(function* (this: DispatchService, host: string, runId: string) {
    while (this.#started) {
      const result = yield* Effect.result(this.#refresh(host, runId));
      if (result._tag === "Success" && result.success) return;
      // A transport failure is unknown, never a successful result. The next poll can reconnect.
      yield* Effect.sleep("2 seconds");
    }
  });

  readonly #refresh = Effect.fn("DispatchService.refresh")(function* (
    this: DispatchService,
    host: string,
    runId: string,
  ) {
    const key = runKey(host, runId);
    const current = this.#runs.find((run) => runKey(run.host, run.runId) === key);
    if (!current || current.delivered) return true;
    let finished = current;
    let exitCode: number | null = null;
    if (!isTerminal(current)) {
      // _run-info is openswarm's fleet result endpoint; runs --json omits summary and verify.
      const output = yield* this.#command(current.binary, current.host, ["fleet", "_run-info", runId]);
      exitCode = output.exitCode;
      const info = yield* parseCommandJson(infoSchema, output);
      if (info.id !== runId) return false;
      if (info.result) {
        const safe = yield* step(() =>
          Schema.decodeUnknownSync(resultSchema)(JSON.parse(this.#options.redact(JSON.stringify(info.result)))),
        );
        finished = {
          ...current,
          status: safe.status,
          branch: safe.branch ?? null,
          commit: safe.commit ?? null,
          summary: safe.result?.summary?.slice(0, 16_000) ?? "",
          verify: safe.verify ?? null,
        };
      } else if (info.state === "dead") {
        finished = {
          ...current,
          status: "failed",
          branch: null,
          commit: info.commit ?? null,
          summary: sourceText("status.agent.dispatchNoResult"),
          verify: null,
        };
      } else {
        return false;
      }
    }
    return yield* this.#writes.withPermit(
      Effect.gen({ self: this }, function* () {
        if (this.#runs.find((run) => runKey(run.host, run.runId) === key)?.delivered) return true;
        const safeFinished = redactDispatchRecord(finished, this.#options.redact);
        yield* this.#save(
          this.#runs.map((run) => (runKey(run.host, run.runId) === key ? safeFinished : run)),
          exitCode,
        );
        yield* this.#options.publish(safeFinished, true);
        yield* this.#save(
          this.#runs.map((run) => (runKey(run.host, run.runId) === key ? { ...safeFinished, delivered: true } : run)),
          exitCode,
        );
        return true;
      }),
    );
  });
}
