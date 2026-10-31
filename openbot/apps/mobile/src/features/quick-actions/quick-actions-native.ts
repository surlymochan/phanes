import { type NativeModule, requireOptionalNativeModule } from "expo";
import { supportLog, supportLogValue } from "@/features/support/model/support-log";
import { type QuickActionItem, type QuickActionTarget, readQuickActionId } from "./model/quick-action-items";

declare class QuickActionsModule extends NativeModule<{ onQuickAction: (event: { id: string }) => void }> {
  /** Returns false when the phone keeps the old items. */
  setItems(items: QuickActionItem[]): Promise<boolean>;
}

/** `modules/quick-actions`. A build without it, such as Expo Go, has no quick actions. */
const native = requireOptionalNativeModule<QuickActionsModule>("OpenBotQuickActions");

let listening = false;
let navigate: ((target: QuickActionTarget) => void) | null = null;
let pending: QuickActionTarget | null = null;
/** An Android shortcut that the user pinned stays without a session. Its action must not wait for the next account. */
let signedOut = false;
/** The items that the phone has. Android limits the updates of an app in the background. */
let published: string | null = null;

/** Hears the chosen actions from the start, so the action that starts the app waits for the screens. */
export function installQuickActions(): void {
  if (listening || !native) return;
  listening = true;
  native.addListener("onQuickAction", ({ id }) => {
    const target = readQuickActionId(id);
    if (!target || signedOut) return;
    if (navigate) navigate(target);
    else pending = target;
  });
}

/** Opens the chosen actions while the signed-in screens show. An action that came before opens now. */
export function setQuickActionNavigator(open: (target: QuickActionTarget) => void): () => void {
  signedOut = false;
  navigate = open;
  const waiting = pending;
  pending = null;
  if (waiting) open(waiting);
  return () => {
    if (navigate === open) navigate = null;
  };
}

export async function publishQuickActions(items: readonly QuickActionItem[]): Promise<void> {
  const value = JSON.stringify(items);
  if (!native || value === published) return;
  published = value;
  let saved = false;
  try {
    saved = await native.setItems([...items]);
  } catch (error) {
    supportLog.add("warn", "app", `Quick actions not saved: ${supportLogValue(error)}`);
  }
  // The next change tries again.
  if (!saved && published === value) published = null;
}

/** Without a session the icon offers nothing, and an action chosen before does not open after sign-in. */
export function resetQuickActions(): void {
  signedOut = true;
  pending = null;
  void publishQuickActions([]);
}
