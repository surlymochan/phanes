import type { PartialTranslation } from "../../../message";
import type { messages as source } from "../../en/status/agent";

export const messages = {
  "status.agent.dispatchApproval":
    "Cet envoi de code comprend une opération à risque. Vérifiez sa carte et ses arguments avant de l’approuver.",
  "status.agent.dispatchStarted": "Travail de programmation démarré : {title}. Hôte : {host}. Exécution : {runId}.",
  "status.agent.dispatchFinished":
    "Travail de programmation terminé : {title}. Hôte : {host}. Résultat : {status}. Vérifiez le résultat avant d’annoncer une réussite.",
  "status.agent.dispatchNoResult": "L’exécuteur openswarm s’est terminé sans fichier de résultat.",
  "status.agent.claudeWriteOutside":
    "Écrire {path}, hors de l’espace de travail de l’agent, du dossier partagé et des dossiers temporaires.",
  "status.agent.contextCleared": "Contexte effacé. Une nouvelle discussion commence ici.",
  "status.agent.modelSwitchedForLimit": "Limite d’utilisation atteinte. Passage à {model}.",
  "status.agent.modelReturnedAfterLimit": "Limite d’utilisation terminée. Retour à {model}.",
  "status.agent.pausedHourBudget":
    "Budget de tokens atteint : {used} tokens dans la dernière heure (limite {limit}). Le nouveau travail attend que vous continuiez.",
  "status.agent.pausedTurnBudget":
    "Arrêté après une étape : ce tour a utilisé {used} tokens (limite {limit}). Le travail est conservé. Continuez pour reprendre.",
  "status.agent.pausedPlanLimit":
    "La limite du plan a arrêté ce tour. Le travail est conservé et reprend quand la limite prend fin.",
  "status.agent.modelSwitchedForError": "Erreur du fournisseur. Passage à {model}.",
  "status.agent.modelReturnedAfterError": "Retour à {model} après l’erreur du fournisseur.",
  "status.agent.marketplaceSuggested": "Application de la Marketplace suggérée : {app}.",
  "status.agent.channelMemberAdded": "{agent} a ajouté {member} au canal. {person} l’a demandé.",
  "status.agent.channelMemberRemoved": "{agent} a retiré {member} du canal. {person} l’a demandé.",
  "status.agent.channelInstructionsChanged": "{agent} a modifié les instructions du canal. {person} l’a demandé.",
  "status.agent.channelRoutineCreated": "{agent} a créé la routine {routine}. {person} l’a demandé.",
  "status.agent.channelRoutineUpdated": "{agent} a modifié la routine {routine}. {person} l’a demandé.",
  "status.agent.channelRoutineDeleted": "{agent} a supprimé la routine {routine}. {person} l’a demandé.",
  "status.agent.screenCaptureScreen": "Capturer l’écran principal et envoyer l’image au modèle de l’agent.",
  "status.agent.screenCaptureChannel":
    "L’image s’affiche dans le canal {channel}, et chaque membre de ce serveur qui lit le canal peut la voir.",
  "status.agent.screenCaptureDisplay": "Capturer l’écran {name} et envoyer l’image au modèle de l’agent.",
  "status.agent.screenCaptureWindow": "Capturer la fenêtre {name} et envoyer l’image au modèle de l’agent.",
  "status.agent.screenCaptureRegion":
    "Capturer la zone de l’écran à {x}, {y}, {width} × {height} points et envoyer l’image au modèle de l’agent.",
  "status.agent.screenCaptureProgress": "Demande de capture d’écran…",
  "status.agent.screenCaptureReviewing": "Analyse de la capture d’écran…",
  "status.agent.starting": "Démarrage des CLI d’agents locaux…",
  "status.agent.memoriesSearching": "Recherche dans ses souvenirs…",
  "status.agent.memoriesSearched": {
    one: "Souvenirs consultés : {count} trouvé",
    other: "Souvenirs consultés : {count} trouvés",
  },
  "status.agent.memoriesUpdating": "Mise à jour de ses souvenirs…",
  "status.agent.skillUsing": "Utilisation de la compétence {name}…",
  "status.agent.skillReading": "Lecture d’une compétence…",
  "status.agent.memoryChanges": "Souvenirs : {saved} enregistrés, {updated} mis à jour, {forgotten} oubliés.",
  "status.agent.memoryChangesWithConflicts":
    "Souvenirs : {saved} enregistrés, {updated} mis à jour, {forgotten} oubliés, {notChanged} non modifiés, car un autre fil les a modifiés avant.",
} as const satisfies PartialTranslation<typeof source>;
