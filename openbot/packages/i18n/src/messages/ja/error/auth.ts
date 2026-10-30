import type { PartialTranslation } from "../../../message";
import type { messages as source } from "../../en/error/auth";

export const messages = {
  // Errors from the OpenBot account service.
  "error.auth.serviceUnavailable":
    "OpenBot がアカウントサービスに接続できませんでした。API が動作していることを確認してから、再試行してください。",
  "error.auth.networkBlocked":
    "このネットワークのファイアウォールまたはプロキシが、OpenBot から {host} への接続をブロックしました。ネットワーク管理者に {host} の許可を依頼してから、再試行してください。",
  "error.auth.signInFirst": "先に OpenBot にサインインしてください。",
  "error.auth.signInRequired": "サインインが必要です。",
  "error.auth.accountChangedDuringRegister": "このサーバーの登録中に、サインイン中のアカウントが変わりました。",
  "error.auth.hostCredentialUnavailable": "リモートホストの認証情報を利用できません。ホストを再登録してください。",
  "error.auth.codeNotVerified": "サインインコードを確認できませんでした。",
  "error.auth.serviceError": "アカウントサービスがエラーを返しました。",
  "error.auth.serviceUnreachable":
    "OpenBot はアカウントサービスに接続できませんでした。接続を確認してから、もう一度お試しください。",
  "error.auth.serviceTimeout": "アカウントサービスが時間内に応答しませんでした。もう一度お試しください。",
  "error.auth.serviceStatus": "アカウントサービスがエラー ({status}) を返しました。後でもう一度お試しください。",
  "error.auth.invalidHostedServer": "アカウントサービスが無効なホスト型サーバーを返しました。",
  "error.auth.codeNotSent": "OpenBot がサインインコードを送信できませんでした。",
  "error.auth.deliveryTimeout":
    "OpenBot が時間内に配信を確認できませんでした。コードが後で届く場合があります。再送信する前に配信を確認してください。",
  "error.auth.deliveryInterrupted":
    "OpenBot が配信を確認する前に接続が終了しました。別のコードを送信しないように、配信を確認してください。",
  "error.auth.deliveryUnknown":
    "サインインコードが送信されたかどうかを OpenBot が確認できませんでした。再送信する前に配信を確認してください。",
  "error.auth.adminKeyRequired": "アカウントサービスの管理者キーを入力してください。",
  "error.auth.adminKeyRejected": "アカウントサービスが管理者キーを受け付けませんでした。",
  "error.auth.sessionNotStorable":
    "OpenBot はこのコンピューターにサインインセッションを保存できないため、管理者キーでサインインできません。",
  "error.auth.hostedServerNoAnswer": "アカウントサーバーが開始時に応答しませんでした。",
  "error.auth.hostedServerNoClaim": "ホスト型サーバーはサインアウトしており、アクセス資格情報がありません。",
  "error.auth.hostedServerClaimMismatch": "このアクセス資格情報は別のホスト型サーバー用です。",
  "error.auth.hostedServerHostMismatch":
    "設定されたホストは、この VM に対して作成されたホスト型サーバーではありません。",
  "error.auth.hostedServerUnknown": "アカウントサーバーにこのホスト型サーバーの記録がありません。",
  "error.auth.invalidUser": "アカウントサービスが無効なユーザーを返しました。",
  "error.auth.invalidHostedServerClaim": "無効なホスト型サーバーのアクセス資格情報です。",
  "error.auth.sessionNotStored": "セッションを保存できませんでした。",
} as const satisfies PartialTranslation<typeof source>;
