import { GlassView } from "expo-glass-effect";
import { Link, router } from "expo-router";
import { Typography } from "heroui-native";
import { useThemeColor } from "heroui-native/hooks";
import ArrowLeft from "lucide-react-native/icons/arrow-left";
import Globe from "lucide-react-native/icons/globe";
import TriangleAlert from "lucide-react-native/icons/triangle-alert";
import { type ReactNode, useMemo } from "react";
import { Pressable, View, type ViewStyle } from "react-native";
import { AgentPinAvatar } from "@/features/agents/components/agent-pin-avatar";
import { BloubAvatar } from "@/features/agents/components/bloub-avatar";
import { useBrowserFeature } from "@/features/browser/components/use-browser-feature";
import { ChannelAvatar } from "@/features/channels/components/channel-avatar";
import { ChatGlassIconButton } from "@/features/chat/components/chat-glass-icon-button";
import { useMobileWorkspaceSelector } from "@/features/workspace/context/mobile-workspace-context";
import { showsOnline } from "@/features/workspace/model/server-status";
import { SheetScrollEdgeEffect } from "@/shared/components/sheet-scroll-edge-effect";
import { haptics } from "@/shared/lib/haptics";
import { useText } from "@/shared/lib/text";
import { typeScale } from "@/shared/lib/type-scale";
import type { ChatTarget } from "../model/chat-target";

interface ChatHeaderProps {
  target: ChatTarget;
  fallbackBackground: ViewStyle["backgroundColor"];
  foreground: ViewStyle["backgroundColor"];
  liquidGlassAvailable: boolean;
  topInset: number;
  onBack: () => void;
  /** A channel task needs the user: the header shows the button that opens the channel actions. */
  needsAction?: boolean;
  /**
   * The member whose browser the browser button of a channel opens, the tab of its request, and the
   * members whose browser the browser screen can switch to. Null hides the button.
   */
  channelBrowser?: { agentId: string; agentIds: readonly string[]; tabId?: string } | null;
  /** Something covers the header, such as the voice mode, so screen readers skip it. */
  accessibilityHidden?: boolean;
  /** Shown next to the avatar in place of the agent's name, such as a task title. */
  title?: string;
  /** Shown before the title with a dot between them, such as "Task". */
  kicker?: string;
  /** Takes the place of the browser button, such as the actions of a task. */
  trailing?: ReactNode;
}

