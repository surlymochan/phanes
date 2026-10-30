// The GitHub connection of the joined server in the browser client (`connectors-v1`). The host runs
// the device flow and opens no page; this browser opens GitHub's pages that the host names.

import { githubPage, type HostGitHubConnectorStatus, withoutGitHubPages } from "@openbot/contracts/ipc";
import type { CONNECTORS_ROUTES } from "@openbot/contracts/team-protocol/connectors-v1";
import { runTeamEffect } from "@openbot/team-client";
import {
  githubConnectorRepositories,
  githubConnectorRequest,
  type TeamAdminRequestError,
} from "@openbot/team-client/team-admin-requests";
import type { TeamApiRequest } from "@openbot/team-client/team-api-requests";
import { Effect } from "effect";
import { type GitHubConnectorPort, polledGitHubConnectorPort } from "../connectors/github-connector-port";

type StatusRoute = Exclude<keyof typeof CONNECTORS_ROUTES, "githubRepositories">;

/** `request` throws when no server is connected. */
export function webGitHubConnectorPort(request: () => TeamApiRequest): GitHubConnectorPort {
  /** The last answer, with the pages that the panel does not show. */
  let last: HostGitHubConnectorStatus | null = null;
  const run = <T>(effect: (request: TeamApiRequest) => Effect.Effect<T, TeamAdminRequestError>) =>
    runTeamEffect(Effect.suspend(() => effect(request())).pipe(Effect.mapError((error) => error.cause)));
  const send = async (route: StatusRoute) => {
    last = await run((target) => githubConnectorRequest(target, route));
    return last;
  };
  const open = (address: string | null) => {
    const page = githubPage(address);
    if (page) window.open(page, "_blank", "noopener,noreferrer");
  };
  /**
   * A browser blocks a page that the user's click did not open, and the page is known only when the
   * host answers. So the tab opens empty at the click, with no await before it, and goes to GitHub after.
   */
  const withTab = async (route: StatusRoute, page: (status: HostGitHubConnectorStatus) => string | null) => {
    const tab = window.open("about:blank", "_blank");
    if (tab) tab.opener = null;
    try {
      const status = await send(route);
      const address = githubPage(page(status));
      if (tab && address) tab.location.href = address;
      else tab?.close();
      return withoutGitHubPages(status);
    } catch (error) {
      tab?.close();
      throw error;
    }
  };
  return polledGitHubConnectorPort({
    status: async () => withoutGitHubPages(await send("githubStatus")),
    connect: () => withTab("githubConnect", (status) => (status.state === "pending" ? status.verificationUri : null)),
    cancel: async () => withoutGitHubPages(await send("githubCancel")),
    // GitHub's revoke page opens only when a sign-in was stored, as on the desktop.
    disconnect: async () => {
      const signedIn = last?.state === "connected" || last?.state === "expired";
      if (signedIn) return withTab("githubDisconnect", (status) => status.revokeUrl);
      return withoutGitHubPages(await send("githubDisconnect"));
    },
    repositories: () => run(githubConnectorRepositories),
    openVerification: async () => open(last?.verificationUri ?? null),
    openInstall: async () => open(last?.installUrl ?? null),
  });
}
