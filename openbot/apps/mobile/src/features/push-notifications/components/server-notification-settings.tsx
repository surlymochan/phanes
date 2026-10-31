import { SERVER_MUTE_DURATIONS_MS, type ServerNotificationLevel } from "@openbot/contracts/ipc";
import type { MobileTextKey } from "@openbot/i18n/mobile";
import { Typography } from "heroui-native";
import { useState } from "react";
import { useUniwind } from "uniwind";
import { SettingsRow, SettingsSection } from "@/features/settings/components/settings-content";
import { SettingsPicker } from "@/features/settings/components/settings-controls";
import { useLiveActivitiesPreference } from "@/features/settings/model/live-activities";
import {
  mutedUntil,
  type ServerNotificationSetting,
  saveServerNotificationSetting,
  serverNotificationSetting,
  usePushNotificationPreference,
} from "@/features/settings/model/push-notifications";
import { useMobileWorkspace } from "@/features/workspace/context/mobile-workspace-context";
import type { MobileServer } from "@/features/workspace/model/workspace-types";
import { haptics } from "@/shared/lib/haptics";
import { isIOS } from "@/shared/lib/platform";
import { useText } from "@/shared/lib/text";

const LEVEL_KEYS = {
  all: "mobile.settings.notifications.level.all",
  "needs-me": "mobile.settings.notifications.level.needsMe",
  nothing: "mobile.settings.notifications.level.nothing",
} as const satisfies Record<ServerNotificationLevel, MobileTextKey>;

/** In the order of `SERVER_MUTE_DURATIONS_MS`, which the desktop server menu offers too. */
const MUTE_KEYS = [
  "mobile.settings.notifications.mute.for15Minutes",
  "mobile.settings.notifications.mute.for1Hour",
  "mobile.settings.notifications.mute.for3Hours",
  "mobile.settings.notifications.mute.for8Hours",
  "mobile.settings.notifications.mute.for24Hours",
] as const satisfies readonly MobileTextKey[];

type MuteChoice = "current" | "off" | "forever" | `${number}`;

/**
 * What one server may notify about on this phone, and a mute, as the desktop server settings have.
 * On iOS the same choices decide whether the Live Activity shows this server and when it plays the
 * sound, so they stay on while Live Activities are on.
 */
export function ServerNotificationSettings({
  server,
  title,
}: {
  server: Pick<MobileServer, "id" | "state">;
  title: string;
}) {
  const { t, format } = useText();
  const { theme } = useUniwind();
  const { canSendNotifications } = useMobileWorkspace();
  const preference = usePushNotificationPreference();
  const liveActivities = useLiveActivitiesPreference();
  const [error, setError] = useState<string | null>(null);
  const setting = serverNotificationSetting(preference, server.id);
  const until = mutedUntil(setting, Date.now());
  // Android has no Live Activity.
  const enabled = preference.ready && !preference.saving && (preference.enabled || (isIOS && liveActivities.enabled));
  const mute: MuteChoice = until === null ? "off" : until === Number.POSITIVE_INFINITY ? "forever" : "current";

  function save(change: Partial<ServerNotificationSetting>) {
    setError(null);
    void haptics.selection();
    void saveServerNotificationSetting(server.id, change).catch(() => {
      setError(t("mobile.settings.saveFailed"));
      void haptics.notification("error");
    });
  }

  const footer =
    error ??
    (server.state === "online" && !canSendNotifications(server.id)
      ? t("mobile.settings.notifications.serverUnsupported")
      : t("mobile.settings.notifications.serverFooter"));
  return (
    <SettingsSection title={title} footer={footer}>
      <SettingsRow
        trailing={
          <SettingsPicker
            value={setting.level}
            options={(["all", "needs-me", "nothing"] as const).map((level) => ({
              value: level,
              label: t(LEVEL_KEYS[level]),
            }))}
            enabled={enabled}
            dark={theme === "dark"}
            label={t("mobile.settings.notifications.notifyAbout")}
            onChange={(level) => save({ level })}
          />
        }
      >
        <Typography.Paragraph>{t("mobile.settings.notifications.notifyAbout")}</Typography.Paragraph>
      </SettingsRow>
      <SettingsRow
        trailing={
          <SettingsPicker<MuteChoice>
            value={mute}
            options={[
              // A timed mute shows its end, as no choice below names it.
              ...(until !== null && until !== Number.POSITIVE_INFINITY
                ? [
                    {
                      value: "current" as const,
                      label: t("mobile.settings.notifications.mute.until", {
                        time: format.date(new Date(until), {
                          ...(new Date(until).toDateString() === new Date().toDateString() ? {} : { weekday: "short" }),
                          hour: "numeric",
                          minute: "2-digit",
                        }),
                      }),
                    },
                  ]
                : []),
              { value: "off", label: t("mobile.settings.notifications.mute.off") },
              ...SERVER_MUTE_DURATIONS_MS.flatMap((duration, index) => {
                const key = MUTE_KEYS[index];
                return key ? [{ value: `${duration}` as const, label: t(key) }] : [];
              }),
              { value: "forever", label: t("mobile.settings.notifications.mute.untilTurnedOn") },
            ]}
            enabled={enabled}
            dark={theme === "dark"}
            label={t("mobile.settings.notifications.mute")}
            onChange={(choice) => {
              if (choice === "current") return;
              save({
                mute: choice === "off" ? null : { until: choice === "forever" ? null : Date.now() + Number(choice) },
              });
            }}
          />
        }
      >
        <Typography.Paragraph>{t("mobile.settings.notifications.mute")}</Typography.Paragraph>
      </SettingsRow>
    </SettingsSection>
  );
}
