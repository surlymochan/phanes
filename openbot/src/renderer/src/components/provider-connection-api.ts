/**
 * How a surface signs in a provider, as one optional prop.
 *
 * Grouped like `ProviderKeyApi` is for Settings, and for the same reason: onboarding, Settings, the
 * composer and the browser client all offer the sign-in and are rendered from stories and tests with
 * no provider behind them, so the whole feature is one object that is either there or not.
 *
 * The flow itself lives in the providers store. Everything here is a reader or a verb, because the
 * dialog that ends up on screen decides nothing: the phase arrives from main, and the ending
 * closes the dialog and becomes a notification the store raises.
 */

import type { AgentProviderId } from "@openbot/contracts/ipc";
import type { ProviderCodeLoginState } from "@openbot/ui/components/ProviderCodeLoginDialog";
import type { ProviderConnection } from "@openbot/ui/components/ProviderPicker";

export type ProviderCodeLoginOpener = "composer";

/** The hand-off to the OpenBot app, and the surface that opened it, as `opener` is for a sign-in. */
export interface ProviderUseApp {
  provider: AgentProviderId;
  opener: ProviderCodeLoginOpener | null;
}

/**
 * The ways in of every provider from where the user connects, and the dialog that a sign-in opens.
 * `connectKind` and `alternativeKind` follow `providerConnectRoute`, so a new provider needs only
 * its descriptor and the capabilities of the computer that runs it.
 */
export interface ProviderConnectionApi extends ProviderConnection {
  /** The provider whose dialog is open, or null while none is. */
  provider: () => AgentProviderId | null;
  state: () => ProviderCodeLoginState;
  /**
   * The surface that started the open sign-in and shows its own dialog for it. Null for Settings
   * and onboarding, which show theirs for any sign-in. A new code from the notification keeps it.
   */
  opener: () => ProviderCodeLoginOpener | null;
  /**
   * Runs the provider's Connect: the browser sign-in of this computer, or the dialog on a sign-in or
   * on the app hand-off. It rejects only when the browser sign-in fails to start.
   */
  connect: (provider: AgentProviderId, opener?: ProviderCodeLoginOpener) => Promise<void>;
  /** Runs the sign-in that the provider's ⋯ menu offers. */
  alternative: (provider: AgentProviderId, opener?: ProviderCodeLoginOpener) => void;
  /** The provider that only the OpenBot app can sign in from here, while that dialog is open. */
  useApp: () => ProviderUseApp | null;
  closeUseApp: () => void;
  /** Opens the page that downloads the OpenBot app. Only a browser tab, which can need the app, has it. */
  openAppDownload?: (() => void) | undefined;
  /** Sends the code the page of a `paste` sign-in showed. */
  submit: (code: string) => void;
  /** Gives up: the code stops working and the dialog closes. */
  cancel: () => void;
  /** Opens the verification page here, for the user who is on this computer after all. */
  openVerificationUrl: (url: string) => void;
  /** Opens the page of a relayed sign-in in this computer's browser, after the user checked its host. */
  openRelayPage: () => Promise<void>;
  /** Sends the address that the browser of a relayed sign-in shows. */
  pasteRelayAddress: (address: string) => Promise<void>;
}
