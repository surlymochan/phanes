import { useEffect } from "react";
import { useAnimatedStyle, useSharedValue, withSequence, withTiming } from "react-native-reanimated";

import { SIGN_IN_EASE_OUT } from "@/features/auth/components/sign-in-motion";

/**
 * A short horizontal shake each time `rejection` grows, for a value that the service refused.
 * Reduced motion keeps only the color change of the field.
 */
export function useShake(rejection: number, reduced: boolean) {
  const offset = useSharedValue(0);
  useEffect(() => {
    if (rejection === 0 || reduced) return;
    offset.set(
      withSequence(
        withTiming(-8, { duration: 50 }),
        withTiming(8, { duration: 60 }),
        withTiming(-6, { duration: 60 }),
        withTiming(6, { duration: 60 }),
        withTiming(0, { duration: 70, easing: SIGN_IN_EASE_OUT }),
      ),
    );
  }, [offset, reduced, rejection]);
  return useAnimatedStyle(() => ({ transform: [{ translateX: offset.get() }] }));
}
