/**
 * The `postMessage` types between the landing page and the app preview in its iframe. The preview
 * sends ready when it has painted, and the landing page sends start when its reveal ends.
 */
export const LANDING_PREVIEW_READY_MESSAGE = "volto:landing-preview-ready";
export const LANDING_PREVIEW_START_MESSAGE = "volto:landing-preview-start";
