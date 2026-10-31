import { createFileRoute, notFound } from "@tanstack/solid-router";
import { DownloadPlatformPage } from "../../components/download/DownloadPlatformPage";
import type { DownloadPageContent } from "../../lib/download-pages";

// In `loader` for the reason given in routes/news/$slug.tsx: an unknown system is a real not-found
// response, not a 200 with an error card. `loader` and `head` import the pages when they run, as in
// routes/compare/$slug.tsx.
async function loadDownloadPage(platform: string): Promise<DownloadPageContent> {
  const { DOWNLOAD_PAGE_ORDER, DOWNLOAD_PAGES } = await import("../../lib/download-pages");
  const known = DOWNLOAD_PAGE_ORDER.find((candidate) => candidate === platform);
  if (!known) throw notFound();
  return DOWNLOAD_PAGES[known];
}

export const Route = createFileRoute("/download/$platform")({
  loader: ({ params }) => loadDownloadPage(params.platform),
  head: async ({ loaderData, match }) => {
    if (!loaderData) return {};
    const { downloadPageHead } = await import("../../lib/download-pages");
    return downloadPageHead(loaderData.platform, match.context.siteUrl);
  },
  component: DownloadPlatformRoute,
});

function DownloadPlatformRoute() {
  const page = Route.useLoaderData();
  return <DownloadPlatformPage page={page()} />;
}
