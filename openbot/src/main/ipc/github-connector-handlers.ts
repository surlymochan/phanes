// The one GitHub connection of this computer: sign in with a device code, list the repositories it
// reaches, and sign out. The `host*` endpoints do the same for a joined server (`connectors-v1`).

import {
  decodeHostGitHubConnectorRepositories,
  decodeHostGitHubConnectorStatus,
  githubPage,
  withoutGitHubPages,
} from "@openbot/contracts/ipc";
import { CONNECTORS_CAPABILITY, CONNECTORS_ROUTES } from "@openbot/contracts/team-protocol/connectors-v1";
import type { TeamCurrentCapability } from "@openbot/contracts/team-protocol/current";
import { sourceText } from "@openbot/i18n/source";
import type { Effect } from "effect";
import { runCauseEffect } from "../../backend/effect-boundary";
import type { GitHubConnectorService } from "../github-connector-service";
import type { ResponseDecoder } from "../remote-host-decoding";
import type { RemoteRequestInit } from "../remote-server-client";
import type { RemoteWorkflowError } from "../remote-service-effects";
import { handler, type IpcGroupHandlers } from "./define-ipc-group";
import { scopedQueryHandler } from "./scoped-handler";

// Only the methods these endpoints call, so a test can pass a double without an assertion.
export interface GitHubConnectorIpcDependencies {
  githubConnector: Pick<
    GitHubConnectorService,
    "status" | "connect" | "cancel" | "disconnect" | "repositories" | "openVerification" | "openInstall"
  >;
  remoteServers: {
    supportsCapability(serverId: string, capability: TeamCurrentCapability): boolean;
    request<T>(
      serverId: string,
      path: string,
      decoder: ResponseDecoder<T>,
      init?: RemoteRequestInit,
    ): Effect.Effect<T, RemoteWorkflowError>;
  };
  /** Opens a GitHub page of a joined server's connection in this computer's browser. */
  openExternal: (url: string) => Promise<void>;
}

/**
 * No endpoint takes a payload but the server: the renderer cannot name a URL, a Client ID or a token.
 * The main process builds each GitHub address from the app that this build ships with. A joined
 * server names its own pages, so only an https page on github.com opens.
 */
export function githubConnectorIpcHandlers({
  githubConnector,
  remoteServers,
  openExternal,
}: GitHubConnectorIpcDependencies): Pick<IpcGroupHandlers, "githubConnector"> {
  function post<T>(serverId: string, path: string, decoder: ResponseDecoder<T>): Promise<T> {
    if (!remoteServers.supportsCapability(serverId, CONNECTORS_CAPABILITY))
      throw new Error(sourceText("error.team.connectorsUnsupported"));
    return runCauseEffect(remoteServers.request(serverId, path, decoder, { method: "POST", body: {} }));
  }
  const hostStatus = (serverId: string, path: string = CONNECTORS_ROUTES.githubStatus) =>
    post(serverId, path, decodeHostGitHubConnectorStatus);
  async function open(address: string | null): Promise<void> {
    const page = githubPage(address);
    if (page) await openExternal(page);
  }

  return {
    githubConnector: {
      status: handler(() => githubConnector.status()),
      connect: handler(() => runCauseEffect(githubConnector.connect(true))),
      cancel: handler(() => githubConnector.cancel()),
      disconnect: handler(() => runCauseEffect(githubConnector.disconnect(true))),
      repositories: handler(() => runCauseEffect(githubConnector.repositories())),
      openVerification: handler(() => runCauseEffect(githubConnector.openVerification())),
      openInstall: handler(() => runCauseEffect(githubConnector.openInstall())),
      hostStatus: scopedQueryHandler({
        local: () => githubConnector.status(),
        remote: async (serverId) => withoutGitHubPages(await hostStatus(serverId)),
      }),
      // The host opens no page, so this computer opens the code page, as a local connect does.
      hostConnect: scopedQueryHandler({
        local: () => runCauseEffect(githubConnector.connect(true)),
        remote: async (serverId) => {
          const status = await hostStatus(serverId, CONNECTORS_ROUTES.githubConnect);
          if (status.state === "pending") void open(status.verificationUri).catch(() => undefined);
          return withoutGitHubPages(status);
        },
      }),
      hostCancel: scopedQueryHandler({
        local: () => githubConnector.cancel(),
        remote: async (serverId) => withoutGitHubPages(await hostStatus(serverId, CONNECTORS_ROUTES.githubCancel)),
      }),
      // GitHub's revoke page opens only when a sign-in was stored, as a local disconnect does.
      hostDisconnect: scopedQueryHandler({
        local: () => runCauseEffect(githubConnector.disconnect(true)),
        remote: async (serverId) => {
          const before = await hostStatus(serverId);
          const status = await hostStatus(serverId, CONNECTORS_ROUTES.githubDisconnect);
          if (before.state === "connected" || before.state === "expired")
            void open(status.revokeUrl).catch(() => undefined);
          return withoutGitHubPages(status);
        },
      }),
      hostRepositories: scopedQueryHandler({
        local: () => runCauseEffect(githubConnector.repositories()),
        remote: (serverId) =>
          post(serverId, CONNECTORS_ROUTES.githubRepositories, decodeHostGitHubConnectorRepositories),
      }),
      hostOpenVerification: scopedQueryHandler({
        local: () => runCauseEffect(githubConnector.openVerification()),
        remote: async (serverId) => open((await hostStatus(serverId)).verificationUri),
      }),
      hostOpenInstall: scopedQueryHandler({
        local: () => runCauseEffect(githubConnector.openInstall()),
        remote: async (serverId) => open((await hostStatus(serverId)).installUrl),
      }),
    },
  };
}
