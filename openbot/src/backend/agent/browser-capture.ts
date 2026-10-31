// A `BROWSER` command for a provider CLI whose sign-in page must not open in a browser on this computer.

import { chmod, mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { registerSecretValue } from "@openbot/logging";
import { Duration, Effect } from "effect";
import { CliLoginFailed } from "./cli-login-flow";

/** How often the page file is read while the CLI has not opened its page. */
const POLL = Duration.millis(250);

/**
 * The environment that makes a CLI give its sign-in page to `openPage` in place of a browser here.
 *
 * A CLI that honors `BROWSER` runs it with the page address. This command writes the address to a file
 * in a private directory, and the first https address in that file goes to `openPage`. The address
 * holds the sign-in's `state`, so it is registered as a secret, and the directory is removed when the
 * scope closes. A CLI that does not honor `BROWSER` opens its own browser, and `openPage` gets nothing.
 */
export const captureBrowser = Effect.fn("captureBrowser")(function* (openPage: (url: string) => Promise<void>) {
  const directory = yield* Effect.acquireRelease(
    Effect.tryPromise({
      try: () => mkdtemp(join(tmpdir(), "openbot-sign-in-")),
      catch: (cause) => new CliLoginFailed({ cause }),
    }),
    (path) => Effect.promise(() => rm(path, { recursive: true, force: true })),
  );
  const page = join(directory, "page");
  const windows = process.platform === "win32";
  const command = join(directory, windows ? "browser.cmd" : "browser.sh");
  yield* Effect.tryPromise({
    try: async () => {
      await writeFile(command, windows ? windowsScript(page) : posixScript(page), { mode: 0o700 });
      await chmod(command, 0o700);
    },
    catch: (cause) => new CliLoginFailed({ cause }),
  });
  yield* Effect.forkScoped(
    Effect.gen(function* () {
      while (true) {
        const url = yield* Effect.promise(() => readFile(page, "utf8").then(firstPage, () => null));
        if (url) {
          registerSecretValue(url);
          return yield* Effect.promise(() => openPage(url).catch(() => undefined));
        }
        yield* Effect.sleep(POLL);
      }
    }),
  );
  return { BROWSER: command };
});

function firstPage(text: string): string | null {
  for (const line of text.split(/\r?\n/u)) {
    const url = URL.parse(line.trim());
    if (url?.protocol === "https:" && !url.username && !url.password) return url.toString();
  }
  return null;
}

function posixScript(page: string): string {
  return `#!/bin/sh\nprintf '%s\\n' "$1" >> '${page.replaceAll("'", `'\\''`)}'\n`;
}

/** Delayed expansion keeps `&` and `%` of the address out of the parse of the line that writes it. */
function windowsScript(page: string): string {
  return `@echo off\r\nsetlocal EnableDelayedExpansion\r\nset "address=%~1"\r\n>>"${page}" echo(!address!\r\n`;
}
