declare module "@volto/renderer-preview" {
  import type { JSX } from "@solidjs/web";

  export interface VoltoPlaygroundProps {
    variant?: "default" | "landing";
    theme?: "dark" | "light";
  }

  export function VoltoPlayground(props: VoltoPlaygroundProps): JSX.Element;
}