export function ChatHeader({
  target,
  fallbackBackground,
  foreground,
  liquidGlassAvailable,
  topInset,
  onBack,
  needsAction = false,
  channelBrowser = null,
  accessibilityHidden = false,
  title,
  kicker,
  trailing,
}: ChatHeaderProps) {
  const { t } = useText();
  const warning = useThemeColor("warning");
  const disconnected = useMobileWorkspaceSelector(
    (current) => !current.servers.some((server) => server.id === target.serverId && showsOnline(server)),
  );
  const browserView = useMobileWorkspaceSelector((current) => current.browserViewSupport(target.serverId).view);
  const browserAllowed = useBrowserFeature();
  // A channel opens the browser of one member: the browser is the browser of an agent.
  const browserAgent =
    target.kind === "agent" ? { agentId: target.id, agentIds: [], tabId: undefined } : channelBrowser;
  const browserShown =
    trailing === undefined && browserAllowed && !disconnected && browserView && Boolean(browserAgent);
  const members = useMemo(
    () => new Map(target.kind === "channel" ? target.members.map((member) => [member.id, member]) : []),
    [target],
  );
  const iconColor = String(foreground);

  return (
    <>
      <View
        className="absolute inset-x-0 z-20 flex-row items-center gap-2 px-4"
        pointerEvents="box-none"
        accessibilityElementsHidden={accessibilityHidden}
        importantForAccessibility={accessibilityHidden ? "no-hide-descendants" : "auto"}
        style={{ top: topInset + 8 }}
      >
        <ChatGlassIconButton
          accessibilityLabel={t("common.back")}
          fallbackBackground={fallbackBackground}
          liquidGlassAvailable={liquidGlassAvailable}
          onPress={() => {
            void haptics.impact("soft");
            onBack();
          }}
        >
          <ArrowLeft color={iconColor} size={24} strokeWidth={2} />
        </ChatGlassIconButton>

        <GlassView
          glassEffectStyle={liquidGlassAvailable ? "regular" : "none"}
          style={{
            alignItems: "center",
            alignSelf: "stretch",
            backgroundColor: liquidGlassAvailable ? "transparent" : fallbackBackground,
            borderCurve: "continuous",
            borderRadius: 24,
            flexDirection: "row",
            // A kicker and a title need the width up to the trailing button.
            maxWidth: kicker ? undefined : 220,
            flexShrink: 1,
            overflow: "hidden",
          }}
        >
          <Pressable
            className="min-w-0 shrink flex-row items-center gap-2 self-stretch pr-4 pl-2.5"
            accessibilityRole="button"
            accessibilityLabel={t("mobile.chat.header.info", { name: target.name })}
            hitSlop={8}
            onPress={() => {
              void haptics.impact("soft");
              if (target.kind === "channel")
                router.push({
                  pathname: "/channel-info/[channelId]",
                  params: { channelId: target.id, serverId: target.serverId },
                });
              else
                router.push({
                  pathname: "/agent-info/[agentId]",
                  params: { agentId: target.id, serverId: target.serverId },
                });
            }}
          >
            <Link.AppleZoomTarget>
              {target.kind === "channel" ? (
                <View collapsable={false}>
                  <ChannelAvatar
                    channel={{ members: target.members.map((member) => ({ agentId: member.id })) }}
                    agents={members}
                    size={28}
                    disconnected={disconnected}
                  />
                </View>
              ) : (
                <AgentPinAvatar agentId={target.id} location="chat" size={28}>
                  <BloubAvatar
                    agentId={target.id}
                    serverId={target.serverId}
                    hue={target.avatarHue}
                    seed={target.avatarSeed}
                    size={28}
                  />
                </AgentPinAvatar>
              )}
            </Link.AppleZoomTarget>
            {kicker ? (
              <>
                <Typography.Paragraph className="font-semibold" numberOfLines={1} style={typeScale.rowTitle}>
                  {kicker}
                </Typography.Paragraph>
                <View className="size-1 rounded-full bg-muted" />
              </>
            ) : null}
            <Typography.Paragraph className="font-semibold min-w-0 shrink" numberOfLines={1} style={typeScale.rowTitle}>
              {title ?? target.name}
            </Typography.Paragraph>
          </Pressable>
        </GlassView>

        <View className="flex-1" />
        {trailing !== undefined ? (
          trailing
        ) : (
          <View className="flex-row items-center gap-2">
            {target.kind === "channel" && needsAction ? (
              <ChatGlassIconButton
                accessibilityLabel={t("mobile.chat.header.actionsNeeded")}
                fallbackBackground={fallbackBackground}
                liquidGlassAvailable={liquidGlassAvailable}
                onPress={() => {
                  void haptics.impact("soft");
                  router.push({
                    pathname: "/channel-actions/[channelId]",
                    params: { channelId: target.id, serverId: target.serverId },
                  });
                }}
              >
                <TriangleAlert color={String(warning)} size={24} strokeWidth={2.5} />
              </ChatGlassIconButton>
            ) : null}
            {/* Not in a BlurReveal: glass stops rendering under an ancestor with zero opacity, so the
                button hides itself, and its glass turns off with the native animation. The agent's
                browser on the host, live. The button shows when the agent has no tab yet too: the
                browser screen opens a new one. A host that cannot stream a tab, or an app version
                the account server has the browser off for, shows no button. */}
            <ChatGlassIconButton
              accessibilityLabel={t("mobile.browser.open")}
              fallbackBackground={fallbackBackground}
              hidden={!browserShown}
              liquidGlassAvailable={liquidGlassAvailable}
              onPress={() => {
                if (!browserAgent) return;
                void haptics.impact("soft");
                router.push({
                  pathname: "/browser/[agentId]",
                  params: {
                    agentId: browserAgent.agentId,
                    serverId: target.serverId,
                    ...(browserAgent.tabId ? { tabId: browserAgent.tabId } : {}),
                    ...(browserAgent.agentIds.length > 1 ? { agentIds: browserAgent.agentIds.join(",") } : {}),
                  },
                });
              }}
            >
              <Globe color={iconColor} size={24} strokeWidth={2} />
            </ChatGlassIconButton>
          </View>
        )}
      </View>
      <SheetScrollEdgeEffect
        style={{ height: topInset + 82, left: 0, position: "absolute", right: 0, top: 0, zIndex: 10 }}
      />
    </>
  );
}
