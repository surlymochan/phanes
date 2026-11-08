import type { PartialTranslation } from "../../../message";
import type { messages as source } from "../../en/status/host";

export const messages = {
  "status.host.registered": "Deze volto is geregistreerd voor WebRTC-toegang.",
  "status.host.keyRotated": "Serversleutel gewijzigd. Gekoppelde apparaten blijven gekoppeld.",
  "status.host.identityUpdated": "Serveridentiteit bijgewerkt.",
  "status.host.starting": "WebRTC-host starten…",
  "status.host.ready": "Deze volto is klaar voor WebRTC-verbindingen.",
  "status.host.stopping": "Deze volto privé maken…",
  "status.host.private": "Deze volto is privé.",
} as const satisfies PartialTranslation<typeof source>;
