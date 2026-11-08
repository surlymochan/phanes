import type { VoltoDesktopApi, ServerSummary } from "@volto/contracts/ipc";

/**
 * What the servers domain reaches in main: the server list and selection, the account changes, the local host, the
 * settings of one server, the name, logo and update of a joined server's host, and the remote desktop
 * setup of a server.
 */
export interface ServersPort {
  agent: Pick<
    VoltoDesktopApi["agent"],
    | "getStatus"
    | "listAgents"
    | "listConversationReads"
    | "listHiddenAgents"
    | "listMcpServers"
    | "listModels"
    | "removeMcpServer"
    | "saveMcpServer"
    | "setMcpServerEnabled"
    | "testMcpServer"
    | "signInMcpServer"
    | "mcpSignInPage"
    | "mcpSignInRelay"
    | "openMcpSignInRelay"
    | "completeMcpSignInRelay"
    | "cancelMcpSignIn"
    | "signOutMcpServer"
    | "listMcpSignIns"
    | "mcpSignInApp"
    | "saveMcpSignInApp"
    | "removeMcpSignInApp"
  >;
  events: VoltoDesktopApi["events"];
  auth: Pick<VoltoDesktopApi["auth"], "onEvent">;
  browser: Pick<VoltoDesktopApi["browser"], "setVisible">;
  hostAdmin: Pick<
    VoltoDesktopApi["hostAdmin"],
    "cancelUpdate" | "getUpdateStatus" | "startUpdate" | "updateIdentity"
  >;
  hostedServers: Pick<VoltoDesktopApi["hostedServers"], "list" | "restart" | "wake">;
  host: Pick<
    VoltoDesktopApi["host"],
    | "configure"
    | "getStatus"
    | "onEvent"
    | "recheckScreenRecording"
    | "rotateIdentityKey"
    | "start"
    | "stop"
    | "updateIdentity"
  >;
  notifications: Pick<VoltoDesktopApi["notifications"], "onOpened">;
  remoteDesktop: Pick<
    VoltoDesktopApi["remoteDesktop"],
    "checkSetup" | "connect" | "disconnect" | "list" | "openSetup" | "test"
  >;
  servers: Pick<
    VoltoDesktopApi["servers"],
    | "getPresenceFor"
    | "join"
    | "list"
    | "onEvent"
    | "onPresence"
    | "refreshIdentity"
    | "remove"
    | "reorder"
    | "retryConnection"
    | "select"
    | "setMuted"
    | "setNotificationLevel"
  >;
}

/** Read on each call: tests and stories replace `window.volto` per case. */
export function serversPort(): ServersPort {
  return window.volto;
}

/**
 * The member and invite calls for one server. This computer answers them for the local server
 * through `host`; a remote server answers them through `servers`, addressed by its id.
 */
export type ServerAdminPort = Pick<
  VoltoDesktopApi["host"],
  "getPresence" | "listMembers" | "listInvites" | "createInvite" | "updateMember" | "removeMember" | "revokeInvite"
>;

export function serverAdminPort(server: Pick<ServerSummary, "id" | "kind">): ServerAdminPort {
  if (server.kind === "local") return window.volto.host;
  const serverId = server.id;
  return {
    getPresence: () => window.volto.servers.getPresenceFor(serverId),
    listMembers: () => window.volto.servers.listMembers(serverId),
    listInvites: () => window.volto.servers.listInvites(serverId),
    createInvite: (input) => window.volto.servers.createInvite(input, serverId),
    updateMember: (input) => window.volto.servers.updateMember(input, serverId),
    removeMember: (memberId) => window.volto.servers.removeMember(memberId, serverId),
    revokeInvite: (inviteId) => window.volto.servers.revokeInvite(inviteId, serverId),
  };
}

/** The server main marks active. A test teardown can remove the bridge while a join is pending. */
export async function activeServerId(): Promise<string> {
  if (!window.volto) return "local";
  return (await window.volto.servers.list()).find((item) => item.active)?.id ?? "local";
}
