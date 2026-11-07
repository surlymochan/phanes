import { AppState } from "react-native";

let reached = AppState.currentState !== "background";

/**
 * Resolves when the user has opened the app once. iOS can start the app in the background, also while
 * the phone is locked. A keychain item that only an unlocked phone opens then fails to load, and a
 * store would keep its default. Such reads wait for this.
 */
export function untilForeground(): Promise<void> {
  if (reached || AppState.currentState === "active") {
    reached = true;
    return Promise.resolve();
  }
  return new Promise((resolve) => {
    const subscription = AppState.addEventListener("change", (state) => {
      if (state !== "active") return;
      reached = true;
      subscription.remove();
      resolve();
    });
  });
}
