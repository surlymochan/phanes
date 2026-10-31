import type { HostTimezoneSettings, OpenBotDesktopApi } from "@openbot/contracts/ipc";
import {
  Item,
  ItemActions,
  ItemContent,
  ItemDescription,
  ItemTitle,
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@openbot/ui";
import { useText } from "@openbot/ui/text";
import { createEffect, createSignal, Show, untrack } from "solid-js";

/** The timezone calls of one server's host. The desktop reaches them through main. */
export type HostTimezoneCalls = Pick<OpenBotDesktopApi["hostAdmin"], "getTimezone" | "setTimezone">;

export interface ServerTimezoneOptions {
  /** The calls of a client with no `window.openbot`, such as the web client. */
  calls?: HostTimezoneCalls;
}

/** The option for `null`. No IANA zone has this name, and the host refuses it as a zone. */
const AUTOMATIC = "automatic";

/**
 * Server Settings > General: the zone of a routine that an agent creates when neither the tool call
 * nor the sender's message names one (`host-timezone-v1`). The host checks the role again.
 */
export function ServerTimezoneSetting(
  props: ServerTimezoneOptions & {
    serverId: string;
    disabled: boolean;
    menuMount: HTMLElement | undefined;
    run: (key: string, action: () => Promise<void>) => Promise<boolean>;
  },
) {
  const { t, errorMessage } = useText();
  const calls = (): HostTimezoneCalls => props.calls ?? window.openbot.hostAdmin;
  const [settings, setSettings] = createSignal<HostTimezoneSettings | null>(null);
  const [loadError, setLoadError] = createSignal<string | null>(null);
  /** Bumped for each server, so an answer for an earlier one is dropped. */
  let generation = 0;

  createEffect(
    () => props.serverId,
    (serverId) => {
      const current = ++generation;
      setSettings(null);
      setLoadError(null);
      untrack(calls)
        .getTimezone(serverId)
        .then(
          (next) => {
            if (current === generation) setSettings(next);
          },
          (error: unknown) => {
            if (current === generation) setLoadError(errorMessage(error, t("server.settings.timezoneLoadFailed")));
          },
        );
    },
  );

  const zones = () => {
    const chosen = settings()?.timezone;
    // V8 lists canonical zones only, and "UTC" is not one of them.
    const known = ["UTC", ...Intl.supportedValuesOf("timeZone").filter((zone) => zone !== "UTC")];
    return [AUTOMATIC, ...(chosen && !known.includes(chosen) ? [chosen] : []), ...known];
  };
  const label = (zone: string) =>
    zone === AUTOMATIC
      ? t("server.settings.timezoneAutomatic", { zone: settings()?.systemTimezone ?? "" })
      : zone.replaceAll("_", " ");

  function choose(zone: string | null): void {
    const current = settings();
    if (zone === null || !current || zone === (current.timezone ?? AUTOMATIC)) return;
    const serverId = props.serverId;
    const request = generation;
    void props.run("timezone", async () => {
      const next = await calls().setTimezone(zone === AUTOMATIC ? null : zone, serverId);
      if (request === generation) setSettings(next);
    });
  }

  return (
    <Item>
      <ItemContent>
        <ItemTitle id="server-settings-timezone-label">{t("server.settings.timezone")}</ItemTitle>
        <ItemDescription>{loadError() ?? t("server.settings.timezoneDescription")}</ItemDescription>
      </ItemContent>
      <Show when={settings()}>
        {(current) => (
          <ItemActions>
            <Select<string>
              options={zones()}
              value={current().timezone ?? AUTOMATIC}
              disabled={props.disabled}
              placement="bottom-end"
              onChange={choose}
              itemComponent={(item) => <SelectItem item={item.item}>{label(item.item.rawValue)}</SelectItem>}
            >
              <SelectTrigger size="sm" aria-labelledby="server-settings-timezone-label">
                <SelectValue<string>>{(state) => label(state.selectedOption())}</SelectValue>
              </SelectTrigger>
              <SelectContent mount={props.menuMount} />
            </Select>
          </ItemActions>
        )}
      </Show>
    </Item>
  );
}
