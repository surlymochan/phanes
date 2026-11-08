import type { UpdateStatus } from "@volto/contracts/ipc";
import { classifyFailure } from "@volto/telemetry";
import { toast } from "@volto/ui";
import { currentText } from "@volto/ui/text";
import { type Accessor, createEffect, onCleanup } from "solid-js";
import { actionToast } from "../../action-toast";

const TOAST_ID = "scheduled-app-update";

/**
 * A notice on the host while a restart that a server admin asked for, or the automatic install
 * scheduled, waits. It stays until the
 * restart starts or someone cancels it, so the host user always knows why Volto will restart.
 */
export function createScheduledUpdateToast(options: {
  status: Accessor<UpdateStatus>;
  cancel: () => Promise<void>;
}): void {
  createEffect(
    () => {
      const restart = options.status().scheduledRestart;
      return restart && options.status().phase !== "installing"
        ? { requestedBy: restart.requestedBy, mode: restart.mode }
        : null;
    },
    (restart) => {
      if (!restart) {
        toast.dismiss(TOAST_ID);
        return;
      }
      const { t } = currentText();
      const title =
        restart.requestedBy === null
          ? t("update.scheduled.automaticTitle")
          : t("update.scheduled.title", { name: restart.requestedBy });
      toast(title, {
        id: TOAST_ID,
        description: restart.mode === "now" ? t("update.scheduled.now") : t("update.scheduled.whenIdle"),
        duration: Number.POSITIVE_INFINITY,
        dismissible: false,
        action: {
          label: t("update.scheduled.cancel"),
          onClick: () => {
            options.cancel().catch((error: unknown) => {
              const text = currentText();
              actionToast.error(text.errorMessage(error, text.t("update.scheduled.cancelFailed")), {
                report: { operation: "update", source: "action", cause_code: classifyFailure(error) },
              });
            });
          },
        },
      });
    },
  );
  onCleanup(() => toast.dismiss(TOAST_ID));
}
