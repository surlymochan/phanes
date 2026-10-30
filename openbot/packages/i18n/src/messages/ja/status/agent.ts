import type { PartialTranslation } from "../../../message";
import type { messages as source } from "../../en/status/agent";

export const messages = {
  "status.agent.dispatchApproval":
    "このコード送信にはリスクの高い操作が含まれます。承認前にカードと引数を確認してください。",
  "status.agent.dispatchStarted": "コーディング作業を開始しました: {title}。ホスト: {host}。実行: {runId}。",
  "status.agent.dispatchFinished":
    "コーディング作業が終了しました: {title}。ホスト: {host}。結果: {status}。成功を報告する前に結果を確認してください。",
  "status.agent.dispatchNoResult": "openswarm の実行は結果ファイルなしで終了しました。",
  "status.agent.claudeWriteOutside":
    "{path} に書き込みます。これはエージェントのワークスペース、共有フォルダー、一時フォルダーの外です。",
  "status.agent.contextCleared": "コンテキストを消去しました。ここから新しいチャットが始まります。",
  "status.agent.modelSwitchedForLimit": "使用量の上限に達しました。{model} に切り替えました。",
  "status.agent.modelReturnedAfterLimit": "使用量の上限が解除されました。{model} に戻しました。",
  "status.agent.pausedHourBudget":
    "トークン上限に達しました: 過去 1 時間で {used} トークンを使いました（上限 {limit}）。続けるまで新しい作業は待機します。",
  "status.agent.pausedTurnBudget":
    "手順の後に停止しました: このターンは {used} トークンを使いました（上限 {limit}）。作業内容は保持されます。続行すると先に進みます。",
  "status.agent.pausedPlanLimit":
    "プランの上限でこのターンを停止しました。作業内容は保持され、上限が終わると再開します。",
  "status.agent.modelSwitchedForError": "プロバイダーのエラーが発生しました。{model} に切り替えました。",
  "status.agent.modelReturnedAfterError": "プロバイダーのエラーの後、{model} に戻しました。",
  "status.agent.marketplaceSuggested": "マーケットプレイスのアプリを提案しました: {app}。",
  "status.agent.channelMemberAdded": "{agent} が {member} をチャンネルに追加しました。{person} の依頼です。",
  "status.agent.channelMemberRemoved": "{agent} が {member} をチャンネルから外しました。{person} の依頼です。",
  "status.agent.channelInstructionsChanged": "{agent} がチャンネルの指示を変更しました。{person} の依頼です。",
  "status.agent.channelRoutineCreated": "{agent} がルーティン {routine} を作成しました。{person} の依頼です。",
  "status.agent.channelRoutineUpdated": "{agent} がルーティン {routine} を変更しました。{person} の依頼です。",
  "status.agent.channelRoutineDeleted": "{agent} がルーティン {routine} を削除しました。{person} の依頼です。",
  "status.agent.screenCaptureScreen": "メイン画面をキャプチャし、その画像をエージェントのモデルに送信します。",
  "status.agent.screenCaptureChannel":
    "画像はチャンネル {channel} に表示され、このサーバーでチャンネルを読むメンバー全員が見ることができます。",
  "status.agent.screenCaptureDisplay":
    "ディスプレイ {name} をキャプチャし、その画像をエージェントのモデルに送信します。",
  "status.agent.screenCaptureWindow": "ウィンドウ {name} をキャプチャし、その画像をエージェントのモデルに送信します。",
  "status.agent.screenCaptureRegion":
    "画面の {x}, {y}, {width} × {height} ポイントの領域をキャプチャし、その画像をエージェントのモデルに送信します。",
  "status.agent.screenCaptureProgress": "画面のキャプチャを要求しています…",
  "status.agent.screenCaptureReviewing": "画面のキャプチャを確認しています…",
  "status.agent.starting": "ローカルエージェントの CLI を起動しています…",
  "status.agent.memoriesSearching": "メモリーを検索しています…",
  "status.agent.memoriesSearched": { other: "メモリーを検索しました: {count} 件見つかりました" },
  "status.agent.memoriesUpdating": "メモリーを更新しています…",
  "status.agent.skillUsing": "スキル {name} を使用しています…",
  "status.agent.skillReading": "スキルを読み込んでいます…",
  "status.agent.memoryChanges": "メモリー: {saved} 件保存、{updated} 件更新、{forgotten} 件忘却。",
  "status.agent.memoryChangesWithConflicts":
    "メモリー: {saved} 件保存、{updated} 件更新、{forgotten} 件忘却、{notChanged} 件は別のスレッドが先に変更したため未変更。",
} as const satisfies PartialTranslation<typeof source>;
