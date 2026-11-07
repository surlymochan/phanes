/// <reference types="vite/client" />

import type { VoltoDesktopApi } from "@volto/contracts/ipc";

declare global {
  interface HighlightRegistry {
    set(name: string, highlight: Highlight): this;
    delete(name: string): boolean;
  }

  interface Window {
    volto: VoltoDesktopApi;
  }
}
