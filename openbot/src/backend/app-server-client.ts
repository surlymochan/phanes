import { type ChildProcessWithoutNullStreams, spawn } from "node:child_process";
import { EventEmitter } from "node:events";
import { type DynamicRecord, isNumber, isString } from "@openbot/contracts/runtime-values";
import { sourceText } from "@openbot/i18n/source";
import { Effect, Fiber } from "effect";
import { configuredFleetTurnGate } from "./agent/fleet-turn-config";
import type { FleetTurnGate } from "./agent/fleet-turn-gate";
import { isPlanLimitDiagnostic } from "./agent/provider-diagnostics";
import { type AgentProvider, RequestTimeoutError } from "./agent-client";
import { cliSpawnTarget } from "./cli";
import { runCauseEffect } from "./effect-boundary";
import { JsonLineDecoder, LineTooLongError } from "./jsonl";
import {
  type AppServerNotification,
  type AppServerRequest,
  decodeRecordResponse,
  isRecord,
  type RequestId,
  type ResponseDecoder,
  type RpcError,
  type RpcMessage,
  type ThreadItem,
} from "./protocol";
import { ProviderClientOperationError, toProviderClientOperationError } from "./provider-client-effects";
import {
  PROVIDER_HISTORY_PAGE_SIZE,
  type ProviderHistoryConsumer,
  type ProviderHistoryFragment,
  type ProviderHistoryRequest,
} from "./provider-history";
import { registerProviderProcess } from "./provider-processes";
import { createDiagnosticStream } from "./stderr-diagnostics";
import { endProcessTree } from "./windows-process-tree";

interface PendingRequest {
  resolve: (value: unknown) => void;
  reject: (error: Error) => void;
}

interface ClientEvents {
  notification: [notification: AppServerNotification];
  request: [request: AppServerRequest];
  exit: [error: Error];
  diagnostic: [message: string];
}

export class AppServerError extends Error {
  constructor(
    message: string,
    readonly code: number,
    readonly data?: unknown,
  ) {
    super(message);
    this.name = "AppServerError";
  }
}

export class CodexAppServerClient extends EventEmitter<ClientEvents> {
  readonly contextCompaction = "events";
  readonly provider: AgentProvider = "codex";
  readonly #executable: string;
  readonly #requestTimeoutMs: number;
  #decoder = new JsonLineDecoder();
  readonly #pending = new Map<RequestId, PendingRequest>();
  readonly #toolRequests = new Map<RequestId, { threadId: string; turnId: string; controller: AbortController }>();
  readonly #interruptedTurns = new Set<string>();
  #process: ChildProcessWithoutNullStreams | null = null;
  /** The end of a process that failed. `stop` waits for it, because `#process` no longer holds it. */
  #ending: Fiber.Fiber<void> | null = null;
  #nextId = 1;
  #stopping = false;
  #probing = false;

  /** Added to the inherited environment, such as the `CODEX_HOME` of an account after the first. */
  readonly #extraEnv: Readonly<Record<string, string>> | undefined;
  readonly #fleet: FleetTurnGate | null;
  readonly #fleetThreads = new Map<string, string>();
  readonly #fleetReleases = new Set<Promise<void>>();
  readonly #fleetAdmissions = new Set<Promise<void>>();
  readonly #fleetPendingAdmissions = new Set<string>();
  readonly #fleetCompactions = new Map<string, { turnId: string | null; confirmed: boolean }>();
  readonly #fleetStarts = new Map<string, { observed: string | null; confirmed: string | null }>();
  #fleetWaitAbort = new AbortController();
  readonly #pendingCompactionAdmissions = new Map<string, AbortController>();

  constructor(
    executable: string,
    requestTimeoutMs = 30_000,
    extraEnv?: Readonly<Record<string, string>>,
    fleet?: FleetTurnGate | null,
  ) {
    super();
    this.#executable = executable;
    this.#requestTimeoutMs = requestTimeoutMs;
    this.#extraEnv = extraEnv;
    this.#fleet = fleet === undefined ? configuredFleetTurnGate() : fleet;
  }

  get running(): boolean {
    return this.#process !== null && this.#process.exitCode === null;
  }

