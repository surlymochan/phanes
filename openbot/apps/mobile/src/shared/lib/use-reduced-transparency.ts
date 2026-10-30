import { useSyncExternalStore } from "react";
import { AccessibilityInfo } from "react-native";
import { isAndroid } from "./platform";

let reduced: boolean | null = null;
let started = false;
const listeners = new Set<() => void>();

function update(value: boolean): void {
  if (reduced === value) return;
  reduced = value;
  for (const notify of listeners) notify();
}

function subscribe(listener: () => void) {
  if (!started) {
    started = true;
    void AccessibilityInfo.isReduceTransparencyEnabled().then(update, () => update(false));
    AccessibilityInfo.addEventListener("reduceTransparencyChanged", update);
  }
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

function snapshot(): boolean {
  // Android has no such setting and always answers false. Until iOS answers, controls draw solid.
  return isAndroid ? false : (reduced ?? true);
}

/**
 * Whether Reduce Transparency is on. The answer is kept for the app run, so only the first screen
 * that asks renders again when it arrives; a chat that opens later draws its controls at once.
 */
export function useReducedTransparency(): boolean {
  return useSyncExternalStore(subscribe, snapshot);
}
