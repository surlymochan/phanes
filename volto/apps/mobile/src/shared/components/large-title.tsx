import { HeaderHeightContext } from "expo-router/react-navigation";
import { Typography } from "heroui-native";
import { type PropsWithChildren, useContext, useState } from "react";
import { Pressable, View } from "react-native";
import Animated, {
  cubicBezier,
  Extrapolation,
  interpolate,
  type SharedValue,
  useAnimatedReaction,
  useAnimatedScrollHandler,
  useAnimatedStyle,
  useSharedValue,
} from "react-native-reanimated";
import { useSafeAreaInsets } from "react-native-safe-area-context";
import { scheduleOnRN } from "react-native-worklets";
import { SheetScrollEdgeEffect } from "@/shared/components/sheet-scroll-edge-effect";
import { useReducedMotion } from "@/shared/lib/motion";
import { isAndroid, isIOS } from "@/shared/lib/platform";
import { typeScale } from "@/shared/lib/type-scale";

const TITLE_EASE = cubicBezier(0.23, 1, 0.32, 1);
/** The small title shows when the text of the big title reaches the bottom of the bar. */
const TITLE_BOTTOM_PADDING = isAndroid ? 16 : 10;
/** The distance over which the big title fades out under the bar. */
const TITLE_FADE = 24;
const IOS_BAR_HEIGHT = 44;
/** The side room of the small title: the glass bar button, 16 pt from the edge, and a 16 pt gap. */
const BAR_TITLE_INSET = 78;
/** The Material 3 small top app bar puts its title on the 72 dp line, after the navigation button. */
const ANDROID_BAR_TITLE_INSET = 72;
/** The fade under the solid Android bar, where the content goes under it. */
const ANDROID_FADE = 32;

export interface LargeTitleScroll {
  onScroll: ReturnType<typeof useAnimatedScrollHandler>;
  /** How far the content moved up from its rest position. Negative while iOS pulls past the top. */
  scrolled: SharedValue<number>;
  titleHeight: SharedValue<number>;
  headerHeight: number;
}

/**
 * The scroll state of a screen with a big header. The native iOS large title does not work under
 * the custom scroll edge effect on iOS 27, and Android has none, so the screens draw their own: the
 * title scrolls with the content, and a small title shows in the transparent native bar. Android
 * draws it as a Material 3 medium top app bar that collapses into the small bar.
 */
export function useLargeTitleScroll(): LargeTitleScroll {
  const headerHeight = useContext(HeaderHeightContext) ?? 0;
  const scrolled = useSharedValue(0);
  const titleHeight = useSharedValue(0);
  // iOS insets the content under the transparent header, so the content rests at minus the header
  // height. Android pads the content by the header height and rests at zero.
  const rest = isIOS ? -headerHeight : 0;
  const onScroll = useAnimatedScrollHandler((event) => {
    scrolled.set(event.contentOffset.y - rest);
  });
  return { onScroll, scrolled, titleHeight, headerHeight };
}

/** The first item of the scroll content: an optional caption above the big title. */
export function LargeTitle({
  scroll,
  title,
  caption,
  accessibilityLabel,
  onPress,
}: {
  scroll: LargeTitleScroll;
  title: string;
  caption?: string;
  /** Names what a press does, for a title that is a button. */
  accessibilityLabel?: string;
  onPress?: () => void;
}) {
  const reduced = useReducedMotion();
  // Worklets take the shared values only: the scroll handler beside them cannot go to the UI thread.
  const { scrolled, titleHeight } = scroll;
  const titleStyle = useAnimatedStyle(() => {
    const height = titleHeight.get();
    // The big title fades out over its last 24 pt under the bar, where the small title comes in,
    // so the two titles never show together. A stale render keeps it visible, never hidden.
    const opacity =
      height > 0
        ? interpolate(
            scrolled.get(),
            [height - TITLE_BOTTOM_PADDING - TITLE_FADE, height - TITLE_BOTTOM_PADDING],
            [1, 0],
            Extrapolation.CLAMP,
          )
        : 1;
    if (!isIOS || reduced) return { opacity, transform: [{ scale: 1 }] };
    // A pull past the top grows the title a little, as the native large title does.
    return {
      opacity,
      transform: [{ scale: interpolate(scrolled.get(), [-160, 0], [1.08, 1], Extrapolation.CLAMP) }],
    };
  });
  const heading = (
    <Animated.View style={[{ transformOrigin: "left center" }, titleStyle]}>
      <Typography.Heading
        className="font-bold"
        numberOfLines={1}
        adjustsFontSizeToFit
        minimumFontScale={0.82}
        style={isAndroid ? typeScale.largeTitleAndroid : typeScale.largeTitle}
      >
        {title}
      </Typography.Heading>
    </Animated.View>
  );
  return (
    <View
      className={isAndroid ? "px-5 pt-2" : "px-5"}
      style={{ paddingBottom: TITLE_BOTTOM_PADDING }}
      onLayout={(event) => titleHeight.set(event.nativeEvent.layout.height)}
    >
      {caption ? (
        <Typography.Paragraph className="text-muted" numberOfLines={1} style={typeScale.caption}>
          {caption}
        </Typography.Paragraph>
      ) : null}
      {onPress ? (
        <Pressable
          accessibilityRole="button"
          accessibilityLabel={accessibilityLabel}
          hitSlop={8}
          className="self-start"
          style={({ pressed }) => ({ opacity: pressed ? 0.6 : 1 })}
          onPress={onPress}
        >
          {heading}
        </Pressable>
      ) : (
        <View accessible accessibilityRole="header">
          {heading}
        </View>
      )}
    </View>
  );
}

