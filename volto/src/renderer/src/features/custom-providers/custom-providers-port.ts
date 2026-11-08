import type { CustomProvidersDesktopApi, VoltoDesktopApi, ProviderAdminDesktopApi } from "@volto/contracts/ipc";

/**
 * What the custom providers context reaches in main: the endpoints the user named.
 */
export interface CustomProvidersPort {
  customProviders: VoltoDesktopApi["customProviders"];
}

/**
 * The endpoint calls of one host. `update` is local only: the Team API has no update, so the list of
 * a joined server offers no Edit.
 */
export type CustomProvidersEndpointApi = Omit<CustomProvidersDesktopApi, "update"> &
  Partial<Pick<CustomProvidersDesktopApi, "update">>;

/** Read on each call: tests and stories replace `window.volto` per case. */
export function customProvidersPort(): CustomProvidersPort {
  return window.volto;
}

/** The endpoints of a joined server's host, for an account that administers it. */
export function hostCustomProvidersApi(
  admin: () => ProviderAdminDesktopApi,
  serverId: string,
): CustomProvidersEndpointApi {
  return {
    list: () => admin().listCustomProviders(serverId),
    save: (input) => admin().saveCustomProvider(input, serverId),
    delete: (input) => admin().deleteCustomProvider(input, serverId),
  };
}

/** What the detection store reaches in main: the scans of this computer and their settings. */
export interface ProviderDetectionPort {
  providerDetection: VoltoDesktopApi["providerDetection"];
}

/** Read on each call, like `customProvidersPort`. */
export function providerDetectionPort(): ProviderDetectionPort {
  return window.volto;
}
