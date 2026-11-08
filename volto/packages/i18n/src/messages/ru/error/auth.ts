import type { PartialTranslation } from "../../../message";
import type { messages as source } from "../../en/error/auth";

export const messages = {
  "error.auth.serviceUnavailable":
    "volto не смог связаться со службой аккаунтов. Убедитесь, что API работает, и повторите попытку.",
  "error.auth.networkBlocked":
    "Брандмауэр или прокси в этой сети не дал volto подключиться к {host}. Попросите сетевого администратора разрешить {host} и повторите попытку.",
  "error.auth.signInFirst": "Сначала войдите в volto.",
  "error.auth.signInRequired": "Требуется вход.",
  "error.auth.accountChangedDuringRegister":
    "Аккаунт, в который выполнен вход, изменился во время регистрации этого сервера.",
  "error.auth.hostCredentialUnavailable": "Учётные данные удалённого хоста недоступны. Зарегистрируйте хост заново.",
  "error.auth.codeNotVerified": "Не удалось проверить код входа.",
  "error.auth.serviceError": "Служба аккаунтов вернула ошибку.",
  "error.auth.serviceUnreachable":
    "volto не смог связаться со службой аккаунтов. Проверьте подключение и повторите попытку.",
  "error.auth.serviceTimeout": "Служба аккаунтов не ответила вовремя. Повторите попытку.",
  "error.auth.serviceStatus": "Служба аккаунтов вернула ошибку ({status}). Повторите попытку позже.",
  "error.auth.invalidHostedServer": "Служба аккаунтов вернула некорректный облачный сервер.",
  "error.auth.codeNotSent": "volto не смог отправить код входа.",
  "error.auth.deliveryTimeout":
    "volto не успел подтвердить доставку. Код всё же может прийти; проверьте доставку, прежде чем отправлять снова.",
  "error.auth.deliveryInterrupted":
    "Соединение оборвалось, прежде чем volto подтвердил доставку. Проверьте доставку, чтобы не отправлять код повторно.",
  "error.auth.deliveryUnknown":
    "volto не смог подтвердить, отправлен ли код входа. Проверьте доставку, прежде чем отправлять снова.",
  "error.auth.adminKeyRequired": "Введите ключ администратора сервиса аккаунтов.",
  "error.auth.adminKeyRejected": "Сервис аккаунтов не принял ключ администратора.",
  "error.auth.sessionNotStorable":
    "volto не может сохранить сеанс входа на этом компьютере, поэтому он не может войти с ключом администратора.",
  "error.auth.hostedServerNoAnswer": "Сервер аккаунтов не ответил в начале.",
  "error.auth.hostedServerNoClaim": "Облачный сервер вышел из учётной записи и не имеет токена доступа.",
  "error.auth.hostedServerClaimMismatch": "Токен доступа относится к другому облачному серверу.",
  "error.auth.hostedServerHostMismatch": "Настроенный хост — не тот облачный сервер, для которого создана эта ВМ.",
  "error.auth.hostedServerUnknown": "На сервере аккаунтов нет записи об этом облачном сервере.",
  "error.auth.invalidUser": "Сервис аккаунтов вернул некорректного пользователя.",
  "error.auth.invalidHostedServerClaim": "Некорректный токен доступа облачного сервера.",
  "error.auth.sessionNotStored": "Не удалось сохранить сеанс.",
} as const satisfies PartialTranslation<typeof source>;
