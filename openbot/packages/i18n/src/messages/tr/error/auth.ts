import type { PartialTranslation } from "../../../message";
import type { messages as source } from "../../en/error/auth";

export const messages = {
  // OpenBot hesap servisinden gelen hatalar.
  "error.auth.serviceUnavailable":
    "OpenBot hesap hizmetine ulaşamadı. API'nin çalıştığını kontrol edin, ardından tekrar deneyin.",
  "error.auth.networkBlocked":
    "Bu ağdaki bir güvenlik duvarı veya proxy, OpenBot'un {host} adresine ulaşmasını engelledi. Ağ yöneticinizden {host} adresine izin vermesini isteyin, ardından tekrar deneyin.",
  "error.auth.signInFirst": "Önce OpenBot'ta oturum açın.",
  "error.auth.signInRequired": "Oturum açılması gerekiyor.",
  "error.auth.accountChangedDuringRegister": "Bu sunucu kaydedilirken oturum açmış olan hesap değişti.",
  "error.auth.hostCredentialUnavailable":
    "Uzak ana makine kimlik bilgisi kullanılamıyor. Ana makineyi tekrar kaydedin.",
  "error.auth.codeNotVerified": "Giriş kodu doğrulanamadı.",
  "error.auth.serviceError": "Hesap hizmeti bir hata döndürdü.",
  "error.auth.serviceUnreachable":
    "OpenBot hesap hizmetine ulaşamadı. Bağlantınızı kontrol edin, ardından tekrar deneyin.",
  "error.auth.serviceTimeout": "Hesap hizmeti zamanında yanıt vermedi. Tekrar deneyin.",
  "error.auth.serviceStatus": "Hesap hizmeti bir hata döndürdü ({status}). Daha sonra tekrar deneyin.",
  "error.auth.invalidHostedServer": "Hesap hizmeti geçersiz bir barındırılan sunucu döndürdü.",
  "error.auth.codeNotSent": "OpenBot giriş kodunu gönderemedi.",
  "error.auth.deliveryTimeout":
    "OpenBot teslimatı zamanında onaylayamadı. Kod yine de gelebilir; tekrar göndermeden önce teslimatı kontrol edin.",
  "error.auth.deliveryInterrupted":
    "OpenBot teslimatı onaylamadan önce bağlantı kesildi. Başka bir kod göndermekten kaçınmak için teslimatı kontrol edin.",
  "error.auth.deliveryUnknown":
    "OpenBot giriş kodunun gönderilip gönderilmediğini onaylayamadı. Tekrar göndermeden önce teslimatı kontrol edin.",
  "error.auth.adminKeyRequired": "Hesap hizmetinin yönetici anahtarını girin.",
  "error.auth.adminKeyRejected": "Hesap hizmeti yönetici anahtarını kabul etmedi.",
  "error.auth.sessionNotStorable":
    "OpenBot oturum bilgisini bu bilgisayarda saklayamıyor, bu yüzden yönetici anahtarıyla oturum açamıyor.",
  "error.auth.hostedServerNoAnswer": "Hesap sunucusu başlangıçta yanıt vermedi.",
  "error.auth.hostedServerNoClaim": "Barındırılan sunucunun oturumu kapalı ve erişim belirteci yok.",
  "error.auth.hostedServerClaimMismatch": "Erişim belirteci başka bir barındırılan sunucu için.",
  "error.auth.hostedServerHostMismatch":
    "Yapılandırılmış ana makine, bu sanal makine için oluşturulan barındırılan sunucu değil.",
  "error.auth.hostedServerUnknown": "Hesap sunucusunda bu barındırılan sunucu için kayıt yok.",
  "error.auth.invalidUser": "Hesap hizmeti geçersiz bir kullanıcı döndürdü.",
  "error.auth.invalidHostedServerClaim": "Geçersiz barındırılan sunucu erişim belirteci.",
  "error.auth.sessionNotStored": "Oturum bilgisi kaydedilemedi.",
} as const satisfies PartialTranslation<typeof source>;
