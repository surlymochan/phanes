import type { PartialTranslation } from "../../../message";
import type { messages as source } from "../../en/status/host";

export const messages = {
  "status.host.registered": "Bu volto, WebRTC erişimi için kaydedildi.",
  "status.host.keyRotated": "Sunucu anahtarı değiştirildi. Eşlenmiş cihazlar eşli kalır.",
  "status.host.identityUpdated": "Sunucu kimliği güncellendi.",
  "status.host.starting": "WebRTC ana makinesi başlatılıyor…",
  "status.host.ready": "Bu volto, WebRTC bağlantıları için hazır.",
  "status.host.stopping": "Bu volto özel yapılıyor…",
  "status.host.private": "Bu volto özel.",
} as const satisfies PartialTranslation<typeof source>;