/**
 * The scroll edge effect under the transparent native bar, and the small title that comes into the
 * bar when the big title scrolls under it. The native bar buttons stay native above it. Android has
 * no blur: as the top fade of the design, the surface is solid under the bar and fades out below it.
 */
export function LargeTitleBar({
  scroll,
  title,
  hidden = false,
  insetLeft = isAndroid ? ANDROID_BAR_TITLE_INSET : BAR_TITLE_INSET,
  insetRight = isAndroid ? ANDROID_BAR_TITLE_INSET : BAR_TITLE_INSET,
  surface = "canvas",
}: {
  scroll: LargeTitleScroll;
  title: string;
  /** Hides the small title, for example while the bar shows the connection state in its place. */
  hidden?: boolean;
  insetLeft?: number;
  insetRight?: number;
  surface?: "canvas" | "sheet";
}) {
  const insets = useSafeAreaInsets();
  const reduced = useReducedMotion();
  const [collapsed, setCollapsed] = useState(false);
  const { scrolled, titleHeight } = scroll;
  // The scroll crosses the line on the UI thread; React learns of it only at the crossing. React then
  // owns the style, so a later render cannot put back a stale first-render opacity.
  useAnimatedReaction(
    () => {
      const height = titleHeight.get();
      return height > 0 && scrolled.get() > height - TITLE_BOTTOM_PADDING;
    },
    (next, previous) => {
      if (next !== previous) scheduleOnRN(setCollapsed, next);
    },
  );
  return (
    <>
      {isAndroid ? (
        <>
          <View
            pointerEvents="none"
            className={surface === "sheet" ? "bg-sheet" : "bg-background"}
            style={{ position: "absolute", top: 0, left: 0, right: 0, height: scroll.headerHeight }}
          />
          <SheetScrollEdgeEffect
            surface={surface}
            style={{ position: "absolute", top: scroll.headerHeight, left: 0, right: 0, height: ANDROID_FADE }}
          />
        </>
      ) : (
        <SheetScrollEdgeEffect
          surface={surface}
          style={{
            position: "absolute",
            top: 0,
            left: 0,
            right: 0,
            // The same depth on every surface, so the blur fades as it does in the sheets.
            height: scroll.headerHeight + 48,
          }}
        />
      )}
      {hidden ? null : (
        <Animated.View
          pointerEvents="none"
          accessibilityElementsHidden
          importantForAccessibility="no-hide-descendants"
          style={[
            {
              position: "absolute",
              top: insets.top,
              // iOS centers its bar buttons in the 44 pt under the status bar; the header can be taller.
              height: isIOS ? IOS_BAR_HEIGHT : Math.max(0, scroll.headerHeight - insets.top),
              left: insetLeft,
              right: insetRight,
              justifyContent: "center",
              opacity: collapsed ? 1 : 0,
              transform: [{ translateY: collapsed || reduced ? 0 : 4 }],
              transitionProperty: ["opacity", "transform"],
              transitionDuration: reduced ? 150 : 200,
              transitionTimingFunction: TITLE_EASE,
            },
          ]}
        >
          <Typography.Paragraph
            className={isAndroid ? "font-normal" : "font-semibold"}
            numberOfLines={1}
            style={isAndroid ? typeScale.barTitleAndroid : typeScale.barTitle}
          >
            {title}
          </Typography.Paragraph>
        </Animated.View>
      )}
    </>
  );
}

/** A full screen of grouped content under a big header, such as Routines and Usage. */
export function LargeTitleScrollView({
  title,
  caption,
  contentContainerClassName,
  children,
}: PropsWithChildren<{ title: string; caption?: string; contentContainerClassName?: string }>) {
  const scroll = useLargeTitleScroll();
  return (
    <View className="flex-1 bg-sheet">
      <Animated.ScrollView
        className="flex-1"
        alwaysBounceVertical={false}
        contentInsetAdjustmentBehavior="always"
        contentContainerClassName="pb-safe-offset-5"
        // Android has no content inset, so the content starts below the transparent header.
        contentContainerStyle={isAndroid ? { paddingTop: scroll.headerHeight } : undefined}
        keyboardShouldPersistTaps="handled"
        scrollEventThrottle={16}
        showsVerticalScrollIndicator={false}
        onScroll={scroll.onScroll}
      >
        <LargeTitle scroll={scroll} title={title} caption={caption} />
        <View className={contentContainerClassName}>{children}</View>
      </Animated.ScrollView>
      <LargeTitleBar scroll={scroll} title={title} surface="sheet" />
    </View>
  );
}
