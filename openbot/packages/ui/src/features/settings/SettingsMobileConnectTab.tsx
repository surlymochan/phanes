import { AppLogo, type MobilePlatformLogoVariant, PlatformLogo } from "@openbot/brand";
import type { MobileConnectedDevice } from "@openbot/contracts/ipc";
import type { AppTextKey, AppTranslate } from "@openbot/i18n";
import {
  Alert,
  AlertContent,
  AlertDescription,
  AlertIcon,
  AlertTitle,
  Badge,
  Button,
  CircleCheck,
  ConfirmDialog,
  CopyButton,
  Eye,
  Info,
  Item,
  ItemActions,
  ItemContent,
  ItemDescription,
  ItemGroup,
  ItemMedia,
  ItemTitle,
  Link2,
  QrCode,
  SettingsSection,
  SlidingTabs,
  Smartphone,
  Text,
} from "@openbot/ui";
import { createSignal, For, Show } from "solid-js";
import { useText } from "../../text";
import { ANDROID_PLAY_STORE_URL } from "../mobile-app/android-play-store";
import { IOS_APP_STORE_URL } from "../mobile-app/ios-app-store";
import { RemoteNetworkRouteChoice } from "./RemoteNetworkRouteChoice";
import type { SettingsMobileConnectStore } from "./stores/mobile-connect-store";

interface SettingsMobileConnectTabProps {
  store: SettingsMobileConnectStore;
  canCreateTicket: boolean;
  canCancelTicket: boolean;
  canManageLink: boolean;
  canRevokeDevice: boolean;
  canSetRoute: boolean;
}

const IOS_PLATFORM_LABEL = "iOS";
const IPHONE_LABEL = "iPhone";
const ANDROID_PLATFORM_LABEL = "Android";

interface MobileAppGuide {
  platform: MobilePlatformLogoVariant;
  tab: string;
  url: string;
  title: AppTextKey;
  badge: AppTextKey;
  description: AppTextKey;
  qrLabel: AppTextKey;
  steps: readonly AppTextKey[];
}

/** The phone apps in the order of the download pages. The QR code opens the store page on the phone. */
const MOBILE_APPS: readonly MobileAppGuide[] = [
  {
    platform: "ios",
    tab: IPHONE_LABEL,
    url: IOS_APP_STORE_URL,
    title: "settings.mobileConnect.iosApp.title",
    badge: "settings.mobileConnect.iosApp.badge",
    description: "settings.mobileConnect.iosApp.description",
    qrLabel: "settings.mobileConnect.iosApp.qrLabel",
    steps: ["settings.mobileConnect.iosApp.step.scan", "settings.mobileConnect.iosApp.step.install"],
  },
  {
    platform: "android",
    tab: ANDROID_PLATFORM_LABEL,
    url: ANDROID_PLAY_STORE_URL,
    title: "settings.mobileConnect.androidApp.title",
    badge: "settings.mobileConnect.androidApp.badge",
    description: "settings.mobileConnect.androidApp.description",
    qrLabel: "settings.mobileConnect.androidApp.qrLabel",
    steps: ["settings.mobileConnect.androidApp.step.scan", "settings.mobileConnect.androidApp.step.install"],
  },
];

function deviceLogo(platform: MobileConnectedDevice["platform"]): MobilePlatformLogoVariant | undefined {
  return platform === "unknown" ? undefined : platform;
}

function devicePlatformLabel(platform: MobileConnectedDevice["platform"], t: AppTranslate): string {
  if (platform === "ios") return IOS_PLATFORM_LABEL;
  if (platform === "android") return ANDROID_PLATFORM_LABEL;
  return t("settings.mobileConnect.platform.mobile");
}

