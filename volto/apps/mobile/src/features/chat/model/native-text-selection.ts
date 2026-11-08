import { isRunningInExpoGo } from "expo";
import { isIOS } from "@/shared/lib/platform";

/**
 * Whether message text is a native iOS text view that selects a range in place. iOS `Text` copies
 * only a whole paragraph, so the chat uses `UITextView` from `react-native-uitextview`. Expo Go
 * does not include that native view: there, and on Android, the chat keeps the read-only text
 * field that covers the message while the reader selects.
 */
export const nativeTextSelection = isIOS && !isRunningInExpoGo();
