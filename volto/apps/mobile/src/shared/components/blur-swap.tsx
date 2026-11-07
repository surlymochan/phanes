import { BlurView } from "expo-blur";
import { createContext, type ReactNode, useCallback, useContext, useEffect, useState } from "react";
import { StyleSheet, View } from "react-native";
import Animated, {
  Easing,
  LayoutAnimationConfig,
  LinearTransition,
  ReduceMotion,
  useAnimatedStyle,
  useSharedValue,
  withDelay,
  withTiming,
} from "react-native-reanimated";
import { scheduleOnRN } from "react-native-worklets";
import type { TransitionMode } from "@/shared/lib/motion";
import { isIOS } from "@/shared/lib/platform";

const EASE_OUT = Easing.bezier(0.23, 1, 0.32, 1);
// Only iOS draws the blur. On Android, `BlurTargetView` moves its children into a native view of its
// own, which stops the layout animations of the rows inside, and a `BlurView` that mounts paints its
// tint and the window background for a frame. Android sheets draw no blur at their edges either.
const BLUR_VEIL = isIOS;
// The values of `BlurReveal`, so a swap looks like the other blurred reveals of the app. A fade for
// reduced motion is shorter: it has no movement to show. The fade carries the change, so it stays.
const DURATION = { blur: { enter: 240, leave: 180 }, fade: { enter: 160, leave: 120 } } as const;
// Items of a list in a layer: they come in, go out and move to their new place.
const ITEM_IN = { duration: 220, easing: EASE_OUT, reduceMotion: ReduceMotion.Never };
const ITEM_OUT = { duration: 160, easing: EASE_OUT, reduceMotion: ReduceMotion.Never };
const ITEM_FADE_IN = { duration: 160, easing: EASE_OUT, reduceMotion: ReduceMotion.Never };
const ITEM_FADE_OUT = { duration: 120, easing: EASE_OUT, reduceMotion: ReduceMotion.Never };
const ITEM_MOVE = LinearTransition.duration(240).easing(EASE_OUT).reduceMotion(ReduceMotion.Never);

/** False while a layer mounts with its first content, so that content does not come in again on its own. */
const SwapLayerSettledContext = createContext(false);

interface Layer {
  key: string;
  node: ReactNode;
  leaving: boolean;
  enters: boolean;
  delay: number;
}

/**
 * Replaces its content when `swapKey` changes: the old content fades out with a blur while the new
 * content fades in. Content with the same key changes in place. The new content is in the layout at
 * once; the old content stays on top of it, out of the layout, until it has faded.
 */
export function BlurSwap({
  swapKey,
  mode,
  enterDelay = 0,
  children,
}: {
  swapKey: string;
  mode: TransitionMode;
  /** Waits before the new content comes in, so content that is replaced soon after never shows. */
  enterDelay?: number;
  children: ReactNode;
}) {
  const [layers, setLayers] = useState<Layer[]>(() => [
    { key: swapKey, node: children, leaving: false, enters: false, delay: 0 },
  ]);
  const shown = layers.at(-1);
  if (shown?.key !== swapKey) {
    setLayers((current) => [
      ...(mode === "none"
        ? []
        : current.filter((layer) => layer.key !== swapKey).map((layer) => ({ ...layer, leaving: true }))),
      { key: swapKey, node: children, leaving: false, enters: mode !== "none", delay: enterDelay },
    ]);
  } else if (shown.node !== children) {
    setLayers((current) => current.map((layer) => (layer.key === swapKey ? { ...layer, node: children } : layer)));
  }
  const remove = useCallback(
    (key: string) => setLayers((current) => current.filter((layer) => !(layer.leaving && layer.key === key))),
    [],
  );
  return (
    <View>
      {layers.map((layer) => (
        <SwapLayer key={layer.key} layer={layer} mode={mode} onLeft={remove} />
      ))}
    </View>
  );
}

