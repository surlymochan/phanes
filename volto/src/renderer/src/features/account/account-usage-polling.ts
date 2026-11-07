import { createEffect, createMemo, untrack } from "solid-js";
import { useAgents } from "../agents/agents-context";
import { useServers } from "../servers/servers-context";
import { useAuth } from "./account-context";
import { createUsageRefreshPoll } from "./usage-refresh-poll";

/**
 * Selects the usage target of the active server and keeps its plan limits fresh
 * for the rail rings.
 */
export function useAccountUsagePolling() {
  const auth = useAuth();
  const { agentStatus, refreshHostAccountUsage } = useAgents();
  const { activeServerId } = useServers();
  const usageReady = createMemo(() => {
    const status = agentStatus();
    return (
      status.phase === "ready" ||
      Boolean(status.providers?.some((item) => item.state === "available" && item.connectionState !== "connecting"))
    );
  });
  const usageTargetKey = createMemo(() => {
    const serverId = activeServerId();
    if (!serverId) return null;
    const connected = (agentStatus().providers ?? [])
      .filter((item) => item.state === "available" && item.connectionState !== "connecting")
      .map((item) => item.id)
      .sort()
      .join(",");
    return `${serverId}:${connected}`;
  });
  /** A provider still starts or checks its sign-in, so a new target and its reading follow. */
  const providersLoading = createMemo(() => {
    const status = agentStatus();
    return (
      status.phase === "starting" ||
      status.phase === "restarting" ||
      Boolean(status.providers?.some((item) => item.state === "checking" || item.connectionState === "connecting"))
    );
  });
  let selectedServerId: string | null = null;

  /* A reading belongs to one server. On that server, the reading stays until the refresh for the new target
   * replaces it, so the rail rings do not blink while models load. */
  createEffect(
    () => usageTargetKey(),
    (targetKey) =>
      untrack(() => {
        const serverId = activeServerId() ?? null;
        if (serverId !== selectedServerId) auth.invalidateAccountUsage();
        selectedServerId = serverId;
        auth.selectAccountUsageTarget(targetKey);
      }),
  );
  /* With no provider ready or loading, no refresh follows, so the reading goes. */
  createEffect(
    () => usageReady() || providersLoading(),
    (refreshFollows) => {
      if (!refreshFollows) untrack(auth.invalidateAccountUsage);
    },
  );

  /* The backend reads only the providers that are available. While a reading shows, a read during the checks
   * would replace it with part of it, so the poll waits for the checks to end. With no reading, it reads at once. */
  const hasReading = createMemo(() => (auth.accountUsage()?.limits.length ?? 0) > 0);
  createUsageRefreshPoll({
    target: () => (usageReady() && !(hasReading() && providersLoading()) ? usageTargetKey() : null),
    refreshAtStart: true,
    refresh: (targetKey) => {
      void refreshHostAccountUsage().catch(() => undefined);
      void auth.refreshAccountUsage(targetKey).catch(() => undefined);
    },
  });
}
