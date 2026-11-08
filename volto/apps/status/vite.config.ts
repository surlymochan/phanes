import solidPlugin from "@solidjs/vite-plugin";
import { defineConfig } from "vite";

export default defineConfig({
  resolve: { dedupe: ["solid-js", "@solidjs/web", "@solidjs/signals"] },
  plugins: [solidPlugin()],
  // Relative URLs, so the page works at the Pages address and at its custom domain.
  base: "./",
  build: { outDir: "dist", emptyOutDir: true },
});
