import type { PartialTranslation } from "../../../message";
import type { messages as source } from "../../en/status/agent";

export const messages = {
  "status.agent.dispatchApproval":
    "Bu kod gönderimi riskli bir işlem içeriyor. Onaylamadan önce kartını ve argümanlarını inceleyin.",
  "status.agent.dispatchStarted": "Kodlama işi başladı: {title}. Ana makine: {host}. Çalıştırma: {runId}.",
  "status.agent.dispatchFinished":
    "Kodlama işi bitti: {title}. Ana makine: {host}. Sonuç: {status}. Başarı bildirmeden önce sonucu inceleyin.",
  "status.agent.dispatchNoResult": "openswarm yürütücüsü sonuç dosyası olmadan bitti.",
  "status.agent.claudeWriteOutside":
    "Ajanın çalışma alanı, paylaşılan klasör ve geçici klasörler dışındaki {path} konumuna yaz.",
  "status.agent.contextCleared": "Bağlam temizlendi. Yeni bir sohbet burada başlıyor.",
  "status.agent.modelSwitchedForLimit": "Kullanım sınırına ulaşıldı. {model} modeline geçildi.",
  "status.agent.modelReturnedAfterLimit": "Kullanım sınırı bitti. {model} modeline geri dönüldü.",
  "status.agent.pausedHourBudget":
    "Token bütçesine ulaşıldı: son bir saatte {used} token kullanıldı (sınır {limit}). Siz devam edene kadar yeni iş bekler.",
  "status.agent.pausedTurnBudget":
    "Bir adımdan sonra durdu: bu tur {used} token kullandı (sınır {limit}). İş saklanır. Devam ederek ilerleyin.",
  "status.agent.pausedPlanLimit": "Plan sınırı bu turu durdurdu. İş saklanır ve sınır bitince devam eder.",
  "status.agent.modelSwitchedForError": "Sağlayıcı hatası. {model} modeline geçildi.",
  "status.agent.modelReturnedAfterError": "Sağlayıcı hatasından sonra {model} modeline geri dönüldü.",
  "status.agent.marketplaceSuggested": "Bir Pazaryeri uygulaması önerildi: {app}.",
  "status.agent.channelMemberAdded": "{agent}, {member} ajanını kanala ekledi. İsteyen: {person}.",
  "status.agent.channelMemberRemoved": "{agent}, {member} ajanını kanaldan çıkardı. İsteyen: {person}.",
  "status.agent.channelInstructionsChanged": "{agent}, kanal talimatlarını değiştirdi. İsteyen: {person}.",
  "status.agent.channelRoutineCreated": "{agent}, {routine} rutinini oluşturdu. İsteyen: {person}.",
  "status.agent.channelRoutineUpdated": "{agent}, {routine} rutinini değiştirdi. İsteyen: {person}.",
  "status.agent.channelRoutineDeleted": "{agent}, {routine} rutinini sildi. İsteyen: {person}.",
  "status.agent.screenCaptureScreen": "Ana ekranı yakala ve görüntüyü ajanın modeline gönder.",
  "status.agent.screenCaptureChannel":
    "Görüntü {channel} kanalında görünür ve bu sunucuda kanalı okuyan her üye onu görebilir.",
  "status.agent.screenCaptureDisplay": "{name} ekranını yakala ve görüntüyü ajanın modeline gönder.",
  "status.agent.screenCaptureWindow": "{name} penceresini yakala ve görüntüyü ajanın modeline gönder.",
  "status.agent.screenCaptureRegion":
    "{x}, {y} konumundaki {width} × {height} noktalık ekran alanını yakala ve görüntüyü ajanın modeline gönder.",
  "status.agent.screenCaptureProgress": "Ekranı yakalamak için izin isteniyor…",
  "status.agent.screenCaptureReviewing": "Ekran görüntüsü inceleniyor…",
  "status.agent.starting": "Yerel ajan CLI'ları başlatılıyor…",
  "status.agent.memoriesSearching": "Belleklerinde arıyor…",
  "status.agent.memoriesSearched": {
    one: "Belleklerinde aradı: {count} bulundu",
    other: "Belleklerinde aradı: {count} bulundu",
  },
  "status.agent.memoriesUpdating": "Belleklerini güncelliyor…",
  "status.agent.skillUsing": "{name} becerisini kullanıyor…",
  "status.agent.skillReading": "Bir beceri okuyor…",
  "status.agent.memoryChanges": "Bellek: {saved} kaydedildi, {updated} güncellendi, {forgotten} unutuldu.",
  "status.agent.memoryChangesWithConflicts":
    "Bellek: {saved} kaydedildi, {updated} güncellendi, {forgotten} unutuldu, {notChanged} değiştirilmedi çünkü önce başka bir mesaj dizisi değiştirdi.",
} as const satisfies PartialTranslation<typeof source>;
