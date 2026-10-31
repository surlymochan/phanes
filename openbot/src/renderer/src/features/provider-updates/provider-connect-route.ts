import { type AgentProviderId, agentProviderDescriptor, type ServerSummary } from "@openbot/contracts/ipc";
import {
  hostSignInProviders,
  localCodeSignInProviders,
  relaySignInProviders,
  serverCodeRouteProviders,
} from "../servers/server-capabilities";

/**
 * How one provider row signs in.
 *
 * - `browser`: this computer's browser, through the provider's own sign-in.
 * - `relay`: a page in this computer's browser, with the callback sent on to the host (`sign-in-relay-v1`).
 * - `host-page`: a page in the host's browser, which the live view shows here.
 * - `code`: the host's or this computer's code route: a device code, a pasted code or a link.
 * - `use-app`: nothing in a browser tab can finish it; the desktop app can.
 */
export type ProviderSignInMethod = "browser" | "relay" | "host-page" | "code" | "use-app";

/** The methods that the sign-in dialog runs. */
export type ProviderCodeLoginMethod = Extract<ProviderSignInMethod, "relay" | "host-page" | "code">;

/** Where the user connects a provider from, and the computer that runs it. */
export type ProviderConnectPlace =
  | { kind: "computer"; platform: string | undefined }
  | { kind: "app-server" | "web-server"; server: ServerSummary | undefined };

/** What the row's Connect does, and what its ⋯ menu offers instead. Null when the row has none. */
export interface ProviderConnectRoute {
  connect: ProviderSignInMethod | null;
  alternative: ProviderSignInMethod | null;
}

const NO_ROUTE: ProviderConnectRoute = { connect: null, alternative: null };

/**
 * The one decision every surface makes for a provider row: onboarding, Settings, Server Settings,
 * the composer and the model picker, in the app and on the web. Connect takes the way that needs
 * nothing copied: the callback when one can reach the provider, otherwise what the place can run.
 * The ⋯ menu offers a second way only when it is a different one.
 *
 * It reads only the provider's descriptor and the server's capabilities, so a new provider needs
 * no change here: its descriptor row and the protocol lists that name it decide its route.
 * A null place is a server that this window cannot sign in, such as one where the account is a
 * member.
 */
export function providerConnectRoute(
  provider: AgentProviderId,
  place: ProviderConnectPlace | null,
): ProviderConnectRoute {
  if (!place) return NO_ROUTE;
  if (place.kind === "computer") {
    return {
      connect: "browser",
      alternative: localCodeSignInProviders(place.platform).includes(provider) ? "code" : null,
    };
  }
  const code = serverCodeRouteProviders(place.server).includes(provider);
  const hostPage = hostSignInProviders(place.server).includes(provider);
  const relay = relaySignInProviders(place.server).includes(provider);
  if (place.kind === "app-server") {
    // This computer takes the callback, so the relay needs nothing from the user.
    const connect = relay ? "relay" : hostPage ? "host-page" : code ? "code" : null;
    return { connect, alternative: connect !== "code" && code ? "code" : null };
  }
  // A browser tab cannot take a callback: the relay there needs the address pasted back, unless the
  // user finishes on the page itself.
  const connect = code
    ? "code"
    : hostPage
      ? "host-page"
      : relay && agentProviderDescriptor(provider).signInPageEnd === "page"
        ? "relay"
        : relay
          ? "use-app"
          : null;
  return { connect, alternative: connect !== "relay" && relay ? "relay" : null };
}