  start(): void {
    if (this.running) return;

    this.#stopping = false;
    this.#fleetWaitAbort = new AbortController();
    this.#decoder = new JsonLineDecoder();
    const target = cliSpawnTarget(this.#executable, ["app-server", "--listen", "stdio://"]);
    const child = spawn(target.command, target.args, {
      stdio: ["pipe", "pipe", "pipe"],
      env: this.#extraEnv ? { ...process.env, ...this.#extraEnv } : process.env,
      windowsVerbatimArguments: target.windowsVerbatimArguments,
      windowsHide: true,
    });
    this.#process = child;
    registerProviderProcess(child, "codex");

    child.stdin.on("error", (error) => this.#fail(error, child));
    child.stdout.on("data", (chunk: Buffer) => {
      try {
        for (const message of this.#decoder.push(chunk)) this.#handleMessage(message);
      } catch (error) {
        // Not wrapped: the screen translates this text only when no prefix is in front of it.
        this.#fail(
          error instanceof LineTooLongError ? error : new Error(`Codex protocol error: ${String(error)}`),
          child,
        );
      }
    });

    // Whole records only. A chunk ends wherever the pipe filled up, and half a record is neither
    // readable nor reliably redactable.
    const diagnostics = createDiagnosticStream({
      // The runtime owns MCP values. Keep the full record until its exact-value redactor runs.
      redact: (message) => message,
      emit: (message) => this.emit("diagnostic", message),
    });
    child.stderr.on("data", (chunk: Buffer) => diagnostics.push(chunk.toString("utf8")));
    child.once("close", () => diagnostics.flush());

    child.once("error", (error) => this.#fail(error, child));
    child.once("exit", (code, signal) => {
      const suffix = signal ? `signal ${signal}` : `code ${code ?? "unknown"}`;
      this.#fail(new Error(`Codex App Server exited with ${suffix}.`), child);
    });
  }

  readonly stop = Effect.fn("CodexAppServer.stop")(function* (this: CodexAppServerClient) {
    this.#fleetWaitAbort.abort();
    yield* Effect.promise(() => Promise.all([...this.#fleetAdmissions]).then(() => undefined));
    const child = this.#process;
    if (!child) {
      if (this.#ending) yield* Fiber.join(this.#ending);
      yield* Effect.promise(() => Promise.all([...this.#fleetReleases]).then(() => undefined));
      return;
    }
    this.#stopping = true;
    this.#retainFleetReservations();
    yield* Effect.promise(() => Promise.all([...this.#fleetReleases]).then(() => undefined));
    this.#cancelToolRequests();
    this.#process = null;
    for (const pending of this.#pending.values()) pending.reject(new Error("Codex App Server stopped."));
    this.#pending.clear();
    child.stdin.end();
    yield* endProcessTree(child).pipe(Effect.orDie);
  }, Effect.uninterruptible);

  /**
   * Drops this connection's hold on one thread and keeps the app server for the others.
   *
   * `thread/unsubscribe` is the non-destructive end of a thread: the app server keeps the thread and
   * its history, stops the thread's MCP event streams at once, and unloads the thread - with the
   * MCP servers it started - once nothing is subscribed to it and it is idle. It does not archive
   * or delete. A refresh after an MCP change starts a replacement session for the same public
   * thread, so without this the old session stays loaded there with the servers the user turned off.
   *
   * A thread this connection never subscribed to answers `NotSubscribed`, which is not an error
   * here: either way this side has stopped using it.
   */
  readonly releaseThread = Effect.fn("CodexAppServer.releaseThread")(function* (
    this: CodexAppServerClient,
    threadId: string,
  ) {
    if (!this.running) return;
    yield* this.request("thread/unsubscribe", { threadId }, decodeRecordResponse);
  });

  /**
   * Moves a replaced thread out of the list that the Codex app and ChatGPT show. Without this, each
   * replacement session adds a row that starts with the OpenBot handoff to the user's own Codex
   * history.
   *
   * `thread/archive` moves the rollout to `archived_sessions` and unloads the thread when it is
   * loaded. `thread/turns/list` and `thread/items/list` still read it, so a history import works,
   * and a resume unarchives it first (`ThreadLifecycle.resumeThread`). A thread that is already
   * archived, or that has no rollout, answers `no rollout found`.
   */
  readonly archiveThread = Effect.fn("CodexAppServer.archiveThread")(function* (
    this: CodexAppServerClient,
    threadId: string,
  ) {
    if (!this.running) return;
    yield* this.request("thread/archive", { threadId }, decodeRecordResponse);
  });

  /**
   * Reads Codex history without asking the app server for the legacy full thread snapshot.
   *
   * The app server returns turns newest first. Items are read one turn at a time in ascending
   * order, so consumers can persist each fragment and stop without retaining the transcript.
   * Cursors belong to this adapter and never cross the provider-history boundary.
   */
  readonly readHistory = Effect.fn("CodexAppServer.readHistory")(function* (
    this: CodexAppServerClient,
    request: ProviderHistoryRequest,
    consume: ProviderHistoryConsumer,
  ) {
    // These endpoints read the persisted rollout directly. Do not resume here: a resume applies
    // runtime settings and can reopen an archived thread while this operation only needs history.
    let cursor: string | undefined;
    const seenTurnCursors = new Set<string>();
    while (true) {
      const page = yield* this.request(
        "thread/turns/list",
        {
          threadId: request.threadId,
          limit: PROVIDER_HISTORY_PAGE_SIZE,
          sortDirection: "desc",
          itemsView: "notLoaded",
          ...(cursor === undefined ? {} : { cursor }),
        },
        decodeTurnHistoryPage,
      );

      for (const turn of page.data) {
        if (request.items === "none") {
          if (!(yield* consume(toHistoryFragment(turn, [], true)))) return;
          continue;
        }

        let itemCursor: string | undefined;
        const seenItemCursors = new Set<string>();
        let itemOffset = 0;
        while (true) {
          const itemPage = yield* this.request(
            "thread/items/list",
            {
              threadId: request.threadId,
              turnId: turn.id,
              limit: PROVIDER_HISTORY_PAGE_SIZE,
              sortDirection: "asc",
              ...(itemCursor === undefined ? {} : { cursor: itemCursor }),
            },
            decodeItemHistoryPage,
          );
          if (itemPage.data.some((entry) => entry.turnId !== turn.id)) {
            return yield* new ProviderClientOperationError({
              cause: new Error("Codex returned an item for the wrong turn."),
            });
          }
          const complete = itemPage.nextCursor === null;
          if (
            !(yield* consume(
              toHistoryFragment(
                turn,
                itemPage.data.map((entry) => entry.item),
                complete,
                itemOffset,
              ),
            ))
          )
            return;
          itemOffset += itemPage.data.length;
          if (complete) break;
          const nextItemCursor = freshCursor(itemPage.nextCursor, itemCursor, seenItemCursors, "item");
          if (nextItemCursor instanceof ProviderClientOperationError) return yield* nextItemCursor;
          itemCursor = nextItemCursor;
        }
      }

      if (page.nextCursor === null) return;
      const nextTurnCursor = freshCursor(page.nextCursor, cursor, seenTurnCursors, "turn");
      if (nextTurnCursor instanceof ProviderClientOperationError) return yield* nextTurnCursor;
      cursor = nextTurnCursor;
    }
  });

  readonly request = Effect.fn("CodexAppServer.request")(function* <T>(
    this: CodexAppServerClient,
    method: string,
    params: unknown,
    decoder: ResponseDecoder<T>,
    timeoutMs = this.#requestTimeoutMs,
  ) {
    let fleetThread: string | null = null;
    let fleetReservation: string | null = null;
    let fleetCompactionAbort: AbortController | null = null;
    if (this.#fleet && (method === "turn/start" || method === "thread/compact/start")) {
      if (!isRecord(params) || !isString(params.threadId) || !params.threadId) {
        return yield* new ProviderClientOperationError({
          cause: new Error(sourceText("error.provider.fleetUnavailable")),
        });
      }
      fleetThread = params.threadId;
      const threadId = fleetThread;
      if (this.#fleetPendingAdmissions.has(threadId)) {
        return yield* new ProviderClientOperationError({
          cause: new Error(sourceText("error.provider.fleetUnavailable")),
        });
      }
      if (!this.running) {
        return yield* new ProviderClientOperationError({ cause: new Error("Codex App Server is not running.") });
      }
      const compactionAbort = method === "thread/compact/start" ? new AbortController() : null;
      const previousReservation = this.#fleetThreads.get(threadId);
      let finishAdmission!: () => void;
      const admission = new Promise<void>((resolve) => {
        finishAdmission = resolve;
      });
      this.#fleetAdmissions.add(admission);
      this.#fleetPendingAdmissions.add(threadId);
      fleetCompactionAbort = compactionAbort;
      if (compactionAbort) this.#pendingCompactionAdmissions.set(threadId, compactionAbort);
      fleetReservation = yield* this.#admitFleetTurn(threadId, compactionAbort?.signal).pipe(
        toProviderClientOperationError,
        Effect.onError(() =>
          Effect.gen({ self: this }, function* () {
            if (compactionAbort && this.#pendingCompactionAdmissions.get(threadId) === compactionAbort)
              this.#pendingCompactionAdmissions.delete(threadId);
            const owned = this.#fleetThreads.get(threadId);
            if (owned && owned !== previousReservation && this.#fleet?.owns(threadId, owned)) {
              yield* this.#fleet.rejectedBeforeTurn(threadId).pipe(Effect.ignore);
              if (!this.#fleet.has(threadId)) this.#fleetThreads.delete(threadId);
            }
          }),
        ),
        Effect.ensuring(
          Effect.sync(() => {
            this.#fleetAdmissions.delete(admission);
            this.#fleetPendingAdmissions.delete(threadId);
            finishAdmission();
          }),
        ),
      );
      if (method === "thread/compact/start") this.#fleetCompactions.set(threadId, { turnId: null, confirmed: false });
      else this.#fleetStarts.set(threadId, { observed: null, confirmed: null });
    }
    const id = this.#nextId++;
    const child = this.#process;
    return yield* Effect.callback<T, ProviderClientOperationError>((resume) => {
      this.#pending.set(id, {
        resolve: (value) => {
          if (fleetThread && fleetReservation && this.#fleet) {
            const gate = this.#fleet;
            const threadId = fleetThread;
            if (!gate.owns(threadId, fleetReservation)) {
              resume(
                Effect.fail(
                  new ProviderClientOperationError({
                    cause: new Error(sourceText("error.provider.fleetIdentityUnconfirmed")),
                  }),
                ),
              );
              return;
            }
            if (method === "thread/compact/start") {
              const compaction = this.#fleetCompactions.get(threadId);
              if (!compaction) {
                gate.uncertain(threadId);
                resume(
                  Effect.fail(
                    new ProviderClientOperationError({
                      cause: new Error(sourceText("error.provider.fleetIdentityUnconfirmed")),
                    }),
                  ),
                );
                return;
              }
              compaction.confirmed = true;
              const decoded = Effect.try({
                try: () => decoder(value),
                catch: (cause) => new ProviderClientOperationError({ cause }),
              });
              resume(
                compaction.turnId
                  ? this.#trackFleetResponse(gate.started(threadId, compaction.turnId)).pipe(
                      toProviderClientOperationError,
                      Effect.tap(() =>
                        Effect.sync(() => {
                          if (!gate.has(threadId)) {
                            this.#fleetThreads.delete(threadId);
                            this.#fleetCompactions.delete(threadId);
                          }
                        }),
                      ),
                      Effect.andThen(decoded),
                    )
                  : decoded,
              );
              return;
            }
            if (!isRecord(value) || !isRecord(value.turn) || !isString(value.turn.id)) {
              gate.uncertain(threadId);
              resume(
                Effect.fail(
                  new ProviderClientOperationError({
                    cause: new Error(sourceText("error.provider.fleetIdentityUnconfirmed")),
                  }),
                ),
              );
              return;
            }
            const turnId = value.turn.id;
            const start = this.#fleetStarts.get(threadId);
            if (!start || (start.observed && start.observed !== turnId)) {
              gate.uncertain(threadId);
              if (this.#process)
                this.#fail(new Error(sourceText("error.provider.fleetIdentityUnconfirmed")), this.#process);
              resume(
                Effect.fail(
                  new ProviderClientOperationError({
                    cause: new Error(sourceText("error.provider.fleetIdentityUnconfirmed")),
                  }),
                ),
              );
              return;
            }
            start.confirmed = turnId;
            resume(
              this.#trackFleetResponse(gate.started(threadId, turnId)).pipe(
                toProviderClientOperationError,
                Effect.andThen(
                  Effect.try({
                    try: () => {
                      if (!gate.has(threadId)) {
                        this.#fleetThreads.delete(threadId);
                        this.#fleetStarts.delete(threadId);
                      }
                      return decoder(value);
                    },
                    catch: (cause) => new ProviderClientOperationError({ cause }),
                  }),
                ),
              ),
            );
          } else {
            resume(
              Effect.try({
                try: () => decoder(value),
                catch: (cause) => new ProviderClientOperationError({ cause }),
              }),
            );
          }
        },
        reject: (cause) => {
          if (
            method === "turn/start" &&
            fleetThread &&
            fleetReservation &&
            this.#fleet?.owns(fleetThread, fleetReservation) &&
            cause instanceof AppServerError &&
            (/\bis archived\b|thread not found:|\bno rollout found\b/i.test(cause.message) ||
              isPlanLimitDiagnostic(cause.message) ||
              (cause.code === -32603 &&
                cause.message.startsWith(
                  "failed to submit turn input: ActiveTurnNotSteerable { turn_kind: Compact",
                ))) &&
            !this.#fleetStarts.get(fleetThread)?.observed
          ) {
            const threadId = fleetThread;
            resume(
              this.#trackFleetResponse(this.#fleet.rejectedBeforeTurn(threadId)).pipe(
                toProviderClientOperationError,
                Effect.tap(() =>
                  Effect.sync(() => {
                    this.#fleetThreads.delete(threadId);
                    this.#fleetStarts.delete(threadId);
                  }),
                ),
                Effect.andThen(Effect.fail(new ProviderClientOperationError({ cause }))),
              ),
            );
            return;
          }
          resume(Effect.fail(new ProviderClientOperationError({ cause })));
        },
      });
      try {
        if (
          fleetCompactionAbort?.signal.aborted &&
          fleetThread &&
          fleetReservation &&
          this.#fleet?.owns(fleetThread, fleetReservation)
        ) {
          const threadId = fleetThread;
          resume(
            this.#trackFleetResponse(this.#fleet.rejectedBeforeTurn(threadId)).pipe(
              toProviderClientOperationError,
              Effect.tap(() =>
                Effect.sync(() => {
                  this.#fleetThreads.delete(threadId);
                  this.#fleetCompactions.delete(threadId);
                }),
              ),
              Effect.andThen(
                Effect.fail(
                  new ProviderClientOperationError({ cause: new Error("Compaction admission was cancelled.") }),
                ),
              ),
            ),
          );
          return Effect.sync(() => this.#pending.delete(id));
        }
        this.#write({ method, id, params: method === "thread/resume" ? metadataOnlyResumeParams(params) : params });
        if (
          fleetCompactionAbort &&
          fleetThread &&
          this.#pendingCompactionAdmissions.get(fleetThread) === fleetCompactionAbort
        )
          this.#pendingCompactionAdmissions.delete(fleetThread);
        if (method === "turn/interrupt" && isRecord(params) && isString(params.threadId) && isString(params.turnId)) {
          this.#interruptedTurns.add(JSON.stringify([params.threadId, params.turnId]));
          this.#cancelToolRequests(params.threadId, params.turnId);
        }
      } catch (cause) {
        resume(Effect.fail(new ProviderClientOperationError({ cause })));
      }
      return Effect.sync(() => this.#pending.delete(id));
    }).pipe(
      Effect.timeoutOrElse({
        duration: timeoutMs,
        orElse: () =>
          Effect.sync(() => this.#checkAnswering(child, timeoutMs)).pipe(
            Effect.andThen(
              Effect.fail(new ProviderClientOperationError({ cause: new RequestTimeoutError("Codex", method) })),
            ),
          ),
      }),
      Effect.ensuring(
        Effect.sync(() => {
          this.#pending.delete(id);
          if (
            fleetCompactionAbort &&
            fleetThread &&
            this.#pendingCompactionAdmissions.get(fleetThread) === fleetCompactionAbort
          )
            this.#pendingCompactionAdmissions.delete(fleetThread);
        }),
      ),
      Effect.tapError(() =>
        Effect.sync(() => {
          if (fleetThread && fleetReservation && this.#fleet?.owns(fleetThread, fleetReservation)) {
            this.#fleet.uncertain(fleetThread);
          }
        }),
      ),
    );
  });

  cancelPendingCompaction(threadId: string): boolean | undefined {
    if (!this.#fleet) return undefined;
    const pending = this.#pendingCompactionAdmissions.get(threadId);
    if (!pending) return false;
    pending.abort();
    return true;
  }

  #trackFleetResponse<A, E>(operation: Effect.Effect<A, E>): Effect.Effect<A, E> {
    let finish!: () => void;
    const pending = new Promise<void>((resolve) => {
      finish = resolve;
    });
    this.#fleetReleases.add(pending);
    return operation.pipe(
      Effect.ensuring(
        Effect.sync(() => {
          this.#fleetReleases.delete(pending);
          finish();
        }),
      ),
    );
  }

  #admitFleetTurn(threadId: string, compactionSignal?: AbortSignal) {
    return Effect.uninterruptibleMask((restore) =>
      Effect.gen({ self: this }, function* () {
        const gate = this.#fleet;
        if (!gate)
          return yield* new ProviderClientOperationError({
            cause: new Error(sourceText("error.provider.fleetUnavailable")),
          });
        const child = this.#process;
        const signal = this.#fleetWaitAbort.signal;
        while (true) {
          if (signal.aborted || compactionSignal?.aborted || !child || this.#process !== child || !this.running)
            return yield* new ProviderClientOperationError({
              cause: new Error(sourceText("error.provider.fleetUnavailable")),
            });
          if (gate.requiresRecovery("codex"))
            return yield* new ProviderClientOperationError({
              cause: new Error(sourceText("error.provider.fleetUnavailable")),
            });
          if (!gate.has(threadId) && (yield* gate.begin(threadId, "codex", null))) {
            if (signal.aborted || compactionSignal?.aborted || this.#process !== child || !this.running) {
              yield* gate.cancelBeforeSend(threadId);
              return yield* new ProviderClientOperationError({
                cause: new Error(sourceText("error.provider.fleetUnavailable")),
              });
            }
            gate.sent(threadId);
            const reservationId = gate.reservationId(threadId);
            if (!reservationId)
              return yield* new ProviderClientOperationError({
                cause: new Error(sourceText("error.provider.fleetIdentityUnconfirmed")),
              });
            this.#fleetThreads.set(threadId, reservationId);
            return reservationId;
          }
          yield* restore(
            Effect.raceFirst(
              Effect.sleep(1_000),
              Effect.callback<void, ProviderClientOperationError>((resume) => {
                const aborted = () =>
                  resume(
                    Effect.fail(
                      new ProviderClientOperationError({
                        cause: new Error(sourceText("error.provider.fleetUnavailable")),
                      }),
                    ),
                  );
                if (signal.aborted || compactionSignal?.aborted) aborted();
                else {
                  signal.addEventListener("abort", aborted, { once: true });
                  compactionSignal?.addEventListener("abort", aborted, { once: true });
                }
                return Effect.sync(() => {
                  signal.removeEventListener("abort", aborted);
                  compactionSignal?.removeEventListener("abort", aborted);
                });
              }),
            ),
          );
        }
      }),
    );
  }

  #retainFleetReservations(): void {
    for (const [threadId, reservationId] of this.#fleetThreads) {
      if (this.#fleet?.owns(threadId, reservationId)) this.#fleet.uncertain(threadId);
    }
    this.#fleetThreads.clear();
    this.#fleetCompactions.clear();
    this.#fleetStarts.clear();
  }

  notify(method: string, params: unknown = {}): void {
    this.#write({ method, params });
  }

  respond(id: RequestId, result: unknown): void {
    this.#toolRequests.delete(id);
    this.#write({ id, result });
  }

  respondError(id: RequestId, error: RpcError): void {
    this.#toolRequests.delete(id);
    this.#write({ id, error });
  }

  #write(message: unknown): void {
    if (!this.running || !this.#process) {
      throw new Error("Codex App Server is not running.");
    }
    this.#process.stdin.write(`${JSON.stringify(message)}\n`);
  }

  #handleMessage(message: RpcMessage): void {
    if ("method" in message) {
      if ("id" in message) {
        let signal: AbortSignal | undefined;
        if (
          message.method === "item/tool/call" &&
          isRecord(message.params) &&
          isString(message.params.threadId) &&
          isString(message.params.turnId)
        ) {
          const controller = new AbortController();
          this.#toolRequests.set(message.id, {
            threadId: message.params.threadId,
            turnId: message.params.turnId,
            controller,
          });
          signal = controller.signal;
          if (this.#interruptedTurns.has(JSON.stringify([message.params.threadId, message.params.turnId]))) {
            this.#toolRequests.delete(message.id);
            controller.abort();
          }
        }
        this.emit("request", {
          ...(signal ? { signal } : {}),
          method: message.method,
          id: message.id,
          params: message.params,
        });
      } else {
        if (
          this.#fleet &&
          (message.method === "turn/started" || message.method === "turn/completed") &&
          isRecord(message.params) &&
          isString(message.params.threadId) &&
          isRecord(message.params.turn) &&
          isString(message.params.turn.id)
        ) {
          const threadId = message.params.threadId;
          const turnId = message.params.turn.id;
          if (!this.#fleetThreads.has(threadId)) {
            if (message.method === "turn/started" && this.#process) {
              this.#fail(new Error("Codex started a turn without fleet admission."), this.#process);
              return;
            }
          } else {
            if (message.method === "turn/started") {
              const compaction = this.#fleetCompactions.get(threadId);
              if (compaction) {
                if (compaction.turnId && compaction.turnId !== turnId) {
                  this.#fleet.uncertain(threadId);
                  if (this.#process)
                    this.#fail(new Error("Codex compaction turn identity is uncertain."), this.#process);
                  return;
                }
                compaction.turnId = turnId;
                if (compaction.confirmed)
                  void runCauseEffect(this.#fleet.started(threadId, turnId)).catch(() => {
                    if (this.#process)
                      this.#fail(new Error("Codex compaction turn identity is uncertain."), this.#process);
                  });
              } else {
                const start = this.#fleetStarts.get(threadId);
                if (
                  !start ||
                  (start.observed && start.observed !== turnId) ||
                  (start.confirmed && start.confirmed !== turnId)
                ) {
                  this.#fleet.uncertain(threadId);
                  if (this.#process) this.#fail(new Error("Codex started an unadmitted turn."), this.#process);
                  return;
                }
                start.observed = turnId;
              }
            }
            if (message.method === "turn/completed") {
              const release = runCauseEffect(this.#fleet.completed(threadId, turnId))
                .then(() => {
                  if (!this.#fleet?.has(threadId)) {
                    this.#fleetThreads.delete(threadId);
                    this.#fleetCompactions.delete(threadId);
                    this.#fleetStarts.delete(threadId);
                  }
                })
                .catch(() => this.emit("diagnostic", "Fleet turn reservation requires operator recovery."))
                .then(() => undefined);
              this.#fleetReleases.add(release);
              void release.finally(() => this.#fleetReleases.delete(release));
            }
          }
        }
        if (
          message.method === "turn/completed" &&
          isRecord(message.params) &&
          isString(message.params.threadId) &&
          isRecord(message.params.turn) &&
          isString(message.params.turn.id)
        ) {
          this.#cancelToolRequests(message.params.threadId, message.params.turn.id);
          this.#interruptedTurns.delete(JSON.stringify([message.params.threadId, message.params.turn.id]));
        }
        this.emit("notification", { method: message.method, params: message.params });
      }
      return;
    }

    const pending = this.#pending.get(message.id);
    if (!pending) return;

    this.#pending.delete(message.id);

    if (message.error && isRecord(message.error)) {
      const code = isNumber(message.error.code) ? message.error.code : -1;
      const text = isString(message.error.message) ? message.error.message : "Unknown error";
      pending.reject(new AppServerError(text, code, message.error.data));
      return;
    }

    pending.resolve(message.result);
  }

  #cancelToolRequests(threadId?: string, turnId?: string): void {
    if (threadId === undefined) this.#interruptedTurns.clear();
    for (const [id, request] of this.#toolRequests) {
      if (threadId !== undefined && (request.threadId !== threadId || request.turnId !== turnId)) continue;
      this.#toolRequests.delete(id);
      request.controller.abort();
    }
  }

  /**
   * Ends a process that stopped answering, so the runtime starts a new one as it does after an exit.
   * Without this a hung app server stays the shared client, and the requests of every agent time out
   * until the user kills it.
   *
   * One request that timed out is not enough: `account/rateLimits/read` waits on the network. So one
   * `config/read` probes the process. That request is local, and every thread start needs it. A
   * process that does not answer it in the request timeout is ended, also when it still sends events:
   * it takes no new work. A request with a shorter timeout, such as a `model/list` that may be slow,
   * starts no probe.
   */
  #checkAnswering(child: ChildProcessWithoutNullStreams | null, timeoutMs: number): void {
    if (this.#probing || !child || this.#process !== child || timeoutMs < this.#requestTimeoutMs) return;
    this.#probing = true;
    Effect.runFork(
      this.request("config/read", { includeLayers: false }, decodeRecordResponse).pipe(
        Effect.catch((failure) =>
          Effect.sync(() => {
            if (this.#process !== child || !(failure.cause instanceof RequestTimeoutError)) return;
            this.#fail(new Error(sourceText("error.provider.codexUnresponsive")), child);
          }),
        ),
        Effect.ensuring(
          Effect.sync(() => {
            this.#probing = false;
          }),
        ),
      ),
    );
  }

  #fail(error: Error, child: ChildProcessWithoutNullStreams): void {
    if (this.#process !== child) return;
    this.#fleetWaitAbort.abort();
    this.#retainFleetReservations();
    this.#cancelToolRequests();
    this.#process = null;

    for (const pending of this.#pending.values()) {
      pending.reject(error);
    }
    this.#pending.clear();

    this.#ending = Effect.runFork(endProcessTree(child).pipe(Effect.orDie));
    if (!this.#stopping) this.emit("exit", error);
  }
}

interface HistoryTurn {
  id: string;
  status?: string;
  startedAt?: number;
}

interface HistoryTurnPage {
  data: HistoryTurn[];
  nextCursor: string | null;
}

interface HistoryItemPage {
  data: Array<{ turnId: string; item: ThreadItem }>;
  nextCursor: string | null;
}

function metadataOnlyResumeParams(params: unknown): DynamicRecord {
  return { ...(isRecord(params) ? params : {}), excludeTurns: true };
}

function decodeTurnHistoryPage(value: unknown): HistoryTurnPage {
  const record = decodeRecordResponse(value);
  if (!Array.isArray(record.data)) throw new Error("Invalid Codex turn history page.");
  return {
    // Do not retain `items` from the response. `itemsView: notLoaded` is the memory boundary, and
    // the item endpoint below is the only path that materializes item records.
    data: record.data.map(decodeHistoryTurn),
    nextCursor: nullableString(record.nextCursor, "turn history cursor"),
  };
}

function decodeHistoryTurn(value: unknown): HistoryTurn {
  if (!isRecord(value) || !isString(value.id)) throw new Error("Invalid Codex history turn.");
  const status = value.status;
  const startedAt = value.startedAt;
  return {
    id: value.id,
    ...(isString(status) ? { status } : {}),
    ...(typeof startedAt === "number" && Number.isFinite(startedAt) ? { startedAt } : {}),
  };
}

function decodeItemHistoryPage(value: unknown): HistoryItemPage {
  const record = decodeRecordResponse(value);
  if (!Array.isArray(record.data)) throw new Error("Invalid Codex item history page.");
  return {
    data: record.data.map(decodeHistoryItemEntry),
    nextCursor: nullableString(record.nextCursor, "item history cursor"),
  };
}

function decodeHistoryItemEntry(value: unknown): { turnId: string; item: ThreadItem } {
  if (!isRecord(value) || !isString(value.turnId) || !isRecord(value.item) || !isString(value.item.type)) {
    throw new Error("Invalid Codex history item.");
  }
  // Preserve provider item fields (tool output, image metadata, command status, and so on) while
  // validating the discriminator at the untrusted app-server boundary.
  return { turnId: value.turnId, item: { ...value.item, type: value.item.type } };
}

function nullableString(value: unknown, label: string): string | null {
  if (value === undefined || value === null) return null;
  if (!isString(value)) throw new Error(`Invalid ${label}.`);
  return value;
}

function toHistoryFragment(
  turn: HistoryTurn,
  items: ThreadItem[],
  complete: boolean,
  itemOffset?: number,
): ProviderHistoryFragment {
  return {
    turnId: turn.id,
    ...(turn.status === undefined ? {} : { status: turn.status }),
    ...(turn.startedAt === undefined ? {} : { startedAt: turn.startedAt }),
    ...(itemOffset === undefined ? {} : { itemOffset }),
    items,
    complete,
  };
}

function freshCursor(
  nextCursor: string | null,
  currentCursor: string | undefined,
  seenCursors: Set<string>,
  kind: "turn" | "item",
): string | ProviderClientOperationError {
  if (nextCursor === null || nextCursor === currentCursor || seenCursors.has(nextCursor)) {
    return new ProviderClientOperationError({ cause: new Error(`Codex returned a repeated ${kind} history cursor.`) });
  }
  seenCursors.add(nextCursor);
  return nextCursor;
}

/**
 * Keep opaque app-server token shapes that the shared redactor may not classify. The caller applies
 * this after MCP exact-value masking, so an MCP URL query stays whole until its configured value is
 * removed.
 */
export function redactAppServerDiagnostic(message: string): string {
  return message.replace(/(?:sk|sess|Bearer|token)[-_a-zA-Z0-9.=]{8,}/gi, "[redacted]");
}

/** Kept inside the adapter; public callers still receive the native protocol error. */
