import type { AvatarMood } from "@openbot/brand/bloub-avatar-motion";
import { type ColorValue, View } from "react-native";
import Animated, { Easing, ReduceMotion, withTiming } from "react-native-reanimated";
import { BloubAvatarPreview } from "@/features/agents/components/bloub-avatar";
import type { MobileAgent } from "@/features/workspace/model/workspace-types";
import { ThinkingTextGradient } from "./thinking-text-gradient";

const EASE_OUT = Easing.bezier(0.23, 1, 0.32, 1);
// The fade carries the state change, so it survives reduced motion. The rise and the settle are
// decoration and stop with the system setting.
const FADE_IN = {
  duration: 260,
  easing: EASE_OUT,
  reduceMotion: ReduceMotion.Never,
};
const MOVE_IN = {
  duration: 320,
  easing: EASE_OUT,
  reduceMotion: ReduceMotion.System,
};
const FADE_OUT = {
  duration: 180,
  easing: EASE_OUT,
  reduceMotion: ReduceMotion.Never,
};
const MOVE_OUT = {
  duration: 180,
  easing: EASE_OUT,
  reduceMotion: ReduceMotion.System,
};
// As on desktop. The label can be a whole commentary step, which must not make the row tall.
const LABEL_LINES = 2;

function activityEntering() {
  "worklet";
  return {
    initialValues: {
      opacity: 0,
      transform: [{ translateY: 10 }, { scale: 0.92 }],
    },
    animations: {
      opacity: withTiming(1, FADE_IN),
      transform: [{ translateY: withTiming(0, MOVE_IN) }, { scale: withTiming(1, MOVE_IN) }],
    },
  };
}

function activityExiting() {
  "worklet";
  return {
    initialValues: { opacity: 1, transform: [{ translateY: 0 }, { scale: 1 }] },
    animations: {
      opacity: withTiming(0, FADE_OUT),
      transform: [{ translateY: withTiming(-6, MOVE_OUT) }, { scale: withTiming(0.94, MOVE_OUT) }],
    },
  };
}

/**
 * What the row shows for one agent. The list rebuilds it every render, and
 * {@link import("./use-activity-presence").useActivityPresence} keeps the last one while the next
 * signal for the same turn arrives, so `key` must identify the agent rather than the signal.
 */
export interface ChatActivitySpec {
  key: string;
  label: string;
  accessibilityLabel: string;
  agent?: Pick<MobileAgent, "id" | "serverId" | "avatarHue" | "avatarSeed">;
  mood: AvatarMood;
  online: boolean;
  animateAvatar: boolean;
  shimmer: boolean;
}

export function ChatActivityRow({
  spec,
  foreground,
  muted,
}: {
  spec: ChatActivitySpec;
  foreground: ColorValue;
  muted: ColorValue;
}) {
  return (
    <Animated.View
      entering={activityEntering}
      exiting={activityExiting}
      className="flex-row items-center gap-2 px-1 py-2"
      accessible
      accessibilityLiveRegion="polite"
      accessibilityRole="text"
      accessibilityLabel={spec.accessibilityLabel}
    >
      {spec.agent ? (
        <BloubAvatarPreview
          agentId={spec.agent.id}
          serverId={spec.agent.serverId}
          hue={spec.agent.avatarHue}
          seed={spec.agent.avatarSeed}
          size={36}
          disconnected={!spec.online}
          mood={spec.mood}
          animateIdle={spec.animateAvatar}
        />
      ) : null}
      <View className="min-w-0 flex-1">
        <ThinkingTextGradient
          text={spec.label}
          numberOfLines={LABEL_LINES}
          foreground={foreground}
          muted={muted}
          enabled={spec.shimmer}
        />
      </View>
    </Animated.View>
  );
}
