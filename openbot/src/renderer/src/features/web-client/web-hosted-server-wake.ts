import { parseHostedServerStatus } from "@openbot/contracts/hosted-servers";
import { createHostedServerStatusCheck, createHostedServerWake } from "@openbot/team-client/hosted-server-wake";
import { Effect } from "effect";

/**
 * The hosted servers of this browser's account. When a connection fails, `unavailable` asks whether the
 * server sleeps, and starts it when it does not. A sleeping server starts with `wakeForInput`. `status`
 * reads the state of a server that starts, or null when the account server does not answer.
 */
export function createWebHostedServerWake(accountFetch: typeof fetch, now: () => number = Date.now) {
  const controller = new AbortController();
  const accountRequest = (hostId: string, action: "status" | "wake") =>
    Effect.tryPromise((signal) =>
      accountFetch(
        new URL(`/api/browser/v2/hosting/servers/${encodeURIComponent(hostId)}/${action}`, window.location.origin),
        action === "wake"
          ? {
              signal: AbortSignal.any([signal, controller.signal, AbortSignal.timeout(15_000)]),
              method: "POST",
              credentials: "same-origin",
              cache: "no-store",
              headers: { "Content-Type": "application/json", "X-OpenBot-Browser": "1" },
              body: "{}",
            }
          : {
              signal: AbortSignal.any([signal, controller.signal, AbortSignal.timeout(15_000)]),
              method: "GET",
              credentials: "same-origin",
              cache: "no-store",
              headers: { "X-OpenBot-Browser": "1" },
            },
      ),
    );
  const wake = createHostedServerWake((hostId) => accountRequest(hostId, "wake"), now);
  return {
    wake,
    status: (hostId: string) =>
      accountRequest(hostId, "status").pipe(
        Effect.flatMap((response) => (response.ok ? Effect.tryPromise(() => response.json()) : Effect.succeed(null))),
        Effect.map(parseHostedServerStatus),
        Effect.catch(() => Effect.succeed(null)),
      ),
    dispose: () => controller.abort(),
    ...createHostedServerStatusCheck((hostId) => accountRequest(hostId, "status"), wake, now),
  };
}
