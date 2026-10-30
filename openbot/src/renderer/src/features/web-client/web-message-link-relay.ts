// The sign-in links of the host's agents in the browser client (`sign-in-relay-v1`). The click opens the
// link as before. When the host relays it, the conversation shows a field for the address that the
// browser shows at the end, because a browser cannot listen for the callback.

import {
  type McpSignInRelayPage,
  type McpTestResult,
  type OpenBotDesktopApi,
  relayableSignInLink,
} from "@openbot/contracts/ipc";
import { sourceText } from "@openbot/i18n/source";
import type { TeamApiRequest } from "@openbot/team-client/team-api-requests";
import { openWebLink } from "./web-attachments";
import { webMcpRelay } from "./web-mcp-relay";

export type WebMessageLinks = Pick<
  OpenBotDesktopApi["agent"],
  "openMessageLink" | "pasteMessageLink" | "cancelMessageLink"
>;

/** `enabled` answers whether the host relays sign-in links and the member can start one. */
export function webMessageLinks(request: () => TeamApiRequest, enabled: () => boolean): WebMessageLinks {
  const shown = new Map<string, (page: McpSignInRelayPage | null) => void>();
  const running = new Map<string, Promise<McpTestResult>>();
  const relay = webMcpRelay((url, page) => {
    if (page) shown.get(url)?.(page);
  });

  return {
    openMessageLink: async ({ agentId, url }) => {
      // The click opens the page, so the browser does not block it. The relay only adds the way back.
      await openWebLink(url);
      const page = relayableSignInLink(url);
      if (!page || !enabled()) return null;
      relay.cancel(page);
      const done = relay.signInLink(request(), agentId, page);
      running.set(page, done);
      const forget = () => {
        if (running.get(page) === done) running.delete(page);
      };
      done.then(forget, forget);
      let show: (view: McpSignInRelayPage | Error | null) => void = () => undefined;
      const view = await new Promise<McpSignInRelayPage | Error | null>((resolve) => {
        show = resolve;
        shown.set(page, resolve);
        // A host that does not relay the link, such as for a member who is not an admin, leaves the
        // page as it opened.
        done.then(
          (result) => resolve(result.error === null ? null : new Error(result.error)),
          () => resolve(null),
        );
      });
      if (shown.get(page) === show) shown.delete(page);
      if (view instanceof Error) throw view;
      return view;
    },
    pasteMessageLink: async ({ url, address }) => {
      const page = relayableSignInLink(url);
      const done = page ? running.get(page) : undefined;
      if (!page || !done) throw new Error(sourceText("error.mcp.relayEnded"));
      await relay.paste(page, address);
      const result = await done;
      if (result.error !== null) throw new Error(result.error);
    },
    cancelMessageLink: async ({ url }) => {
      const page = relayableSignInLink(url);
      if (page) relay.cancel(page);
    },
  };
}
