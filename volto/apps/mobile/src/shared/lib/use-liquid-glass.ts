import { isLiquidGlassAvailable } from "expo-glass-effect";
import { useReducedTransparency } from "./use-reduced-transparency";

/**
 * Whether glass controls can draw as glass: the system has Liquid Glass and Reduce Transparency
 * is off. Until the setting is known, controls draw solid, as with Reduce Transparency on.
 */
export function useLiquidGlass(): boolean {
  const reducedTransparency = useReducedTransparency();
  return isLiquidGlassAvailable() && !reducedTransparency;
}