export function SettingsMobileConnectTab(props: SettingsMobileConnectTabProps) {
  const { t, format } = useText();
  const [platform, setPlatform] = createSignal<MobilePlatformLogoVariant>("ios");
  return (
    <SettingsSection title={t("settings.mobileConnect.title")} description={t("settings.mobileConnect.description")}>
      {/* Always here, so the install steps stay available after the sidebar announcement is closed. */}
      <ItemGroup class="settings-modal-card settings-mobile-connect-card">
        <SlidingTabs.Root
          class="settings-mobile-app-install"
          value={platform()}
          onChange={(value) => {
            if (value === "ios" || value === "android") setPlatform(value);
          }}
        >
          <Item
            data-settings-row="settings.mobileConnect.install.title"
            class="settings-modal-row settings-mobile-connect-action-row"
          >
            <ItemMedia class="settings-mobile-connect-step" aria-hidden="true">
              1
            </ItemMedia>
            <ItemContent>
              <ItemTitle>{t("settings.mobileConnect.install.title")}</ItemTitle>
              <ItemDescription>{t("settings.mobileConnect.install.description")}</ItemDescription>
            </ItemContent>
            <ItemActions>
              <SlidingTabs.List aria-label={t("settings.mobileConnect.install.platformLabel")}>
                <For each={MOBILE_APPS}>
                  {(app) => (
                    <SlidingTabs.Trigger class="settings-mobile-app-tab" value={app.platform}>
                      <PlatformLogo platform={app.platform} />
                      {app.tab}
                    </SlidingTabs.Trigger>
                  )}
                </For>
              </SlidingTabs.List>
            </ItemActions>
          </Item>
          <SlidingTabs.ContentSlot class="settings-mobile-app-panels">
            <For each={MOBILE_APPS}>
              {(app) => (
                <SlidingTabs.Content class="settings-mobile-app-panel" value={app.platform}>
                  <QrCode class="settings-mobile-app-qr" value={app.url} size={132} label={t(app.qrLabel)}>
                    <PlatformLogo platform={app.platform} class="settings-mobile-app-qr-platform" />
                    <Link2 class="settings-mobile-app-qr-link" />
                    <AppLogo variant="production" class="settings-mobile-app-qr-logo" />
                  </QrCode>
                  <div class="settings-mobile-app-details">
                    <div class="settings-mobile-app-heading">
                      <Text class="settings-mobile-app-title" variant="body">
                        {t(app.title)}
                      </Text>
                      <Badge variant={app.platform === "ios" ? "info-light" : "success-light"}>{t(app.badge)}</Badge>
                    </div>
                    <Text variant="caption" tone="muted">
                      {t(app.description)}
                    </Text>
                    <ol class="settings-mobile-app-steps">
                      <For each={app.steps}>{(step) => <li>{t(step)}</li>}</For>
                    </ol>
                    <CopyButton
                      class="settings-mobile-app-copy"
                      variant="outline"
                      size="sm"
                      value={app.url}
                      label={t("settings.mobileConnect.app.copyLink")}
                      copiedLabel={t("settings.mobileConnect.app.linkCopied")}
                    />
                  </div>
                </SlidingTabs.Content>
              )}
            </For>
          </SlidingTabs.ContentSlot>
        </SlidingTabs.Root>
      </ItemGroup>

      <ItemGroup class="settings-modal-card settings-mobile-connect-card">
        <Item
          data-settings-row="settings.mobileConnect.signIn.title"
          class="settings-modal-row settings-mobile-connect-action-row"
        >
          <ItemMedia class="settings-mobile-connect-step" aria-hidden="true">
            {props.canManageLink ? "2.1" : "2"}
          </ItemMedia>
          <ItemContent>
            <ItemTitle>{t("settings.mobileConnect.signIn.title")}</ItemTitle>
            <ItemDescription>{t("settings.mobileConnect.signIn.description")}</ItemDescription>
            <Show when={props.store.state.connect.error}>
              {(error) => (
                <ItemDescription class="settings-modal-error" role="alert">
                  {error()}
                </ItemDescription>
              )}
            </Show>
          </ItemContent>
          <ItemActions>
            <Button
              class="settings-mobile-connect-generate"
              type="button"
              size="sm"
              loading={props.store.state.connect.busy}
              loadingLabel={t("settings.mobileConnect.generating")}
              disabled={!props.canCreateTicket}
              onClick={() => void props.store.createTicket()}
            >
              {props.store.state.connect.session
                ? t("settings.mobileConnect.generateNew")
                : t("settings.mobileConnect.generate")}
            </Button>
          </ItemActions>
        </Item>

        <Show when={props.store.state.connect.session}>
          {(session) => (
            <div
              class="settings-mobile-connect-code-collapse"
              data-collapsing={session().collapsing ? "" : undefined}
              aria-hidden={session().collapsing ? "true" : undefined}
            >
              <div class="settings-mobile-connect-code-collapse-body">
                <div class="settings-mobile-connect-link">
                  <Show
                    when={!props.store.expired()}
                    fallback={
                      <div class="settings-mobile-connect-expired" role="status">
                        <Smartphone aria-hidden="true" />
                        <Text class="settings-mobile-connect-code-title" variant="body">
                          {t("settings.mobileConnect.expired.title")}
                        </Text>
                        <Text variant="caption" tone="muted">
                          {t("settings.mobileConnect.expired.description")}
                        </Text>
                      </div>
                    }
                  >
                    <div class="settings-mobile-connect-link-bar">
                      <Text class="settings-mobile-connect-expiry" variant="caption" aria-atomic="true">
                        {t("settings.mobileConnect.expiresIn", { time: props.store.expiryLabel() })}
                      </Text>
                      <div class="settings-mobile-connect-link-actions">
                        <CopyButton
                          variant="outline"
                          size="sm"
                          value={session().ticket.qrData}
                          label={t("settings.mobileConnect.code.copy")}
                          copiedLabel={t("settings.mobileConnect.app.linkCopied")}
                        />
                        <Button
                          type="button"
                          size="sm"
                          variant="destructive-ghost"
                          loading={props.store.state.connect.cancelling}
                          disabled={!props.canCancelTicket || props.store.state.connect.busy}
                          onClick={() => void props.store.cancelTicket()}
                        >
                          {t("settings.mobileConnect.code.cancel")}
                        </Button>
                      </div>
                    </div>
                    <MobileConnectQr
                      value={session().ticket.qrData}
                      label={t("settings.mobileConnect.qrLabel")}
                      successDeviceName={session().successDeviceName}
                    />
                  </Show>
                </div>
              </div>
            </div>
          )}
        </Show>
      </ItemGroup>

      <Show when={props.canManageLink}>
        <ItemGroup class="settings-modal-card settings-mobile-connect-card">
          <Item
            data-settings-row="settings.mobileConnect.link.title"
            class="settings-modal-row settings-mobile-connect-action-row"
          >
            <ItemMedia class="settings-mobile-connect-step" aria-hidden="true">
              2.2
            </ItemMedia>
            <ItemContent>
              <ItemTitle>{t("settings.mobileConnect.link.title")}</ItemTitle>
              <ItemDescription>{t("settings.mobileConnect.link.description")}</ItemDescription>
              <Show when={props.store.state.link.error}>
                {(error) => (
                  <ItemDescription class="settings-modal-error" role="alert">
                    {error()}
                  </ItemDescription>
                )}
              </Show>
            </ItemContent>
            <ItemActions>
              <Button
                class="settings-mobile-connect-generate"
                type="button"
                size="sm"
                loading={props.store.state.link.busy === "create"}
                loadingLabel={t("settings.mobileConnect.generating")}
                disabled={!props.store.state.link.loaded || props.store.state.link.busy !== null}
                onClick={() =>
                  props.store.state.link.link ? props.store.setLinkConfirm("replace") : void props.store.createLink()
                }
              >
                {props.store.state.link.link
                  ? t("settings.mobileConnect.link.replace")
                  : t("settings.mobileConnect.link.create")}
              </Button>
            </ItemActions>
          </Item>

          <Show when={props.store.state.link.link}>
            {(link) => (
              <div class="settings-mobile-connect-link">
                <div class="settings-mobile-connect-link-bar">
                  <Text variant="caption" tone="muted">
                    {t("settings.mobileConnect.link.created", {
                      date: format.date(link().createdAt, { dateStyle: "medium", timeStyle: "short" }),
                    })}
                  </Text>
                  <div class="settings-mobile-connect-link-actions">
                    <Button type="button" size="sm" variant="outline" onClick={() => props.store.toggleLinkRevealed()}>
                      <Eye aria-hidden="true" />
                      {props.store.state.link.revealed
                        ? t("settings.mobileConnect.link.hide")
                        : t("settings.mobileConnect.link.show")}
                    </Button>
                    <CopyButton
                      variant="outline"
                      size="sm"
                      value={link().url}
                      label={t("settings.mobileConnect.link.copy")}
                      copiedLabel={t("settings.mobileConnect.app.linkCopied")}
                    />
                    <Button
                      type="button"
                      size="sm"
                      variant="destructive-ghost"
                      disabled={props.store.state.link.busy !== null}
                      onClick={() => props.store.setLinkConfirm("revoke")}
                    >
                      {t("settings.mobileConnect.link.revoke")}
                    </Button>
                  </div>
                </div>
                {/* Always mounted, so showing and hiding the link animate like the one-time code. */}
                <div
                  class="settings-mobile-connect-link-reveal"
                  data-expanded={props.store.state.link.revealed ? "" : undefined}
                  aria-hidden={props.store.state.link.revealed ? undefined : "true"}
                  inert={props.store.state.link.revealed ? undefined : true}
                >
                  <div class="settings-mobile-connect-code-collapse-body">
                    <MobileConnectQr
                      value={link().url}
                      label={t("settings.mobileConnect.link.qrLabel")}
                      successDeviceName={props.store.state.link.successDeviceName}
                    />
                  </div>
                </div>
              </div>
            )}
          </Show>
        </ItemGroup>

        <ConfirmDialog
          open={props.store.state.link.confirm === "replace"}
          onCancel={() => props.store.setLinkConfirm(null)}
          onConfirm={() => props.store.createLink()}
          tone="default"
          title={t("settings.mobileConnect.link.replaceConfirm.title")}
          description={t("settings.mobileConnect.link.confirm.description")}
          confirmLabel={t("settings.mobileConnect.link.replace")}
        />
        <ConfirmDialog
          open={props.store.state.link.confirm === "revoke"}
          onCancel={() => props.store.setLinkConfirm(null)}
          onConfirm={() => props.store.revokeLink()}
          title={t("settings.mobileConnect.link.revokeConfirm.title")}
          description={t("settings.mobileConnect.link.revokeConfirm.description")}
          confirmLabel={t("settings.mobileConnect.link.revoke")}
        />
      </Show>

      <section
        data-settings-row="settings.mobileConnect.devices.title"
        class="settings-mobile-devices"
        aria-labelledby="settings-mobile-devices-title"
      >
        <div class="settings-mobile-devices-heading">
          <h3 id="settings-mobile-devices-title">{t("settings.mobileConnect.devices.title")}</h3>
          <Show when={props.store.state.devices.loading}>
            <Text as="span" variant="caption" tone="muted" role="status">
              {t("settings.mobileConnect.devices.refreshing")}
            </Text>
          </Show>
        </div>
        <div class="settings-mobile-devices-states">
          <div
            class="settings-mobile-devices-state"
            data-expanded={props.store.state.devices.devices.length === 0 ? "" : undefined}
            aria-hidden={props.store.state.devices.devices.length > 0 ? "true" : undefined}
          >
            <div class="settings-mobile-devices-state-body">
              <div class="settings-mobile-devices-empty" role="status">
                <Smartphone aria-hidden="true" />
                <div>
                  <Text class="settings-mobile-devices-empty-title" variant="body">
                    {t("settings.mobileConnect.devices.empty.title")}
                  </Text>
                  <Text variant="caption" tone="muted">
                    {t("settings.mobileConnect.devices.empty.description")}
                  </Text>
                </div>
              </div>
            </div>
          </div>
          <div
            class="settings-mobile-devices-state"
            data-expanded={props.store.state.devices.devices.length > 0 ? "" : undefined}
            aria-hidden={props.store.state.devices.devices.length === 0 ? "true" : undefined}
          >
            <div class="settings-mobile-devices-state-body settings-mobile-devices-list">
              <div class="settings-mobile-devices-table-frame">
                <table class="settings-mobile-devices-table">
                  <thead>
                    <tr>
                      <th scope="col">{t("settings.mobileConnect.devices.column.device")}</th>
                      <th scope="col">{t("settings.mobileConnect.devices.column.platform")}</th>
                      <th scope="col">{t("settings.mobileConnect.devices.column.via")}</th>
                      <th scope="col">{t("settings.mobileConnect.devices.column.connected")}</th>
                      <th scope="col">{t("settings.mobileConnect.devices.column.lastActive")}</th>
                      <th scope="col">
                        <span class="sr-only">{t("settings.mobileConnect.devices.column.actions")}</span>
                      </th>
                    </tr>
                  </thead>
                  <tbody>
                    <For each={props.store.state.devices.devices}>
                      {(device) => (
                        <tr>
                          <td>
                            <span class="settings-mobile-device-name">
                              <Smartphone aria-hidden="true" />
                              {device.name}
                            </span>
                          </td>
                          <td>
                            <span class="settings-mobile-device-platform">
                              <Show when={deviceLogo(device.platform)} fallback={<Smartphone aria-hidden="true" />}>
                                {(platform) => <PlatformLogo platform={platform()} />}
                              </Show>
                              {devicePlatformLabel(device.platform, t)}
                            </span>
                          </td>
                          <td>
                            {device.via === "link"
                              ? t("settings.mobileConnect.devices.via.link")
                              : t("settings.mobileConnect.devices.via.code")}
                          </td>
                          <td>{props.store.deviceTimeLabel(device.connectedAt)}</td>
                          <td>{props.store.deviceTimeLabel(device.lastActiveAt)}</td>
                          <td class="settings-mobile-device-action">
                            <Button
                              type="button"
                              variant="destructive-ghost"
                              size="xs"
                              loading={props.store.state.devices.revokingSessionId === device.sessionId}
                              loadingLabel={t("settings.disconnect.pending")}
                              disabled={!props.canRevokeDevice}
                              data-cuelume-tap="close"
                              data-cuelume-emphasis="strong"
                              aria-label={t("settings.mobileConnect.devices.disconnectLabel", { name: device.name })}
                              onClick={() => void props.store.revokeDevice(device)}
                            >
                              {t("settings.disconnect.action")}
                            </Button>
                          </td>
                        </tr>
                      )}
                    </For>
                  </tbody>
                </table>
              </div>
              {/* Only beside a device that can be disconnected. */}
              <Alert tone="neutral">
                <AlertIcon>
                  <Info />
                </AlertIcon>
                <AlertContent>
                  <AlertTitle>{t("settings.mobileConnect.disconnectInfo.title")}</AlertTitle>
                  <AlertDescription>{t("settings.mobileConnect.disconnectInfo.description")}</AlertDescription>
                </AlertContent>
              </Alert>
            </div>
          </div>
        </div>
        <Show when={props.store.state.devices.error}>
          {(error) => (
            <Text class="settings-modal-error" variant="caption" role="alert">
              {error()}
            </Text>
          )}
        </Show>
      </section>

      <Show when={props.store.state.route.current || props.store.state.route.error}>
        <section
          data-settings-row="settings.mobileConnect.route.title"
          class="settings-mobile-route"
          aria-labelledby="settings-mobile-route-title"
        >
          <div class="settings-mobile-route-heading">
            <h3 id="settings-mobile-route-title">{t("settings.mobileConnect.route.title")}</h3>
            <Text variant="caption" tone="muted">
              {t("settings.mobileConnect.route.description")}
            </Text>
          </div>
          <Show when={props.store.state.route.current}>
            {(current) => (
              <RemoteNetworkRouteChoice
                value={current().route}
                tailscaleConnected={current().tailscaleConnected}
                disabled={!props.canSetRoute || props.store.state.route.busy}
                onChange={(route) => void props.store.setRoute(route)}
              />
            )}
          </Show>
          <Show when={props.store.state.route.error}>
            {(error) => (
              <Text class="settings-modal-error" variant="caption" role="alert">
                {error()}
              </Text>
            )}
          </Show>
        </section>
      </Show>
    </SettingsSection>
  );
}

