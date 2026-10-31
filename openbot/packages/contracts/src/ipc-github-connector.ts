import { isBoolean, isDynamicRecord, isNumber, isString } from "./runtime-values";

/**
 * The built-in GitHub connection of this computer. One sign-in to the GitHub App "OpenBotGit", shared
 * by every agent on this computer. The token never crosses to the renderer.
 *
 * - `disconnected`: no sign-in is stored.
 * - `pending`: the device flow waits for the user to type `userCode` at `verificationUri`.
 * - `connected`: agents get the GitHub MCP server and a token for `gh` and `git`.
 * - `expired`: the stored sign-in cannot refresh. The user must connect again.
 */
export type GitHubConnectorState = "disconnected" | "pending" | "connected" | "expired";

function isGitHubConnectorState(value: unknown): value is GitHubConnectorState {
  return value === "disconnected" || value === "pending" || value === "connected" || value === "expired";
}

export interface GitHubConnectorStatus {
  /** False when this build has no GitHub App Client ID. The UI then shows no connect button. */
  available: boolean;
  state: GitHubConnectorState;
  login: string | null;
  avatarUrl: string | null;
  userCode: string | null;
  verificationUri: string | null;
  /** The last sign-in failure, as a sentence. Null after a success or a cancel. */
  error: string | null;
}

export const DISCONNECTED_GITHUB_CONNECTOR: GitHubConnectorStatus = {
  available: false,
  state: "disconnected",
  login: null,
  avatarUrl: null,
  userCode: null,
  verificationUri: null,
  error: null,
};

function isNullableString(value: unknown): value is string | null {
  return value === null || isString(value);
}

/** Returns null for a value that is not a connector status in the desktop shape. */
export function parseGitHubConnectorStatus(value: unknown): GitHubConnectorStatus | null {
  if (
    !isDynamicRecord(value) ||
    !isBoolean(value.available) ||
    !isGitHubConnectorState(value.state) ||
    !isNullableString(value.login) ||
    !isNullableString(value.avatarUrl) ||
    !isNullableString(value.userCode) ||
    !isNullableString(value.verificationUri) ||
    !isNullableString(value.error)
  ) {
    return null;
  }
  return {
    available: value.available,
    state: value.state,
    login: value.login,
    avatarUrl: value.avatarUrl,
    userCode: value.userCode,
    verificationUri: value.verificationUri,
    error: value.error,
  };
}

export interface GitHubConnectorRepository {
  /** `owner/name`. */
  fullName: string;
  private: boolean;
}

/** The repositories that the connection reaches: those where the GitHub App is installed for the user. */
export interface GitHubConnectorRepositories {
  /** Sorted by `fullName`. At most a few hundred, so `total` can be larger. */
  repositories: GitHubConnectorRepository[];
  total: number;
}

function parseGitHubConnectorRepository(value: unknown): GitHubConnectorRepository | null {
  if (!isDynamicRecord(value) || !isString(value.fullName) || !isBoolean(value.private)) return null;
  return { fullName: value.fullName, private: value.private };
}

/** Returns null for a value that is not a repository list in the desktop shape. */
export function parseGitHubConnectorRepositories(value: unknown): GitHubConnectorRepositories | null {
  if (!isDynamicRecord(value) || !Array.isArray(value.repositories) || !isNumber(value.total)) return null;
  const repositories: GitHubConnectorRepository[] = [];
  for (const item of value.repositories) {
    const repository = parseGitHubConnectorRepository(item);
    if (!repository) return null;
    repositories.push(repository);
  }
  return { repositories, total: value.total };
}

/**
 * The status of a joined server's connection (`connectors-v1`), with the GitHub pages that its client
 * opens: the host opens no page. Both are `null` when the host has no GitHub App.
 */
export interface HostGitHubConnectorStatus extends GitHubConnectorStatus {
  installUrl: string | null;
  revokeUrl: string | null;
}

/** Returns null for a value that is not a host connector status. */
export function parseHostGitHubConnectorStatus(value: unknown): HostGitHubConnectorStatus | null {
  const status = parseGitHubConnectorStatus(value);
  if (!status || !isDynamicRecord(value) || !isNullableString(value.installUrl) || !isNullableString(value.revokeUrl))
    return null;
  return { ...status, installUrl: value.installUrl, revokeUrl: value.revokeUrl };
}

/** Throws for a value that is not a host connector status. */
export function decodeHostGitHubConnectorStatus(value: unknown): HostGitHubConnectorStatus {
  const status = parseHostGitHubConnectorStatus(value);
  if (!status) throw new Error("Invalid GitHub connector response.");
  return status;
}

/** Throws for a value that is not a repository list. */
export function decodeHostGitHubConnectorRepositories(value: unknown): GitHubConnectorRepositories {
  const repositories = parseGitHubConnectorRepositories(value);
  if (!repositories) throw new Error("Invalid GitHub connector response.");
  return repositories;
}

/** The status without the pages, for the panel. */
export function withoutGitHubPages(status: HostGitHubConnectorStatus): GitHubConnectorStatus {
  const { available, state, login, avatarUrl, userCode, verificationUri, error } = status;
  return { available, state, login, avatarUrl, userCode, verificationUri, error };
}

/**
 * `address` when it is an https page on github.com, otherwise null. A joined server names the pages
 * that its client opens, so the client opens no other site.
 */
export function githubPage(address: string | null): string | null {
  const url = address === null ? null : URL.parse(address);
  return url?.protocol === "https:" && url.hostname === "github.com" && url.port === "" ? url.toString() : null;
}