function SwapLayer({ layer, mode, onLeft }: { layer: Layer; mode: TransitionMode; onLeft: (key: string) => void }) {
  const progress = useSharedValue(layer.enters ? 0 : 1);
  const [settled, setSettled] = useState(false);
  useEffect(() => setSettled(true), []);
  // The blur costs a render pass while it is mounted, so it shows only while the layer moves.
  const [veiled, setVeiled] = useState(BLUR_VEIL && layer.enters && mode === "blur");
  const { key, leaving, enters, delay } = layer;
  useEffect(() => {
    if (mode === "none") {
      // Settings turned the motion off while the layer moved.
      if (leaving) onLeft(key);
      progress.set(1);
      setVeiled(false);
      return;
    }
    const duration = DURATION[mode];
    if (leaving) {
      setVeiled(BLUR_VEIL && mode === "blur");
      progress.set(
        withTiming(0, { duration: duration.leave, easing: EASE_OUT, reduceMotion: ReduceMotion.Never }, (finished) => {
          if (finished) scheduleOnRN(onLeft, key);
        }),
      );
    } else if (enters) {
      progress.set(
        withDelay(
          delay,
          withTiming(
            1,
            { duration: duration.enter, easing: EASE_OUT, reduceMotion: ReduceMotion.Never },
            (finished) => {
              if (finished) scheduleOnRN(setVeiled, false);
            },
          ),
          ReduceMotion.Never,
        ),
      );
    }
  }, [mode, leaving, enters, delay, key, onLeft, progress]);
  const moves = mode === "blur";
  const style = useAnimatedStyle(() => ({
    opacity: progress.get(),
    transform: [{ translateY: moves ? (1 - progress.get()) * 3 : 0 }],
  }));
  const veilStyle = useAnimatedStyle(() => ({ opacity: 1 - progress.get() }));
  return (
    <Animated.View
      pointerEvents={leaving ? "none" : "auto"}
      accessibilityElementsHidden={leaving}
      importantForAccessibility={leaving ? "no-hide-descendants" : "auto"}
      style={[leaving ? { position: "absolute", top: 0, left: 0, right: 0 } : undefined, style]}
    >
      {/* The items of a layer move with it: they do not come in when it does, or go out after it. */}
      <SwapLayerSettledContext value={settled}>
        <LayoutAnimationConfig skipEntering skipExiting>
          {layer.node}
        </LayoutAnimationConfig>
      </SwapLayerSettledContext>
      {veiled ? (
        <Animated.View pointerEvents="none" style={[StyleSheet.absoluteFill, veilStyle]}>
          <BlurView intensity={16} tint="systemUltraThinMaterial" style={StyleSheet.absoluteFill} />
        </Animated.View>
      ) : null}
    </Animated.View>
  );
}

function itemEntering() {
  "worklet";
  return {
    initialValues: { opacity: 0, transform: [{ translateY: 6 }, { scale: 0.98 }] },
    animations: {
      opacity: withTiming(1, ITEM_IN),
      transform: [{ translateY: withTiming(0, ITEM_IN) }, { scale: withTiming(1, ITEM_IN) }],
    },
  };
}

function itemExiting() {
  "worklet";
  return {
    initialValues: { opacity: 1, transform: [{ scale: 1 }] },
    animations: { opacity: withTiming(0, ITEM_OUT), transform: [{ scale: withTiming(0.98, ITEM_OUT) }] },
  };
}

function itemFadeIn() {
  "worklet";
  return { initialValues: { opacity: 0 }, animations: { opacity: withTiming(1, ITEM_FADE_IN) } };
}

function itemFadeOut() {
  "worklet";
  return { initialValues: { opacity: 1 }, animations: { opacity: withTiming(0, ITEM_FADE_OUT) } };
}

/**
 * The layout animations of an item in a list inside a layer, such as a search result or a section of
 * results. With reduced motion the items only fade, and the others take their new place at once.
 */
export function swapItemMotion(mode: TransitionMode) {
  if (mode === "none") return { entering: undefined, exiting: undefined, layout: undefined };
  if (mode === "fade") return { entering: itemFadeIn, exiting: itemFadeOut, layout: undefined };
  return { entering: itemEntering, exiting: itemExiting, layout: ITEM_MOVE };
}

/**
 * Blurs its content for a moment when it comes into a layer that is already shown, such as a section
 * of results that a search adds. The parent places the blur: give it no padding around the content.
 */
export function BlurEnter({ mode, children }: { mode: TransitionMode; children: ReactNode }) {
  const settled = useContext(SwapLayerSettledContext);
  const [veiled, setVeiled] = useState(BLUR_VEIL && mode === "blur" && settled);
  const veil = useSharedValue(1);
  useEffect(() => {
    if (!veiled) return;
    veil.set(
      withTiming(
        0,
        { duration: DURATION.blur.enter, easing: EASE_OUT, reduceMotion: ReduceMotion.Never },
        (finished) => {
          if (finished) scheduleOnRN(setVeiled, false);
        },
      ),
    );
  }, [veiled, veil]);
  const veilStyle = useAnimatedStyle(() => ({ opacity: veil.get() }));
  return (
    <View>
      {children}
      {veiled ? (
        <Animated.View pointerEvents="none" style={[StyleSheet.absoluteFill, veilStyle]}>
          <BlurView intensity={16} tint="systemUltraThinMaterial" style={StyleSheet.absoluteFill} />
        </Animated.View>
      ) : null}
    </View>
  );
}
