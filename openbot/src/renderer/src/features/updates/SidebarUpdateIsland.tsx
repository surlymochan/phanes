import { AccountUpdateIsland } from "@openbot/ui/features/account/AccountUpdateIsland";
import { useText } from "@openbot/ui/text";
import { createEffect, createSignal } from "solid-js";
import { useUpdates } from "./updates-context";

/**
 * The update island of the old account box, at the end of the sidebar. It shows only while an update
 * is available, downloads or waits for a restart, and keeps the failure of the last action until the
 * updater moves to another phase.
 */
export function SidebarUpdateIsland() {
  const { t, errorMessage } = useText();
  const updates = useUpdates();
  const [updateError, setUpdateError] = createSignal<string | null>(null);

  createEffect(
    () => updates.status().phase,
    () => {
      setUpdateError(null);
    },
  );

  async function runUpdateAction(): Promise<void> {
    setUpdateError(null);
    try {
      await updates.runAction();
    } catch (cause) {
      setUpdateError(errorMessage(cause, t("account.menu.updateFailed")));
    }
  }

  return (
    <div class="sidebar-update-island">
      <AccountUpdateIsland
        updateStatus={updates.status()}
        errorMessage={updateError()}
        onUpdateAction={runUpdateAction}
      />
    </div>
  );
}
