import { createFileRoute } from "@tanstack/solid-router";

export const Route = createFileRoute("/app-preview")({
  head: () => ({
    meta: [{ title: "volto application preview" }, { name: "robots", content: "noindex, follow" }],
  }),
  headers: () => ({ "X-Robots-Tag": "noindex, follow" }),
});
