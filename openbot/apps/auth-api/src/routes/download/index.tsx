import { createFileRoute } from "@tanstack/solid-router";
import { DownloadHubPage } from "../../components/download/DownloadHubPage";

// `head` imports the pages when it runs, as in routes/compare/$slug.tsx.
export const Route = createFileRoute("/download/")({
  head: async ({ match }) => {
    const { downloadPageHead } = await import("../../lib/download-pages");
    return downloadPageHead("hub", match.context.siteUrl);
  },
  component: DownloadHubPage,
});
