/** Development builds get their push tokens from the APNs sandbox. */
export function pushEnvironment(): "production" | "development" {
  return __DEV__ || process.env.EXPO_PUBLIC_APP_ENV === "development" ? "development" : "production";
}
