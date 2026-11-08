import { resolve } from "node:path";
import solidPlugin from "@solidjs/vite-plugin";
import tailwindcss from "@tailwindcss/vite";
import { defineConfig, externalizeDepsPlugin } from "electron-vite";

// Keep the former environment variable prefix working for existing build scripts.
for (const [name, value] of Object.entries(process.env)) {
  if (name.startsWith("volto_")) {
    const voltoName = `VOLTO_${name.slice("volto_".length)}`;
    process.env[voltoName] ??= value;
  }
}

const rendererPort = readRendererPort(process.env.VOLTO_DEV_RENDERER_PORT ?? process.env.OPENBOT_DEV_RENDERER_PORT);

export default defineConfig({
  main: {
    // Workspace sources ship as TypeScript and must be bundled for the packaged app.
    plugins: [
      externalizeDepsPlugin({
        exclude: [
          "@volto/brand",
          "@volto/contracts",
          "@volto/i18n",
          "@volto/logging",
          "@volto/team-client",
          "@volto/telemetry",
          "@volto/user-errors",
        ],
      }),
    ],
    build: {
      rollupOptions: {
        input: {
          index: resolve("src/main/index.ts"),
          // The database host is its own process. Only node:* and the installed Effect package
          // are runtime imports; the unpacked host must not load the main process bundle.
          "agent-database-host": resolve("src/backend/agent-data/agent-database-host.ts"),
          // The voice hosts are their own processes too. They import only node:* and load the
          // sherpa-onnx addon by path.
          "voice-transcription-host": resolve("src/main/voice-transcription-host.ts"),
          "voice-speech-host": resolve("src/main/voice-speech-host.ts"),
        },
      },
    },
  },
  preload: {
    plugins: [externalizeDepsPlugin({ exclude: ["@volto/contracts"] })],
    build: {
      rollupOptions: {
        input: {
          index: resolve("src/preload/index.ts"),
          teamWebrtc: resolve("src/preload/team-webrtc.ts"),
        },
        output: {
          format: "cjs",
          entryFileNames: "[name].cjs",
        },
      },
    },
  },
  renderer: {
    plugins: [solidPlugin(), tailwindcss({ optimize: false })],
    optimizeDeps: {
      include: ["@norbert_bodziony/bloub"],
    },
    resolve: {
      dedupe: ["solid-js", "@solidjs/web"],
    },
    ...(rendererPort ? { server: { port: rendererPort, strictPort: true } } : {}),
    build: {
      rollupOptions: {
        input: {
          index: resolve("src/renderer/index.html"),
          helper: resolve("src/renderer/helper.html"),
          browserPip: resolve("src/renderer/browser-pip.html"),
          browserPipControls: resolve("src/renderer/browser-pip-controls.html"),
          teamWebrtc: resolve("src/renderer/team-webrtc.html"),
        },
      },
    },
  },
});

function readRendererPort(value: string | undefined): number | undefined {
  if (value === undefined) return undefined;
  const port = Number(value);
  if (!Number.isInteger(port) || port < 1_024 || port > 65_535) {
    throw new Error("VOLTO_DEV_RENDERER_PORT must be an integer from 1024 to 65535.");
  }
  return port;
}
