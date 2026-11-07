import { isRemoteNetworkRoute, type RemoteNetworkRoute } from "@volto/contracts/ipc";
import { Badge, RadioGroup } from "@volto/ui";
import { Show } from "solid-js";
import { useText } from "../../text";

interface RemoteNetworkRouteChoiceProps {
  value: RemoteNetworkRoute;
  tailscaleConnected: boolean;
  disabled: boolean;
  onChange: (route: RemoteNetworkRoute) => void;
}

/** The two routes as cards with a radio dot. Volto is the default and has the recommended badge. */
export function RemoteNetworkRouteChoice(props: RemoteNetworkRouteChoiceProps) {
  const { t } = useText();
  return (
    <RadioGroup.Root
      class="settings-mobile-route-choice"
      aria-label={t("settings.mobileConnect.route.title")}
      value={props.value}
      disabled={props.disabled}
      onChange={(value) => {
        if (isRemoteNetworkRoute(value)) props.onChange(value);
      }}
    >
      <RadioGroup.Item class="settings-mobile-route-option" value="openbot">
        <RadioGroup.ItemInput />
        <RadioGroup.ItemControl class="settings-mobile-route-card">
          <span class="settings-mobile-route-dot" aria-hidden="true" />
          <span class="settings-mobile-route-text">
            <RadioGroup.ItemLabel class="settings-mobile-route-name">
              {t("settings.mobileConnect.route.volto.title")}
              <Badge variant="info-light">{t("settings.mobileConnect.route.volto.badge")}</Badge>
            </RadioGroup.ItemLabel>
            <RadioGroup.ItemDescription class="settings-mobile-route-hint">
              {t("settings.mobileConnect.route.volto.description")}
            </RadioGroup.ItemDescription>
          </span>
        </RadioGroup.ItemControl>
      </RadioGroup.Item>
      <RadioGroup.Item class="settings-mobile-route-option" value="private">
        <RadioGroup.ItemInput />
        <RadioGroup.ItemControl class="settings-mobile-route-card">
          <span class="settings-mobile-route-dot" aria-hidden="true" />
          <span class="settings-mobile-route-text">
            <RadioGroup.ItemLabel class="settings-mobile-route-name">
              {t("settings.mobileConnect.route.private.title")}
              <Show when={props.tailscaleConnected}>
                <Badge variant="success-light">{t("settings.mobileConnect.route.private.tailscale")}</Badge>
              </Show>
            </RadioGroup.ItemLabel>
            <RadioGroup.ItemDescription class="settings-mobile-route-hint">
              {t("settings.mobileConnect.route.private.description")}
            </RadioGroup.ItemDescription>
          </span>
        </RadioGroup.ItemControl>
      </RadioGroup.Item>
    </RadioGroup.Root>
  );
}
