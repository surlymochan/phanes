import {
  decodeWebMessagingInstallMark,
  decodeWebMessagingInstallReceipt,
  WEB_MESSAGING_INSTALL_CHANNEL,
  type WebMessagingInstallPlatform,
  webMessagingInstallKey,
} from "@openbot/contracts/web-messaging-install";

/** How long the page waits for the client tab's answer. The tab answers before any network call. */
const RECEIPT_TIMEOUT_MS = 3_000;

/**
 * Gives a Slack or Discord install's sealed grant to the browser client tab that started it. Null when
 * no tab of this browser marked the nonce: the page then opens the `openbot://` link. Otherwise true
 * when the tab took the grant, and false when no tab answered, for example after a reload.
 */
export function handOffToWebClient(
  platform: WebMessagingInstallPlatform,
  fragment: URLSearchParams,
): Promise<boolean> | null {
  const nonce = fragment.get("nonce");
  const grant = fragment.get("grant");
  if (!nonce || !grant) return null;
  try {
    const key = webMessagingInstallKey(nonce);
    const marked = decodeWebMessagingInstallMark(localStorage.getItem(key), Date.now());
    localStorage.removeItem(key);
    if (marked !== platform) return null;
  } catch {
    return null;
  }
  const channel = new BroadcastChannel(WEB_MESSAGING_INSTALL_CHANNEL);
  return new Promise<boolean>((resolve) => {
    const done = (received: boolean) => {
      clearTimeout(timer);
      channel.close();
      resolve(received);
    };
    const timer = setTimeout(() => done(false), RECEIPT_TIMEOUT_MS);
    channel.onmessage = (event) => {
      if (decodeWebMessagingInstallReceipt(event.data) === nonce) done(true);
    };
    channel.postMessage({ platform, nonce, grant });
  });
}
