// The app's structured data, with the two facts that `site-metadata.ts` can not
// import: the version, from the repository's `package.json`, and a picture of the app.
// A named import, so the bundle holds the version and not the whole file. The
// release script changes it and `CHANGELOG.md` in one step.

import { version } from "../../../../package.json";
import channelScreenshot from "../content/guides/media/openbot-101/a-channel.webp";
import { VOLTO_SITE_URL, voltoSoftwareApplication } from "./site-metadata";

export const VOLTO_SOFTWARE_APPLICATION = voltoSoftwareApplication({
  version,
  screenshot: new URL(channelScreenshot, VOLTO_SITE_URL).toString(),
});
