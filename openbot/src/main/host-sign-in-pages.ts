// The pages of sign-ins that an administrator starts on this host from another computer. The page opens
// in a private tab and the administrator finishes through its live view (`mcp-sign-in-v1`,
// `provider-host-sign-in-v1`), or the host only records the page address and the administrator's own
// computer opens it and relays the callback (`sign-in-relay-v1`).

import { createOpenBotLogger, toLogValue } from "@openbot/logging";
import { Duration, Effect, Exit, Fiber, Scope } from "effect";
import type { BrowserHost } from "../backend/browser-host";
import { runCauseEffect } from "../backend/effect-boundary";

const logger = createOpenBotLogger("host-sign-in-pages");

/** How long a finished sign-in still answers `status`, so the client that polls it reads the end. */
const RESULT_KEPT_MS = 60_000;

/** What `status` answers: the tab with the page while it is open, and how the sign-in ended. */
export interface HostSignInPage<R> {
  tabId: string | null;
  result: R | null;
}

/** How a sign-in shows its page: in a private tab here, or only as an address that a client opens. */
export type HostSignInPageMode = "tab" | "relay";

interface HostSignIn<R> extends HostSignInPage<R> {
  mode: HostSignInPageMode;
  /** The page address, in `relay` mode, once the sign-in asked to open it. */
  pageUrl: string | null;
  /** The sign-in. `#finish` waits for it apart, so an interrupt still ends the sign-in. */
  run?: Fiber.Fiber<unknown, unknown>;
}

/**
 * The grant comes back to a loopback listener on this host, which only a browser here can reach. So
 * the page opens in a private tab of this host's browser. One sign-in runs for each key, as on this
 * computer: a second start replaces the first. Every ending gives a result, so the client stops
 * reading `status` and the tab closes.
 */
export class HostSignInPages<K, R> {
  readonly #browser: Pick<BrowserHost, "openPrivate" | "privateTab" | "close">;
  readonly #signIns = new Map<K, HostSignIn<R>>();
  readonly #scope = Scope.makeUnsafe();

  constructor(browser: Pick<BrowserHost, "openPrivate" | "privateTab" | "close">) {
    this.#browser = browser;
  }

  /** Starts `run`, which opens its page with the opener it receives, and returns at once. */
  readonly start = Effect.fn("HostSignInPages.start")(function* <A, E>(
    this: HostSignInPages<K, R>,
    key: K,
    run: (openPage: (url: string) => Promise<void>) => Effect.Effect<A, E>,
    resultOf: (exit: Exit.Exit<A, E>) => R,
    mode: HostSignInPageMode = "tab",
  ) {
    const signIn: HostSignIn<R> = { mode, tabId: null, pageUrl: null, result: null };
    this.#signIns.set(key, signIn);
    const fiber = yield* Effect.forkIn(
      run((url) => runCauseEffect(this.#show(key, signIn, url))),
      this.#scope,
      { startImmediately: true },
    );
    signIn.run = fiber;
    yield* Effect.forkIn(
      Fiber.await(fiber).pipe(Effect.flatMap((exit) => this.#finish(key, signIn, resultOf(exit)))),
      this.#scope,
    );
  });

  /** The page address of a `relay` sign-in of `key`; `null` until the sign-in asked to open it. */
  pageUrl(key: K): string | null {
    const signIn = this.#signIns.get(key);
    return signIn?.mode === "relay" && signIn.result === null ? signIn.pageUrl : null;
  }

  /** Undefined when no sign-in of `key` runs or ended a short time ago. */
  status(key: K): HostSignInPage<R> | undefined {
    const signIn = this.#signIns.get(key);
    if (!signIn) return undefined;
    // A sign-in can continue in a popup; the live view follows it and returns when it closes.
    return { tabId: signIn.tabId && this.#browser.privateTab(signIn.tabId), result: signIn.result };
  }

  /** Stops the sign-in of `key`. It ends as interrupted, and its page closes. */
  readonly interrupt = Effect.fn("HostSignInPages.interrupt")(function* (this: HostSignInPages<K, R>, key: K) {
    const run = this.#signIns.get(key)?.run;
    if (run) yield* Fiber.interrupt(run);
  });

  readonly close = Effect.fn("HostSignInPages.close")(function* (this: HostSignInPages<K, R>) {
    yield* Scope.close(this.#scope, Exit.void);
  });

  /** The page opens; a sign-in that a later one replaced or that already ended closes it again. */
  readonly #show = Effect.fn("HostSignInPages.show")(function* (
    this: HostSignInPages<K, R>,
    key: K,
    signIn: HostSignIn<R>,
    url: string,
  ) {
    if (signIn.mode === "relay") {
      signIn.pageUrl = url;
      return;
    }
    const tab = yield* this.#browser.openPrivate(url);
    if (this.#signIns.get(key) === signIn && signIn.result === null) {
      signIn.tabId = tab.id;
      return;
    }
    yield* this.#closeTab(tab.id);
  });

  readonly #finish = Effect.fn("HostSignInPages.finish")(function* (
    this: HostSignInPages<K, R>,
    key: K,
    signIn: HostSignIn<R>,
    result: R,
  ) {
    signIn.result = result;
    const tabId = signIn.tabId;
    signIn.tabId = null;
    if (tabId) yield* this.#closeTab(tabId);
    yield* Effect.sleep(Duration.millis(RESULT_KEPT_MS));
    if (this.#signIns.get(key) === signIn) this.#signIns.delete(key);
  });

  #closeTab(tabId: string) {
    return this.#browser
      .close(tabId)
      .pipe(
        Effect.catch((error) =>
          Effect.sync(() => logger.warn("Unable to close a sign-in tab", { error: toLogValue(error) })),
        ),
      );
  }
}
