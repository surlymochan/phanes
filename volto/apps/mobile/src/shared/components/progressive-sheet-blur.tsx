import MaskedView from "@react-native-masked-view/masked-view";
import { BlurView } from "expo-blur";
import { memo, useId } from "react";
import { type StyleProp, StyleSheet, useColorScheme, View, type ViewStyle } from "react-native";
import Svg, { Defs, LinearGradient, Rect, Stop } from "react-native-svg";

// Overlapping weak blurs approximate a radius ramp. Each layer is transparent
// before the physical view edge, so clipping cannot leave a visible seam.
const OUTER_BLUR_LAYER_END = 0.94;
const BLUR_LAYER_ENDS = [OUTER_BLUR_LAYER_END, 0.8, 0.66, 0.52, 0.38, 0.24];
// The dark system material is lighter than the dark surface, so a masked
// surface color pulls the blurred edge back to the surface's tone.
const DARK_SHEET_TINT_OPACITY = 0.7;
const FADE_STOPS = Array.from({ length: 17 }, (_, index) => {
  const progress = index / 16;
  return { progress, opacity: 1 - progress * progress * (3 - 2 * progress) };
});

export const ProgressiveSheetBlur = memo(function ProgressiveSheetBlur({
  style,
  edge = "top",
  surface = "sheet",
}: {
  style: StyleProp<ViewStyle>;
  edge?: "top" | "bottom";
  /** The color of the dark tint: a sheet, or the canvas of a full screen. */
  surface?: "canvas" | "sheet";
}) {
  const id = useId().replaceAll(":", "");
  const colorScheme = useColorScheme();

  function fadeMask(maskId: string, end: number) {
    return (
      <Svg width="100%" height="100%">
        <Defs>
          <LinearGradient
            id={maskId}
            x1="0%"
            x2="0%"
            y1={edge === "top" ? "0%" : "100%"}
            y2={edge === "top" ? "100%" : "0%"}
          >
            {FADE_STOPS.map(({ progress, opacity }) => (
              <Stop key={progress} offset={progress * end} stopColor="#000000" stopOpacity={opacity} />
            ))}
          </LinearGradient>
        </Defs>
        <Rect width="100%" height="100%" fill={`url(#${maskId})`} />
      </Svg>
    );
  }

  return (
    <View
      pointerEvents="none"
      accessibilityElementsHidden
      importantForAccessibility="no-hide-descendants"
      style={style}
    >
      {BLUR_LAYER_ENDS.map((end, index) => {
        const maskId = `sheet-blur-${id}-${index}`;
        return (
          <MaskedView key={end} style={StyleSheet.absoluteFill} maskElement={fadeMask(maskId, end)}>
            <BlurView tint="systemUltraThinMaterial" intensity={12} style={StyleSheet.absoluteFill} />
          </MaskedView>
        );
      })}
      {colorScheme === "dark" ? (
        <MaskedView style={StyleSheet.absoluteFill} maskElement={fadeMask(`sheet-tint-${id}`, OUTER_BLUR_LAYER_END)}>
          <View
            className={surface === "sheet" ? "flex-1 bg-sheet" : "flex-1 bg-background"}
            style={{ opacity: DARK_SHEET_TINT_OPACITY }}
          />
        </MaskedView>
      ) : null}
    </View>
  );
});
