import { spawn } from "node:child_process";
import { extname } from "node:path";
import { sourceText } from "@openbot/i18n/source";
import { createOpenBotLogger } from "@openbot/logging";
import { Effect, Schema } from "effect";
import { resolveAgentCommand } from "../acp-agent-command";
import { agentLaunchPath, cliSpawnTarget } from "../cli";
import { endProcessTree } from "../windows-process-tree";

export class DispatchFailed extends Schema.TaggedError<DispatchFailed>()("DispatchFailed", {
  cause: Schema.Defect(),
  reason: Schema.optional(Schema.String),
  step: Schema.optional(Schema.Literals(["spawn", "exit code", "parse", "save"])),
  exitCode: Schema.optional(Schema.NullOr(Schema.Number)),
  timedOut: Schema.optional(Schema.Boolean),
}) {}

const logger = createOpenBotLogger("dispatch");
const WINDOWS_SCRIPT_FORBIDDEN = /[&|<>^%!"\r\n]/u;
const WINDOWS_SCRIPT_EXTENSIONS = new Set([".cmd", ".bat"]);

export interface DispatchInputValue {
  name: string;
  value: string;
}

/** Dispatch inputs follow dispatch_work's field limits. Reject NUL before any file or process use. */
export function assertDispatchInputValues(
  values: readonly DispatchInputValue[],
  executable: string,
  platform: NodeJS.Platform = process.platform,
): void {
  const windowsScript = platform === "win32" && WINDOWS_SCRIPT_EXTENSIONS.has(extname(executable).toLowerCase());
  for (const { name, value } of values) {
    if (value.includes("\0")) throw new Error(`Dispatch argument ${name} contains a NUL byte.`);
    // The card is written to a file, so its content does not enter cmd.exe's command line.
    if (windowsScript && name !== "--card" && WINDOWS_SCRIPT_FORBIDDEN.test(value)) {
      throw new Error(`Dispatch argument ${name} contains a character that is not safe for a Windows command wrapper.`);
    }
  }
  if (windowsScript && WINDOWS_SCRIPT_FORBIDDEN.test(executable)) {
    throw new Error("The openswarm command path contains a character that is not safe for a Windows command wrapper.");
  }
}

function dispatchArgumentName(args: readonly string[], index: number): string {
  const previous = args[index - 1];
  if (
    previous &&
    ["--cwd", "--task-file", "--title", "--budget-minutes", "--verify", "--backend", "--model"].includes(previous)
  ) {
    return previous;
  }
  if (index === 2 && args[0] === "fleet" && args[1] === "exec") return "--host";
  if (index === 2 && args[0] === "lane" && args[1] === "assign") return "--lane";
  return `argument ${index + 1}`;
}

/** Reject NUL for every dispatch command. Windows script values also stay on one safe command line. */
export function assertDispatchArgs(
  executable: string,
  args: readonly string[],
  platform: NodeJS.Platform = process.platform,
): void {
  const windowsScript = platform === "win32" && WINDOWS_SCRIPT_EXTENSIONS.has(extname(executable).toLowerCase());
  for (const [index, value] of args.entries()) {
    const name = dispatchArgumentName(args, index);
    if (value.includes("\0")) throw new Error(`Dispatch argument ${name} contains a NUL byte.`);
    if (windowsScript && WINDOWS_SCRIPT_FORBIDDEN.test(value)) {
      throw new Error(`Dispatch argument ${name} contains a character that is not safe for a Windows command wrapper.`);
    }
  }
  if (windowsScript && WINDOWS_SCRIPT_FORBIDDEN.test(executable)) {
    throw new Error("The openswarm command path contains a character that is not safe for a Windows command wrapper.");
  }
}

export interface DispatchFileTransport {
  kind: "ssh" | "instabox";
  destination: string;
}

function shellQuote(value: string): string {
  return `'${value.replaceAll("'", `'"'"'`)}'`;
}

function remotePythonCommand(source: string, args: readonly string[] = []): string {
  return ["python3", "-c", source, ...args].map(shellQuote).join(" ");
}

function transportArgs(transport: DispatchFileTransport, command: string): [string, string[]] {
  return transport.kind === "ssh"
    ? ["ssh", [transport.destination, "--", command]]
    : ["instabox", ["ssh", transport.destination, "--", command]];
}

const remoteCardPathPattern = /^\/tmp\/openbot-dispatch-[A-Za-z0-9_-]+\.md$/u;

/** Copy a private card file to a remote host over the transport stdin. */
export const writeRemoteDispatchCard = Effect.fn("DispatchProcess.writeRemoteCard")(function* (
  transport: DispatchFileTransport,
  card: string,
) {
  const copyScript =
    'import os, sys, tempfile\nfd, path = tempfile.mkstemp(dir="/tmp", prefix="openbot-dispatch-", suffix=".md")\nwith os.fdopen(fd, "wb") as stream: stream.write(sys.stdin.buffer.read())\nprint(path)';
  const [binary, args] = transportArgs(transport, remotePythonCommand(copyScript));
  const result = yield* dispatchCommand(binary, args, { stdin: card });
  const path = result.output.trim().split(/\r?\n/u).at(-1) ?? "";
  if (!remoteCardPathPattern.test(path)) {
    return yield* Effect.fail(
      dispatchFailure({
        cause: new Error("remote card copy returned an invalid path"),
        reason: sourceText("error.backend.dispatchSpawnFailed"),
        step: "spawn",
      }),
    );
  }
  return path;
});

/** Remove a remote card after openswarm has read it. */
export const removeRemoteDispatchCard = Effect.fn("DispatchProcess.removeRemoteCard")(function* (
  transport: DispatchFileTransport,
  path: string,
) {
  if (!remoteCardPathPattern.test(path)) return;
  const removeScript = "import os, sys; os.unlink(sys.argv[1])";
  const [binary, args] = transportArgs(transport, remotePythonCommand(removeScript, [path]));
  yield* dispatchCommand(binary, args);
});

export const dispatchFailure = (options: {
  cause: unknown;
  reason: string;
  step: "spawn" | "exit code" | "parse" | "save";
  exitCode?: number | null;
  timedOut?: boolean;
}) => {
  const exitCode = options.exitCode ?? null;
  const timedOut = options.timedOut ?? false;
  logger.warn("The openswarm request failed.", { step: options.step, exitCode, timedOut });
  return new DispatchFailed({
    cause: options.cause,
    reason: options.reason,
    step: options.step,
    exitCode,
    timedOut,
  });
};

export interface DispatchCommandOutput {
  output: string;
  exitCode: number;
}

export interface DispatchCommandOptions {
  /** Optional input for the OpenSwarm host file transport. */
  stdin?: string;
}

/** Owns one bounded CLI request. The detached executor belongs to openswarm. */
export const dispatchCommand = Effect.fn("DispatchProcess.command")(function* (
  binary: string,
  args: string[],
  options: DispatchCommandOptions = {},
) {
  // A packaged app starts with a short PATH, so look for the CLI in the login shell folders too.
  const resolved = yield* resolveAgentCommand(binary).pipe(Effect.catch(() => Effect.succeed(null)));
  const launchPath = yield* agentLaunchPath().pipe(Effect.catch(() => Effect.succeed(null)));
  const executable = resolved ?? binary;
  yield* Effect.try({
    try: () => {
      assertDispatchArgs(executable, args);
    },
    catch: (cause) =>
      dispatchFailure({
        cause,
        reason: cause instanceof Error ? cause.message : sourceText("error.backend.dispatchSpawnFailed"),
        step: "spawn",
      }),
  });
  const target = cliSpawnTarget(executable, args);
  return yield* Effect.acquireUseRelease(
    Effect.try({
      try: () =>
        spawn(target.command, target.args, {
          detached: process.platform !== "win32",
          stdio: [options.stdin === undefined ? "ignore" : "pipe", "pipe", "pipe"],
          env: launchPath ? { ...process.env, PATH: launchPath } : process.env,
          windowsVerbatimArguments: target.windowsVerbatimArguments,
          windowsHide: true,
        }),
      catch: (cause) =>
        dispatchFailure({
          cause,
          reason: sourceText("error.backend.dispatchSpawnFailed"),
          step: "spawn",
        }),
    }),
    (child) =>
      Effect.callback<DispatchCommandOutput, DispatchFailed>((resume) => {
        const chunks: Buffer[] = [];
        let size = 0;
        let complete = false;
        const finish = (value: Effect.Effect<DispatchCommandOutput, DispatchFailed>) => {
          if (complete) return;
          complete = true;
          resume(value);
        };
        child.stdout?.on("data", (chunk: Buffer) => {
          size += chunk.length;
          if (size > 1024 * 1024) {
            finish(
              Effect.fail(
                dispatchFailure({
                  cause: new Error("openswarm output exceeded the size limit"),
                  reason: sourceText("error.backend.dispatchOutputUnreadable"),
                  step: "parse",
                }),
              ),
            );
          } else chunks.push(chunk);
        });
        // CLI errors can contain card content or credentials. Never retain them.
        child.stderr?.resume();
        child.once("error", (cause) =>
          finish(
            Effect.fail(
              dispatchFailure({
                cause,
                reason: sourceText("error.backend.dispatchSpawnFailed"),
                step: "spawn",
              }),
            ),
          ),
        );
        child.once("close", (code) => {
          if (code !== 0) {
            finish(
              Effect.fail(
                dispatchFailure({
                  cause: new Error("openswarm exited unsuccessfully"),
                  reason:
                    code === null
                      ? sourceText("error.backend.dispatchExited")
                      : sourceText("error.backend.dispatchExitCode", { code }),
                  step: "exit code",
                  exitCode: code,
                }),
              ),
            );
          } else {
            finish(Effect.succeed({ output: Buffer.concat(chunks).toString("utf8"), exitCode: code }));
          }
        });
        if (options.stdin !== undefined) {
          child.stdin?.on("error", () => undefined);
          child.stdin?.end(options.stdin);
        }
      }).pipe(
        Effect.timeoutOrElse({
          duration: "30 seconds",
          orElse: () =>
            Effect.fail(
              dispatchFailure({
                cause: new Error("openswarm command timed out"),
                reason: sourceText("error.backend.dispatchTimeout"),
                step: "exit code",
                timedOut: true,
              }),
            ),
        }),
      ),
    (child) => endProcessTree(child).pipe(Effect.orDie),
  );
});
