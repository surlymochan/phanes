import type { PartialTranslation } from "../../../message";
import type { messages as source } from "../../en/error/update";

export const messages = {
  "error.update.unsupported": "アップデートはインストール済みのデスクトップ版でご利用いただけます。",
  "error.update.notReady": "インストールできるアップデートがありません。",
  "error.update.restartFailed": "アップデートをインストールするための再起動ができませんでした。",
  "error.update.downloadStalled": "アップデートのダウンロードが応答しなくなりました。もう一度お試しください。",
  "error.update.installFailed":
    "アップデートをインストールできませんでした。volto を終了してから開き直し、もう一度お試しください。",
  "error.update.downloadFailed": "アップデートをダウンロードできませんでした。もう一度お試しください。",
  "error.update.checkFailed": "アップデートを確認できませんでした。もう一度お試しください。",
  "error.update.checkStalled": "アップデートの確認が応答しなくなりました。もう一度お試しください。",
  "error.update.checkOffline":
    "アップデートサービスに接続できませんでした。インターネット接続を確認してから、もう一度お試しください。",
  "error.update.checkUnavailable":
    "アップデートサービスから応答がありませんでした。数分後に volto が自動でもう一度試します。",
  "error.update.checkNoRelease":
    "このプラットフォーム向けに公開されたアップデートは見つかりませんでした。数分後に volto が自動でもう一度試します。",
  "error.update.managedByNix": "この volto のアップデートは Nix がインストールします。Nix で更新してください。",
  "error.update.managedByHost":
    "この Mac のアップデートはホストがインストールします。ホストのメンテナンスが実行されるまで、アップデートは準備済みのままです。",
  "error.update.siblingSession":
    "このアプリケーションから別の volto セッションがまだ実行中です。先に他のすべての macOS ユーザーアカウントで volto を終了してから、アップデートをもう一度インストールしてください。",
  "error.update.siblingSessionSameAccount":
    "このユーザーアカウントで別の volto プロセスがまだ実行中です。それを終了してから、もう一度アップデートをインストールしてください。",
  "error.update.siblingCheckFailed":
    "他の volto セッションを確認できませんでした。インストールする前にもう一度お試しください。",
  "error.update.remoteDisabled": "このコンピューターでは、サーバー管理者からのアップデートはオフになっています。",
  "error.update.restartStarted": "volto はアップデートをインストールするため、すでに再起動しています。",
  "error.update.alreadyRestarting": "volto はすでに再起動しています。",
  "error.update.downloadCancelled": "ダウンロードはキャンセルされました。",
  "error.update.noStagedRelease": "サーバーはリリースを準備しませんでした。",
  "error.update.feedUnavailable": "リリースフィードを利用できません",
  "error.update.feedTooLarge": "リリースフィードが大きすぎます",
  "error.update.noCompatibleRelease": "互換性のあるリリースはありません",
  "error.update.noReleaseForMachine": "このマシン向けのリリースはありません",
} as const satisfies PartialTranslation<typeof source>;
