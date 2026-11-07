import type { PartialTranslation } from "../../../message";
import type { messages as source } from "../../en/error/billing";

export const messages = {
  "error.billing.lifecycleFailed": "Не удалось изменить тариф сервера. Обновите раздел «Оплата» и повторите попытку.",
  "error.billing.confirmMismatch": "Введите название сервера, чтобы удалить его.",
  "error.billing.restartBusy": "Сейчас сервер нельзя перезапустить. Повторите попытку через минуту.",
  "error.billing.planRequired": "Тариф этого сервера закончился. Продлите его, чтобы запустить сервер.",
  "error.billing.planSoldOut": "Этот тариф распродан. Выберите другой тариф.",
  "error.billing.licenseExists": "У этого аккаунта уже есть лицензия. Вместо этого измените число пользователей.",
  "error.billing.licenseCurrency": "Лицензия должна использовать валюту ваших других планов.",
  "error.billing.licenseUsersInUse":
    "В лицензии не может быть меньше пользователей, чем людей, которые сейчас ею пользуются.",
  "error.billing.licensePaymentFailed":
    "Оплата за добавленных пользователей не прошла. Обновите способ оплаты и попробуйте снова.",
  "error.billing.licenseUnavailable": "Лицензии сейчас недоступны.",
  "error.billing.noLicense": "У этого аккаунта нет лицензии.",
  "error.billing.invalidRequest": "Некорректный запрос оплаты.",
  "error.billing.invalidResponse": "Некорректный ответ оплаты.",
  "error.billing.notStripePage": "Страница оплаты не принадлежит Stripe.",
} as const satisfies PartialTranslation<typeof source>;