/** The QR code of a sign-in code or link, with the scan steps, or the phone that it connected. */
function MobileConnectQr(props: { value: string; label: string; successDeviceName: string | null }) {
  const { t } = useText();
  return (
    <div class="settings-mobile-connect-link-code" aria-live="polite">
      <div class="settings-mobile-connect-qr-stage" data-success={props.successDeviceName ? "" : undefined}>
        <QrCode value={props.value} size={132} label={props.label} />
        <Show when={props.successDeviceName}>
          <div class="settings-mobile-connect-success-mark" aria-hidden="true">
            <CircleCheck />
          </div>
        </Show>
      </div>
      <div class="settings-mobile-connect-code-copy">
        <Show
          when={props.successDeviceName}
          fallback={
            <>
              <Text class="settings-mobile-connect-code-title" variant="body">
                {t("settings.mobileConnect.scan.title")}
              </Text>
              <Text variant="caption" tone="muted">
                {t("settings.mobileConnect.scan.description")}
              </Text>
            </>
          }
        >
          {(deviceName) => (
            <>
              <Text class="settings-mobile-connect-code-title settings-mobile-connect-success-title" variant="body">
                {t("settings.mobileConnect.connected.title")}
              </Text>
              <Text variant="caption" tone="muted" role="status">
                {t("settings.mobileConnect.connected.description", { name: deviceName() })}
              </Text>
            </>
          )}
        </Show>
      </div>
    </div>
  );
}
